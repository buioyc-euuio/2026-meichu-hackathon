"""
整條流程：一張畫面進去 → YOLO 找球和人 → 追蹤小幫手修正、給編號 → 一個可以直接轉成 JSON 的 dict。
track_ball.py（看畫面用）和 server.py（FastAPI 串流用）都用這個。

    pipeline = Pipeline()                 # 預設：YOLO（GPU）+ CV 幫忙
    result = pipeline.process(frame)      # dict，json.dumps(result) 就能送出去
    pipeline.draw(frame, result)          # 把結果畫在畫面上（看畫面、除錯用）

result 長這樣（座標都是像素，畫面左上角是 0,0）：
    {
      "frame": 123,                  第幾張畫面
      "time": 1726740000.123,        時間（Unix 秒）
      "width": 640, "height": 480,
      "fps": 30.0,
      "balls": [                     所有球，大（近）的排前面
        {"id": 1, "x": 320.5, "y": 240.1, "r": 40.6, "d": 81.2, "size": 0.127,
         "ex": 0.0, "ey": 0.0, "box": [x1, y1, x2, y2], "vx": 1.2, "vy": -0.5,
         "source": "yolo+cv", "conf": 0.62, "misses": 0}
      ],
      "people": [                    所有人，大（近）的排前面
        {"id": 3, "x": 300.0, "y": 250.0, "ex": -0.06, "ey": 0.04, "h": 0.71,
         "box": [x1, y1, x2, y2], "conf": 0.88, "misses": 0}
      ]
    }

id 換畫面不會變（同一顆球、同一個人一直是同一個號碼）；misses > 0 表示這張沒看到、是用預測或上一張的位置。
"""

import time

import cv2

from ball_color import load_settings
from tracking_helper import MultiBallTracker, PersonTracker


def _round(obj):
    """小數點留短一點，JSON 比較小。"""
    if isinstance(obj, float):
        return round(obj, 3) if abs(obj) < 10 else round(obj, 1)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


class Pipeline:
    def __init__(self, use_yolo=True, device="gpu", conf=0.05, raw=False, settings=None):
        """use_yolo=False：只用顏色找球、不找人。raw=True：只用 YOLO 原本的框，不修正（比較用）。"""
        self.detector = None
        if use_yolo:
            from yolo_detector import YoloBallDetector  # 用到才載入（torch 很大，--no-yolo 不用等）
            self.detector = YoloBallDetector(conf=conf, device=device)
        self.raw = raw
        self.balls = MultiBallTracker(settings or load_settings(), use_yolo=use_yolo)
        self.people = PersonTracker()
        self.frame_no = 0
        self.fps = 0.0
        self._last = None

    @property
    def mode(self):
        if self.raw:
            return "只用 YOLO"
        return "YOLO + CV 幫忙" if self.detector else "顏色 + 前後畫面"

    def process(self, frame):
        H, W = frame.shape[:2]
        ball_boxes, person_boxes = self.detector(frame) if self.detector else ([], [])
        if self.raw:
            self.balls.candidates = []
            balls = [{"id": None, "x": (x1 + x2) / 2, "y": (y1 + y2) / 2, "r": (x2 - x1 + y2 - y1) / 4,
                      "d": (x2 - x1 + y2 - y1) / 2, "size": (x2 - x1 + y2 - y1) / 2 / W,
                      "ex": ((x1 + x2) / 2 - W / 2) / (W / 2), "ey": ((y1 + y2) / 2 - H / 2) / (H / 2),
                      "box": [x1, y1, x2, y2], "source": "raw", "conf": conf, "misses": 0}
                     for x1, y1, x2, y2, conf in ball_boxes]
        else:
            balls = self.balls.update(frame, ball_boxes)
        people = self.people.update(person_boxes, W, H) if self.detector else []

        now = time.monotonic()
        if self._last is not None:
            self.fps = 0.9 * self.fps + 0.1 / max(now - self._last, 1e-3)
        self._last = now
        self.frame_no += 1
        return _round({"frame": self.frame_no, "time": time.time(), "width": W, "height": H,
                       "fps": self.fps, "balls": balls, "people": people})

    def draw(self, frame, result):
        """畫面上：紫框 = 人、綠圈 = 球（旁邊寫 #編號 和來源）、黃點 = 預測、藍/紅細框 = YOLO 的球框（通過/淘汰）。"""
        H, W = frame.shape[:2]
        cv2.drawMarker(frame, (W // 2, H // 2), (200, 200, 200), cv2.MARKER_CROSS, 30, 1)
        for c in self.balls.candidates:
            x1, y1, x2, y2 = map(int, c["box"])
            color = (255, 128, 0) if c["ok"] else (0, 0, 255)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 1)
        for p in result["people"]:
            x1, y1, x2, y2 = map(int, p["box"])
            thick = 1 if p["misses"] else 2
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 255), thick)
            cv2.putText(frame, f"person #{p['id']} {p['conf']:.2f}", (x1 + 3, y1 + 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1)
        for b in result["balls"]:
            center = (int(b["x"]), int(b["y"]))
            color = (0, 200, 255) if b["source"] == "predict" else (0, 255, 0)
            cv2.circle(frame, center, int(b["r"]), color, 2)
            cv2.circle(frame, center, 3, color, -1)
            label = f"#{b['id']} {b['source']}" if b["id"] is not None else b["source"]
            cv2.putText(frame, label, (center[0] + int(b["r"]) + 4, center[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        cv2.putText(frame, f"balls {len(result['balls'])}  people {len(result['people'])}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame, f"{result['fps']:.0f} fps", (10, H - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
