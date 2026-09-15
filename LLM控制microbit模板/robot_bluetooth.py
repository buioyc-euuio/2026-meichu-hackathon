"""
藍牙連線 + 給 AI 呼叫的車子函式

電腦 → micro:bit 的指令（結尾都是 #）：
    F#  前進        B#  後退
    L#  左轉        R#  右轉
    S#  停止

每個動作函式會：送出指令 → 等指定秒數 → 自動送 S# 停車。
micro:bit 端只要「收到哪個字母就讓馬達怎麼轉」即可。
"""

import asyncio
import threading
import time

from bleak import BleakClient, BleakScanner

# ====== 想改的東西都在這裡 ======
DEVICE_NAME = "micro:bit"   # 藍牙名稱包含這段就連；教室很多片可改成 "vapup"
MAX_SECONDS = 3.0           # 單一動作最多幾秒（避免 AI 叫車子衝太遠）

COMMANDS = {
    "forward":  "F#",
    "backward": "B#",
    "left":     "L#",
    "right":    "R#",
    "stop":     "S#",
}
# ================================

UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


class Robot:
    """在背景執行藍牙連線，對外提供一般（非 async）函式，方便 AI 工具直接呼叫。"""

    def __init__(self, fake=False):
        self.fake = fake            # True = 不連藍牙，只印出指令（沒有 micro:bit 也能測 AI）
        self.client = None
        self.write_char = None
        # bleak 需要 asyncio，所以開一個背景執行緒專門跑藍牙
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    # ---------- 連線 ----------
    def connect(self):
        if self.fake:
            print("🧪 假機器人模式：不連藍牙，只印出指令")
            return
        self._run(self._connect())

    async def _connect(self):
        print("🔍 搜尋 micro:bit ...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, adv: DEVICE_NAME.lower() in (d.name or adv.local_name or "").lower(),
            timeout=10)
        if device is None:
            raise SystemExit("❌ 找不到 micro:bit（確認已開機顯示愛心、手機 App 沒連著它）")

        self.client = BleakClient(device)
        await self.client.connect()
        self.write_char = next(
            c for c in self.client.services.get_service(UART_SERVICE).characteristics
            if "write" in c.properties or "write-without-response" in c.properties
        )
        print(f"✅ 已連線 {device.name}")

    def disconnect(self):
        if self.fake or not self.client:
            return
        self.send("stop")
        self._run(self.client.disconnect())
        print("👋 已斷線")

    # ---------- 送指令 ----------
    def send(self, action):
        text = COMMANDS[action]
        print(f"   📡 送出 {text}")
        if not self.fake:
            self._run(self.client.write_gatt_char(self.write_char, text.encode()))

    def do_for(self, action, seconds):
        seconds = max(0.1, min(MAX_SECONDS, float(seconds)))
        self.send(action)
        time.sleep(seconds)
        self.send("stop")
        return seconds


# ====== 給 AI 用的工具函式 ======
# AI 會讀「函式名稱、參數型別、說明文字」來決定要不要呼叫、怎麼呼叫，
# 所以說明寫得越清楚，AI 越會用。Args: 底下的格式請保留。

robot = Robot()


def move_forward(seconds: float = 1.0) -> str:
    """讓車子往前走一段時間，時間到自動停下。

    Args:
        seconds: 前進幾秒，0.1 到 3 之間，沒特別說就用 1
    """
    s = robot.do_for("forward", seconds)
    return f"已前進 {s} 秒"


def move_backward(seconds: float = 1.0) -> str:
    """讓車子往後退一段時間，時間到自動停下。

    Args:
        seconds: 後退幾秒，0.1 到 3 之間，沒特別說就用 1
    """
    s = robot.do_for("backward", seconds)
    return f"已後退 {s} 秒"


def turn_left(seconds: float = 0.5) -> str:
    """讓車子原地向左轉一段時間，時間到自動停下。大約 0.5 秒轉 90 度（依實際車子調整）。

    Args:
        seconds: 左轉幾秒，0.1 到 3 之間，沒特別說就用 0.5
    """
    s = robot.do_for("left", seconds)
    return f"已左轉 {s} 秒"


def turn_right(seconds: float = 0.5) -> str:
    """讓車子原地向右轉一段時間，時間到自動停下。大約 0.5 秒轉 90 度（依實際車子調整）。

    Args:
        seconds: 右轉幾秒，0.1 到 3 之間，沒特別說就用 0.5
    """
    s = robot.do_for("right", seconds)
    return f"已右轉 {s} 秒"


def stop() -> str:
    """立刻讓車子停止。"""
    robot.send("stop")
    return "已停止"


TOOLS = [move_forward, move_backward, turn_left, turn_right, stop]
