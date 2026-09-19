"""
輪子控制（跟 LLM_control_motor/robot_bluetooth.py 一樣的指令）：

    M,左輪,右輪#   -255~255：正數=前進、負數=後退
    S#             全部馬達停止

micro:bit 0.5 秒沒收到指令就停車，所以背景工作每 KEEPALIVE_INTERVAL 秒重送一次。
只有「加速」會慢慢來（每秒最多 ACCEL，起步才不會頓）；減速、停車、換方向都馬上生效
（慢慢減速會讓車子多轉、多走一段，轉彎就會轉過頭）。指令改變時馬上送，不等下一次 keep-alive。
"""

import asyncio
import time

# ====== 想改的東西都在這裡 ======
KEEPALIVE_INTERVAL = 0.1
ACCEL = 600              # 加速時輪子轉速每秒最多增加多少（0~255 的單位）
MAX_WHEEL = 200          # 任何時候輪子轉速上限（保護）
# ================================


def clamp(value, low, high):
    return max(low, min(high, value))


class Chassis:
    def __init__(self, robot):
        self.robot = robot                 # camera_control/robot_ble.RobotBLE
        self.want = (0, 0)                 # 想要的 (左, 右)
        self.smooth = True                 # False = 連加速都不慢慢來（原地轉的短脈衝）
        self.now = [0.0, 0.0]              # 現在送出去的 (左, 右)
        self.moving = False
        self.changed = asyncio.Event()
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            self.task = None
        await self.stop()

    def set(self, left, right, smooth=True):
        """設定想要的輪子轉速。smooth=True：加速慢慢來；False：馬上到（短脈衝用）。"""
        want = (clamp(int(left), -MAX_WHEEL, MAX_WHEEL), clamp(int(right), -MAX_WHEEL, MAX_WHEEL))
        if want != self.want or smooth != self.smooth:
            self.want, self.smooth = want, smooth
            self.changed.set()             # 馬上送，不等 keep-alive

    async def stop(self):
        """馬上停。"""
        self.want = (0, 0)
        self.now = [0.0, 0.0]
        if self.moving:
            await self.robot.send("S#", reliable=True)
            self.moving = False

    async def _loop(self):
        last = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self.changed.clear()
            now = time.monotonic()
            step = ACCEL * (now - last)
            last = now
            for i in (0, 1):
                want, cur = self.want[i], self.now[i]
                speeding_up = cur * want >= 0 and abs(want) > abs(cur)    # 同方向（或從 0 起步）而且變快
                if self.smooth and speeding_up:
                    self.now[i] = cur + clamp(want - cur, -step, step)   # 加速：慢慢來
                else:
                    self.now[i] = float(want)                            # 減速、停、換方向：馬上
            left, right = int(round(self.now[0])), int(round(self.now[1]))
            if left or right:
                await self.robot.send(f"M,{left},{right}#")     # 每次都送：同時也是 keep-alive
                self.moving = True
            elif self.moving:
                await self.robot.send("S#", reliable=True)
                self.moving = False
