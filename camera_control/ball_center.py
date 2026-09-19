"""
讓相機一直對準網球：連到 tennis_tracking/server.py 拿球和人的位置，
球離畫面中心超過門檻（像素）就轉相機（左右 = 伺服馬達 P、上下 = 直流馬達 Z），把球拉回中間。

    # 1. 先開追蹤伺服器（它負責開相機、跑 YOLO）
    .venv/bin/python tennis_tracking/server.py
    # 2. 再開這支
    .venv/bin/python camera_control/ball_center.py --fake                 # 不連車，只印指令
    .venv/bin/python camera_control/ball_center.py                        # 連 micro:bit，真的轉
    .venv/bin/python camera_control/ball_center.py --threshold 40         # 門檻 40 px（左右、上下一樣）
    .venv/bin/python camera_control/ball_center.py --threshold-x 30 --threshold-y 50
    .venv/bin/python camera_control/ball_center.py --json > log.jsonl     # 每張畫面印一行 JSON

看畫面：瀏覽器開 http://127.0.0.1:8000/video（tennis_tracking 的標框影像）。

執行中在終端機打指令、按 Enter（不用停程式就能調）：
    t 40      左右、上下門檻都設成 40 px       tx 30 / ty 50   只設左右 / 上下
    kp 0.8    每次修正誤差的幾成（0~1）        n 1      每次看幾張畫面再決定
    p         暫停／繼續      c   相機回正前方      s   顯示目前設定      q   離開

控制方式：看畫面決定，一個循環一個循環來（不一直送「再轉 1 度」的小指令，馬達才不會一抽一抽）
    1. 觀察：收集連續 N 張畫面（--frames，預設 1 = 每張都直接跟）的球位置
    2. 決定：取平均位置，再加上這幾張看出來的移動速度往前預估一點（球在動時才跟得上）；
       大部分畫面都在門檻外、而且在同一邊才動（單張的雜訊不會觸發）
    3. 移動：左右送一個目標角度（一步最多 PAN_MAX_STEP 度）
    4. 左右等畫面穩定：這段時間的畫面不拿來決定左右；然後回到 1
    上下（直流馬達）不用等：實測轉速跟畫面移動速度很線性，每張畫面直接依誤差設轉速
    （誤差越大轉越快），球回到門檻內就停；往上從靜止起步先衝一下
    左右才要等畫面反應過來（伺服一次跳到位，太快再決定會轉過頭來回）

方向：伺服 0 = 最右、180 = 最左 → 球在畫面右邊（dx > 0）角度要變小；
      上下實測：這台車 Z 負數 = 相機往上（跟 micro:bit 程式的註解相反，見 TILT_UP_SIGN）。
      實際方向相反就加 --invert-pan / --invert-tilt。
"""

import argparse
import asyncio
import json
import sys
import threading
import time

import websockets

from robot_ble import DEVICE_NAME, RobotBLE, log

# ====== 想改的東西都在這裡 ======
SERVER_URL = "ws://127.0.0.1:8000/ws"
THRESHOLD_PX = 30        # 球心離畫面中心幾個像素以內就不動（死區）；--threshold 可以改
# 觀察
FRAMES = 1               # 每次看幾張畫面再決定（1 = 每張都直接跟；調大會比較穩但比較慢）
AGREE = 0.75             # 這幾張裡至少這個比例在門檻外、而且在同一邊，才動
LEAD = 0.0               # 球在動時，照這幾張看出來的速度往前預估幾秒（0 = 不預估）
# 左右（伺服馬達，給絕對角度）
PAN_GAIN = 0.8           # 每次修正誤差的幾成（1 = 一次轉到算出來的位置；小一點比較不會衝過頭）
PAN_MIN_STEP = 2.0       # 最少轉幾度（不送「再轉 1 度」這種小指令）
PAN_MAX_STEP = 5.0       # 防護：任何一個 P 指令最多只比上一個差這麼多度（一次轉太多會噴出去；大誤差分幾步走）
PAN_SETTLE = 0.2         # 送完角度，等幾秒讓畫面反應過來（實測延遲約 0.12 秒 + 伺服轉動）
PAN_PX_PER_DEG = 13.5    # 實測：伺服轉 1 度，畫面移動幾個像素（640×480，水平視角約 47 度）
PAN_LATENCY = 0.12       # 實測：送出 P 指令到畫面上看到相機動
PAN_MIN, PAN_MAX, PAN_CENTER = 0, 180, 90
# 上下（直流馬達，只能控制轉速）：每張畫面直接設轉速，不等（實測轉速跟畫面移動速度很線性）
# 實測（vapup 那台，2026-09-19 重量）：Z 正數 = 相機往上；上下幾乎對稱；
#   畫面每秒移動 ≈ 6.5 × (轉速 − 40)：40 幾乎不動、60 ≈ 130 px/s、80 ≈ 280 px/s、100 ≈ 400 px/s、120 ≈ 490 px/s
#   延遲約 0.13～0.19 秒；轉速 80 以上停下後還會滑 25～70 px → 轉速上限不要太高
TILT_UP_SIGN = 1         # Z 正數 = 相機往上（跟 micro:bit 程式的註解一樣；另一台車是反的，要改成 -1）
# 球剛超過門檻 → 球在畫面最上／最下，轉速線性變化
TILT_MIN_SPEED_UP, TILT_MAX_SPEED_UP = 60, 90
TILT_MIN_SPEED_DOWN, TILT_MAX_SPEED_DOWN = 60, 90
TILT_SPEED_STEP = 5      # 轉速取到 5 的倍數，速度小變化不用一直重送藍牙指令
TILT_KICK_SPEED_UP = 0   # 往上從靜止起步先衝一下的轉速（0 = 不衝；這台 60 從靜止就推得動）
TILT_KICK_SECONDS = 0.1
TILT_DEADBAND = 40       # 實測：轉速低於這個幾乎不動（給模擬和 motor_test 估算用）
TILT_PX_PER_SPEED = 6.5  # 實測：超過 TILT_DEADBAND 之後，每 1 單位轉速畫面每秒多移動幾像素
TILT_LATENCY = 0.16      # 實測：送出 Z 指令到畫面上看到相機動（模擬用）
KEEPALIVE_INTERVAL = 0.1  # 上下在轉的時候每幾秒重送一次 Z（micro:bit 0.5 秒沒收到就停）
# 其他
STALE_SECONDS = 0.5      # 超過幾秒沒收到新畫面就當作斷線，上下馬達停下來
STATUS_INTERVAL = 0.2    # 沒加 --json 時，每幾秒印一次狀態
# ================================


def clamp(value, low, high):
    return max(low, min(high, value))


# ---------- 從 server 收資料：只留最新的一筆 ----------

class Feed:
    def __init__(self):
        self.latest = None
        self.seq = 0
        self.received_at = 0.0
        self.event = asyncio.Event()

    async def receive_forever(self, url):
        """背景工作：一直收，只留最新的；斷線就一秒後重連。"""
        while True:
            try:
                async with websockets.connect(url) as ws:
                    log(f"✅ 連上追蹤伺服器 {url}")
                    async for message in ws:
                        self.latest = json.loads(message)
                        self.seq += 1
                        self.received_at = time.monotonic()
                        self.event.set()
            except (OSError, websockets.ConnectionClosed, websockets.InvalidHandshake):
                log(f"⚠️  連不上 {url}（tennis_tracking/server.py 有開嗎？），一秒後重試 ...")
                await asyncio.sleep(1)


# ---------- 選要對準哪顆球 ----------

def choose_target(balls, locked_id):
    """一直跟同一顆球（locked_id）；它不見了（tracker 刪掉了）才換成現在最大（最近）、這張真的看到的球。"""
    for b in balls:
        if b["id"] == locked_id:
            return b
    seen = [b for b in balls if b["misses"] == 0]
    return max(seen, key=lambda b: b["d"]) if seen else None


# ---------- 控制 ----------

class CameraController:
    def __init__(self, robot, args):
        self.robot = robot
        self.threshold_x = args.threshold_x
        self.threshold_y = args.threshold_y
        self.kp = args.kp            # 左右每次修正誤差的幾成
        self.frames = args.frames    # 每次看幾張畫面再決定
        self.pan_sign = -1 if args.invert_pan else 1
        self.tilt_sign = -1 if args.invert_tilt else 1
        self.use_tilt = not args.no_tilt
        self.paused = False
        self.locked_id = None
        self.state = "waiting"
        self.phase = "observe"
        # 左右、上下各自收集畫面、各自等（上下轉一段要比較久，不要拖到左右）
        self.win_x, self.win_y = [], []     # 這一輪收集到的 (時間, dx) / (時間, dy)
        self.pan_busy = 0.0                 # 這個時間之前左右在轉或等畫面穩定（上下不用等）
        self.ax = self.ay = None        # 上一次決定用的平均（含預估）位置，畫面顯示用
        self.seen_disconnects = 0
        # 左右
        self.pan = float(PAN_CENTER)    # 最後送出的角度
        self.pan_step = 0.0             # 最後一次轉了幾度（畫面顯示用）
        # 上下
        self.tilt_speed = 0             # 現在送出去的 Z 值（0 = 停）
        self.tilt_until = 0.0           # 這一段轉到什麼時候
        self.kick_until = 0.0
        self.kick_speed = 0             # 衝完要換回的 Z 值
        self.half_h = 240.0
        self.last_tilt_send = 0.0

    async def send_pan(self, angle, now):
        """送左右角度。防護：一次最多只轉 PAN_MAX_STEP 度（不管是誰要轉、要轉多少）。"""
        angle = clamp(angle, self.pan - PAN_MAX_STEP, self.pan + PAN_MAX_STEP)
        angle = int(round(clamp(angle, PAN_MIN, PAN_MAX)))
        self.pan_step = angle - self.pan
        self.pan = float(angle)
        await self.robot.send(f"P,{angle}#")

    async def set_tilt(self, speed, now):
        """設定上下；速度變了、或該 keep-alive 了才送。"""
        if speed == 0:
            if self.tilt_speed != 0:
                await self.robot.send("S#", reliable=True)
            self.tilt_speed = 0
            return
        if speed != self.tilt_speed or now - self.last_tilt_send >= KEEPALIVE_INTERVAL:
            await self.robot.send(f"Z,{speed}#")
            self.last_tilt_send = now
        self.tilt_speed = speed

    def z_value(self, up, speed):
        return abs(speed) * (1 if up else -1) * TILT_UP_SIGN * self.tilt_sign

    async def keep_tilt(self, now):
        """上下在轉的時候：衝完換回正常轉速、keep-alive；沒真的看到球就停。"""
        if not self.tilt_speed:
            return
        if self.state != "tracking":
            await self.set_tilt(0, now)
        elif now >= self.kick_until and self.kick_speed:
            await self.set_tilt(self.kick_speed, now)
            self.kick_speed = 0
        else:
            await self.set_tilt(self.tilt_speed, now)   # keep-alive

    @staticmethod
    def summarize(window):
        """幾張的平均 + 速度預估；回傳 (預估位置, 平均位置, 平均時間, 速度, 全部的值)。"""
        t = [w[0] for w in window]
        v = [w[1] for w in window]
        mean, tm = sum(v) / len(v), sum(t) / len(t)
        span = t[-1] - t[0]
        speed = (v[-1] - v[0]) / span if span > 0.05 else 0.0
        return mean + speed * LEAD, mean, tm, speed, v

    @staticmethod
    def agree(values, predicted, threshold):
        """在門檻外、而且跟預估同一邊的比例。"""
        return sum(1 for v in values if abs(v) > threshold and (v > 0) == (predicted > 0)) / len(values)

    async def decide_pan(self, now):
        px, _, _, _, xs = self.summarize(self.win_x)
        self.win_x = []
        self.ax = px
        if abs(px) > self.threshold_x and self.agree(xs, px, self.threshold_x) >= AGREE:
            step = -self.kp * px / PAN_PX_PER_DEG * self.pan_sign     # 球在右（px > 0）→ 角度變小
            if abs(step) < PAN_MIN_STEP:
                step = PAN_MIN_STEP if step > 0 else -PAN_MIN_STEP
            await self.send_pan(self.pan + step, now)                 # send_pan 會限制一次最多 PAN_MAX_STEP 度
            self.pan_busy = now + PAN_SETTLE

    async def decide_tilt(self, now):
        """上下：誤差越大轉越快（線性），球回到門檻內就停。每張畫面都直接更新，不等。"""
        py, _, _, _, ys = self.summarize(self.win_y)
        self.win_y = []
        self.ay = py
        if abs(py) <= self.threshold_y or self.agree(ys, py, self.threshold_y) < AGREE:
            await self.set_tilt(0, now)
            return
        up = py < 0
        low, high = (TILT_MIN_SPEED_UP, TILT_MAX_SPEED_UP) if up else (TILT_MIN_SPEED_DOWN, TILT_MAX_SPEED_DOWN)
        over = (abs(py) - self.threshold_y) / max(self.half_h - self.threshold_y, 1)
        speed = low + (high - low) * clamp(over, 0, 1)
        speed = int(round(speed / TILT_SPEED_STEP) * TILT_SPEED_STEP)
        z = self.z_value(up, speed)
        starting = self.tilt_speed == 0 or (self.tilt_speed > 0) != (z > 0)
        if up and starting and TILT_KICK_SPEED_UP and speed < TILT_KICK_SPEED_UP:   # 往上從靜止起步：先衝一下
            self.kick_until, self.kick_speed = now + TILT_KICK_SECONDS, z
            await self.set_tilt(self.z_value(True, TILT_KICK_SPEED_UP), now)
        elif now < self.kick_until and self.kick_speed:
            self.kick_speed = z                                   # 還在衝：衝完再換成最新的轉速
        else:
            await self.set_tilt(z, now)

    async def update(self, data, fresh, now):
        """data = server 送來的最新一張；fresh = 這張是不是剛收到、還沒處理過的。回傳要輸出的 control 資訊。"""
        target = None
        if data is None or now - data["_received_at"] > STALE_SECONDS:
            self.state = "no_data"
        else:
            target = choose_target(data["balls"], self.locked_id)
            if target is None:
                self.state = "lost"
            else:
                if target["id"] != self.locked_id:
                    self.win_x, self.win_y = [], []    # 換了一顆球：重新收集
                self.locked_id = target["id"]
                self.state = "paused" if self.paused else ("predict" if target["misses"] else "tracking")

        # micro:bit 斷線重連（可能重置過，伺服會自己跳回 90）：把我們以為的角度重送一次
        if self.robot.disconnects != self.seen_disconnects and self.robot.connected:
            self.seen_disconnects = self.robot.disconnects
            log(f"🔄 重連後重送角度 P,{int(self.pan)}")
            await self.robot.send(f"P,{int(self.pan)}#", reliable=True)
            self.tilt_speed = 0

        await self.keep_tilt(now)

        dx = dy = None
        if target is not None:
            dx, dy = target["x"] - data["width"] / 2, target["y"] - data["height"] / 2
            self.half_h = data["height"] / 2
        n = max(1, int(self.frames))
        if self.state != "tracking":
            self.win_x, self.win_y = [], []      # 沒真的看到球：這一輪重來
        elif fresh:
            if now >= self.pan_busy:             # 左右在轉或等畫面穩定時，這張不拿來決定左右
                self.win_x.append((now, dx))
                if len(self.win_x) >= n:
                    await self.decide_pan(now)
            if self.use_tilt:                    # 上下不用等：每張都直接更新轉速
                self.win_y.append((now, dy))
                if len(self.win_y) >= n:
                    await self.decide_tilt(now)
        self.phase = "move" if now < self.pan_busy or self.tilt_speed else "observe"

        return target, {
            "state": self.state,
            "phase": self.phase,
            "locked_id": self.locked_id,
            "pan": int(self.pan),
            "pan_step": round(self.pan_step, 1) if now < self.pan_busy else 0.0,
            "tilt_speed": self.tilt_speed,
            "threshold_x": self.threshold_x,
            "threshold_y": self.threshold_y,
            "kp": self.kp,
            "frames": self.frames,
            "dx_px": dx, "dy_px": dy,
            "dx_avg": self.ax if target is not None else None,
            "dy_avg": self.ay if target is not None else None,
            "centered_x": None if dx is None else abs(dx) <= self.threshold_x,
            "centered_y": None if dy is None else abs(dy) <= self.threshold_y,
        }

    async def center(self, now):
        """回正：每次最多轉 PAN_MAX_STEP 度，一步一步轉回 90。"""
        await self.set_tilt(0, now)
        while int(self.pan) != PAN_CENTER:
            await self.send_pan(PAN_CENTER, now)
            await asyncio.sleep(PAN_SETTLE / 2)
        now = time.monotonic()
        self.win_x, self.win_y, self.pan_busy = [], [], now + PAN_SETTLE

    def handle_command(self, line):
        """終端機輸入的指令；回傳 'quit'、'center' 或 None。"""
        parts = line.split()
        if not parts:
            return None
        cmd, value = parts[0].lower(), parts[1] if len(parts) > 1 else None
        try:
            if cmd in ("t", "tx", "ty", "kp", "n") and value is None:
                raise ValueError
            if cmd == "t":
                self.threshold_x = self.threshold_y = max(0.0, float(value))
            elif cmd == "tx":
                self.threshold_x = max(0.0, float(value))
            elif cmd == "ty":
                self.threshold_y = max(0.0, float(value))
            elif cmd == "kp":
                self.kp = clamp(float(value), 0.0, 1.5)
            elif cmd == "n":
                self.frames = int(clamp(float(value), 1, 30))
            elif cmd == "p":
                self.paused = not self.paused
                log("⏸️  暫停" if self.paused else "▶️  繼續")
                return None
            elif cmd == "c":
                return "center"
            elif cmd == "q":
                return "quit"
            elif cmd != "s":
                log("指令：t 40 | tx 30 | ty 50 | kp 0.8 | n 1 | p 暫停 | c 回正 | s 設定 | q 離開")
                return None
        except ValueError:
            log(f"❌ 看不懂「{line}」，例如：t 40")
            return None
        log(f"⚙️  門檻 x {self.threshold_x:g}px  y {self.threshold_y:g}px  Kp {self.kp:g}  每 {self.frames} 張決定一次"
            + ("  （暫停中）" if self.paused else ""))
        return None


# ---------- 輸出 ----------

_TARGET_KEYS = ("dx_px", "dy_px", "dx_avg", "dy_avg", "centered_x", "centered_y")


def build_output(data, target, control):
    """一張畫面的完整結果：要對準的球 + 所有球 + 所有人 + 控制狀態。"""
    out = {k: data[k] for k in ("frame", "time", "width", "height", "fps")} if data else {}
    out["target"] = None if target is None else {
        **target,
        "dx_px": round(control["dx_px"], 1), "dy_px": round(control["dy_px"], 1),
        "dx_avg": None if control["dx_avg"] is None else round(control["dx_avg"], 1),
        "dy_avg": None if control["dy_avg"] is None else round(control["dy_avg"], 1),
        "centered_x": control["centered_x"], "centered_y": control["centered_y"],
    }
    out["balls"] = data["balls"] if data else []
    out["people"] = data["people"] if data else []
    out["control"] = {k: v for k, v in control.items() if k not in _TARGET_KEYS}
    return out


def status_line(out):
    c, t = out["control"], out["target"]
    ball = "沒有球" if t is None else (
        f"球 #{t['id']} dx {t['dx_px']:+6.1f} dy {t['dy_px']:+6.1f} d {t['d']:5.1f}px "
        f"{'✅置中' if t['centered_x'] and t['centered_y'] else '↔️ 對準中'}")
    return (f"[{c['state']:>8}] {ball}  |  pan {c['pan']}  tilt {c['tilt_speed']:+d}  "
            f"門檻 {c['threshold_x']:g}/{c['threshold_y']:g}px  人 {len(out['people'])}")


def start_stdin_reader(loop, queue):
    """背景執行緒讀終端機輸入，丟進 asyncio queue；在終端機按 Ctrl-D 等於 q。"""
    def run():
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line.strip())
        if sys.stdin.isatty():
            loop.call_soon_threadsafe(queue.put_nowait, "q")
    threading.Thread(target=run, daemon=True).start()


async def control_loop(controller, feed, commands, on_output):
    """主迴圈（ball_center.py 和 test_view.py 共用）：等新畫面 → 處理指令 → 控制相機 → on_output(out)。
    commands 是放「t 40」這種文字指令的 asyncio.Queue；收到 q 就結束。"""
    handled_seq = 0
    while True:
        # 等新畫面；最多等 KEEPALIVE_INTERVAL，上下在轉的時候才能準時重送、斷線也能馬上停
        try:
            await asyncio.wait_for(feed.event.wait(), timeout=KEEPALIVE_INTERVAL)
        except asyncio.TimeoutError:
            pass
        feed.event.clear()

        while not commands.empty():
            action = controller.handle_command(commands.get_nowait())
            if action == "quit":
                return
            if action == "center":
                await controller.center(time.monotonic())

        now = time.monotonic()
        data = feed.latest
        if data is not None:
            data = {**data, "_received_at": feed.received_at}
        fresh = feed.seq != handled_seq
        handled_seq = feed.seq
        target, control = await controller.update(data, fresh, now)
        if fresh:
            on_output(build_output(feed.latest, target, control))


async def stop_camera(robot):
    """結束時：馬達停、相機回正、斷線。"""
    try:
        await robot.send("S#", reliable=True)
        await robot.send(f"P,{PAN_CENTER}#", reliable=True)
    finally:
        await robot.disconnect()


# ---------- 主程式 ----------

async def run(args):
    robot = RobotBLE(args.device_name, fake=args.fake, quiet=args.json or args.quiet)
    await robot.connect()
    controller = CameraController(robot, args)
    await controller.center(time.monotonic())

    feed = Feed()
    receiver = asyncio.create_task(feed.receive_forever(args.url))
    commands = asyncio.Queue()
    start_stdin_reader(asyncio.get_running_loop(), commands)

    log(f"🎯 對準網球  門檻 x {controller.threshold_x:g}px  y {controller.threshold_y:g}px  Kp {controller.kp:g}"
        f"  每 {controller.frames} 張決定一次"
        + ("" if controller.use_tilt else "  （不控制上下）")
        + "\n   終端機打 t 40 改門檻、p 暫停、c 回正、q 離開（打 ? 看全部指令）")
    last_status = 0.0

    def on_output(out):
        nonlocal last_status
        if args.json:
            print(json.dumps(out, ensure_ascii=False), flush=True)
        elif time.monotonic() - last_status >= STATUS_INTERVAL:
            log(status_line(out))
            last_status = time.monotonic()

    try:
        await control_loop(controller, feed, commands, on_output)
    finally:
        receiver.cancel()
        await stop_camera(robot)


def main():
    parser = argparse.ArgumentParser(description="讓相機一直對準網球（需要先開 tennis_tracking/server.py）")
    parser.add_argument("--url", default=SERVER_URL, help="tennis_tracking server 的 WebSocket 網址")
    parser.add_argument("--threshold", type=float, default=THRESHOLD_PX, help="門檻（像素），左右上下一樣")
    parser.add_argument("--threshold-x", type=float, help="左右門檻（像素），沒給就用 --threshold")
    parser.add_argument("--threshold-y", type=float, help="上下門檻（像素），沒給就用 --threshold")
    parser.add_argument("--kp", type=float, default=PAN_GAIN, help="左右每次修正誤差的幾成（0~1）")
    parser.add_argument("--frames", type=int, default=FRAMES, help="每次看幾張畫面再決定（預設 1 = 每張都直接跟）")
    parser.add_argument("--invert-pan", action="store_true", help="左右轉的方向相反時使用")
    parser.add_argument("--invert-tilt", action="store_true", help="上下轉的方向相反時使用")
    parser.add_argument("--no-tilt", action="store_true", help="只控制左右，上下不動")
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit，只印出指令")
    parser.add_argument("--json", action="store_true", help="每張畫面在 stdout 印一行 JSON（其他訊息在 stderr）")
    parser.add_argument("--quiet", action="store_true", help="不印每一個送出的藍牙指令")
    parser.add_argument("--device-name", default=DEVICE_NAME, help="micro:bit 藍牙名稱，逗號分開依優先順序（預設 vapup,zutog）")
    args = parser.parse_args()
    args.threshold_x = args.threshold if args.threshold_x is None else args.threshold_x
    args.threshold_y = args.threshold if args.threshold_y is None else args.threshold_y
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    log("🔚 結束")


if __name__ == "__main__":
    main()
