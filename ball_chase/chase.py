"""
車子追網球：啟動後看到球，先原地轉把球轉到畫面中間（recenter），再記下那時球的大小和位置當基準，之後一直維持：
球變小 → 往前；變大 → 往後；球偏離基準位置（≈ 畫面中間）→ 轉回來。不用量距離、不用 settings.json。
相機鎖住不動（左右 90 度、上下不動）。

    .venv/bin/python tennis_tracking/server.py        # 1. 追蹤伺服器（開相機、YOLO）
    .venv/bin/python ball_chase/chase.py --fake       # 2. 不連車，只看算出來的輪子指令
    .venv/bin/python ball_chase/chase.py              #    連 micro:bit，真的開
    .venv/bin/python ball_chase/chase.py --max-speed 120   # 限速
    .venv/bin/python ball_chase/chase.py --view        # 開視窗看畫面和判斷（debug 用，見 view.py）

基準：看到球 → 原地一段一段轉到球在畫面中間 ±RECENTER_DEG 度 → 停著收集 REF_FRAMES 張可以量的畫面
    （球要真的看到、沒被邊緣切到），取中位數當目標的直徑和位置；收集到一半球又偏掉就重新對準、重新收集。
    執行中打 r + Enter 可以重新抓基準（一樣先 recenter）。
    --use-settings：改用 calibrate.py 存的 settings.json（目標直徑；方位 = 正前方）

每次執行都自動存 log：ball_chase/logs/chase_日期_時間.jsonl（第一行是設定、之後每張畫面一行、最後一行是摘要）
    .venv/bin/python ball_chase/analyze_log.py              # 看最新一份 log 的時間軸和統計

狀態：
    recenter  還沒有基準：原地轉，把球轉到畫面中間
    reference 對準中間了，停著收集基準
    lost      連續 LOST_GRACE 秒沒真的看到球（跟丟、只有預測位置、server 沒資料）→ 停
              （YOLO 常常隔一張漏抓一次，短暫漏抓時維持原本的動作，不會一頓一頓）
    align     球偏離基準方位太多（> ALIGN_DEG 度）→ 原地轉過去
    approach  比基準小（太遠）→ 邊走邊修方向，越遠走越快
    arrived   大小在基準附近 → 停（只修方向）
    backoff   比基準大很多（太近）→ 慢慢後退

執行中在終端機打指令 + Enter：r 重新抓基準、p 暫停／繼續（暫停時停車）、s 顯示設定、q 離開。
"""

import argparse
import asyncio
import collections
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "camera_control"))

from ball_center import (PAN_CENTER, PAN_PX_PER_DEG, SERVER_URL, STALE_SECONDS, Feed,  # noqa: E402
                         choose_target, start_stdin_reader)
from chassis import Chassis, clamp  # noqa: E402
from robot_ble import DEVICE_NAME, RobotBLE, log  # noqa: E402

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
LOG_DIR = Path(__file__).resolve().parent / "logs"

# ====== 想改的東西都在這裡 ======
# 距離（用球的直徑 d 跟校正的 target_d 比）
SIZE_WINDOW = 5          # 直徑取最近幾張的中位數（偵測的大小會抖）
EDGE_MARGIN = 4          # 球被畫面邊緣切到時量到的直徑會偏小 → 離邊緣這麼近就不拿來算大小
ARRIVE_BAND = 0.12       # 距離誤差（target_d / d − 1 ≈ 距離 / 目標距離 − 1）在 ±這個以內 → 到了
LEAVE_BAND = 0.25        # 已經到了之後，誤差超過這個才重新開始走（遲滯，不會在邊界一直走走停停）
BACKOFF_BAND = 0.25      # 比目標近這麼多（誤差 < −這個）才往後退
# 基準
REF_FRAMES = 10          # 對準中間後，收集幾張可以量的畫面當基準（取中位數）
RECENTER_DEG = 3.0       # 抓基準前先把球轉到畫面中間：偏離中間幾度以內才算對準
# 方向（相機鎖在正前方：球在畫面右邊幾度，就是在車頭右邊幾度）
TURN_SIGN = -1           # 實測（vapup 那台）：M 指令的左右輪跟車子實際的左右是反的 → 轉彎要反過來；前進後退沒反
HEADING_DEADBAND = 4.0   # 邊走邊修方向：誤差幾度以內不修
TURN_START_DEG = 6.0     # 停著（到了、還不能算距離）的時候，偏超過幾度才原地轉（比 HEADING_DEADBAND 大 = 遲滯，不會一直抖）
ALIGN_DEG = 15.0         # 超過幾度就先原地轉，不前進
# 原地轉：一段一段轉（轉一小段 → 停 → 等畫面穩定 → 再看），不會一直轉到看到才停而轉過頭
TURN_SPEED = 130         # 原地轉的轉速
TURN_RATE = 90.0         # 用 TURN_SPEED 原地轉，每秒大約轉幾度（起始值；log 實測約 100；之後看畫面自動修正）
TURN_PULSE_GAIN = 0.7    # 每段只轉「算出來的角度」的這個比例（寧可少轉、多轉幾段，也不要轉過頭）
TURN_MIN_PULSE, TURN_MAX_PULSE = 0.06, 0.35   # 每段最短／最長轉幾秒
TURN_SETTLE = 0.20       # 轉完一段停下來，等幾秒讓畫面穩定。⚠️ 不要低於 0.15（相機延遲）：模擬 0.10 會一直左右來回、抓不到基準；0.20 最快又不會來回
# 輪子轉速（M 指令，0~255；太低推不動車）
MIN_WHEEL = 100          # 要動的時候最低轉速（推得動車子的最小值，實測後調）
FORWARD_GAIN = 250       # 前進速度 = MIN_WHEEL + 這個 × 距離誤差
MAX_FORWARD = 170        # 前進最快
BACK_SPEED = 110         # 後退轉速
TURN_GAIN = 2.5          # 邊走邊轉：每度誤差左右輪差多少（太大會左右晃）
# 跟丟
LOST_GRACE = 0.3         # YOLO 常常隔一張漏抓一次：連續這麼久都沒真的看到球才停車（短暫漏抓時維持原本的動作）
# 安全
PERSON_STOP_H = 0.9      # --person-stop 時：畫面裡有人的框高度超過畫面的這個比例（人很近）→ 停
                         # 預設關：拿著球站在前面的人本來就很近，開著車子會完全不動
# 其他
STATUS_INTERVAL = 0.2
# ================================


def size_ok(ball, W, H):
    """這張的直徑能不能拿來算距離：要真的看到（不是預測）、是顏色擬合的圓（不是只有 YOLO 框）、沒被畫面邊緣切到。"""
    x, y, r = ball["x"], ball["y"], ball["r"]
    return (ball["misses"] == 0 and ball["source"] in ("yolo+cv", "cv")     # 都是用顏色擬合的圓，大小準
            and x - r > EDGE_MARGIN and y - r > EDGE_MARGIN and x + r < W - EDGE_MARGIN and y + r < H - EDGE_MARGIN)


class BallChaser:
    def __init__(self, chassis, target_d, max_speed, person_stop=False, runlog=None):
        self.chassis = chassis
        self.target_d = target_d         # None = 還沒抓基準
        self.ref_angle = 0.0             # 基準方位（度，+ = 在右邊）
        self.ref_samples = []
        self.runlog = runlog
        self.max_forward = min(MAX_FORWARD, max_speed)
        self.max_turn = min(TURN_SPEED, max_speed)
        self.turn_rate = TURN_RATE       # 原地轉每秒幾度（看畫面學）
        self.dist_state = "unknown"      # 距離：approach / arrived / backoff / unknown
        self.pulse_until = 0.0           # 這一段原地轉轉到什麼時候
        self.settle_until = 0.0          # 等畫面穩定到什麼時候（這之前不決定轉向）
        self.last_pulse = None           # (轉之前的角度, 秒數)：下一次用來修正 turn_rate
        self.paused = False
        self.locked_id = None
        self.state = "reference" if target_d is None else "lost"
        self.sizes = collections.deque(maxlen=SIZE_WINDOW)
        self.person_stop = person_stop
        self.last_seen = 0.0             # 最後一次真的看到球的時間
        self.last_wheels = (0, 0)

    async def update(self, data, now):
        """每張新畫面呼叫一次；回傳要輸出的資訊。"""
        if self.pulse_until and now >= self.pulse_until:     # 原地轉的這一段時間到了：停（不管有沒有看到球）
            self.pulse_until = 0.0
            self.chassis.set(0, 0, smooth=False)
            self.last_wheels = (0, 0)
        target = None
        if data is None or now - data["_received_at"] > STALE_SECONDS:
            reason = "no_data"
        else:
            target = choose_target(data["balls"], self.locked_id)
            reason = None if target and target["misses"] == 0 else "lost"
        if target is None or reason:
            if reason == "lost" and now - self.last_seen < LOST_GRACE:   # 短暫漏抓：維持原本的動作
                return self._out(data, target, None, None, "miss", self.last_wheels)
            self.sizes.clear()
            self.state, self.dist_state = "lost", "unknown"
            self.last_wheels = (0, 0)
            self.pulse_until, self.last_pulse = 0.0, None
            await self.chassis.stop()
            return self._out(data, target, None, None, reason)
        self.last_seen = now
        if target["id"] != self.locked_id:
            self.sizes.clear()
        self.locked_id = target["id"]
        W, H = data["width"], data["height"]
        if size_ok(target, W, H):
            self.sizes.append(target["d"])

        # 方向：球在畫面右邊（dx > 0）→ 在車頭右邊
        raw_angle = (target["x"] - W / 2) / PAN_PX_PER_DEG
        centering = self.target_d is None                 # 還沒有基準：先把球轉到畫面中間（recenter）再抓
        angle = raw_angle if centering else raw_angle - self.ref_angle
        # 距離誤差：target_d / d − 1 ≈ 距離 / 基準距離 − 1（正 = 太遠）
        err = self.target_d / statistics.median(self.sizes) - 1 if self.sizes and not centering else None

        near_person = self.person_stop and any(p["h"] > PERSON_STOP_H and p["misses"] == 0 for p in data["people"])
        if self.paused or near_person:
            self.state = "paused" if self.paused else "person"
            self.last_wheels = (0, 0)
            self.pulse_until = self.settle_until = 0.0
            await self.chassis.stop()
            return self._out(data, target, angle, err, self.state)

        # 原地轉的一段還在轉、或在等畫面穩定：不做新的決定
        if now < self.settle_until:
            self.state = "recenter" if centering else "align"
            return self._out(data, target, angle, err, "turning" if self.pulse_until else "settle", self.last_wheels)
        if self.last_pulse is not None:                   # 上一段轉完了：看實際轉了幾度，修正 turn_rate
            before, seconds = self.last_pulse
            self.last_pulse = None
            turned = abs(before - angle)
            if abs(before) > 3 and turned > 1:
                self.turn_rate = clamp(0.7 * self.turn_rate + 0.3 * turned / seconds, 30, 400)

        if centering:
            if abs(angle) > RECENTER_DEG:                 # 還沒對準中間：原地轉一段；收到一半的基準作廢
                self.ref_samples = []
                self.state = "recenter"
                return self._start_spin(data, target, angle, now)
            self.state = "reference"                      # 對準了：停著收集基準
            await self.chassis.stop()
            self.last_wheels = (0, 0)
            if size_ok(target, W, H):
                self.ref_samples.append((target["d"], raw_angle))
            if len(self.ref_samples) >= REF_FRAMES:
                self.target_d = round(statistics.median(d for d, _ in self.ref_samples), 1)
                self.ref_angle = round(statistics.median(a for _, a in self.ref_samples), 1)
                message = f"🎯 基準（對準中間之後）：直徑 {self.target_d}px、方位 {self.ref_angle:+.1f}°（之後維持這樣）"
                log(message)
                if self.runlog:
                    self.runlog.write("event", message=message, target_d=self.target_d, ref_angle=self.ref_angle)
            return self._out(data, target, angle, None, f"reference {len(self.ref_samples)}/{REF_FRAMES}")

        if err is not None:                               # 距離狀態（有遲滯；跟轉向分開記，轉完才不會忘記「已經到了」）
            if self.dist_state == "arrived" and -LEAVE_BAND < err < LEAVE_BAND:
                pass                                      # 到了就待著，除非差很多
            elif abs(err) <= ARRIVE_BAND:
                self.dist_state = "arrived"
            elif err < -BACKOFF_BAND:
                self.dist_state = "backoff"
            elif err > 0:
                self.dist_state = "approach"
            else:
                self.dist_state = "arrived"               # 稍微太近但還不到要退的程度
        else:
            self.dist_state = "unknown"                   # 大小還不能用（例如被邊緣切到）：只修方向
        self.state = self.dist_state if self.dist_state != "unknown" else "align"
        moving_forward = self.dist_state == "approach"
        need_spin = abs(angle) > ALIGN_DEG or (not moving_forward and self.dist_state != "backoff"
                                               and abs(angle) > TURN_START_DEG)

        left = right = 0
        if need_spin:                                     # 原地轉一小段，轉完停下來等畫面
            self.state = "align"
            return self._start_spin(data, target, angle, now, err)
        if moving_forward:
            forward = clamp(MIN_WHEEL + FORWARD_GAIN * err, MIN_WHEEL, self.max_forward)
            turn = 0.0 if abs(angle) < HEADING_DEADBAND else TURN_GAIN * angle * TURN_SIGN   # 球在右 → 往右
            left, right = forward + turn, forward - turn
        elif self.dist_state == "backoff":
            left = right = -BACK_SPEED
        cap = max(self.max_forward, self.max_turn)     # 前進 + 轉彎加起來也不能超過上限
        left, right = clamp(left, -cap, cap), clamp(right, -cap, cap)
        self.chassis.set(left, right)
        self.last_wheels = (int(left), int(right))
        return self._out(data, target, angle, err, None, (int(left), int(right)))

    def _start_spin(self, data, target, angle, now, err=None):
        """原地往球那邊轉一小段（轉完自動停，再等 TURN_SETTLE 秒讓畫面穩定）。"""
        seconds = clamp(TURN_PULSE_GAIN * abs(angle) / self.turn_rate, TURN_MIN_PULSE, TURN_MAX_PULSE)
        left, right = self._spin(self.max_turn, angle)
        self.chassis.set(left, right, smooth=False)
        self.pulse_until = now + seconds
        self.settle_until = self.pulse_until + TURN_SETTLE
        self.last_pulse = (angle, seconds)
        self.last_wheels = (int(left), int(right))
        return self._out(data, target, angle, err, f"spin {seconds:.2f}s", self.last_wheels)

    @staticmethod
    def _spin(s, angle):
        """原地轉向球那邊（angle > 0 = 球在右 → 往右轉）。"""
        right_turn = (s, -s) if TURN_SIGN > 0 else (-s, s)
        return right_turn if angle > 0 else (-right_turn[0], -right_turn[1])

    def _out(self, data, target, angle, err, reason, wheels=(0, 0)):
        out = {k: data[k] for k in ("frame", "time", "width", "height", "fps")} if data else {}
        d_med = statistics.median(self.sizes) if self.sizes else None
        out["target"] = None if target is None else {
            **target, "angle_deg": None if angle is None else round(angle, 1),
            "d_median": d_med, "dist_err": None if err is None else round(err, 3),
            # 估計距離（公分）：只給人看，控制不用它
            "dist_cm_est": None if not d_med or not self.settings_cm or not self.target_d
            else round(self.settings_cm * self.target_d / d_med),
            "dist_ratio": None if not d_med or not self.target_d else round(self.target_d / d_med, 2),   # 距離 / 基準距離
        }
        out["balls"] = data["balls"] if data else []
        out["people"] = data["people"] if data else []
        out["drive"] = {"state": self.state, "reason": reason, "wheels": list(wheels), "target_d": self.target_d,
                        "ref_angle": self.ref_angle, "turn_rate": round(self.turn_rate)}
        return out

    settings_cm = None

    def handle_command(self, line):
        cmd = line.strip().lower()
        if cmd == "q":
            return "quit"
        if cmd == "r":
            self.target_d, self.ref_samples, self.state = None, [], "reference"
            self.sizes.clear()
            log("🎯 重新抓基準：先把球轉到畫面中間，再記下大小和位置 ...")
            if self.runlog:
                self.runlog.write("event", message="重新抓基準")
        elif cmd == "p":
            self.paused = not self.paused
            log("⏸️  暫停（停車）" if self.paused else "▶️  繼續")
        elif cmd == "s":
            log(f"⚙️  基準直徑 {self.target_d}px  方位 {self.ref_angle:+.1f}°  前進上限 {self.max_forward}  轉彎上限 {self.max_turn}")
        elif cmd:
            log("指令：r 重新抓基準 | p 暫停／繼續 | s 設定 | q 離開")
        return None


class RunLog:
    """把這次執行記成 JSONL：{"type": "config"|"frame"|"event"|"summary", "t": 開始後幾秒, ...}"""

    def __init__(self, enabled=True):
        self.t0 = time.monotonic()
        self.path = None
        self.file = None
        self.states = collections.Counter()
        self.frames = 0
        if enabled:
            LOG_DIR.mkdir(exist_ok=True)
            self.path = LOG_DIR / time.strftime("chase_%Y%m%d_%H%M%S.jsonl")
            self.file = self.path.open("w", encoding="utf-8")

    def write(self, kind, **fields):
        if self.file:
            record = {"type": kind, "t": round(time.monotonic() - self.t0, 3), "wall": round(time.time(), 3), **fields}
            self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.file.flush()

    def frame(self, out, chassis):
        self.frames += 1
        self.states[out["drive"]["state"] if out["drive"]["reason"] != "miss" else "miss"] += 1
        t = out["target"]
        self.write("frame", frame=out.get("frame"), fps=out.get("fps"),
                   state=out["drive"]["state"], reason=out["drive"]["reason"],
                   want=out["drive"]["wheels"], sent=[round(v) for v in chassis.now],
                   ball=None if t is None else {k: t.get(k) for k in (
                       "id", "x", "y", "d", "source", "misses", "conf", "angle_deg", "d_median", "dist_err",
                       "dist_cm_est", "dist_ratio")},
                   target_d=out["drive"]["target_d"], ref_angle=out["drive"]["ref_angle"],
                   balls=len(out["balls"]), people=[round(p["h"], 2) for p in out["people"]])

    def event(self, message, **fields):
        log(message)
        self.write("event", message=message, **fields)

    def close(self):
        duration = time.monotonic() - self.t0
        summary = {"seconds": round(duration, 1), "frames": self.frames, "states": dict(self.states)}
        self.write("summary", **summary)
        if self.file:
            self.file.close()
            share = ", ".join(f"{k} {100 * v / max(self.frames, 1):.0f}%" for k, v in self.states.most_common())
            log(f"📝 log：{self.path.relative_to(LOG_DIR.parent.parent)}（{duration:.0f} 秒、{self.frames} 張：{share}）")


def status_line(out):
    t, d = out["target"], out["drive"]
    if t is None:
        ball = "沒有球"
    else:
        dist = f"{t['dist_cm_est']}cm" if t["dist_cm_est"] else (f"×{t['dist_ratio']}" if t["dist_ratio"] else "?")
        err = f"{t['dist_err']:+.2f}" if t["dist_err"] is not None else "  ? "
        angle = f"{t['angle_deg']:+5.1f}°" if t["angle_deg"] is not None else "   ?  "   # 短暫漏抓（miss）時沒有角度
        ball = f"球 #{t['id']} 偏 {angle} 直徑 {t['d']:5.1f}px 誤差 {err} 距離{dist}"
    return f"[{d['state']:>8}] {ball}  |  輪子 {d['wheels']}" + (f"  （{d['reason']}）" if d["reason"] else "")


async def run(args, shared=None):
    """shared：給 view.py 用的 dict（放最新的輸出、讓視窗可以丟指令進來）；None = 不開視窗。"""
    target_d, cm = None, None           # 預設：啟動後用看到的第一顆球當基準
    if args.target_d:
        target_d = args.target_d
    elif args.use_settings:
        if not SETTINGS_PATH.exists():
            raise SystemExit("❌ 沒有 settings.json：先跑 ball_chase/calibrate.py，或拿掉 --use-settings")
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        target_d, cm = settings["target_d"], settings.get("distance_cm")

    runlog = RunLog(enabled=not args.no_log)
    robot = RobotBLE(args.device_name, fake=args.fake, quiet=args.json or not args.verbose)
    await robot.connect()
    import chassis as chassis_module
    runlog.write("config", target_d=target_d, distance_cm=cm, device=robot.device_label, args=vars(args),
                 params={k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float))},
                 chassis={k: getattr(chassis_module, k) for k in ("ACCEL", "MAX_WHEEL", "KEEPALIVE_INTERVAL")})
    chassis = Chassis(robot)
    chaser = BallChaser(chassis, target_d, args.max_speed, person_stop=args.person_stop, runlog=runlog)
    chaser.settings_cm = cm
    # 相機鎖住：左右回正、上下停（上下是直流馬達，停在哪就是哪，追球前先調好）
    await robot.send(f"P,{PAN_CENTER}#", reliable=True)
    await robot.send("S#", reliable=True)
    chassis.start()

    feed = Feed()
    receiver = asyncio.create_task(feed.receive_forever(args.url))
    commands = asyncio.Queue()
    start_stdin_reader(asyncio.get_running_loop(), commands)
    if shared is not None:
        shared.update(loop=asyncio.get_running_loop(), commands=commands, chaser=chaser, chassis=chassis,
                      device=robot.device_label)
    log((f"🚗 追球：目標直徑 {target_d}px" + (f"（= {cm:g} cm）" if cm else "")) if target_d else
        f"🚗 追球：先抓基準——把球放在想維持的位置，別動（收 {REF_FRAMES} 張）")
    log(f"   速度上限 {args.max_speed}；終端機打 r 重新抓基準、p 暫停（停車）、q 離開")

    handled, last_status = 0, 0.0
    seen_disconnects, stale_logged = 0, False
    try:
        while True:
            try:
                await asyncio.wait_for(feed.event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            feed.event.clear()
            while not commands.empty():
                line = commands.get_nowait()
                if chaser.handle_command(line) == "quit":
                    runlog.write("event", message="使用者離開（q）")
                    return
                runlog.write("event", message=f"指令 {line!r}", paused=chaser.paused)
                if chaser.paused:
                    await chassis.stop()
            if robot.disconnects != seen_disconnects:
                seen_disconnects = robot.disconnects
                runlog.write("event", message="micro:bit 斷線", disconnects=seen_disconnects)
            now = time.monotonic()
            data = None if feed.latest is None else {**feed.latest, "_received_at": feed.received_at}
            if feed.seq == handled:
                if data is None or now - data["_received_at"] > STALE_SECONDS:
                    if not stale_logged:
                        runlog.event("⚠️  超過 0.5 秒沒收到 tennis_tracking 的資料 → 停車")
                        stale_logged = True
                    out = await chaser.update(data, now)    # server 沒資料了：停車
                    if shared is not None:
                        shared["out"] = out
                continue
            if stale_logged:
                runlog.event("✅ 又收到 tennis_tracking 的資料")
                stale_logged = False
            handled = feed.seq
            out = await chaser.update(data, now)
            runlog.frame(out, chassis)
            if shared is not None:
                shared["out"] = out
            if args.json:
                print(json.dumps(out, ensure_ascii=False), flush=True)
            elif now - last_status >= STATUS_INTERVAL:
                log(status_line(out))
                last_status = now
    finally:
        receiver.cancel()
        try:
            await chassis.close()
        finally:
            await robot.disconnect()
            runlog.close()


def main():
    parser = argparse.ArgumentParser(description="車子追網球：停在球前面（先跑 calibrate.py）")
    parser.add_argument("--url", default=SERVER_URL, help="tennis_tracking server 的 WebSocket 網址")
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit，只印出指令")
    parser.add_argument("--json", action="store_true", help="每張畫面在 stdout 印一行 JSON")
    parser.add_argument("--verbose", action="store_true", help="印出每一個藍牙指令")
    parser.add_argument("--max-speed", type=int, default=180, help="輪子轉速上限（0~255）")
    parser.add_argument("--no-log", action="store_true", help="不存 log")
    parser.add_argument("--view", action="store_true", help="開視窗看畫面和判斷（debug 用）")
    parser.add_argument("--view-headless", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
    parser.add_argument("--person-stop", action="store_true", help="畫面裡有人很近就停車（預設關）")
    parser.add_argument("--use-settings", action="store_true", help="改用 calibrate.py 存的 settings.json 當目標（方位 = 正前方）")
    parser.add_argument("--target-d", type=float, help="直接給目標直徑（px），不抓基準")
    parser.add_argument("--device-name", default=DEVICE_NAME, help="micro:bit 藍牙名稱，逗號分開依優先順序")
    args = parser.parse_args()
    try:
        if args.view or args.view_headless:
            from view import run_with_view
            run_with_view(args, run)
        else:
            asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    log("🔚 結束")


if __name__ == "__main__":
    main()
