"""
看畫面用：追蹤網球 + 偵測人（可以同時很多顆球、很多人，每個都有固定編號）。
預設用 YOLO（GPU）找球和人，球再用傳統 CV（tracking_helper.py）幫忙修正、驗證、補抓。
要給別的程式用，改跑 server.py（FastAPI 即時串流 JSON）。

    .venv/bin/python tennis_tracking/track_ball.py                    # 開視窗，按 q 離開
    .venv/bin/python tennis_tracking/track_ball.py --json             # 每張畫面印一行 JSON（跟 server 送的一樣）
    .venv/bin/python tennis_tracking/track_ball.py --video test.mp4   # 用影片測試（每張畫面都會印）
    .venv/bin/python tennis_tracking/track_ball.py --image photo.jpg  # 用一張照片測試（按任意鍵關掉）
    .venv/bin/python tennis_tracking/track_ball.py --headless         # 不開視窗，跑 5 秒印數值並存截圖
    .venv/bin/python tennis_tracking/track_ball.py --no-yolo          # 不用 YOLO，只靠顏色 + 前後畫面（不找人）
    .venv/bin/python tennis_tracking/track_ball.py --raw              # 只用 YOLO，不修正（比較用）
    .venv/bin/python tennis_tracking/track_ball.py --device cpu       # YOLO 用 CPU（預設是 GPU）
    .venv/bin/python tennis_tracking/track_ball.py --tune             # 用滑桿調網球的顏色範圍，按 s 存檔

每個數值的意思寫在 pipeline.py 最上面。
"""

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from ball_color import load_settings, make_mask, save_settings
from pipeline import Pipeline
from tracking_helper import find_circles

# ====== 想改的東西都在這裡 ======
CAMERA_INDEX = 0                 # camera_follow/find_camera.py 找到的相機編號
FRAME_WIDTH, FRAME_HEIGHT = 640, 480
PRINT_INTERVAL = 0.2             # 終端機每幾秒印一次數值
# ================================

HERE = Path(__file__).resolve().parent


def open_camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    if not cap.isOpened():
        raise SystemExit(f"❌ 打不開 /dev/video{index}，先跑 camera_follow/find_camera.py")
    return cap


class ImageSource:
    """讓一張照片用起來像相機：第一次 read() 給照片，之後就結束。"""

    def __init__(self, path):
        self.frame = cv2.imread(path)
        if self.frame is None:
            raise SystemExit(f"❌ 打不開 {path}")

    def read(self):
        frame, self.frame = self.frame, None
        return frame is not None, frame

    def release(self):
        pass


def open_source(args):
    if args.image:
        return ImageSource(args.image)
    if args.video:
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise SystemExit(f"❌ 打不開 {args.video}")
        return cap
    return open_camera(args.camera)


def print_result(result, with_people):
    if not result["balls"]:
        print("   沒看到球")
    for b in result["balls"]:
        tag = f"#{b['id']}" if b["id"] is not None else "  "
        print(f"🎾 {tag:>3}  x {b['x']:5.1f}  y {b['y']:5.1f}  ex {b['ex']:+.2f}  ey {b['ey']:+.2f}  "
              f"d {b['d']:5.1f}px  size {b['size']:.3f}  [{b['source']}]")
    if not with_people:
        return
    if not result["people"]:
        print("   沒看到人")
    for p in result["people"]:
        print(f"🧍 #{p['id']:<2}  x {p['x']:5.1f}  y {p['y']:5.1f}  ex {p['ex']:+.2f}  ey {p['ey']:+.2f}  "
              f"h {p['h']:.2f}  conf {p['conf']:.2f}" + ("  (沒看到，用上一張的位置)" if p["misses"] else ""))


def run_track(cap, args, settings):
    pipeline = Pipeline(use_yolo=not args.no_yolo, device=args.device, conf=args.conf, raw=args.raw, settings=settings)
    from_file = args.video or args.image
    start = last_print = time.monotonic()
    shown = None
    print(f"🎾 開始追蹤（{pipeline.mode}）" + ("" if args.headless or args.image else "，按 q 離開"))
    while True:
        ok, frame = cap.read()
        if not ok:
            if not from_file:
                print("❌ 讀取相機失敗")
            break
        result = pipeline.process(frame)

        now = time.monotonic()
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif from_file or now - last_print >= PRINT_INTERVAL:  # 影片每張都印（跑得比即時快）
            print_result(result, with_people=pipeline.detector is not None)
            print()
            last_print = now
        pipeline.draw(frame, result)
        shown = frame

        if args.headless:
            if not from_file and now - start > 5:
                break
        else:
            cv2.imshow("tennis_tracking", frame)
            if cv2.waitKey(0 if args.image else 1) & 0xFF == ord("q"):  # 照片：按任意鍵關掉
                break
    if (args.headless or args.image) and shown is not None:
        shot = HERE / "track_snapshot.jpg"
        cv2.imwrite(str(shot), shown)
        print(f"📸 存了 {shot.name}")


def run_tune(cap, settings):
    """開滑桿調 HSV 範圍：右邊白色的地方就是被當成網球的顏色。按 s 存檔、q 離開。"""
    win = "tune (s = save, q = quit)"
    cv2.namedWindow(win)
    names = ("H low", "S low", "V low", "H high", "S high", "V high")
    values = settings["hsv_lower"] + settings["hsv_upper"]
    for name, value in zip(names, values):
        cv2.createTrackbar(name, win, int(value), 179 if name.startswith("H") else 255, lambda _: None)
    print("🎚️  調到只有網球是白色，按 s 存檔、q 離開")
    while True:
        ok, frame = cap.read()
        if not ok:
            print("❌ 讀取相機失敗")
            break
        values = [cv2.getTrackbarPos(name, win) for name in names]
        settings["hsv_lower"], settings["hsv_upper"] = values[:3], values[3:]
        mask = make_mask(frame, settings["hsv_lower"], settings["hsv_upper"])
        for c in find_circles(mask):
            cv2.circle(frame, (int(c["x"]), int(c["y"])), int(c["r"]), (0, 255, 0) if c["clean"] else (0, 200, 255), 2)
        cv2.imshow(win, np.hstack([frame, cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)]))
        key = cv2.waitKey(1) & 0xFF
        if key == ord("s"):
            save_settings(settings)
        elif key == ord("q"):
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, default=CAMERA_INDEX)
    parser.add_argument("--video", help="用影片檔代替相機")
    parser.add_argument("--image", help="用一張照片代替相機（存標好的結果到 track_snapshot.jpg）")
    parser.add_argument("--headless", action="store_true", help="不開視窗；相機跑 5 秒、影片跑完，存最後一張截圖")
    parser.add_argument("--json", action="store_true", help="每張畫面印一行 JSON（跟 server.py 送的一樣）")
    parser.add_argument("--no-yolo", action="store_true", help="不用 YOLO，只靠顏色 + 前後畫面（不找人）")
    parser.add_argument("--raw", action="store_true", help="只用 YOLO 原本的結果，不修正（比較用）")
    parser.add_argument("--tune", action="store_true", help="用滑桿調顏色範圍")
    parser.add_argument("--conf", type=float, default=0.05, help="YOLO 找球的信心門檻（預設故意放低）")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu", help="YOLO 用 GPU（預設，找不到會自動改 CPU）或 CPU")
    args = parser.parse_args()
    if args.raw and args.no_yolo:
        parser.error("--raw 和 --no-yolo 不能一起用")

    settings = load_settings()
    cap = open_source(args)
    try:
        if args.tune:
            run_tune(cap, settings)
        else:
            run_track(cap, args, settings)
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
