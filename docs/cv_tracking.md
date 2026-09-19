# CV Tracking: Tennis Balls and People

Code: `tennis_tracking/` (`yolo_detector.py`, `tracking_helper.py`, `ball_color.py`, `pipeline.py`, `server.py`)

**Main idea:** YOLO is good at *finding* objects, but its boxes are loose, it misses small or blurry balls, and it
sometimes detects the wrong thing. Classical CV fixes the geometry, and a per-object Kalman filter keeps IDs stable
from frame to frame.

```
camera frame ─▶ YOLO11n (ball + person, one pass) ─▶ color refine / verify ─▶ Kalman match ─▶ JSON (WebSocket)
```

## 1. Detection: YOLO11n

- Uses the COCO classes `sports ball` (32) and `person` (0), both detected in the same inference.
- The ball confidence threshold is set very low (**0.05**): in recorded footage, 90% of boxes with confidence 0.01 to 0.05 were real balls.
  False positives are removed by the color check below.
- The person threshold is 0.4, because there is no color check for people.

## 2. Ball refinement: classical CV

| Technique | Purpose |
|---|---|
| **HSV color mask** (H 28–46, S ≥ 60, V ≥ 90) | Find tennis-ball-colored pixels |
| **Circle fit** (`minEnclosingCircle`) + circularity check | Re-fit a tight circle inside the YOLO box, which gives an accurate center and diameter |
| **Color verification** (≥ 35% ball color in the circle) | Reject wrong YOLO detections |
| **Distance transform** + "neck" check | Split touching balls: repeatedly take the largest circle that fits. Two balls only count as two if the blob narrows where they touch, so a yellow bar is not split into balls |
| **Adaptive color** (`AdaptiveColor`) | Learn the ball's HSV range from confirmed detections so the mask follows the lighting. Hue stays within 12–60, and the limits can loosen but never tighten |

## 3. Tracking across frames

| Technique | Purpose |
|---|---|
| **Kalman filter** (constant velocity, one per ball) | Predict where each ball will be in the next frame |
| **Global greedy assignment** (cost = distance to prediction + size difference) | Match all balls in one pass so IDs don't swap |
| **Local color re-search** | If YOLO misses a known ball, search for its color only near the predicted position |
| **Coasting** | If nothing is found, report the predicted position (`source: predict`) |
| **Track lifecycle** | A new ball needs YOLO to confirm it. A track is deleted after 8 missed frames, after leaving the frame, or after 3 s without YOLO support |
| **ID recovery** | If a ball reappears nearby within 2 s with a similar size, it gets its old ID back (for example, after a hand blocked it) |
| **IoU matching + smoothing** | Tracking people (`PersonTracker`) |

## 4. Results (1096 hand-labeled frames, venue lighting)

| | YOLO only | YOLO + CV |
|---|---|---|
| Frames with ball detected | 70.6% | **97.2%** |
| Center error | 7.5 px | **3.2 px** |
| Diameter error | 13.1 px | **5.8 px** |
| ID switches | — | **2** |

## 5. How the output is used

- **Camera centering** (`camera_control/`): moves the pan servo and tilt motor to bring the ball's `(ex, ey)` offset back to the center of the frame.
- **Ball chase** (`ball_chase/`): distance is estimated as `reference diameter / current diameter`, so no camera
  calibration is needed. A state machine handles `recenter → reference → align / approach / arrived / backoff`.
- **Fall alert** (`e2e/fall_alert.py`): a person box with width/height ≥ 1.3 for 1 s counts as "lying down" and plays a video.

## Known limits

- Two identical balls that overlap almost completely can swap IDs when they separate.
- A ball shown on a screen, such as the camera preview, is still a ball and will be tracked.
- Thresholds were tuned under venue lighting. For very different lighting, re-tune with `track_ball.py --tune`.
