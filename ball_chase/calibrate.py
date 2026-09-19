"""
互動校正「停在哪裡」：開視窗看即時畫面，把網球放到你想讓車子停下的距離，按 Enter 量球的直徑（px）。
之後 chase.py 就追到球看起來一樣大為止——不用算公分、也不用管相機高度和角度（校正時都包含進去了）。

    .venv/bin/python tennis_tracking/server.py             # 先開追蹤伺服器
    .venv/bin/python ball_chase/calibrate.py               # 開視窗：球放好 → 按 Enter
    .venv/bin/python ball_chase/calibrate.py --cm 40       # 順便記下這是幾公分（只是記錄，控制不用）

視窗按鍵：Enter 量（約 1.5 秒取中位數，存到 ball_chase/settings.json；再按一次會覆蓋）   q / Esc 離開

⚠️ 相機要先鎖在追球時的角度（左右 90 度；上下調到球放在目標距離也看得到），之後追球時不要再動相機。
"""

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "camera_control"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from chase import SETTINGS_PATH, SERVER_URL, size_ok  # noqa: E402
from test_view import mjpeg_reader, text  # noqa: E402

MEASURE_SECONDS = 1.5
MIN_SAMPLES = 8
WIN = "ball_chase calibrate (Enter = measure, q = quit)"
GREEN, ORANGE, RED, CYAN, WHITE, GRAY = (0, 220, 0), (0, 165, 255), (0, 0, 255), (255, 255, 0), (255, 255, 255), (150, 150, 150)


def json_reader(url, shared, stop):
    """背景執行緒：一直收 tennis_tracking 的 JSON，留最新一筆，也記下量測期間的每一張。"""
    while not stop.is_set():
        try:
            with connect(url, open_timeout=3) as ws:
                shared["connected"] = True
                while not stop.is_set():
                    data = json.loads(ws.recv(timeout=2))
                    shared["latest"] = data
                    if shared["recording"] is not None:
                        shared["recording"].append(data)
        except Exception:  # noqa: BLE001 — server 還沒開、斷線：一秒後重連
            shared["connected"] = False
            time.sleep(1)


def pick_ball(data):
    balls = [b for b in data["balls"] if b["misses"] == 0]
    return max(balls, key=lambda b: b["d"]) if balls else None


def ball_status(ball, W, H):
    """這顆球能不能拿來量：回傳 (可以嗎, 說明)。"""
    if ball["source"] not in ("yolo+cv", "cv"):
        return False, f"no color fit ({ball['source']})"
    if not size_ok(ball, W, H):
        return False, "cut by image edge"
    return True, "OK"


def measure(frames):
    sizes = []
    for data in frames:
        ball = pick_ball(data)
        if ball and ball_status(ball, data["width"], data["height"])[0]:
            sizes.append(ball["d"])
    return sizes


def draw(frame, data, saved, message):
    h, w = frame.shape[:2]
    cv2.drawMarker(frame, (w // 2, h // 2), WHITE, cv2.MARKER_CROSS, 24, 1)
    ball = pick_ball(data) if data else None
    if ball:
        ok, why = ball_status(ball, data["width"], data["height"])
        color = GREEN if ok else ORANGE
        cv2.circle(frame, (int(ball["x"]), int(ball["y"])), int(ball["r"]), color, 3)
        text(frame, f"d = {ball['d']:.1f}px  [{why}]", (int(ball["x"] - ball["r"]), int(ball["y"] + ball["r"] + 22)),
             color, 0.6, 2)
        if ball["y"] + ball["r"] > h - 30:
            text(frame, "ball near bottom edge: tilt camera up a bit", (10, h - 68), ORANGE, 0.55)
    else:
        text(frame, "no ball", (10, 60), RED, 0.7, 2)
    if saved:
        cm = f" = {saved['distance_cm']:g} cm" if saved.get("distance_cm") else ""
        text(frame, f"saved target: {saved['target_d']:.1f}px{cm}", (10, 30), CYAN, 0.65, 2)
    text(frame, message, (10, h - 40), WHITE, 0.55)          # 最下面一行是 server 畫的 fps
    return frame


def save(d, sizes, cm):
    settings = {"target_d": round(d, 1), "distance_cm": cm, "spread_px": round(statistics.pstdev(sizes), 1),
                "samples": len(sizes), "measured": time.strftime("%Y-%m-%d %H:%M:%S")}
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return settings


def main():
    parser = argparse.ArgumentParser(description="互動校正：球放到想停的距離，按 Enter 量直徑")
    parser.add_argument("--url", default=SERVER_URL, help="tennis_tracking server 的 WebSocket 網址")
    parser.add_argument("--video-url", help="server 的影像網址，預設從 --url 推（.../video）")
    parser.add_argument("--cm", type=float, help="球放在幾公分（只是記錄、顯示用，控制不用）")
    parser.add_argument("--auto", type=float, metavar="SECONDS",
                        help="不開視窗：等幾秒後自動量一次、存 calibrate_snapshot.jpg（沒有螢幕時測試用）")
    args = parser.parse_args()

    video_url = args.video_url or args.url.replace("ws://", "http://").rsplit("/", 1)[0] + "/video"
    shared = {"latest": None, "jpeg": None, "recording": None, "connected": False}
    stop = threading.Event()
    threading.Thread(target=json_reader, args=(args.url, shared, stop), daemon=True).start()
    threading.Thread(target=mjpeg_reader, args=(video_url, shared, stop), daemon=True).start()

    saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else None
    message = "put the ball where the car should stop, then press Enter"
    measure_until = None
    start = time.monotonic()
    print("📏 把球放到想讓車子停下的距離，在視窗按 Enter 量（q 離開）", flush=True)
    try:
        while True:
            now = time.monotonic()
            if measure_until and now >= measure_until:          # 量完了
                frames, shared["recording"], measure_until = shared["recording"], None, None
                sizes = measure(frames)
                if len(sizes) < MIN_SAMPLES:
                    message = f"FAILED: only {len(sizes)} usable frames (need {MIN_SAMPLES}) - try again"
                    print(f"❌ 只量到 {len(sizes)} 張可用的（{len(frames)} 張裡），球有被看到、沒被邊緣切到嗎？", flush=True)
                else:
                    d = statistics.median(sizes)
                    saved = save(d, sizes, args.cm)
                    message = f"saved {d:.1f}px (+-{saved['spread_px']}px, {len(sizes)} frames) - Enter again to redo"
                    print(f"💾 目標直徑 {d:.1f}px（抖動 ±{saved['spread_px']}px，{len(sizes)} 張）"
                          + (f"，{args.cm:g} cm" if args.cm else "") + f" → {SETTINGS_PATH.name}", flush=True)
                if args.auto:
                    break
            elif measure_until:
                message = f"measuring... keep the ball still ({measure_until - now:.1f}s)"
            elif not shared["connected"]:
                message = f"waiting for tennis_tracking server ({args.url}) ..."

            jpeg = shared["jpeg"]
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
            if frame is None:
                frame = np.zeros((480, 640, 3), np.uint8)
            frame = draw(frame, shared["latest"], saved, message)

            start_measure = False
            if args.auto:
                time.sleep(0.03)
                start_measure = measure_until is None and now - start > args.auto
                if now - start > args.auto + 10:
                    break
            else:
                cv2.imshow(WIN, frame)
                key = cv2.waitKey(15) & 0xFF
                if key in (ord("q"), 27) or cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break
                start_measure = key in (13, 10) and measure_until is None
            if start_measure:
                shared["recording"] = []
                measure_until = time.monotonic() + MEASURE_SECONDS
                print("   量測中，球別動 ...", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if args.auto:
            cv2.imwrite(str(Path(__file__).resolve().parent / "calibrate_snapshot.jpg"), frame)
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
