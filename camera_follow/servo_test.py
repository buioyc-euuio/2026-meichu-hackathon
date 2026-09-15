"""
手動測試伺服：連上 micro:bit 後，自己輸入角度、按 Enter 就轉，邊看邊測。

    .venv/bin/python camera_follow/servo_test.py

輸入 0~180 的數字（0 最右、90 正前方、180 最左），直接按 Enter 輪流 45 → 135 → 90，輸入 q 離開。
"""

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "LLM_control_motor"))

from robot_bluetooth import clamp, robot  # noqa: E402

robot.connect()
sweep = itertools.cycle((45, 135, 90))
try:
    while True:
        text = input("角度（Enter = 下一個、q = 離開）> ").strip()
        if text.lower() == "q":
            break
        try:
            angle = next(sweep) if text == "" else int(clamp(float(text), 0, 180))
        except ValueError:
            print("請輸入 0~180 的數字")
            continue
        robot.send(f"P,{angle}#")
except (KeyboardInterrupt, EOFError):
    pass
finally:
    robot.send("P,90#")
    robot.disconnect()
