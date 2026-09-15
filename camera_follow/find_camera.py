"""
找相機：掃描 /dev/video0~9，列出哪些能讀到畫面，存一張截圖，可選擇開預覽視窗。

    .venv/bin/python camera_follow/find_camera.py            # 掃描 + 存截圖
    .venv/bin/python camera_follow/find_camera.py --show 0   # 開 0 號相機預覽，按 q 離開
"""

import argparse
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent


def open_camera(index, width=640, height=480):
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


def scan(max_index=10):
    found = []
    for i in range(max_index):
        if not Path(f"/dev/video{i}").exists():
            continue
        name = Path(f"/sys/class/video4linux/video{i}/name").read_text().strip()
        cap = open_camera(i)
        # 前幾張常常是黑的（自動曝光還沒調好），多讀幾張
        ok, frame = False, None
        for _ in range(10):
            ok, frame = cap.read()
        if ok:
            h, w = frame.shape[:2]
            fps = cap.get(cv2.CAP_PROP_FPS)
            shot = HERE / f"snapshot_video{i}.jpg"
            cv2.imwrite(str(shot), frame)
            print(f"✅ /dev/video{i}  {name}  {w}x{h} @ {fps:.0f}fps  → 截圖 {shot.name}")
            found.append(i)
        else:
            print(f"➖ /dev/video{i}  {name}  讀不到畫面（通常是 metadata 節點，正常）")
        cap.release()
    if not found:
        print("❌ 沒找到能用的相機：確認 USB 有插好、lsusb 看得到、使用者有 video 權限")
    return found


def show(index):
    cap = open_camera(index)
    print(f"🎥 預覽 /dev/video{index}，按 q 離開")
    while True:
        ok, frame = cap.read()
        if not ok:
            print("❌ 讀取失敗")
            break
        cv2.imshow(f"video{index}", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", type=int, metavar="INDEX", help="開預覽視窗")
    args = parser.parse_args()
    if args.show is not None:
        show(args.show)
    else:
        scan()
