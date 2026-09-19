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

from latency import latency, timed

# ====== 想改的東西都在這裡 ======
DEVICE_NAME = "micro:bit"    # 藍牙名稱包含這段就連；教室很多片可改成 "vapup"
DEFAULT_SPEED = 140          # 前進後退的預設轉速（0~255；太低可能推不動車子）
TURN_SPEED = 200             # 轉彎時外側輪子的轉速
TURN_INNER_SPEED = 60        # 轉彎時內側輪子的轉速（比外側慢 → 走弧線；負數 = 原地轉，但容易卡住）
TURN_90_SECONDS = 2.0        # 轉 90 度大約幾秒（還沒校正的粗估值）
# ↑ 以上是還沒校正時的數值；跑過校正後會改用 LLM校正馬達轉彎/calibration.json
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
_straight = CALIBRATION.get("move_forward", {})
_left_turn = CALIBRATION.get("turn_left", {})
_right_turn = CALIBRATION.get("turn_right", {})
# 直走：兩個馬達快慢不同，校正出「左 140、右 115 才走直線」這種比例，前進後退都照比例縮放
_sl, _sr = _straight.get("left_speed", DEFAULT_SPEED), _straight.get("right_speed", DEFAULT_SPEED)
DEFAULT_SPEED = max(_sl, _sr)
STRAIGHT_LEFT_RATIO, STRAIGHT_RIGHT_RATIO = _sl / DEFAULT_SPEED, _sr / DEFAULT_SPEED
# 轉彎：左轉和右轉各自的左右輪速度（外側 = speed、內側 = inner_speed）
TURN_LEFT_SPEED = _left_turn.get("right_speed", TURN_SPEED)
TURN_LEFT_INNER_SPEED = _left_turn.get("left_speed", TURN_INNER_SPEED)
TURN_RIGHT_SPEED = _right_turn.get("left_speed", TURN_SPEED)
TURN_RIGHT_INNER_SPEED = _right_turn.get("right_speed", TURN_INNER_SPEED)
TURN_LEFT_90_SECONDS = _left_turn.get("seconds_for_90", TURN_90_SECONDS)
TURN_RIGHT_90_SECONDS = _right_turn.get("seconds_for_90", TURN_90_SECONDS)


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
        self.tail_angle = 90  # 記住尾巴角度，給 get_status 回報
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
        t = time.monotonic()
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
        print(f"✅ 已連線 {device.name}（花了 {time.monotonic() - t:.1f}s）")

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
        t = time.monotonic()
        if not self.fake:
            self._run(self.client.write_gatt_char(self.write_char, text.encode()))
        latency.bluetooth_write(time.monotonic() - t, text)

    def sleep(self, seconds):
        if not self.fast:
            time.sleep(seconds)

    def hold(self, text, seconds):
        """送出指令，持續重送 seconds 秒（keep-alive），然後全部停止。"""
        self.send(text)
        if not self.fast:
            last = time.monotonic()
            end = last + seconds
            max_gap = 0
            while end - time.monotonic() > KEEPALIVE_INTERVAL:
                time.sleep(KEEPALIVE_INTERVAL)
                self.send(text, quiet=True)
                max_gap, last = max(max_gap, time.monotonic() - last), time.monotonic()
            time.sleep(max(0, end - time.monotonic()))
            if max_gap > 0.4:   # 超過 0.5 秒 micro:bit 就會自動停車，動作會一頓一頓
                latency.log(f"⚠️ keep-alive 最久隔了 {max_gap:.2f}s 才重送（超過 0.5s 車子會中途停下）")
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
    s = _wheels(_speed(speed) * STRAIGHT_LEFT_RATIO, _speed(speed) * STRAIGHT_RIGHT_RATIO, seconds)
    return f"已前進 {s} 秒，速度 {_speed(speed)}"


def move_backward(seconds: float = 1.0, speed: int = DEFAULT_SPEED) -> str:
    """車子直直往後退，時間到自動停下。

    Args:
        seconds: 後退幾秒，0.1 到 5
        speed: 轉速 0 到 255，越大越快；慢速約 120、一般 180、全速 255
    """
    robot.record("move_backward", seconds=seconds, speed=speed)
    s = _wheels(-_speed(speed) * STRAIGHT_LEFT_RATIO, -_speed(speed) * STRAIGHT_RIGHT_RATIO, seconds)
    return f"已後退 {s} 秒，速度 {_speed(speed)}"


def _inner(inner_speed):
    return int(clamp(float(inner_speed), -255, 255))


def turn_left(seconds: float = TURN_LEFT_90_SECONDS, speed: int = TURN_LEFT_SPEED,
              inner_speed: int = TURN_LEFT_INNER_SPEED) -> str:
    """車子向左轉（逆時針），時間到自動停下。預設是邊前進邊轉的弧線轉彎：
    右輪（外側）用 speed、左輪（內側）用比較慢的 inner_speed。不指定參數時，預設轉約 90 度。
    要轉其他角度時，依照 system prompt 裡的「校正知識」換算秒數，並使用相同的兩個速度。

    Args:
        seconds: 轉幾秒，0.1 到 5
        speed: 外側（右）輪轉速 0 到 255；換速度會讓轉的角度改變，沒必要就用預設
        inner_speed: 內側（左）輪轉速 -255 到 255；比 speed 小越多彎得越急。
            0 = 以左輪為中心轉；負數（例如 -speed）= 原地轉圈，但地面摩擦大時容易卡住
    """
    robot.record("turn_left", seconds=seconds, speed=speed, inner_speed=inner_speed)
    s = _wheels(_inner(inner_speed), _speed(speed), seconds)
    return f"已左轉 {s} 秒（左輪 {_inner(inner_speed)}、右輪 {_speed(speed)}）"


def turn_right(seconds: float = TURN_RIGHT_90_SECONDS, speed: int = TURN_RIGHT_SPEED,
               inner_speed: int = TURN_RIGHT_INNER_SPEED) -> str:
    """車子向右轉（順時針），時間到自動停下。預設是邊前進邊轉的弧線轉彎：
    左輪（外側）用 speed、右輪（內側）用比較慢的 inner_speed。不指定參數時，預設轉約 90 度。
    要轉其他角度時，依照 system prompt 裡的「校正知識」換算秒數，並使用相同的兩個速度。

    Args:
        seconds: 轉幾秒，0.1 到 5
        speed: 外側（左）輪轉速 0 到 255；換速度會讓轉的角度改變，沒必要就用預設
        inner_speed: 內側（右）輪轉速 -255 到 255；比 speed 小越多彎得越急。
            0 = 以右輪為中心轉；負數（例如 -speed）= 原地轉圈，但地面摩擦大時容易卡住
    """
    robot.record("turn_right", seconds=seconds, speed=speed, inner_speed=inner_speed)
    s = _wheels(_speed(speed), _inner(inner_speed), seconds)
    return f"已右轉 {s} 秒（左輪 {_speed(speed)}、右輪 {_inner(inner_speed)}）"


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


# ---------- 尾巴（伺服馬達，micro:bit P13）----------
# 搖尾巴由 micro:bit 在背景執行，送出指令後馬上回傳，
# 所以 LLM 可以接著呼叫 turn_left 等動作，做到「一邊搖尾巴一邊轉圈」。
TAIL_EMOTIONS = {         # 情緒 → (來回次數, 每擺一邊幾毫秒, 左右各擺幾度)
    "開心": (6, 180, 40),
    "興奮": (20, 100, 60),
    "好奇": (3, 350, 20),
    "難過": (2, 700, 10),
}


def _wag(times, interval_ms, amplitude):
    times = int(clamp(float(times), 1, 50))
    interval_ms = int(clamp(float(interval_ms), 80, 1000))
    amplitude = int(clamp(float(amplitude), 5, 90))
    robot.send(f"W,{times},{interval_ms},{amplitude}#")
    robot.tail_angle = 90
    return times, interval_ms, amplitude, round(times * interval_ms * 2 / 1000, 1)


def tail_angle(angle: int) -> str:
    """把尾巴轉到指定角度並停住（會中斷正在進行的搖尾巴）。90 = 中間、0 = 最左、180 = 最右。

    Args:
        angle: 角度 0 到 180
    """
    robot.record("tail_angle", angle=angle)
    a = int(clamp(float(angle), 0, 180))
    robot.send(f"T,{a}#")
    robot.tail_angle = a
    robot.sleep(SERVO_SETTLE_SECONDS)
    note = "" if a == float(angle) else f"（{angle} 超出範圍，已限制為 {a}）"
    return f"尾巴已轉到 {a} 度{note}"


def wag_tail(times: int = 6, interval_ms: int = 180, amplitude: int = 40) -> str:
    """搖尾巴：以中間為中心左右來回擺動，搖完自動回到中間。
    尾巴在背景搖，這個工具會馬上回傳，可以接著呼叫其他動作（例如邊搖尾巴邊轉圈）。
    只是想表現情緒時，優先用 tail_emotion。

    Args:
        times: 左右來回幾次，1 到 50
        interval_ms: 每擺一邊停幾毫秒，80 到 1000；越小搖越快，快約 100、普通約 180、慢約 400
        amplitude: 左右各擺幾度，5 到 90；越大擺越開
    """
    robot.record("wag_tail", times=times, interval_ms=interval_ms, amplitude=amplitude)
    t, ms, amp, total = _wag(times, interval_ms, amplitude)
    return f"開始搖尾巴 {t} 下（每邊 {ms} 毫秒、擺幅 {amp} 度），約 {total} 秒搖完"


def tail_emotion(emotion: str) -> str:
    """用尾巴表現情緒。尾巴在背景搖，會馬上回傳，可以接著做其他動作。

    Args:
        emotion: 開心、興奮、好奇、難過、平靜 其中之一（平靜 = 停止搖動、回到中間）
    """
    robot.record("tail_emotion", emotion=emotion)
    if emotion == "平靜":
        robot.send("T,90#")
        robot.tail_angle = 90
        return "尾巴停下來，回到中間"
    if emotion not in TAIL_EMOTIONS:
        return f"不支援「{emotion}」，只能用：{'、'.join(TAIL_EMOTIONS)}、平靜"
    t, ms, amp, total = _wag(*TAIL_EMOTIONS[emotion])
    return f"尾巴表現「{emotion}」：搖 {t} 下，約 {total} 秒"


def stop() -> str:
    """立刻停止所有馬達（輪子、相機上下，尾巴也停止搖動回到中間）。"""
    robot.record("stop")
    robot.send("S#")
    robot.tail_angle = 90
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
    """查詢機器人目前狀態，例如相機左右角度、尾巴角度。"""
    robot.record("get_status")
    return (f"相機左右角度 {robot.pan_angle} 度（0 最右、90 正前方、180 最左）；"
            f"尾巴角度 {robot.tail_angle} 度（90 中間）")


# timed：記錄每個工具的延遲（見 latency.py）
TOOLS = [timed(f) for f in (
    move_forward, move_backward, turn_left, turn_right, drive,
    camera_up, camera_down, camera_pan,
    tail_angle, wag_tail, tail_emotion,
    stop, wait, get_status,
)]
