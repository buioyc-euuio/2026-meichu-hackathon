"""
測試相機的兩顆馬達：左右（伺服）、上下（直流馬達）各轉約 60 度再回來，最後回到中間。

    .venv/bin/python camera_control/motor_test.py --fake         # 不連車，只印指令和順序
    .venv/bin/python camera_control/motor_test.py                # 連 micro:bit，自動跑完
    .venv/bin/python camera_control/motor_test.py --step         # 每一步按 Enter 才繼續（邊看邊測）
    .venv/bin/python camera_control/motor_test.py --only pan     # 只測左右（--only tilt 只測上下）
    .venv/bin/python camera_control/motor_test.py --only tilt --degrees 20 --step      # 上下先小角度試

順序：左 60° → 中間 → 右 60° → 中間 → 上 60° → 中間 → 下 60° → 中間

左右是伺服馬達，給角度就會準確轉到（90 = 正前方、150 = 往左 60°、30 = 往右 60°）。
上下是直流馬達，只能「用某個速度轉幾秒」，沒有角度回報，所以角度是用實測的速度估的
（ball_center.py 的 TILT_*：這台車 Z 負數才是往上；往上 110 約 220 px/s、往下 75 約 225 px/s、
 假設上下跟左右一樣 1 度 ≈ 13.5 px）。看實際轉了幾度，照比例改 --up-seconds / --down-seconds
（例如轉了 40° → 秒數 × 1.5）。回中間用反方向、依速度換算的秒數，會有一點誤差，最後可能不會完全回正。
⚠️ 沒有限位開關：先把相機扶到平衡點（正前方）再跑，建議第一次加 --step、--degrees 20 邊看邊測。
"""

import argparse
import asyncio
import time

from ball_center import PAN_PX_PER_DEG, TILT_PX_PER_SPEED_DOWN, TILT_PX_PER_SPEED_UP, TILT_UP_SIGN
from robot_ble import DEVICE_NAME, RobotBLE, log


def tilt_seconds(degrees, up, speed):
    """用實測的速度估計：上下轉 degrees 度要幾秒（假設上下 1 度的畫面移動跟左右一樣）。"""
    px_per_sec = speed * (TILT_PX_PER_SPEED_UP if up else TILT_PX_PER_SPEED_DOWN)
    return round(degrees * PAN_PX_PER_DEG / px_per_sec, 2)

# ====== 想改的東西都在這裡 ======
DEGREES = 60              # 每個方向轉幾度
PAN_CENTER = 90           # 伺服：0 = 最右、90 = 正前方、180 = 最左
PAN_SWEEP_DEG_PER_SEC = 90   # 左右慢慢轉的速度（一次跳過去太猛，也比較看不清楚）
PAN_STEP_DEG = 2
UP_SPEED, DOWN_SPEED = 110, 75   # 上下馬達轉速（實測往上 < 60 推不動；往下有重力幫忙，慢一點比較好控制）
MAX_TILT_SECONDS = 4.0    # 單一動作最多轉幾秒（沒有限位開關，轉太久會卡住）
KEEPALIVE_INTERVAL = 0.1  # 轉動中每幾秒重送一次（micro:bit 0.5 秒沒收到就停）
PAUSE_SECONDS = 1.0       # 每個動作之間停多久，方便看
# ================================


class MotorTest:
    def __init__(self, robot, args):
        self.robot, self.args = robot, args
        self.pan = PAN_CENTER

    async def pause(self, label):
        if self.args.step:
            await asyncio.to_thread(input, f"   ⏎ 按 Enter 繼續（下一步：{label}）")
        else:
            await asyncio.sleep(PAUSE_SECONDS)

    async def pan_to(self, target):
        """慢慢轉到 target 度（每次 PAN_STEP_DEG 度）。"""
        target = max(0, min(180, int(target)))
        delay = PAN_STEP_DEG / PAN_SWEEP_DEG_PER_SEC
        while self.pan != target:
            step = max(-PAN_STEP_DEG, min(PAN_STEP_DEG, target - self.pan))
            self.pan += step
            await self.robot.send(f"P,{self.pan}#")
            await asyncio.sleep(delay)
        await asyncio.sleep(0.3)  # 讓伺服轉到定位

    async def tilt(self, up, speed, seconds):
        """相機往上（up=True）或往下，用 speed 轉 seconds 秒，中間一直重送，最後停下來。"""
        seconds = min(seconds, MAX_TILT_SECONDS)
        speed = abs(speed) * (1 if up else -1) * TILT_UP_SIGN   # 這台車 Z 負數 = 往上
        await self.robot.send(f"Z,{speed}#")
        end = time.monotonic() + seconds
        while end - time.monotonic() > KEEPALIVE_INTERVAL:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            await self.robot.send(f"Z,{speed}#")
        await asyncio.sleep(max(0.0, end - time.monotonic()))
        await self.robot.send("S#", reliable=True)

    async def run(self):
        a, deg = self.args, self.args.degrees
        steps = []
        if a.only in (None, "pan"):
            steps += [
                (f"左 {deg}°（P,{PAN_CENTER + deg}）", lambda: self.pan_to(PAN_CENTER + deg)),
                ("回中間（P,90）", lambda: self.pan_to(PAN_CENTER)),
                (f"右 {deg}°（P,{PAN_CENTER - deg}）", lambda: self.pan_to(PAN_CENTER - deg)),
                ("回中間（P,90）", lambda: self.pan_to(PAN_CENTER)),
            ]
        if a.only in (None, "tilt"):
            us, ds = a.up_speed, a.down_speed
            up = a.up_seconds if a.up_seconds is not None else tilt_seconds(deg, True, us)
            down = a.down_seconds if a.down_seconds is not None else tilt_seconds(deg, False, ds)
            zu, zd = us * TILT_UP_SIGN, -ds * TILT_UP_SIGN
            steps += [
                (f"上 約{deg}°（Z,{zu} 轉 {up} 秒）", lambda: self.tilt(True, us, up)),
                (f"回中間（Z,{zd} 轉 {down} 秒）", lambda: self.tilt(False, ds, down)),
                (f"下 約{deg}°（Z,{zd} 轉 {down} 秒）", lambda: self.tilt(False, ds, down)),
                (f"回中間（Z,{zu} 轉 {up} 秒）", lambda: self.tilt(True, us, up)),
            ]

        log(f"🎬 相機馬達測試，共 {len(steps)} 步"
            + ("（每步按 Enter 繼續）" if a.step else ""))
        await self.robot.send(f"P,{PAN_CENTER}#")
        await asyncio.sleep(0.5)
        for i, (label, action) in enumerate(steps, 1):
            if a.step:
                await self.pause(label)
            log(f"▶️  {i}/{len(steps)} {label}")
            await action()
            if not a.step and i < len(steps):
                await self.pause(label)
        log("✅ 測試完成，相機已回到中間"
            + ("（上下是用時間估的，如果沒有完全回正，調 --up-seconds / --down-seconds）"
               if a.only in (None, "tilt") else ""))


async def main_async(args):
    robot = RobotBLE(args.device_name, fake=args.fake)
    await robot.connect()
    try:
        await MotorTest(robot, args).run()
    finally:
        await robot.send("S#", reliable=True)
        await robot.send(f"P,{PAN_CENTER}#", reliable=True)
        await robot.disconnect()


def main():
    parser = argparse.ArgumentParser(description="相機馬達測試：左右、上下各轉約 60 度再回中間")
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit，只印出指令")
    parser.add_argument("--step", action="store_true", help="每一步按 Enter 才繼續")
    parser.add_argument("--only", choices=("pan", "tilt"), help="只測左右（pan）或上下（tilt）")
    parser.add_argument("--degrees", type=int, default=DEGREES, help="左右轉幾度（上下是用秒數估的）")
    parser.add_argument("--up-seconds", type=float, help="往上轉要幾秒（沒給就用實測速度估）")
    parser.add_argument("--down-seconds", type=float, help="往下轉要幾秒（沒給就用實測速度估）")
    parser.add_argument("--up-speed", type=int, default=UP_SPEED, help="往上的轉速 0~255")
    parser.add_argument("--down-speed", type=int, default=DOWN_SPEED, help="往下的轉速 0~255")
    parser.add_argument("--device-name", default=DEVICE_NAME)
    args = parser.parse_args()
    args.degrees = max(0, min(90, args.degrees))
    args.up_speed = max(0, min(255, args.up_speed))
    args.down_speed = max(0, min(255, args.down_speed))
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log("⏹️  中斷（已送出停止並回正）")


if __name__ == "__main__":
    main()
