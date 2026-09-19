"""
藍牙連 micro:bit，送相機控制指令（從 LLM_control_motor/robot_bluetooth.py 抽出來、改成 async 的精簡版）。

電腦 → micro:bit 指令（結尾都是 #），micro:bit 程式是 LLM_control_motor/microbit_llm_motor.js：
    Z,速度#        相機上下馬達 -255~255：正數=往上、負數=往下
    P,角度#        相機左右伺服馬達 0~180：0=最右、90=正前方、180=最左
    S#             全部馬達停止

安全機制：micro:bit 超過 0.5 秒沒收到指令就會停掉馬達（Z），
所以相機上下在轉的時候，要每 0.1 秒左右重送一次同樣的指令（keep-alive）。
"""

import sys

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "micro:bit"    # 藍牙名稱包含這段就連；教室很多片可改成 "vapup"
UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


class RobotBLE:
    def __init__(self, device_name=DEVICE_NAME, fake=False, quiet=False):
        self.device_name = device_name
        self.fake = fake      # True = 不連藍牙，只印出指令
        self.quiet = quiet    # True = 不印每一個送出的指令
        self.client = None
        self.write_char = None

    async def connect(self):
        if self.fake:
            log("🧪 假機器人模式：不連藍牙，只印出指令")
            return
        log("🔍 搜尋 micro:bit ...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, adv: self.device_name.lower() in (d.name or adv.local_name or "").lower(),
            timeout=10)
        if device is None:
            raise SystemExit("❌ 找不到 micro:bit（確認已開機顯示愛心、手機 App 沒連著它）")
        self.client = BleakClient(device)
        await self.client.connect()
        self.write_char = next(
            c for c in self.client.services.get_service(UART_SERVICE).characteristics
            if "write" in c.properties or "write-without-response" in c.properties
        )
        log(f"✅ 已連線 {device.name}")

    async def send(self, text, reliable=False):
        """送指令。預設不等 micro:bit 回應（write-without-response）：
        實測等回應的寫法中位數 30 ms、偶爾 130 ms，控制迴圈會被卡住，相機就會轉過頭；
        不等回應約 1 ms（藍牙底層還是會重傳，不會掉）。reliable=True 用在停止、斷線這種一定要到的指令。"""
        if not self.quiet:
            log(f"   📡 {text}")
        if not self.fake and self.client is not None and self.client.is_connected:
            response = reliable or "write-without-response" not in self.write_char.properties
            await self.client.write_gatt_char(self.write_char, text.encode(), response=response)

    async def disconnect(self):
        if self.fake or self.client is None:
            return
        try:
            if self.client.is_connected:
                await self.client.write_gatt_char(self.write_char, b"S#")
                await self.client.disconnect()
        finally:
            log("👋 已斷線")
