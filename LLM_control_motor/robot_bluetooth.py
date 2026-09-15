"""
藍牙連線 + 給 LLM 呼叫的機器人工具（馬達版）

電腦 → micro:bit 指令（結尾都是 #）：
    M,左輪,右輪#   輪子速度 -255~255：正數=前進、負數=後退、0=停
    Z,速度#        相機上下馬達 -255~255：正數=往上、負數=往下
    P,角度#        相機左右伺服馬達 0~180：0=最右、90=正前方、180=最左
    S#             全部馬達停止

安全機制：micro:bit 超過 0.5 秒沒收到指令就自動停車，
所以動作進行中，電腦每 0.1 秒重送一次同樣的指令（keep-alive）。
"""

import asyncio
import json
import threading
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

# ====== 想改的東西都在這裡 ======
DEVICE_NAME = "micro:bit"    # 藍牙名稱包含這段就連；教室很多片可改成 "vapup"
DEFAULT_SPEED = 140          # 前進後退的預設轉速（0~255；太低可能推不動車子）
TURN_SPEED = 180             # 轉彎的預設轉速
TURN_90_SECONDS = 1.1        # 轉 90 度大約幾秒（粗估：速度 180 轉 0.5 秒約 40 度）
# ↑ 以上三個是還沒校正時的數值；跑過校正後會改用 LLM校正馬達轉彎/calibration.json
MAX_MOVE_SECONDS = 5.0       # 輪子單一動作最多幾秒
MAX_CAMERA_Z_SECONDS = 1.0   # 相機上下單一動作最多幾秒（沒有限位開關，轉太久可能卡住）
KEEPALIVE_INTERVAL = 0.1     # 動作中每幾秒重送一次指令
SERVO_SETTLE_SECONDS = 0.4   # 伺服馬達轉到定位需要的時間
# ================================

UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

# 讀取校正結果（LLM校正馬達轉彎/calibrate.py 產生）；沒有檔案就用上面的數值
CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "LLM校正馬達轉彎" / "calibration.json"
try:
    CALIBRATION = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError):
    CALIBRATION = {}
DEFAULT_SPEED = CALIBRATION.get("move_speed", DEFAULT_SPEED)
TURN_SPEED = CALIBRATION.get("turn_speed", TURN_SPEED)
TURN_LEFT_90_SECONDS = CALIBRATION.get("turn_left", {}).get("seconds_for_90", TURN_90_SECONDS)
TURN_RIGHT_90_SECONDS = CALIBRATION.get("turn_right", {}).get("seconds_for_90", TURN_90_SECONDS)


def clamp(value, low, high):
    return max(low, min(high, value))


class Robot:
    """在背景執行藍牙連線，對外提供一般（非 async）函式，方便 LLM 工具直接呼叫。"""

    def __init__(self):
        self.fake = False     # True = 不連藍牙，只印出指令
        self.fast = False     # True = 不真的等待秒數（自動測試用）
        self.client = None
        self.write_char = None
        self.pan_angle = 90   # 記住相機左右角度，給 get_status 回報
        self.calls = []       # 紀錄 LLM 呼叫過的工具（自動測試用）
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
        self.send("S#")
        self._run(self.client.disconnect())
        print("👋 已斷線")

    # ---------- 送指令 ----------
    def send(self, text, quiet=False):
        if not quiet:
            print(f"   📡 {text}")
        if not self.fake:
            self._run(self.client.write_gatt_char(self.write_char, text.encode()))

    def sleep(self, seconds):
        if not self.fast:
            time.sleep(seconds)

    def hold(self, text, seconds):
        """送出指令，持續重送 seconds 秒（keep-alive），然後全部停止。"""
        self.send(text)
        if not self.fast:
            end = time.monotonic() + seconds
            while end - time.monotonic() > KEEPALIVE_INTERVAL:
                time.sleep(KEEPALIVE_INTERVAL)
                self.send(text, quiet=True)
            time.sleep(max(0, end - time.monotonic()))
        self.send("S#")

    def record(self, name, **args):
        self.calls.append((name, args))
        shown = ", ".join(f"{k}={v}" for k, v in args.items())
        print(f"🔧 LLM 呼叫 {name}({shown})")


robot = Robot()


def _wheels(left, right, seconds):
    seconds = round(clamp(float(seconds), 0.1, MAX_MOVE_SECONDS), 2)
    left = int(clamp(float(left), -255, 255))
    right = int(clamp(float(right), -255, 255))
    robot.hold(f"M,{left},{right}#", seconds)
    return seconds


def _speed(speed):
    return int(clamp(abs(float(speed)), 0, 255))


# ====== 給 LLM 用的工具函式 ======
# LLM 會讀「函式名稱、參數型別、說明文字」來決定要不要呼叫、怎麼呼叫，
# 所以說明寫得越清楚，LLM 越會用。Args: 底下的格式請保留。

def move_forward(seconds: float = 1.0, speed: int = DEFAULT_SPEED) -> str:
    """車子直直往前走，時間到自動停下。

    Args:
        seconds: 前進幾秒，0.1 到 5
        speed: 轉速 0 到 255，越大越快；慢速約 120、一般 180、全速 255
    """
    robot.record("move_forward", seconds=seconds, speed=speed)
    s = _wheels(_speed(speed), _speed(speed), seconds)
    return f"已前進 {s} 秒，速度 {_speed(speed)}"


def move_backward(seconds: float = 1.0, speed: int = DEFAULT_SPEED) -> str:
    """車子直直往後退，時間到自動停下。

    Args:
        seconds: 後退幾秒，0.1 到 5
        speed: 轉速 0 到 255，越大越快；慢速約 120、一般 180、全速 255
    """
    robot.record("move_backward", seconds=seconds, speed=speed)
    s = _wheels(-_speed(speed), -_speed(speed), seconds)
    return f"已後退 {s} 秒，速度 {_speed(speed)}"


def turn_left(seconds: float = TURN_LEFT_90_SECONDS, speed: int = TURN_SPEED) -> str:
    """車子原地向左轉（逆時針），時間到自動停下。不指定秒數和速度時，預設轉約 90 度。
    要轉其他角度時，依照 system prompt 裡的「校正知識」換算秒數，並使用相同的速度。

    Args:
        seconds: 轉幾秒，0.1 到 5
        speed: 轉速 0 到 255；換速度會讓轉的角度改變，沒必要就用預設
    """
    robot.record("turn_left", seconds=seconds, speed=speed)
    s = _wheels(-_speed(speed), _speed(speed), seconds)
    return f"已原地左轉 {s} 秒"


def turn_right(seconds: float = TURN_RIGHT_90_SECONDS, speed: int = TURN_SPEED) -> str:
    """車子原地向右轉（順時針），時間到自動停下。不指定秒數和速度時，預設轉約 90 度。
    要轉其他角度時，依照 system prompt 裡的「校正知識」換算秒數，並使用相同的速度。

    Args:
        seconds: 轉幾秒，0.1 到 5
        speed: 轉速 0 到 255；換速度會讓轉的角度改變，沒必要就用預設
    """
    robot.record("turn_right", seconds=seconds, speed=speed)
    s = _wheels(_speed(speed), -_speed(speed), seconds)
    return f"已原地右轉 {s} 秒"


def drive(left_speed: int, right_speed: int, seconds: float = 1.0) -> str:
    """分別設定左輪和右輪的速度，用來邊走邊轉彎（走弧線）。時間到自動停下。
    例：左 120、右 200 → 往前並向左彎；左 200、右 120 → 往前並向右彎；兩個都負數 → 倒車。

    Args:
        left_speed: 左輪速度 -255 到 255，正數前進、負數後退
        right_speed: 右輪速度 -255 到 255，正數前進、負數後退
        seconds: 持續幾秒，0.1 到 5
    """
    robot.record("drive", left_speed=left_speed, right_speed=right_speed, seconds=seconds)
    s = _wheels(left_speed, right_speed, seconds)
    return f"已用左輪 {left_speed}、右輪 {right_speed} 行駛 {s} 秒"


def camera_up(seconds: float = 0.3, speed: int = 150) -> str:
    """把相機往上抬（馬達轉動一段時間，不是設定角度）。

    Args:
        seconds: 轉動幾秒，0.1 到 1；「一點點」約 0.2
        speed: 轉速 0 到 255
    """
    robot.record("camera_up", seconds=seconds, speed=speed)
    s = round(clamp(float(seconds), 0.1, MAX_CAMERA_Z_SECONDS), 2)
    robot.hold(f"Z,{_speed(speed)}#", s)
    return f"相機已往上抬 {s} 秒"


def camera_down(seconds: float = 0.3, speed: int = 150) -> str:
    """把相機往下壓（馬達轉動一段時間，不是設定角度）。

    Args:
        seconds: 轉動幾秒，0.1 到 1；「一點點」約 0.2
        speed: 轉速 0 到 255
    """
    robot.record("camera_down", seconds=seconds, speed=speed)
    s = round(clamp(float(seconds), 0.1, MAX_CAMERA_Z_SECONDS), 2)
    robot.hold(f"Z,{-_speed(speed)}#", s)
    return f"相機已往下壓 {s} 秒"


def camera_pan(angle: int) -> str:
    """把相機轉到指定的左右角度（絕對角度）。0 = 最右邊、90 = 正前方、180 = 最左邊。
    注意方向：角度變大是往左、變小是往右。「再往左一點」就加角度，「再往右一點」就減角度
    （不知道目前角度就先呼叫 get_status）。

    Args:
        angle: 角度 0 到 180
    """
    robot.record("camera_pan", angle=angle)
    a = int(clamp(float(angle), 0, 180))
    robot.send(f"P,{a}#")
    robot.pan_angle = a
    robot.sleep(SERVO_SETTLE_SECONDS)
    note = "" if a == float(angle) else f"（{angle} 超出範圍，已限制為 {a}）"
    return f"相機已轉到 {a} 度{note}"


def stop() -> str:
    """立刻停止所有馬達（輪子和相機上下）。"""
    robot.record("stop")
    robot.send("S#")
    return "已全部停止"


def wait(seconds: float) -> str:
    """原地不動，等待一段時間再做下一個動作。

    Args:
        seconds: 等幾秒，0.1 到 10
    """
    robot.record("wait", seconds=seconds)
    s = round(clamp(float(seconds), 0.1, 10), 2)
    robot.sleep(s)
    return f"已等待 {s} 秒"


def get_status() -> str:
    """查詢機器人目前狀態，例如相機左右角度。"""
    robot.record("get_status")
    return f"相機左右角度 {robot.pan_angle} 度（0 最右、90 正前方、180 最左）"


TOOLS = [
    move_forward, move_backward, turn_left, turn_right, drive,
    camera_up, camera_down, camera_pan,
    stop, wait, get_status,
]
