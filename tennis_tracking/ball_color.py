"""
網球的顏色（螢光黃綠）：顏色範圍設定、把畫面變成「哪裡是網球顏色」的遮罩。
track_ball.py 和 tracking_helper.py 共用；範圍用 track_ball.py --tune 調，存在 settings.json。
"""

import json
from pathlib import Path

import cv2
import numpy as np

# 螢光黃綠的顏色範圍（OpenCV 的 H 是 0~179），用實際錄影量的：場館燈光下球是 H 36～38、S 92～152、V 197～246。
# S 下限原本 100 太嚴，會把球比較暗、比較白的那半邊切掉（球心和大小都算歪）；60 在錄影裡誤差最小
HSV_LOWER = (28, 60, 90)
HSV_UPPER = (46, 255, 255)       # 燈光差很多就用 --tune 重調

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"


def load_settings():
    settings = {"hsv_lower": list(HSV_LOWER), "hsv_upper": list(HSV_UPPER)}
    if SETTINGS_PATH.exists():
        settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    return settings


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"💾 存到 {SETTINGS_PATH.name}")


def make_mask(frame, lower, upper):
    blurred = cv2.GaussianBlur(frame, (5, 5), 0)  # 模糊太大會讓球看起來變大
    hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(lower), np.array(upper))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)   # 去掉小雜點
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)  # 補上球上的白線和反光
    return mask
