"""
測試用視窗：看「球動了，相機怎麼跟著動」。偵測結果、門檻框、誤差、送出的控制指令都畫在畫面上，
門檻、gain、平滑程度可以用視窗上的滑桿即時調。控制邏輯跟 ball_center.py 完全一樣（直接共用）。

兩種模式：

    # 模擬（不用相機、不用車）：滑鼠拖球，模擬的相機會照控制指令轉，最適合先調門檻和 Kp
    .venv/bin/python camera_control/test_view.py --sim

    # 實機：先開 tennis_tracking/server.py，畫面來自它的 /video（上面已經有它畫的偵測框）
    .venv/bin/python tennis_tracking/server.py
    .venv/bin/python camera_control/test_view.py --fake      # 不連車，只看算出來的指令
    .venv/bin/python camera_control/test_view.py             # 連 micro:bit，真的轉

按鍵：q / Esc 離開   p 暫停/繼續控制   c 相機回正
      （模擬）a 球自動繞圈 開/關   + / - 自動繞圈速度   r 球放回正中間   滑鼠左鍵拖球

畫面：
    白色十字        畫面中心
    門檻框          球心在框內就不動（綠 = 置中、橘 = 還沒置中）
    青色粗圈 + 線   正在對準的球、跟中心的距離（dx, dy）
    洋紅色 ×        上一次決定用的位置（幾張畫面平均 + 速度預估）
    黃色箭頭        相機現在轉的方向（左右箭頭越長 = 這一步轉越多度）
    下方面板        狀態、左右角度（量表：左邊 = 相機看左邊）、上下馬達速度、門檻、Kp
"""

import argparse
import asyncio
import collections
import math
import random
import threading
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from ball_center import (DEVICE_NAME, PAN_CENTER, PAN_GAIN, PAN_LATENCY, PAN_PX_PER_DEG, SERVER_URL, FRAMES,
                         THRESHOLD_PX, TILT_DEADBAND, TILT_LATENCY, TILT_PX_PER_SPEED, TILT_UP_SIGN,
                         CameraController, Feed, control_loop, stop_camera)
from robot_ble import RobotBLE, log

W, H = 640, 480          # 模擬畫面大小（實機用 server 的畫面大小）
PANEL_H = 150
WIN = "camera_control test (q = quit)"

# ====== 模擬的相機（--sim），數值來自實機量測（見 ball_center.py）======
SIM_PX_PER_DEG = PAN_PX_PER_DEG   # 伺服轉 1 度，畫面移動幾個像素
SIM_SERVO_DEG_PER_SEC = 250   # 伺服轉多快（實測 10 度約 40 ms 到位）
SIM_TILT_RANGE = 400          # 上下最多能轉到離中間幾個像素
SIM_LATENCY = PAN_LATENCY     # 從送指令到畫面上看到相機動（藍牙 + 馬達 + 相機），左右約 0.12 秒
SIM_TILT_EXTRA_DELAY = TILT_LATENCY - PAN_LATENCY   # 上下馬達起步比伺服再慢一點
SIM_NOISE_PX = 2.0            # 偵測的抖動
SIM_BALL_R = 22
SIM_FPS = 20                  # 實測相機約 20 fps
# ================================


# ---------- 模擬世界 ----------

class SimWorld:
    """一個很大的「世界」，相機只看得到其中 W×H 的一塊。世界座標原點 = 相機正前方（pan 90、tilt 0）的畫面中心。"""

    def __init__(self):
        self.pan = float(PAN_CENTER)   # 伺服實際的角度（會慢慢轉到指令的角度）
        self.tilt = 0.0                # 相機上下：畫面中心在世界的 y（負 = 往上看）
        self.ball = [0.0, 0.0]
        self.auto, self.speed, self.t = True, 0.6, 0.0
        self.dragging = False
        self.tilt_cmds = collections.deque()   # (時間, Z 指令)：上下馬達起步比較慢
        span_x = int(90 * SIM_PX_PER_DEG) + W // 2 + 60
        span_y = SIM_TILT_RANGE + H // 2 + 60
        self.origin = (span_x, span_y)
        self.bg = self._make_background(2 * span_x, 2 * span_y)

    @staticmethod
    def _make_background(w, h):
        bg = np.full((h, w, 3), 60, np.uint8)
        rng = np.random.default_rng(0)
        for _ in range(400):   # 隨便畫點東西，轉動的時候才看得出畫面在動
            x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
            color = tuple(int(c) for c in rng.integers(70, 140, 3))
            cv2.rectangle(bg, (x, y), (x + int(rng.integers(10, 60)), y + int(rng.integers(10, 60))), color, -1)
        for x in range(0, w, 100):
            cv2.line(bg, (x, 0), (x, h), (90, 90, 90), 1)
        for y in range(0, h, 100):
            cv2.line(bg, (0, y), (w, y), (90, 90, 90), 1)
        return bg

    def view_center(self):
        return (PAN_CENTER - self.pan) * SIM_PX_PER_DEG, self.tilt   # 角度變小 = 往右看

    def step(self, dt, cmd_pan, tilt_speed):
        if cmd_pan is not None:
            diff = cmd_pan - self.pan
            self.pan += max(-SIM_SERVO_DEG_PER_SEC * dt, min(SIM_SERVO_DEG_PER_SEC * dt, diff))
        now = time.monotonic()
        self.tilt_cmds.append((now, tilt_speed))
        z = 0
        while self.tilt_cmds and self.tilt_cmds[0][0] <= now - SIM_TILT_EXTRA_DELAY:
            z = self.tilt_cmds.popleft()[1]
            self._z = z
        z = getattr(self, "_z", 0)
        up = z * TILT_UP_SIGN                                    # 正 = 相機往上
        if abs(up) > TILT_DEADBAND:                              # 實測：每秒移動 ≈ 6.5 ×（轉速 − 40）
            v = TILT_PX_PER_SPEED * (abs(up) - TILT_DEADBAND)
            self.tilt -= v * dt * (1 if up > 0 else -1)          # 往上看 → 畫面中心在世界的 y 變小
        self.tilt = max(-SIM_TILT_RANGE, min(SIM_TILT_RANGE, self.tilt))
        if self.auto and not self.dragging:
            self.t += dt * self.speed
            self.ball = [520 * math.sin(self.t), 260 * math.sin(2 * self.t + 0.5)]

    def observe(self, frame_no):
        """模擬 tennis_tracking 送出來的 JSON：球在畫面裡就回報它的位置。"""
        cx, cy = self.view_center()
        x = self.ball[0] - cx + W / 2 + random.gauss(0, SIM_NOISE_PX)
        y = self.ball[1] - cy + H / 2 + random.gauss(0, SIM_NOISE_PX)
        r = SIM_BALL_R
        balls = []
        if 0 <= x < W and 0 <= y < H:
            balls.append({"id": 1, "x": round(x, 1), "y": round(y, 1), "r": r, "d": 2 * r, "size": round(2 * r / W, 3),
                          "ex": round((x - W / 2) / (W / 2), 3), "ey": round((y - H / 2) / (H / 2), 3),
                          "box": [x - r, y - r, x + r, y + r], "vx": 0.0, "vy": 0.0,
                          "source": "sim", "conf": 1.0, "misses": 0})
        return {"frame": frame_no, "time": time.time(), "width": W, "height": H, "fps": float(SIM_FPS),
                "balls": balls, "people": []}

    def render(self):
        cx, cy = self.view_center()
        x0 = int(round(self.origin[0] + cx - W / 2))
        y0 = int(round(self.origin[1] + cy - H / 2))
        frame = self.bg[y0:y0 + H, x0:x0 + W].copy()
        bx, by = int(self.ball[0] - cx + W / 2), int(self.ball[1] - cy + H / 2)
        cv2.circle(frame, (bx, by), SIM_BALL_R, (60, 230, 200), -1)          # 螢光黃綠的球
        cv2.ellipse(frame, (bx, by), (SIM_BALL_R, SIM_BALL_R // 2), 30, 0, 180, (240, 240, 240), 2)
        return frame

    def put_ball(self, x, y):
        """滑鼠點畫面上的 (x, y) → 把球放到世界裡對應的位置。"""
        cx, cy = self.view_center()
        self.ball = [x - W / 2 + cx, y - H / 2 + cy]


class SimFeed(Feed):
    """跟 Feed 一樣的介面，只是資料來自模擬世界（加上延遲），不是 server。"""

    def __init__(self, world):
        super().__init__()
        self.world = world

    async def run(self, controller):
        delayed = collections.deque()
        frame_no, last = 0, time.monotonic()
        while True:
            await asyncio.sleep(1 / SIM_FPS)
            now = time.monotonic()
            self.world.step(now - last, controller.pan, controller.tilt_speed)
            last = now
            frame_no += 1
            delayed.append((now + SIM_LATENCY, self.world.observe(frame_no)))
            while delayed and delayed[0][0] <= now:
                self.latest = delayed.popleft()[1]
                self.seq += 1
                self.received_at = now
                self.event.set()


# ---------- 實機畫面：讀 server 的 /video（MJPEG）----------

def mjpeg_reader(url, shared, stop):
    while not stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                buf = b""
                while not stop.is_set():
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2) if a != -1 else -1
                        if b == -1:
                            break
                        shared["jpeg"] = buf[a:b + 2]
                        buf = buf[b + 2:]
                    if len(buf) > 4_000_000:
                        buf = b""
        except OSError:
            time.sleep(1)


# ---------- 畫圖 ----------

GREEN, ORANGE, CYAN, YELLOW, WHITE, GRAY, RED = (0, 220, 0), (0, 165, 255), (255, 255, 0), (0, 255, 255), \
    (255, 255, 255), (150, 150, 150), (0, 0, 255)
STATE_COLOR = {"tracking": GREEN, "predict": ORANGE, "lost": RED, "paused": GRAY, "no_data": RED, "waiting": GRAY}


def text(img, s, pos, color=WHITE, scale=0.5, thick=1):
    cv2.putText(img, s, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def draw_frame(frame, out, draw_detections, use_tilt):
    h, w = frame.shape[:2]
    cx, cy = w // 2, h // 2
    c = out["control"] if out else None
    t = out["target"] if out else None

    if draw_detections and out:   # 實機的偵測框 server 已經畫好了；模擬才要自己畫
        for b in out["balls"]:
            cv2.circle(frame, (int(b["x"]), int(b["y"])), int(b["r"]), GREEN, 1)
            text(frame, f"#{b['id']} {b['source']}", (int(b["x"] + b["r"] + 4), int(b["y"])), GREEN, 0.45)
        for p in out["people"]:
            x1, y1, x2, y2 = map(int, p["box"])
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 255), 1)

    cv2.drawMarker(frame, (cx, cy), WHITE, cv2.MARKER_CROSS, 24, 1)
    if c:
        tx, ty = int(c["threshold_x"]), int(c["threshold_y"])
        centered = t is not None and t["centered_x"] and (t["centered_y"] or not use_tilt)
        cv2.rectangle(frame, (cx - tx, cy - ty), (cx + tx, cy + ty), GREEN if centered else ORANGE, 2)
        text(frame, f"threshold {tx}x{ty}px", (cx - tx, cy - ty - 6), GREEN if centered else ORANGE, 0.45)

    if t is not None:
        bx, by = int(t["x"]), int(t["y"])
        cv2.line(frame, (cx, cy), (bx, by), CYAN, 1)
        cv2.circle(frame, (bx, by), int(t["r"]) + 4, CYAN, 3)
        if t["dx_avg"] is not None:   # 上一次決定用的「幾張平均 + 速度預估」位置
            sx, sy = int(cx + t["dx_avg"]), int(cy + t["dy_avg"])
            cv2.drawMarker(frame, (sx, sy), (255, 0, 255), cv2.MARKER_TILTED_CROSS, 14, 2)
        text(frame, f"target #{t['id']}  dx {t['dx_px']:+.0f}  dy {t['dy_px']:+.0f}  d {t['d']:.0f}px",
             (bx - 90, by + int(t["r"]) + 22), CYAN, 0.5)
        if c["state"] == "tracking":   # 相機現在轉的方向（左右箭頭長度 = 這一步轉幾度）
            if c["pan_step"]:
                d = -1 if c["pan_step"] > 0 else 1      # 角度變大 = 往左
                length = 30 + int(min(abs(c["pan_step"]), 15) / 15 * 100)
                cv2.arrowedLine(frame, (cx + d * 40, cy), (cx + d * (40 + length), cy), YELLOW, 3, tipLength=0.3)
            if use_tilt and c["tilt_speed"] != 0:
                d = -1 if c["tilt_speed"] * TILT_UP_SIGN > 0 else 1   # 往上 → 箭頭朝上
                cv2.arrowedLine(frame, (cx, cy + d * 40), (cx, cy + d * 110), YELLOW, 3, tipLength=0.35)


def draw_panel(width, out, fps, mode, use_tilt):
    panel = np.full((PANEL_H, width, 3), 30, np.uint8)
    if not out:
        text(panel, "waiting for data ...", (10, 30), GRAY, 0.6)
        return panel
    c, t = out["control"], out["target"]
    state = c["state"]
    text(panel, state.upper(), (10, 28), STATE_COLOR.get(state, WHITE), 0.75, 2)
    if t is None:
        info = "no ball"
    else:
        ok = lambda v: "OK" if v else "--"
        info = (f"ball #{t['id']}  dx {t['dx_px']:+.0f}px [{ok(t['centered_x'])}]  "
                f"dy {t['dy_px']:+.0f}px [{ok(t['centered_y'])}]  src {t['source']}")
    text(panel, info, (150, 28), WHITE, 0.5)

    # 左右角度量表：左邊 = 180（相機看左邊）、右邊 = 0
    x0, x1, y = 70, width - 200, 65
    text(panel, "pan", (10, y + 5), WHITE, 0.5)
    cv2.line(panel, (x0, y), (x1, y), GRAY, 2)
    for a in (180, 135, 90, 45, 0):
        x = int(x0 + (180 - a) / 180 * (x1 - x0))
        cv2.line(panel, (x, y - 6), (x, y + 6), GRAY, 1)
        text(panel, str(a), (x - 10, y + 22), GRAY, 0.4)
    if c["pan"] is not None:
        x = int(x0 + (180 - c["pan"]) / 180 * (x1 - x0))
        cv2.circle(panel, (x, y), 8, YELLOW, -1)
        step = f"  {c['pan_step']:+.0f}" if c["pan_step"] else ""
        text(panel, f"{c['pan']} deg{step}", (x1 + 10, y + 5), YELLOW, 0.5)

    # 上下馬達
    y = 115
    if use_tilt:
        speed = c["tilt_speed"]
        arrow = "stop" if speed == 0 else ("UP" if speed * TILT_UP_SIGN > 0 else "DOWN")
        text(panel, f"tilt Z {speed:+d} {arrow}", (10, y), WHITE, 0.55)
    else:
        text(panel, "tilt: off (--no-tilt)", (10, y), GRAY, 0.55)
    text(panel, f"thr {c['threshold_x']:g}/{c['threshold_y']:g}px  gain {c['kp']:g}  frames {c['frames']}",
         (300, y), WHITE, 0.5)
    text(panel, f"{mode}  {fps:.0f} fps  people {len(out['people'])}", (10, 140), GRAY, 0.45)
    return panel


# ---------- 主程式 ----------

def main():
    parser = argparse.ArgumentParser(description="測試相機對準網球：畫面 + 門檻 + 控制指令")
    parser.add_argument("--sim", action="store_true", help="模擬模式：不用相機、不用車，滑鼠拖球")
    parser.add_argument("--url", default=SERVER_URL, help="tennis_tracking server 的 WebSocket 網址")
    parser.add_argument("--video-url", help="server 的影像網址，預設從 --url 推（.../video）")
    parser.add_argument("--threshold", type=float, default=THRESHOLD_PX)
    parser.add_argument("--threshold-x", type=float)
    parser.add_argument("--threshold-y", type=float)
    parser.add_argument("--kp", type=float, default=PAN_GAIN, help="左右每次修正誤差的幾成（0~1）")
    parser.add_argument("--frames", type=int, default=FRAMES, help="每次看幾張畫面再決定")
    parser.add_argument("--invert-pan", action="store_true")
    parser.add_argument("--invert-tilt", action="store_true")
    parser.add_argument("--no-tilt", action="store_true")
    parser.add_argument("--fake", action="store_true", help="實機模式但不連 micro:bit")
    parser.add_argument("--verbose", action="store_true", help="印出每一個藍牙指令")
    parser.add_argument("--device-name", default=DEVICE_NAME)
    parser.add_argument("--headless", type=float, metavar="SECONDS",
                        help="不開視窗，跑幾秒後把最後一張畫面存成 test_snapshot.jpg（沒有螢幕時測試用）")
    args = parser.parse_args()
    args.threshold_x = args.threshold if args.threshold_x is None else args.threshold_x
    args.threshold_y = args.threshold if args.threshold_y is None else args.threshold_y

    world = SimWorld() if args.sim else None
    shared = {"out": None, "jpeg": None, "controller": None, "commands": None}

    # 控制跑在背景的 asyncio 執行緒；畫面（OpenCV 視窗）在主執行緒
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def control_main():
        robot = RobotBLE(args.device_name, fake=args.sim or args.fake, quiet=not args.verbose)
        try:
            await robot.connect()
        except SystemExit as e:   # SystemExit 丟出 asyncio 執行緒會把整個 loop 弄掛，換成一般錯誤
            raise RuntimeError(str(e)) from None
        controller = CameraController(robot, args)
        await controller.center(time.monotonic())
        commands = asyncio.Queue()
        shared["controller"], shared["commands"] = controller, commands
        if world:
            feed = SimFeed(world)
            task = asyncio.create_task(feed.run(controller))
        else:
            feed = Feed()
            task = asyncio.create_task(feed.receive_forever(args.url))

        def on_output(out):
            shared["out"] = out

        try:
            await control_loop(controller, feed, commands, on_output)
        finally:
            task.cancel()
            await stop_camera(robot)

    future = asyncio.run_coroutine_threadsafe(control_main(), loop)

    def command(line):
        if shared["commands"] is not None:
            loop.call_soon_threadsafe(shared["commands"].put_nowait, line)

    def set_value(name, value):
        if shared["controller"] is not None:
            loop.call_soon_threadsafe(setattr, shared["controller"], name, value)

    stop = threading.Event()
    if not world:
        video_url = args.video_url or args.url.replace("ws://", "http://").replace("wss://", "https://") \
            .rsplit("/", 1)[0] + "/video"
        threading.Thread(target=mjpeg_reader, args=(video_url, shared, stop), daemon=True).start()
        log(f"📺 畫面來自 {video_url}")

    if not args.headless:
        setup_window(args, world, set_value)
    mode = "SIM" if world else ("LIVE (fake robot)" if args.fake else "LIVE")
    fps, last, start = 0.0, time.monotonic(), time.monotonic()
    blank = np.zeros((H, W, 3), np.uint8)
    try:
        while not future.done():
            if world:
                frame = world.render()
            else:
                jpeg = shared["jpeg"]
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
                if frame is None:
                    frame = blank.copy()
                    text(frame, "waiting for video from tennis_tracking server ...", (60, H // 2), GRAY, 0.6)
            out = shared["out"]
            draw_frame(frame, out, draw_detections=bool(world), use_tilt=not args.no_tilt)
            now = time.monotonic()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-3)
            last = now
            view = np.vstack([frame, draw_panel(frame.shape[1], out, fps, mode, not args.no_tilt)])

            if args.headless:
                time.sleep(0.03)
                if now - start > args.headless:
                    shot = Path(__file__).resolve().parent / "test_snapshot.jpg"
                    cv2.imwrite(str(shot), view)
                    log(f"📸 存了 {shot.name}")
                    break
                continue
            cv2.imshow(WIN, view)
            key = cv2.waitKey(15) & 0xFF
            if key in (ord("q"), 27) or cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
            elif key == ord("p"):
                command("p")
            elif key == ord("c"):
                command("c")
            elif world and key == ord("a"):
                world.auto = not world.auto
            elif world and key in (ord("+"), ord("=")):
                world.speed = min(3.0, world.speed + 0.2)
            elif world and key == ord("-"):
                world.speed = max(0.2, world.speed - 0.2)
            elif world and key == ord("r"):
                world.auto = False
                world.put_ball(W / 2, H / 2)
    except KeyboardInterrupt:
        pass
    finally:
        command("q")
        stop.set()
        try:
            future.result(timeout=5)
        except Exception as e:  # noqa: BLE001 — 連線失敗等錯誤，印出來就好
            if not isinstance(e, TimeoutError):
                log(f"❌ {e}")
        cv2.destroyAllWindows()
        log("🔚 結束")


def setup_window(args, world, set_value):
    cv2.namedWindow(WIN)
    cv2.createTrackbar("thr x px", WIN, int(args.threshold_x), 240, lambda v: set_value("threshold_x", float(v)))
    cv2.createTrackbar("thr y px", WIN, int(args.threshold_y), 240, lambda v: set_value("threshold_y", float(v)))
    cv2.createTrackbar("gain %", WIN, int(round(args.kp * 100)), 150, lambda v: set_value("kp", v / 100))
    cv2.createTrackbar("frames", WIN, int(args.frames), 15, lambda v: set_value("frames", max(1, v)))

    if world:
        def on_mouse(event, x, y, flags, _):
            if event == cv2.EVENT_LBUTTONDOWN and y < H:
                world.dragging, world.auto = True, False
                world.put_ball(x, y)
            elif event == cv2.EVENT_MOUSEMOVE and world.dragging and y < H:
                world.put_ball(x, y)
            elif event == cv2.EVENT_LBUTTONUP:
                world.dragging = False
        cv2.setMouseCallback(WIN, on_mouse)
        log("🧪 模擬模式：滑鼠拖球、a 自動繞圈、+/- 速度、r 球回中間、p 暫停、c 回正、q 離開")


if __name__ == "__main__":
    main()
