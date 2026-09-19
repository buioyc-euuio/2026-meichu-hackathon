"""
輪子控制：設定「左右輪速度 + 維持到什麼時候」，背景工作每 KEEPALIVE_INTERVAL 秒重送一次 M 指令
（micro:bit 0.5 秒沒收到指令就停車），時間到就送 S#。每次送出前都先經過防撞（safety.py）。

    M,左輪,右輪#   -255~255：正數=前進、負數=後退（microbit_llm_motor.js）
"""

import asyncio
import time

from commands import BACKWARD, FORWARD, LEFT, RIGHT
from robot_ble import log

# ====== 想改的東西都在這裡 ======
DRIVE_SPEED = 140        # 前進後退的轉速（跟 LLM_control_motor 的 DEFAULT_SPEED 一樣）
TURN_SPEED = 180         # 原地轉彎的轉速
KEEPALIVE_INTERVAL = 0.1
# ================================


def wheel_speeds(action, speed=None):
    """動作 → (左輪, 右輪)。"""
    if action in (FORWARD, BACKWARD):
        s = speed or DRIVE_SPEED
        return (s, s) if action == FORWARD else (-s, -s)
    if action in (LEFT, RIGHT):
        s = speed or TURN_SPEED
        return (-s, s) if action == LEFT else (s, -s)
    return 0, 0


class WheelDriver:
    def __init__(self, robot, guard):
        self.robot = robot        # camera_control/robot_ble.RobotBLE（async send）
        self.guard = guard        # safety.CollisionGuard
        self.left = self.right = 0
        self.until = 0.0
        self.moving = False       # 最後送出的是 M（不是 S#）
        self.last_block = None
        self.last_logged = None   # RobotBLE 通常開 quiet，這裡只在指令改變時印一次
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            self.task = None
        await self.stop()

    def set(self, left, right, seconds):
        """開始（或改成）用這個速度轉 seconds 秒；背景工作負責送。"""
        self.left, self.right = int(left), int(right)
        self.until = time.monotonic() + seconds

    async def stop(self):
        self.until = 0.0
        self.left = self.right = 0
        await self._send("S#", reliable=True)
        self.moving = False

    async def _send(self, text, reliable=False):
        if text != self.last_logged:
            log(f"   🚗 {text}")
            self.last_logged = text
        await self.robot.send(text, reliable=reliable)

    async def _loop(self):
        while True:
            now = time.monotonic()
            if now < self.until and (self.left or self.right):
                left, right, reason = self.guard.filter(self.left, self.right)
                if reason and reason != self.last_block:
                    log(f"🛑 防撞：{reason}")
                self.last_block = reason
                if left or right:
                    await self._send(f"M,{int(left)},{int(right)}#")
                    self.moving = True
                elif self.moving:
                    await self._send("S#", reliable=True)
                    self.moving = False
            elif self.moving:
                await self._send("S#", reliable=True)
                self.moving = False
            await asyncio.sleep(KEEPALIVE_INTERVAL)
