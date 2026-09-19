"""
模式一：用語音（和鍵盤）開車。

    語音「前進兩秒」→ 前進 2 秒自動停；沒講秒數就走 VOICE_SECONDS 秒
    鍵盤 w/a/s/d   → 按著才動：每收到一次按鍵（按住時終端機會自動重複）就延長 KEY_HOLD_SECONDS 秒
    「停」／空白鍵  → 立刻停
"""

from commands import STOP, Drive
from drive import wheel_speeds
from modes.base import Mode
from robot_ble import log

# ====== 想改的東西都在這裡 ======
VOICE_SECONDS = 1.0      # 語音沒講秒數時，一個動作走幾秒
MAX_SECONDS = 5.0        # 單一動作最多幾秒（跟 LLM_control_motor 的 MAX_MOVE_SECONDS 一樣）
KEY_HOLD_SECONDS = 0.6   # 鍵盤：最後一次按鍵後再走幾秒（要比終端機「按住開始重複」的延遲長，約 0.5 秒）
# ================================


class VoiceMode(Mode):
    name = "voice"

    def __init__(self, driver):
        self.driver = driver   # drive.WheelDriver

    async def enter(self):
        log("🎙️  語音模式：說「前進 / 後退 / 左轉 / 右轉 / 停」，或用鍵盤 w a s d、空白鍵停")

    async def exit(self):
        await self.driver.stop()

    async def handle(self, command):
        if not isinstance(command, Drive):
            return False
        if command.action == STOP:
            await self.driver.stop()
            return True
        if command.source == "keyboard" and command.seconds is None:
            seconds = KEY_HOLD_SECONDS
        else:
            seconds = min(command.seconds or VOICE_SECONDS, MAX_SECONDS)
            log(f"🚗 {command.action} {seconds:g} 秒（{command.source}）")
        left, right = wheel_speeds(command.action, command.speed)
        self.driver.set(left, right, seconds)
        return True
