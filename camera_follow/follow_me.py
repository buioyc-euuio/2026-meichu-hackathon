"""
雲台模式：相機左右（伺服馬達）自動轉向人，車子和相機上下都不動。

    .venv/bin/python camera_follow/follow_me.py --fake              # 不連車，只印出 P,角度#
    .venv/bin/python camera_follow/follow_me.py                     # 連 micro:bit，真的轉
    .venv/bin/python camera_follow/follow_me.py --fake --headless   # 不開視窗跑 10 秒（測試用）

視窗按鍵：
    q 離開     空白鍵 暫停/繼續追蹤     c 相機回正前方
    + / -  調大/調小 Kp          ] / [  調大/調小 死區

方向：伺服 0 = 最右、180 = 最左。人在畫面右邊（ex > 0）→ 角度要變小。
如果實際轉的方向相反（例如相機裝反），加 --invert。
"""

import argparse
import sys
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "LLM_control_motor"))

from detect_person import CAMERA_INDEX, FRAME_HEIGHT, FRAME_WIDTH, create_detector, draw, find_person  # noqa: E402
from robot_bluetooth import clamp, robot  # noqa: E402

# ====== 想改的東西都在這裡 ======
KP = 8.0                 # 人在畫面最邊邊（ex = ±1）時，一張畫面轉幾度；太大會來回晃、太小跟不上
DEADZONE = 0.12          # |ex| 小於這個就不動，避免人站著不動時相機一直抖
MAX_STEP = 6.0           # 一張畫面最多轉幾度（保護伺服，也避免跟丟）
PAN_MIN, PAN_MAX = 0, 180
PAN_CENTER = 90
SEND_INTERVAL = 0.05     # 最快每幾秒送一次 P 指令（藍牙送太快會塞車）
# ================================


def pan_step(pan, ex, kp, deadzone, invert=False):
    """根據人在畫面的左右位置，算出新的伺服角度。"""
    if abs(ex) < deadzone:
        return pan
    step = clamp(kp * ex, -MAX_STEP, MAX_STEP)
    if invert:
        step = -step
    return clamp(pan - step, PAN_MIN, PAN_MAX)   # 人在右（ex>0）→ 角度變小 → 往右轉


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit，只印出指令")
    parser.add_argument("--headless", action="store_true", help="不開視窗，跑 10 秒")
    parser.add_argument("--invert", action="store_true", help="轉的方向相反時使用")
    parser.add_argument("--camera", type=int, default=CAMERA_INDEX)
    parser.add_argument("--kp", type=float, default=KP)
    parser.add_argument("--deadzone", type=float, default=DEADZONE)
    args = parser.parse_args()
    kp, deadzone = args.kp, args.deadzone

    cap = cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    if not cap.isOpened():
        raise SystemExit(f"❌ 打不開 /dev/video{args.camera}，先跑 find_camera.py")

    robot.fake = args.fake
    robot.connect()
    detector = create_detector()

    pan = float(PAN_CENTER)
    sent_angle = PAN_CENTER
    robot.send(f"P,{PAN_CENTER}#")
    last_send = 0.0
    tracking = True

    start = time.monotonic()
    last, fps = start, 0.0
    print(f"🎯 雲台模式  Kp={kp}  死區={deadzone}" + ("  （方向反轉）" if args.invert else ""))
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

            # 看到人才轉；沒看到人就停在原本角度
            if tracking and person:
                pan = pan_step(pan, person["ex"], kp, deadzone, args.invert)
            angle = int(round(pan))
            if angle != sent_angle and now - last_send >= SEND_INTERVAL:
                robot.send(f"P,{angle}#")
                sent_angle, last_send = angle, now
                robot.pan_angle = angle

            draw(frame, person, fps)
            status = f"pan {sent_angle}  Kp {kp:.1f}  dz {deadzone:.2f}" + ("" if tracking else "  PAUSED")
            cv2.putText(frame, status, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)
            # 死區範圍畫成兩條直線：人在兩線之間就不轉
            W = frame.shape[1]
            for x in (int(W / 2 * (1 - deadzone)), int(W / 2 * (1 + deadzone))):
                cv2.line(frame, (x, 0), (x, frame.shape[0]), (255, 200, 0), 1)

            if args.headless:
                if now - start > 10:
                    shot = HERE / "follow_snapshot.jpg"
                    cv2.imwrite(str(shot), frame)
                    print(f"📸 存了 {shot.name}")
                    break
                continue

            cv2.imshow("follow_me", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord(" "):
                tracking = not tracking
                print("⏸️  暫停追蹤" if not tracking else "▶️  繼續追蹤")
            elif key == ord("c"):
                pan = float(PAN_CENTER)
            elif key in (ord("+"), ord("=")):
                kp = round(kp + 1, 1)
                print(f"Kp = {kp}")
            elif key == ord("-"):
                kp = round(max(0.5, kp - 1), 1)
                print(f"Kp = {kp}")
            elif key == ord("]"):
                deadzone = round(min(0.5, deadzone + 0.02), 2)
                print(f"死區 = {deadzone}")
            elif key == ord("["):
                deadzone = round(max(0.0, deadzone - 0.02), 2)
                print(f"死區 = {deadzone}")
    except KeyboardInterrupt:
        pass
    finally:
        print(f"🔚 結束，最後設定：--kp {kp} --deadzone {deadzone}")
        robot.send(f"P,{PAN_CENTER}#")
        robot.disconnect()
        detector.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
