"""
藍牙連 micro:bit，送相機控制指令（從 LLM_control_motor/robot_bluetooth.py 抽出來、改成 async 的精簡版）。

電腦 → micro:bit 指令（結尾都是 #），micro:bit 程式是 LLM_control_motor/microbit_llm_motor.js：
    Z,速度#        相機上下馬達 -255~255（程式註解寫正數往上，但實測這台車負數才是往上，見 ball_center.TILT_UP_SIGN）
    P,角度#        相機左右伺服馬達 0~180：0=最右、90=正前方、180=最左
    S#             全部馬達停止

安全機制：micro:bit 超過 0.5 秒沒收到指令就會停掉馬達（Z），
所以相機上下在轉的時候，要每 0.1 秒左右重送一次同樣的指令（keep-alive）。

斷線（例如 micro:bit 電壓不夠重開機）時會印警告，並在背景自動重連；斷線期間的指令直接丟掉。
"""

import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "vapup,zutog"  # 要連的 micro:bit 藍牙名稱，逗號分開、依優先順序：先找 vapup，找不到才連 zutog
                             # （教室有很多片，只寫 "micro:bit" 會連到別人的）
UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RECONNECT_INTERVAL = 2.0     # 斷線後最快每幾秒試著重連一次


def log(*args):
    print(*args, file=sys.stderr, flush=True)


class RobotBLE:
    def __init__(self, device_name=DEVICE_NAME, fake=False, quiet=False):
        self.names = [n.strip().lower() for n in device_name.split(",") if n.strip()]
        self.fake = fake      # True = 不連藍牙，只印出指令
        self.quiet = quiet    # True = 不印每一個送出的指令
        self.client = None
        self.write_char = None
        self.closing = False
        self.disconnects = 0          # 斷線幾次（除錯用）
        self.reconnect_task = None
        self.device_label = "fake" if fake else None   # 連到的 micro:bit 名稱
        self.last_reconnect = 0.0

    @property
    def connected(self):
        return self.fake or (self.client is not None and self.client.is_connected)

    def _on_disconnect(self, _client):
        if not self.closing:
            self.disconnects += 1
            log("⚠️  micro:bit 斷線了（電池沒電、或馬達同時啟動電壓不夠讓它重開機？）→ 背景自動重連")

    async def connect(self, timeout=10):
        if self.fake:
            log("🧪 假機器人模式：不連藍牙，只印出指令")
            return
        log(f"🔍 搜尋 micro:bit（依序：{' → '.join(self.names)}）...")
        device = await self._find(timeout)
        if device is None:
            raise SystemExit(f"❌ 找不到 micro:bit {' / '.join(self.names)}（確認已開機顯示愛心、手機 App 沒連著它）")
        self.client = BleakClient(device, disconnected_callback=self._on_disconnect)
        await self.client.connect()
        self.write_char = next(
            c for c in self.client.services.get_service(UART_SERVICE).characteristics
            if "write" in c.properties or "write-without-response" in c.properties
        )
        self.device_label = device.name
        log(f"✅ 已連線 {device.name}")

    async def _find(self, timeout):
        """掃描 timeout 秒：看到第一順位就馬上用；時間到了用找到的裡面順位最前的。"""
        found = {}                      # 順位 → 裝置
        first = asyncio.Event()

        def seen(d, adv):
            name = (d.name or adv.local_name or "").lower()
            for rank, want in enumerate(self.names):
                if want in name and rank not in found:
                    found[rank] = d
                    if rank == 0:
                        first.set()

        async with BleakScanner(detection_callback=seen):
            try:
                await asyncio.wait_for(first.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return found[min(found)] if found else None

    async def send(self, text, reliable=False):
        """送指令。預設不等 micro:bit 回應（write-without-response）：
        實測等回應的寫法中位數 30 ms、偶爾 130 ms，控制迴圈會被卡住，相機就會轉過頭；
        不等回應約 1 ms（藍牙底層還是會重傳，不會掉）。reliable=True 用在停止、斷線這種一定要到的指令。"""
        if not self.quiet:
            log(f"   📡 {text}")
        if self.fake:
            return
        if not self.connected:
            self._start_reconnect()
            return
        response = reliable or "write-without-response" not in self.write_char.properties
        try:
            await self.client.write_gatt_char(self.write_char, text.encode(), response=response)
        except Exception as e:  # noqa: BLE001 — 寫到一半斷線之類的，丟掉這個指令、等重連
            log(f"⚠️  送 {text} 失敗：{e}")

    def _start_reconnect(self):
        if self.closing or (self.reconnect_task and not self.reconnect_task.done()):
            return
        if time.monotonic() - self.last_reconnect < RECONNECT_INTERVAL:
            return
        self.last_reconnect = time.monotonic()

        async def reconnect():
            try:
                await self.connect(timeout=5)
            except (SystemExit, Exception) as e:  # noqa: BLE001 — 找不到就下次再試
                log(f"   重連失敗：{e}")
        self.reconnect_task = asyncio.create_task(reconnect())

    async def disconnect(self):
        self.closing = True
        if self.reconnect_task:
            self.reconnect_task.cancel()
        if self.fake or self.client is None:
            return
        try:
            if self.client.is_connected:
                await self.client.write_gatt_char(self.write_char, b"S#")
                await self.client.disconnect()
        finally:
            log("👋 已斷線")
