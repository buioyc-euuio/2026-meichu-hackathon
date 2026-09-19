"""
chase.py 的 debug 視窗：畫面（tennis_tracking 的 /video）上疊基準、現在的球、判斷和輪子轉速。

    .venv/bin/python ball_chase/chase.py --view            # 真的開車 + 視窗
    .venv/bin/python ball_chase/chase.py --view --fake     # 不連車，只看判斷

畫面上：
    青色線            還沒有基準時：recenter 的目標（畫面中間），灰線之間 = 算對準
    黃色虛線圓        基準：球「應該」在的位置和大小
    兩個淡黃圓        「到了」的大小範圍（比基準近／遠 ARRIVE_BAND 以內）
    黃色直線          基準方位；淡灰線 = 不修方向的範圍（HEADING_DEADBAND）；紅線 = 超過就原地轉（ALIGN_DEG）
    綠 / 橘圈         現在的球（綠 = 大小可以拿來算距離、橘 = 不能，例如被邊緣切到）
    下方面板          狀態、原因、左右輪（淡 = 程式要的、實心 = 加減速後正在送的）、距離比例

按鍵（視窗）：r 重新抓基準   p 暫停／繼續（停車）   q / Esc 離開
"""

import asyncio
import threading
import time

import cv2
import numpy as np

from ball_center import PAN_PX_PER_DEG, SERVER_URL
from chase import ALIGN_DEG, ARRIVE_BAND, BACKOFF_BAND, HEADING_DEADBAND, RECENTER_DEG, size_ok
from test_view import mjpeg_reader, text

WIN = "ball_chase (r = reference, p = pause, q = quit)"
PANEL_H = 170
YELLOW, PALE, GREEN, ORANGE, RED, WHITE, GRAY, CYAN = ((0, 230, 255), (120, 200, 230), (0, 220, 0), (0, 165, 255),
                                                        (0, 0, 255), (255, 255, 255), (140, 140, 140), (255, 255, 0))
STATE_COLOR = {"recenter": CYAN, "reference": CYAN, "lost": RED, "align": ORANGE, "approach": GREEN, "arrived": YELLOW,
               "backoff": ORANGE, "paused": GRAY, "person": RED}


def dashed_circle(img, center, radius, color, thick=2, segments=36):
    for i in range(0, segments, 2):
        cv2.ellipse(img, center, (radius, radius), 0, i * 360 / segments, (i + 1) * 360 / segments, color, thick)


def draw_frame(frame, out):
    h, w = frame.shape[:2]
    cx = w // 2
    drive = out["drive"] if out else None
    target = out["target"] if out else None
    if drive and drive["target_d"]:
        ref_x = int(cx + drive["ref_angle"] * PAN_PX_PER_DEG)
        # 方位線：基準、不修方向的範圍、超過就原地轉
        cv2.line(frame, (ref_x, 0), (ref_x, h), YELLOW, 1)
        for deg, color in ((HEADING_DEADBAND, GRAY), (ALIGN_DEG, RED)):
            for sign in (-1, 1):
                x = int(ref_x + sign * deg * PAN_PX_PER_DEG)
                cv2.line(frame, (x, 0), (x, h), color, 1)
        # 基準大小：畫在球現在的高度（沒有球就畫在中間）
        cy = int(target["y"]) if target else h // 2
        d = drive["target_d"]
        dashed_circle(frame, (ref_x, cy), int(d / 2), YELLOW, 2)
        for ratio in (1 + ARRIVE_BAND, 1 - ARRIVE_BAND):     # 距離比例 ±ARRIVE_BAND → 直徑 d / ratio
            cv2.circle(frame, (ref_x, cy), int(d / ratio / 2), PALE, 1)
        text(frame, f"reference d {d:.0f}px  {drive['ref_angle']:+.1f} deg", (ref_x + int(d / 2) + 6, cy - 8), YELLOW, 0.5)
    elif drive:                                           # 還沒有基準：recenter 的目標 = 畫面中間
        cv2.line(frame, (cx, 0), (cx, h), CYAN, 1)
        for sign in (-1, 1):
            x = int(cx + sign * RECENTER_DEG * PAN_PX_PER_DEG)
            cv2.line(frame, (x, 0), (x, h), GRAY, 1)
        text(frame, "recenter: turning ball to the middle, then capture reference", (10, 30), CYAN, 0.55)
    if target:
        ok = size_ok(target, w, h)
        color = GREEN if ok else ORANGE
        center = (int(target["x"]), int(target["y"]))
        cv2.circle(frame, center, int(target["r"]), color, 3)
        cv2.circle(frame, center, 3, color, -1)
        angle = target.get("angle_deg")
        ratio = target.get("dist_ratio")
        label = (f"#{target['id']} d {target['d']:.0f}px {target['source']}"
                 + (f"  off {angle:+.1f} deg" if angle is not None else "")
                 + (f"  dist x{ratio:.2f}" if ratio else ""))
        width = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)[0][0]
        lx = min(max(center[0] - int(target["r"]), 5), w - width - 5)            # 別超出畫面
        ly = min(center[1] + int(target["r"]) + 22, h - 10)
        text(frame, label, (lx, ly), color, 0.55, 2)
        if not ok:
            text(frame, "size not usable (edge / YOLO box only / predicted)", (10, 60), ORANGE, 0.5)
    return frame


def wheel_bar(panel, x, y, height, label, want, sent, limit=255):
    """直條：中線 = 0，往上 = 前進；淡色 = 要的、實心 = 正在送的。"""
    mid = y + height // 2
    cv2.rectangle(panel, (x, y), (x + 36, y + height), GRAY, 1)
    cv2.line(panel, (x - 4, mid), (x + 40, mid), GRAY, 1)
    for value, color, inset in ((want, (90, 90, 90), 0), (sent, GREEN if sent >= 0 else ORANGE, 8)):
        top = int(mid - value / limit * (height // 2))
        cv2.rectangle(panel, (x + inset, min(mid, top)), (x + 36 - inset, max(mid, top)), color, -1)
    text(panel, label, (x + 6, y + height + 16), WHITE, 0.5)
    text(panel, f"{sent:+d}", (x - 2, y - 6), WHITE, 0.45)


def draw_panel(width, out, shared, fps):
    panel = np.full((PANEL_H, width, 3), 30, np.uint8)
    if not out:
        text(panel, "waiting for tennis_tracking data ...", (10, 30), GRAY, 0.6)
        return panel
    drive, target = out["drive"], out["target"]
    state = drive["state"] if drive["reason"] != "miss" else "miss"
    text(panel, state.upper(), (10, 32), STATE_COLOR.get(drive["state"], WHITE), 0.9, 2)
    if drive["reason"]:
        text(panel, f"({drive['reason']})", (10, 56), GRAY, 0.5)
    chassis = shared.get("chassis")
    sent = [int(round(v)) for v in chassis.now] if chassis else [0, 0]
    wheel_bar(panel, width - 150, 22, 110, "L", drive["wheels"][0], sent[0])
    wheel_bar(panel, width - 80, 22, 110, "R", drive["wheels"][1], sent[1])

    # 距離比例條：0.5 ~ 2.0（對數），中間 = 基準
    x0, x1, y = 20, width - 200, 95
    cv2.line(panel, (x0, y), (x1, y), GRAY, 2)
    to_x = lambda r: int(x0 + (np.log2(r) + 1) / 2 * (x1 - x0))
    for r, color in ((1 - ARRIVE_BAND, PALE), (1 + ARRIVE_BAND, PALE), (1 - BACKOFF_BAND, ORANGE), (1.0, YELLOW)):
        cv2.line(panel, (to_x(r), y - 8), (to_x(r), y + 8), color, 2)
    text(panel, "near", (x0, y + 24), GRAY, 0.45)
    text(panel, "far", (x1 - 24, y + 24), GRAY, 0.45)
    ratio = target.get("dist_ratio") if target else None
    if ratio:
        rx = to_x(min(max(ratio, 0.5), 2.0))
        cv2.circle(panel, (rx, y), 8, GREEN, -1)
        text(panel, f"dist x{ratio:.2f}", (rx - 30, y - 14), GREEN, 0.5)
    ref = f"reference d {drive['target_d']}px {drive['ref_angle']:+.1f} deg" if drive["target_d"] else "no reference yet"
    text(panel, ref, (10, 150), GRAY, 0.45)
    text(panel, f"{shared.get('device')}  {fps:.0f} fps   r=reference p=pause q=quit", (260, 150), GRAY, 0.45)
    return panel


def run_with_view(args, run):
    """chase.run 跑在背景執行緒（asyncio），視窗在主執行緒。"""
    shared = {"out": None, "jpeg": None}
    done = threading.Event()
    errors = []

    def control():
        try:
            asyncio.run(run(args, shared))
        except (Exception, SystemExit) as e:  # noqa: BLE001 — 印出來、關視窗
            errors.append(e)
        finally:
            done.set()

    threading.Thread(target=control, daemon=True).start()
    stop = threading.Event()
    video_url = args.url.replace("ws://", "http://").rsplit("/", 1)[0] + "/video" if args.url else SERVER_URL
    threading.Thread(target=mjpeg_reader, args=(video_url, shared, stop), daemon=True).start()

    def command(line):
        if shared.get("loop") and shared.get("commands") is not None:
            shared["loop"].call_soon_threadsafe(shared["commands"].put_nowait, line)

    fps, last, start = 0.0, time.monotonic(), time.monotonic()
    blank = np.zeros((480, 640, 3), np.uint8)
    view = blank
    try:
        while not done.is_set():
            jpeg = shared["jpeg"]
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
            if frame is None:
                frame = blank.copy()
                text(frame, "waiting for video from tennis_tracking server ...", (60, 240), GRAY, 0.6)
            out = shared["out"]
            now = time.monotonic()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-3)
            last = now
            view = np.vstack([draw_frame(frame, out), draw_panel(frame.shape[1], out, shared, fps)])
            if args.view_headless:
                time.sleep(0.03)
                if now - start > args.view_headless:
                    break
                continue
            cv2.imshow(WIN, view)
            key = cv2.waitKey(15) & 0xFF
            if key in (ord("q"), 27) or cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key in (ord("r"), ord("p")):
                command(chr(key))
    except KeyboardInterrupt:
        pass
    finally:
        command("q")
        done.wait(timeout=5)
        stop.set()
        if args.view_headless:
            cv2.imwrite("ball_chase/view_snapshot.jpg", view)
        cv2.destroyAllWindows()
    for e in errors:
        print(f"❌ {e}")
