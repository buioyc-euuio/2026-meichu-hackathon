"""
用 MediaPipe 偵測人：畫出身體關鍵點、人框、軀幹中心，並算出追蹤要用的誤差。

    .venv/bin/python camera_follow/detect_person.py              # 開視窗，按 q 離開
    .venv/bin/python camera_follow/detect_person.py --headless   # 不開視窗，印數值並存截圖（測試用）

畫面上的數值（之後控制馬達會用到）：
    ex   人在左右哪裡：-1 = 最左、0 = 正中間、+1 = 最右
    ey   人在上下哪裡：-1 = 最上、0 = 正中間、+1 = 最下
    h    人框高度佔畫面比例：越小代表人越遠
"""

import argparse
import time
import urllib.request
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

# ====== 想改的東西都在這裡 ======
CAMERA_INDEX = 0             # find_camera.py 找到的相機編號
FRAME_WIDTH, FRAME_HEIGHT = 640, 480
MIN_VISIBILITY = 0.5         # 關鍵點可見度低於這個就不算（被擋住或在畫面外）
# ================================

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "models" / "pose_landmarker_lite.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task")

# MediaPipe Pose 33 個點裡，軀幹用到的編號
SHOULDERS_HIPS = (11, 12, 23, 24)


def ensure_model():
    if not MODEL_PATH.exists():
        print("⬇️  下載 MediaPipe 模型 ...")
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)


def create_detector():
    ensure_model()
    options = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
    )
    return vision.PoseLandmarker.create_from_options(options)


def find_person(detector, frame, timestamp_ms):
    """回傳 None（沒看到人）或 dict：關鍵點像素座標、人框、軀幹中心、ex/ey/h。"""
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    result = detector.detect_for_video(mp.Image(mp.ImageFormat.SRGB, rgb), timestamp_ms)
    if not result.pose_landmarks:
        return None

    H, W = frame.shape[:2]
    landmarks = result.pose_landmarks[0]
    points = {i: (int(p.x * W), int(p.y * H)) for i, p in enumerate(landmarks)
              if (p.visibility or 0) >= MIN_VISIBILITY and 0 <= p.x <= 1 and 0 <= p.y <= 1}
    if len(points) < 3:
        return None

    xs = [x for x, _ in points.values()]
    ys = [y for _, y in points.values()]
    box = (min(xs), min(ys), max(xs), max(ys))

    # 優先用肩膀＋臀部的平均當中心（比人框中心穩，手揮來揮去不會影響）
    torso = [points[i] for i in SHOULDERS_HIPS if i in points]
    if len(torso) >= 2:
        cx = sum(x for x, _ in torso) / len(torso)
        cy = sum(y for _, y in torso) / len(torso)
    else:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

    return {
        "points": points,
        "box": box,
        "center": (int(cx), int(cy)),
        "ex": (cx - W / 2) / (W / 2),
        "ey": (cy - H / 2) / (H / 2),
        "h": (box[3] - box[1]) / H,
    }


def draw(frame, person, fps):
    H, W = frame.shape[:2]
    cv2.drawMarker(frame, (W // 2, H // 2), (200, 200, 200), cv2.MARKER_CROSS, 30, 1)
    if person is None:
        cv2.putText(frame, "no person", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    else:
        for x, y in person["points"].values():
            cv2.circle(frame, (x, y), 3, (0, 255, 255), -1)
        x1, y1, x2, y2 = person["box"]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.circle(frame, person["center"], 8, (0, 0, 255), -1)
        cv2.line(frame, (W // 2, H // 2), person["center"], (0, 0, 255), 2)
        text = f"ex {person['ex']:+.2f}  ey {person['ey']:+.2f}  h {person['h']:.2f}"
        cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.putText(frame, f"{fps:.0f} fps", (10, H - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="不開視窗，跑 5 秒印數值並存截圖")
    parser.add_argument("--camera", type=int, default=CAMERA_INDEX)
    args = parser.parse_args()

    cap = cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    if not cap.isOpened():
        raise SystemExit(f"❌ 打不開 /dev/video{args.camera}，先跑 find_camera.py")

    detector = create_detector()
    start = time.monotonic()
    last, fps = start, 0.0
    print("🎥 開始偵測" + ("（5 秒）" if args.headless else "，按 q 離開"))
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("❌ 讀取相機失敗")
                break
            now = time.monotonic()
            person = find_person(detector, frame, int((now - start) * 1000))
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-3)
            last = now
            draw(frame, person, fps)

            if args.headless:
                if person:
                    print(f"👤 ex {person['ex']:+.2f}  ey {person['ey']:+.2f}  h {person['h']:.2f}  {fps:.0f}fps")
                else:
                    print(f"   沒看到人  {fps:.0f}fps")
                if now - start > 5:
                    shot = HERE / "detect_snapshot.jpg"
                    cv2.imwrite(str(shot), frame)
                    print(f"📸 存了 {shot.name}")
                    break
            else:
                cv2.imshow("detect_person", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        detector.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
