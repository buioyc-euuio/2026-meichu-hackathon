"""
整條 pipeline 裡流動的「指令」。鍵盤、語音（transcript → intent.py）都轉成這幾種，丟進同一個 queue，
由 controller.py 依照目前的模式決定要不要執行。
"""

from dataclasses import dataclass

VOICE = "voice"   # 模式一：用語音（和鍵盤）控制車子
BALL = "ball"     # 模式二：相機追球（camera_control）
MODES = (VOICE, BALL)

# Drive.action 可以是這幾個
FORWARD, BACKWARD, LEFT, RIGHT, STOP = "forward", "backward", "left", "right", "stop"


@dataclass
class SwitchMode:
    mode: str                # VOICE 或 BALL
    source: str = "?"        # "voice" / "keyboard"，印訊息用


@dataclass
class Drive:
    action: str              # FORWARD / BACKWARD / LEFT / RIGHT / STOP
    seconds: float | None = None   # 動多久；None = 用預設（語音）或「按著才動」（鍵盤）
    speed: int | None = None       # None = 用預設轉速
    source: str = "?"


@dataclass
class Transcript:
    """語音辨識出來的一句話（或鍵盤 v 打的一句話），還沒解析。"""
    text: str
    source: str = "voice"
    captured_at: float | None = None   # monotonic；辨識完成時間不是錄音時間


@dataclass
class Quit:
    source: str = "?"


@dataclass
class LlmResult:
    revision: int
    action: str
    seconds: float
    reason: str
    source: str
    captured_at: float | None
    submitted_at: float
    completed_at: float | None = None


@dataclass
class LlmFailure:
    revision: int
    message: str
