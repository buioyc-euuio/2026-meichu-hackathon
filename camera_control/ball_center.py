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
    kp 110    左右的 Kp（球在畫面邊邊時每秒轉幾度）   sm 0.5   平滑程度（EMA 的 alpha）
    p         暫停／繼續      c   相機回正前方      s   顯示目前設定      q   離開

平滑機制（讓相機不抖、又不會太慢）：
    1. EMA：球的位置先做指數平滑（alpha 越小越平滑、但反應越慢），把偵測的雜訊濾掉
    2. 遲滯：球超過門檻才開始轉，要回到「門檻 × STOP_RATIO」以內才停，不會在門檻邊緣一直開開關關
    3. 左右用「速度」控制：轉速跟誤差成正比（Kp），速度變化有加速度限制，起步、停下都是漸進的

    4. 延遲補償（Smith predictor）：從送指令到畫面上看到相機動，左右約 0.12 秒、上下約 0.2 秒。
       記住最近送出的指令，先把「已經送出、但畫面還沒反應」的那段移動算進去，才不會一直轉過頭

方向：伺服 0 = 最右、180 = 最左 → 球在畫面右邊（dx > 0）角度要變小；
      上下實測：這台車 Z 負數 = 相機往上（跟 micro:bit 程式的註解相反，見 TILT_UP_SIGN）。
      實際方向相反就加 --invert-pan / --invert-tilt。
"""

import argparse
import asyncio
import collections
import json
import sys
import threading
import time

import websockets

from robot_ble import DEVICE_NAME, RobotBLE, log

# ====== 想改的東西都在這裡 ======
SERVER_URL = "ws://127.0.0.1:8000/ws"
THRESHOLD_PX = 30        # 球心離畫面中心幾個像素以內就不動（死區）；--threshold 可以改
# 平滑
SMOOTH = 0.5             # EMA 的 alpha（0~1）：球的位置每張畫面往新量到的值靠多少；1 = 不平滑，越小越平滑但越慢
STOP_RATIO = 0.5         # 遲滯：超過門檻開始轉，回到「門檻 × 這個」以內才停
# 左右（伺服馬達，絕對角度）
PAN_KP = 110.0           # 球在畫面最邊邊時，每秒轉幾度（誤差越小轉越慢）；太大會來回晃、太小跟不上
PAN_MAX_SPEED = 120.0    # 最快每秒轉幾度
PAN_ACCEL = 500.0        # 轉速每秒最多變化幾度/秒（加減速限制，起步和停下才不會一頓一頓）
PAN_PX_PER_DEG = 13.5    # 實測：伺服轉 1 度，畫面移動幾個像素（640×480，水平視角約 47 度）
PAN_LATENCY = 0.12       # 實測：送出 P 指令到畫面上看到相機動，大約幾秒（藍牙 + 伺服 + 相機）
PAN_MIN, PAN_MAX, PAN_CENTER = 0, 180, 90
PAN_SEND_INTERVAL = 0.05  # 最快每幾秒送一次 P 指令（藍牙送太快會塞車）
# 上下（直流馬達，只能控制速度）
TILT_UP_SIGN = -1        # 實測：這台車送 Z 負數相機往上（micro:bit 程式寫正數往上，但接線是反的）
# 往上要對抗重力、往下有重力幫忙，兩個方向差很多，所以分開設（實測，轉速 → 畫面每秒移動）：
#   往上：50 推不動、70 ≈ 140 px/s、90 ≈ 180 px/s、110 ≈ 250 px/s
#   往下：50 ≈ 135 px/s、70 ≈ 210 px/s、90 ≈ 285 px/s、110 ≈ 400 px/s
TILT_MIN_SPEED_UP, TILT_MAX_SPEED_UP = 85, 115        # 剛超過門檻 → 球在畫面最上面的轉速
TILT_MIN_SPEED_DOWN, TILT_MAX_SPEED_DOWN = 45, 75
TILT_KICK_SPEED_UP = 130      # 往上從靜止起步：先用這個轉速衝一下，突破靜摩擦 + 重力（實測 75～80 從靜止常常推不動）
TILT_KICK_SECONDS = 0.12
TILT_PX_PER_SPEED_UP = 2.0    # 每 1 單位轉速，畫面每秒移動幾個像素（給延遲補償估算用）
TILT_PX_PER_SPEED_DOWN = 3.0
TILT_SPEED_STEP = 5      # 轉速取到 5 的倍數，速度小變化不用一直重送藍牙指令
TILT_LATENCY = 0.19      # 實測：送出 Z 指令到畫面上看到相機動（停也一樣會晚這麼久）
TILT_LIMIT_PX = 250      # 軟體限位：依指令估計相機上下離「啟動位置」多遠（畫面像素，約 18 度），超過就不再往那邊轉
                         # → 啟動前先把相機扶到平衡點（大概正前方）
TILT_MAX_SECONDS = 2.0   # 同一個方向最多連續轉幾秒（沒有限位開關，轉太久可能卡住；TILT_LIMIT_PX 之外的第二道保險）
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
        self.kp = args.kp
        self.smooth = args.smooth
        self.pan_sign = -1 if args.invert_pan else 1
        self.tilt_sign = -1 if args.invert_tilt else 1
        self.use_tilt = not args.no_tilt
        self.paused = False
        self.locked_id = None
        self.state = "waiting"
        self.last_update = None
        # 平滑後的球位置（離畫面中心的像素）；換球或跟丟就重來
        self.fx = self.fy = None
        self.filter_id = None
        self.moving_x = self.moving_y = False   # 遲滯：現在是不是在轉
        # 左右
        self.pan = float(PAN_CENTER)
        self.pan_vel = 0.0           # 現在的轉速（度/秒；正 = 角度變大 = 往左）
        # 延遲補償：最近送出的指令，預測「畫面還沒反應」的移動。(時間, 送出的角度, 上下累積的預測畫面位移)
        self.history = collections.deque()
        self.tilt_shift = 0.0        # 依上下指令積分出來的畫面位移（px，+ = 畫面內容往下 = 相機往上）
        self.pred_x = self.pred_y = None
        self.sent_pan = None
        self.last_pan_send = 0.0
        # 上下
        self.tilt_speed = 0          # 現在送出去的 Z 速度（有正負號，0 = 停）
        self.last_tilt_send = 0.0
        self.tilt_started = 0.0      # 這個方向開始連續轉的時間
        self.tilt_blocked = 0        # 轉太久被擋住的方向（+1 / -1），球回到門檻內或換邊才解除
        self.kick_until = 0.0        # 往上起步「衝一下」到什麼時候

    async def send_pan(self, now, force=False):
        angle = int(round(self.pan))
        if force or (angle != self.sent_pan and now - self.last_pan_send >= PAN_SEND_INTERVAL):
            await self.robot.send(f"P,{angle}#")
            self.sent_pan, self.last_pan_send = angle, now

    async def set_tilt(self, speed, now):
        """設定上下速度；速度變了、或該 keep-alive 了才送。"""
        if speed == 0:
            if self.tilt_speed != 0:
                await self.robot.send("S#", reliable=True)
            self.tilt_speed = 0
            return
        if (speed > 0) != (self.tilt_speed > 0) or self.tilt_speed == 0:
            self.tilt_started = now   # 換方向或從停止開始：重新計時
            if speed * TILT_UP_SIGN * self.tilt_sign > 0:
                self.kick_until = now + TILT_KICK_SECONDS   # 往上起步：先衝一下
        if now < self.kick_until and speed * TILT_UP_SIGN * self.tilt_sign > 0:
            speed = TILT_KICK_SPEED_UP * (1 if speed > 0 else -1)
        if speed != self.tilt_speed or now - self.last_tilt_send >= KEEPALIVE_INTERVAL:
            await self.robot.send(f"Z,{speed}#")
            self.last_tilt_send = now
        self.tilt_speed = speed

    def tilt_speed_for(self, dy, H):
        """回傳 Z 指令的速度。球在上面（dy < 0）→ 相機往上。超過門檻越多轉越快。"""
        up = dy < 0
        low, high = (TILT_MIN_SPEED_UP, TILT_MAX_SPEED_UP) if up else (TILT_MIN_SPEED_DOWN, TILT_MAX_SPEED_DOWN)
        over = (abs(dy) - self.threshold_y) / max(H / 2 - self.threshold_y, 1)
        speed = low + (high - low) * clamp(over, 0, 1)
        speed = int(round(speed / TILT_SPEED_STEP) * TILT_SPEED_STEP)
        return speed * (1 if up else -1) * TILT_UP_SIGN * self.tilt_sign

    def predict(self, now):
        """延遲補償：畫面上的球位置是 LATENCY 秒前的；把之後送出、畫面還沒反應的移動加上去。"""
        pan_then = tilt_then = None
        for t, pan, shift in self.history:
            if t <= now - PAN_LATENCY:
                pan_then = pan
            if t <= now - TILT_LATENCY:
                tilt_then = shift
        # 左右：角度變大（往左）→ 畫面內容往右 → dx 變大
        dpan = 0.0 if pan_then is None else (self.pan - pan_then)
        # 上下：累積畫面位移的差
        dtilt = 0.0 if tilt_then is None else (self.tilt_shift - tilt_then)
        return self.fx + dpan * PAN_PX_PER_DEG * self.pan_sign, self.fy + dtilt

    def record(self, now, dt):
        """記下這次送出的指令（給 predict 用）。"""
        up = self.tilt_speed * TILT_UP_SIGN * self.tilt_sign    # 正 = 相機往上
        gain = TILT_PX_PER_SPEED_UP if up > 0 else TILT_PX_PER_SPEED_DOWN
        self.tilt_shift += up * gain * dt                       # 相機往上 → 畫面內容往下（+y）
        self.history.append((now, self.pan, self.tilt_shift))
        while self.history and self.history[0][0] < now - max(PAN_LATENCY, TILT_LATENCY) - 0.5:
            self.history.popleft()

    @staticmethod
    def hysteresis(moving, err, threshold):
        """超過門檻開始轉；在轉的話，要回到 threshold × STOP_RATIO 以內才停。"""
        return abs(err) > threshold * STOP_RATIO if moving else abs(err) > threshold

    def filter(self, target, dx, dy):
        """EMA 平滑球的位置；換了一顆球就從新的位置重新開始。"""
        if self.fx is None or target["id"] != self.filter_id:
            self.fx, self.fy, self.filter_id = dx, dy, target["id"]
        else:
            a = clamp(self.smooth, 0.01, 1.0)
            self.fx += a * (dx - self.fx)
            self.fy += a * (dy - self.fy)

    async def update(self, data, fresh, now):
        """data = server 送來的最新一張；fresh = 這張是不是剛收到、還沒處理過的。回傳要輸出的 control 資訊。"""
        dt = 0.0 if self.last_update is None else clamp(now - self.last_update, 0.0, 0.2)
        self.last_update = now
        target = None
        if data is None or now - data["_received_at"] > STALE_SECONDS:
            self.state = "no_data"
        else:
            target = choose_target(data["balls"], self.locked_id)
            if target is None:
                self.state = "lost"
            else:
                self.locked_id = target["id"]
                self.state = "paused" if self.paused else ("predict" if target["misses"] else "tracking")

        # 只用「這張真的看到的」球來控制；預測的位置、暫停、跟丟都先不動
        acting = self.state == "tracking"
        dx = dy = None
        if target is None:
            self.fx = self.fy = None
        else:
            W, H = data["width"], data["height"]
            dx, dy = target["x"] - W / 2, target["y"] - H / 2
            if fresh:
                self.filter(target, dx, dy)

        # 延遲補償後的誤差（控制都用這個）
        px = py = None
        if self.fx is not None:
            px, py = self.predict(now)
        self.pred_x, self.pred_y = px, py

        # 左右：目標轉速跟誤差成正比，實際轉速用加速度限制慢慢追上去
        want = 0.0
        self.moving_x = acting and self.hysteresis(self.moving_x, px, self.threshold_x)
        if self.moving_x:
            want = clamp(-self.kp * px / (W / 2), -PAN_MAX_SPEED, PAN_MAX_SPEED) * self.pan_sign
        if self.state == "no_data":
            self.pan_vel = 0.0
        else:
            self.pan_vel += clamp(want - self.pan_vel, -PAN_ACCEL * dt, PAN_ACCEL * dt)
        self.pan += self.pan_vel * dt
        if not PAN_MIN <= self.pan <= PAN_MAX:
            self.pan, self.pan_vel = clamp(self.pan, PAN_MIN, PAN_MAX), 0.0
        await self.send_pan(now)

        # 上下
        speed = 0
        self.moving_y = acting and self.use_tilt and self.hysteresis(self.moving_y, py, self.threshold_y)
        if acting and self.use_tilt:
            if not self.moving_y:
                self.tilt_blocked = 0                 # 球回到門檻內：解除
            else:
                speed = self.tilt_speed_for(py, H)
                direction = 1 if speed > 0 else -1
                if self.tilt_blocked and direction != self.tilt_blocked:
                    self.tilt_blocked = 0             # 球換到另一邊了：解除
                going_up = speed * TILT_UP_SIGN * self.tilt_sign > 0
                if direction == self.tilt_blocked:
                    speed = 0
                elif (self.tilt_shift > TILT_LIMIT_PX and going_up) or (self.tilt_shift < -TILT_LIMIT_PX and not going_up):
                    speed = 0                         # 軟體限位：估計已經轉很遠了，不再往這邊轉
                elif self.tilt_speed and (self.tilt_speed > 0) == (speed > 0) \
                        and now - self.tilt_started > TILT_MAX_SECONDS:
                    self.tilt_blocked = direction
                    log(f"⛔ 相機{'上' if direction > 0 else '下'}轉超過 {TILT_MAX_SECONDS} 秒，先停"
                        "（球回到門檻內或換邊才解除）")
                    speed = 0
        await self.set_tilt(speed, now)
        self.record(now, dt)

        return target, {
            "state": self.state,
            "locked_id": self.locked_id,
            "pan": self.sent_pan,
            "pan_speed": round(self.pan_vel, 1),
            "tilt_speed": self.tilt_speed,
            "tilt_blocked": self.tilt_blocked,
            "tilt_pos_px": round(self.tilt_shift),
            "threshold_x": self.threshold_x,
            "threshold_y": self.threshold_y,
            "kp": self.kp,
            "smooth": self.smooth,
            "dx_px": dx, "dy_px": dy,
            "dx_smooth": self.fx, "dy_smooth": self.fy,
            "dx_pred": px, "dy_pred": py,
            "centered_x": None if dx is None else abs(dx) <= self.threshold_x,
            "centered_y": None if dy is None else abs(dy) <= self.threshold_y,
        }

    async def center(self, now):
        self.pan, self.pan_vel = float(PAN_CENTER), 0.0
        self.tilt_blocked = 0     # 回正也順便解除「上下轉太久」的鎖
        await self.set_tilt(0, now)
        await self.send_pan(now, force=True)

    def handle_command(self, line):
        """終端機輸入的指令；回傳 'quit'、'center' 或 None。"""
        parts = line.split()
        if not parts:
            return None
        cmd, value = parts[0].lower(), parts[1] if len(parts) > 1 else None
        try:
            if cmd in ("t", "tx", "ty", "kp", "sm") and value is None:
                raise ValueError
            if cmd == "t":
                self.threshold_x = self.threshold_y = max(0.0, float(value))
            elif cmd == "tx":
                self.threshold_x = max(0.0, float(value))
            elif cmd == "ty":
                self.threshold_y = max(0.0, float(value))
            elif cmd == "kp":
                self.kp = max(0.0, float(value))
            elif cmd == "sm":
                self.smooth = clamp(float(value), 0.01, 1.0)
            elif cmd == "p":
                self.paused = not self.paused
                log("⏸️  暫停" if self.paused else "▶️  繼續")
                return None
            elif cmd == "c":
                return "center"
            elif cmd == "q":
                return "quit"
            elif cmd != "s":
                log("指令：t 40 | tx 30 | ty 50 | kp 110 | sm 0.5 | p 暫停 | c 回正 | s 設定 | q 離開")
                return None
        except ValueError:
            log(f"❌ 看不懂「{line}」，例如：t 40")
            return None
        log(f"⚙️  門檻 x {self.threshold_x:g}px  y {self.threshold_y:g}px  Kp {self.kp:g}  平滑 {self.smooth:g}"
            + ("  （暫停中）" if self.paused else ""))
        return None


# ---------- 輸出 ----------

_TARGET_KEYS = ("dx_px", "dy_px", "dx_smooth", "dy_smooth", "dx_pred", "dy_pred", "centered_x", "centered_y")


def build_output(data, target, control):
    """一張畫面的完整結果：要對準的球 + 所有球 + 所有人 + 控制狀態。"""
    out = {k: data[k] for k in ("frame", "time", "width", "height", "fps")} if data else {}
    out["target"] = None if target is None else {
        **target,
        "dx_px": round(control["dx_px"], 1), "dy_px": round(control["dy_px"], 1),
        "dx_smooth": round(control["dx_smooth"], 1), "dy_smooth": round(control["dy_smooth"], 1),
        "dx_pred": round(control["dx_pred"], 1), "dy_pred": round(control["dy_pred"], 1),
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
        f"  平滑 {controller.smooth:g}"
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
    parser.add_argument("--kp", type=float, default=PAN_KP, help="左右的 Kp：球在最邊邊時每秒轉幾度")
    parser.add_argument("--smooth", type=float, default=SMOOTH, help="EMA 平滑的 alpha（0~1，越小越平滑但越慢）")
    parser.add_argument("--invert-pan", action="store_true", help="左右轉的方向相反時使用")
    parser.add_argument("--invert-tilt", action="store_true", help="上下轉的方向相反時使用")
    parser.add_argument("--no-tilt", action="store_true", help="只控制左右，上下不動")
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit，只印出指令")
    parser.add_argument("--json", action="store_true", help="每張畫面在 stdout 印一行 JSON（其他訊息在 stderr）")
    parser.add_argument("--quiet", action="store_true", help="不印每一個送出的藍牙指令")
    parser.add_argument("--device-name", default=DEVICE_NAME, help="micro:bit 的藍牙名稱（包含這段就連）")
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
