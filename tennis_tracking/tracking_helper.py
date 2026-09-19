"""
追蹤小幫手：把 YOLO 的偵測結果用傳統 CV 修正，再用前後畫面的關係讓追蹤更穩。
畫面裡可以同時有好幾顆球、好幾個人，每個都有自己的編號（id），換畫面也不會變。

YOLO 很會「找到東西」，但框不準、會漏抓（球太小、動太快變模糊）、偶爾抓錯。
每顆球每張畫面做四件事：

    1. 修正（refine）：YOLO 的框通常比球大一圈，在框裡用顏色遮罩重新找圓，
       得到準確的球心和直徑
    2. 驗證（verify）：框裡真的有網球顏色、而且是圓的才算數，
       所以 YOLO 的信心門檻可以放很低（多抓一點），抓錯的交給顏色檢查淘汰
    3. 前後畫面（temporal）：每顆球有自己的卡爾曼濾波器（Kalman filter），根據前幾張畫面的位置和速度，
       預測這張畫面應該在哪；把這張的候選配給「離預測最近、大小最像」的那顆球，所以編號不會亂跳
    4. 補抓（recover）：某顆球 YOLO 這張沒抓到時，只在「它的預測位置附近」用顏色找；
       連顏色也找不到就先用預測值頂著，連續太多張都沒看到、或跑出畫面才刪掉

用法：
    balls = MultiBallTracker(settings)       # settings 是 ball_color.load_settings() 的結果
    people = PersonTracker()
    ball_list = balls.update(frame, ball_boxes)          # ball_boxes = [(x1, y1, x2, y2, conf), ...]
    person_list = people.update(person_boxes, W, H)

source 告訴你這顆球這次的結果從哪來：
    yolo+cv   YOLO 的框 + 顏色修正過的圓（最準）
    yolo      YOLO 的框，但框裡找不到網球顏色（信心很高、而且是已經在追的球才會接受）
    cv        YOLO 沒抓到，在預測位置附近用顏色找到的
    predict   什麼都沒找到，用卡爾曼濾波器預測的位置
"""

import math

import cv2
import numpy as np

from ball_color import make_mask

# ====== 想改的東西都在這裡 ======
# 下面的門檻是用實際錄影（camera-stream/recordings，場館燈光）調出來的，改之前先用錄影測
MIN_COLOR = 0.35         # YOLO 框裡的圓，至少要這個比例是網球顏色才算「驗證通過」（錄影裡 0.2～0.5 結果都一樣）
# 燈光一變（太亮、太暗、偏黃、偏藍），球的顏色會跑出設定的範圍，但 YOLO 還是找得到，所以：
YOLO_KEEP_CONF = 0.15    # 顏色不對的 YOLO 框，只要落在「已經在追的球」的預測位置附近，信心 ≥ 這個就用來更新那顆球
YOLO_START_CONF = 0.3    # 顏色不對的 YOLO 框，信心 ≥ 這個可以開「待確認」的新球
COLORLESS_YOLO_HITS = 5  # ……要被 YOLO 看到這麼多次才算數（只有 YOLO 說是球，證據要多一點）
COLORLESS_MEAN_CONF = 0.35  # ……而且這幾次 YOLO 的平均信心要 ≥ 這個
# （顏色驗證通過的新球：YOLO 看到一次就算數。錄影測試裡這樣抓得最多、假球也最少）
TENTATIVE_MAX_AGE = 10   # 待確認的新球，幾張畫面內沒確認就丟掉
MAX_NO_YOLO = 90         # 一顆球連續幾張都只靠顏色、沒被 YOLO 看到，就刪掉（約 3 秒；避免黃色的東西一直被當成球）
# 從 YOLO 找到的球「學」現在燈光下球的顏色，顏色範圍跟著燈光變
LEARN_CONF = 0.2         # 信心 ≥ 這個、而且是已確認的球，才拿來學顏色
LEARN_RATE = 0.1         # 每次學多少（0.1 ≈ 十張畫面左右跟上新的燈光）
HUE_LIMITS = (12, 60)    # 學到的色相最多只能在這個範圍（黃綠色系），免得學歪去追別的顏色
MIN_FILL = 0.75          # 色塊面積 ÷ 外接圓面積：圓 ≥ 0.84、正方形只有 0.64，低於這個就不算乾淨的圓
MAX_RING = 0.35          # 球黏到別的東西時：圓外一圈最多這個比例也是色塊（太多代表是一大片顏色）
MIN_RADIUS_PX = 4
MIN_SPLIT_RATIO = 0.4    # 黏在一起的色塊拆開時，第二顆以後至少要是最大那顆的這個比例，太小的當碎屑
NECK_RATIO = 0.7         # 兩顆球接觸的地方寬度 < 這個 × 小球直徑，才算「細腰」（真的是兩顆球）
GATE_RADII = 3.0         # 候選離預測位置超過「幾個球半徑」就不要（至少 GATE_MIN_PX）
GATE_MIN_PX = 40
GATE_SIGMA = 3.0         # 再加上卡爾曼自己估的「位置不確定度」× 這個：漏看越久越不確定，範圍自動變大
MEAS_NOISE = 4           # 量測雜訊：顏色修正過的圓很準
MEAS_NOISE_BOX = 100     # 只有 YOLO 框（沒顏色）很粗略，少相信一點，免得一個歪掉的框把速度帶偏
MAX_SIZE_CHANGE = 1.8    # 大小跟上一張差超過這個倍數就不要（球不會一張畫面突然變兩倍大）
# 只靠顏色找到的（YOLO 沒看到）證據比較弱，要離預測更近、大小更像，
# 不然燈光一變、球的顏色認不出來時，追蹤會被旁邊顏色剛好像的東西「搶走」
GATE_RADII_CV = 2.0
MAX_SIZE_CHANGE_CV = 1.35
MAX_MISSES = 8           # 連續幾張都沒看到就刪掉這顆球
COAST_DAMP = 0.8         # 沒看到球時，每張畫面速度乘上這個（不確定就慢慢停下來，預測才不會亂飛）
START_MIN_RADIUS = 8     # 只靠顏色開新球（沒有 YOLO 時）：要夠大、而且是乾淨的圓
MIN_HITS = 3             # 只靠顏色開的新球（沒有 YOLO 時），要連續看到幾次才算數（輸出），避免一閃而過的雜訊
RADIUS_SMOOTH = 0.6      # 半徑的平滑程度（位置交給卡爾曼濾波器）
REID_FRAMES = 60         # 球跟丟後，這麼多張畫面內（約 2 秒）在附近又出現，就還給它原本的編號（例如被手擋住一下）
REID_DIST = 250          # ……離跟丟的地方最遠幾個像素
REID_SIZE = 2.0          # ……大小最多差幾倍

PERSON_IOU = 0.3         # 人框跟上一張的重疊（IoU）超過這個才算同一個人
PERSON_MAX_MISSES = 5    # 人連續幾張沒看到就刪掉
PERSON_SMOOTH = 0.5      # 人框的平滑程度（0 = 不平滑）
# ================================


# ---------- 顏色找圓（傳統 CV） ----------

def ring_ratio(blob, x, y, r):
    """圓外面一圈（1.15r ~ 1.5r）有多少比例也是色塊：球的話外圈應該大部分是空的。"""
    ring = np.zeros_like(blob)
    cv2.circle(ring, (int(x), int(y)), int(r * 1.5), 255, -1)
    cv2.circle(ring, (int(x), int(y)), int(r * 1.15), 0, -1)
    area = cv2.countNonZero(ring)
    return cv2.countNonZero(cv2.bitwise_and(blob, ring)) / area if area else 1.0


def find_circles(mask, min_radius=MIN_RADIUS_PX):
    """在遮罩裡找所有像球的色塊，回傳 [{x, y, r, clean}, ...]。

    clean = True 表示色塊本身就很圓；False 表示是從黏在一起的色塊裡挖出來的（比較不可靠）。
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    circles = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        (x, y), r = cv2.minEnclosingCircle(c)
        if r < min_radius:
            continue
        # 圓心落在已經找到的圓裡 → 是同一顆球的另一半（白色縫線會把球切成兩塊），跳過
        if any(math.hypot(x - o["x"], y - o["y"]) < o["r"] for o in circles):
            continue
        # 用「圓裡所有網球顏色的像素」算填滿率，不是只算這一塊，被縫線切開的球才不會被當成不圓
        if color_ratio(mask, x, y, r) >= MIN_FILL:
            circles.append({"x": x, "y": y, "r": r, "clean": True})
            continue
        # 不夠圓：可能是兩顆球黏在一起、球黏到旁邊一樣顏色的東西（例如黃色長條）、被擋住一部分、或拖影
        blob = np.zeros_like(mask)
        cv2.drawContours(blob, [c], -1, 255, -1)
        circles += split_blob(blob, min_radius)
    return circles


def has_neck(blob, a, b):
    """兩個圓接起來的地方是不是「細腰」。

    兩顆球黏在一起，接觸的地方會變細；一根長條被切成好幾個圓的話，接的地方跟其他地方一樣粗。
    在接觸點量垂直方向的色塊寬度，比小的那顆球的直徑窄很多才算細腰。
    """
    (x1, y1, r1), (x2, y2, r2) = a, b
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    t = r1 / (r1 + r2)                          # 接觸點：兩個圓心之間，照半徑比例
    mx, my = x1 + dx * t, y1 + dy * t
    half = int(max(r1, r2) * 1.2)
    H, W = blob.shape
    width = 0
    for s in range(-half, half + 1):            # 沿著垂直方向數有幾個色塊像素
        px, py = int(round(mx - uy * s)), int(round(my + ux * s))
        if 0 <= px < W and 0 <= py < H and blob[py, px]:
            width += 1
    return width < NECK_RATIO * 2 * min(r1, r2)


def split_blob(blob, min_radius, max_balls=4):
    """把黏在一起的色塊拆成一顆一顆球。

    距離轉換（distance transform）最亮的點 = 色塊裡能塞下的最大圓的圓心，值就是半徑；
    細長的東西塞不下大圓，所以找到的就是球本身。挖掉這顆，再找下一個最亮的點，就能拆開黏在一起的好幾顆球
    （跟 watershed 分水嶺切割是同一個想法）。
    """
    work = blob.copy()
    found = []
    for _ in range(max_balls):
        dist = cv2.distanceTransform(work, cv2.DIST_L2, 5)
        _, r, _, (x, y) = cv2.minMaxLoc(dist)
        r += 1  # 距離轉換量到的是「到邊界前一格」
        if r < min_radius or (found and r < MIN_SPLIT_RATIO * found[0][2]):  # 太小的是碎屑，不是另一顆球
            break
        cand = (float(x), float(y), r)
        cv2.circle(work, (x, y), int(r), 0, -1)  # 挖掉這顆，找下一顆
        # 跟前面找到的球黏在一起的話，中間一定要有「細腰」；長條中間一樣粗 → 是同一個東西，不是另一顆球
        touching = [f for f in found if math.hypot(f[0] - cand[0], f[1] - cand[1]) < 1.2 * (f[2] + cand[2])]
        if all(has_neck(blob, f, cand) for f in touching):
            found.append(cand)
    circles = []
    for i, (x, y, r) in enumerate(found):
        # 外圈檢查時，把「別顆球」的地方扣掉：旁邊黏著另一顆球不代表這顆不圓
        rest = blob.copy()
        for j, (ox, oy, orr) in enumerate(found):
            if j != i:
                cv2.circle(rest, (int(ox), int(oy)), int(orr * 1.15), 0, -1)
        if ring_ratio(rest, x, y, r) <= MAX_RING:  # 外圈也滿滿的 → 一大片顏色，不是球
            circles.append({"x": x, "y": y, "r": r, "clean": False})
    return circles


def color_ratio(mask, x, y, r):
    """圓裡面有多少比例是網球顏色（0~1）。"""
    circle = np.zeros_like(mask)
    cv2.circle(circle, (int(round(x)), int(round(y))), max(int(round(r)), 1), 255, -1)
    area = cv2.countNonZero(circle)
    return cv2.countNonZero(cv2.bitwise_and(mask, circle)) / area if area else 0.0


def crop(frame, x1, y1, x2, y2):
    """裁切並夾在畫面內，回傳 (子圖, 左上角 x, 左上角 y)。"""
    H, W = frame.shape[:2]
    x1, y1 = max(int(x1), 0), max(int(y1), 0)
    x2, y2 = min(int(x2), W), min(int(y2), H)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None, x1, y1
    return frame[y1:y2, x1:x2], x1, y1


# ---------- 會跟著燈光變的顏色範圍 ----------

class AdaptiveColor:
    """網球在「現在這個燈光」下的顏色範圍。

    一開始用 settings.json 的範圍；之後從 YOLO 找到的球裡面取樣，慢慢學「現在球長什麼顏色」：
    太亮 → 球變白（飽和度 S 變低）、太暗 → 亮度 V 變低、黃光／白光 → 色相 H 偏移。
    色相只能在黃綠色系裡移動；S、V 的下限只會放寬、不會比設定的更嚴。
    """

    def __init__(self, settings):
        self.base_lower = [int(v) for v in settings["hsv_lower"]]
        self.base_upper = [int(v) for v in settings["hsv_upper"]]
        self.h = self.h_spread = self.s_low = self.v_low = None
        self.samples = 0

    def learn(self, frame, x, y, r):
        """從球心附近（半徑的 60%，避開邊緣的背景）取樣。"""
        rr = max(3, int(r * 0.6))
        sub, ox, oy = crop(frame, x - rr, y - rr, x + rr + 1, y + rr + 1)
        if sub is None:
            return
        hsv = cv2.cvtColor(cv2.GaussianBlur(sub, (5, 5), 0), cv2.COLOR_BGR2HSV)
        inside = np.zeros(sub.shape[:2], np.uint8)
        cv2.circle(inside, (int(x - ox), int(y - oy)), rr, 255, -1)
        px = hsv[inside > 0]
        px = px[px[:, 1] >= 30]  # 去掉白色縫線和反光（幾乎沒有顏色）
        if len(px) < 15:
            return
        h = float(np.median(px[:, 0]))
        if not HUE_LIMITS[0] <= h <= HUE_LIMITS[1]:  # 不是黃綠色系：可能配錯了，不學
            return
        spread = float(np.median(np.abs(px[:, 0].astype(np.float32) - h)))
        s_low, v_low = float(np.percentile(px[:, 1], 25)), float(np.percentile(px[:, 2], 25))
        if self.h is None:
            self.h, self.h_spread, self.s_low, self.v_low = h, spread, s_low, v_low
        else:
            # 色相突然差很多 = 燈光剛換（例如從黃光走到白光）→ 學快一點，不然會有好幾張畫面認不出球
            a = LEARN_RATE * 3 if abs(h - self.h) > 6 else LEARN_RATE
            self.h = (1 - a) * self.h + a * h
            self.h_spread = (1 - a) * self.h_spread + a * spread
            self.s_low = (1 - a) * self.s_low + a * s_low
            self.v_low = (1 - a) * self.v_low + a * v_low
        self.samples += 1

    @property
    def range(self):
        """回傳 (lower, upper)，給 make_mask 用。學到的樣本太少就先用設定的。"""
        if self.samples < 3:
            return self.base_lower, self.base_upper
        # 下限用「球現在典型的 S、V」的一個比例：正常燈光下球很鮮豔 → 跟設定的一樣（不會放寬）；
        # 只有球真的變白（太亮）或變暗時才放寬，米色、灰色的東西才不會混進來
        dh = min(max(2.5 * self.h_spread + 4, 8), 12)
        lower = [max(HUE_LIMITS[0], self.h - dh),
                 min(self.base_lower[1], max(40, 0.6 * self.s_low)),
                 min(self.base_lower[2], max(25, 0.5 * self.v_low))]
        upper = [min(HUE_LIMITS[1], self.h + dh), 255, 255]
        return [int(v) for v in lower], [int(v) for v in upper]


# ---------- 一顆球 ----------

class BallTrack:
    """一顆正在追的球：自己的卡爾曼濾波器（狀態 x, y, vx, vy）、半徑、編號。"""

    def __init__(self, tid, cand, confirmed, need_yolo=True):
        kf = cv2.KalmanFilter(4, 2)
        kf.transitionMatrix = np.array([[1, 0, 1, 0],
                                        [0, 1, 0, 1],
                                        [0, 0, 1, 0],
                                        [0, 0, 0, 1]], np.float32)
        kf.measurementMatrix = np.array([[1, 0, 0, 0],
                                         [0, 1, 0, 0]], np.float32)
        kf.processNoiseCov = np.diag([1, 1, 10, 10]).astype(np.float32)  # 球會突然被打、會彈，速度要允許變化
        kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * 4
        kf.errorCovPost = np.diag([10, 10, 1000, 1000]).astype(np.float32)  # 一開始不知道速度
        kf.statePost = np.array([[cand["x"]], [cand["y"]], [0], [0]], np.float32)
        self.kf, self.id, self.r = kf, tid, cand["r"]
        self.misses, self.hits, self.confirmed = 0, 1, confirmed
        self.need_yolo = need_yolo  # True：待確認時要靠 YOLO 確認；False（沒 YOLO）：顏色看到 MIN_HITS 次
        self.reused_id = False      # 編號是不是從跟丟的球拿回來的
        from_yolo = cand["source"] in ("yolo", "yolo+cv")
        self.yolo_hits, self.since_yolo, self.age = int(from_yolo), 0 if from_yolo else 1, 0
        self.conf_sum = cand["conf"] if from_yolo else 0.0
        self.colorless = cand["color"] < MIN_COLOR  # 還沒被顏色確認過（燈光怪、或根本不是球）
        self.source, self.conf = cand["source"], cand["conf"]
        self.pred = (cand["x"], cand["y"])

    @property
    def pos(self):
        return float(self.kf.statePost[0, 0]), float(self.kf.statePost[1, 0])

    def predict(self):
        p = self.kf.predict()
        self.pred = (float(p[0, 0]), float(p[1, 0]))
        self.age += 1

    def gate(self, cv_only=False):
        """候選離預測位置最遠可以多遠：球的大小 + 卡爾曼的位置不確定度（predict() 之後才準）。
        cv_only=True：只靠顏色找到的候選，範圍比較小。"""
        P = self.kf.errorCovPre
        sigma = math.sqrt(max(float(P[0, 0]), float(P[1, 1])))
        if cv_only:
            return GATE_RADII_CV * self.r + GATE_SIGMA * sigma
        return max(GATE_RADII * self.r, GATE_MIN_PX) + GATE_SIGMA * sigma

    def cost(self, cand):
        """候選跟這顆球有多不像：None = 不可能是它；數字越小越像（距離 + 大小差）。"""
        cv_only = cand["source"] == "cv"
        gate = self.gate(cv_only)
        max_change = MAX_SIZE_CHANGE_CV if cv_only else MAX_SIZE_CHANGE
        dist = math.hypot(cand["x"] - self.pred[0], cand["y"] - self.pred[1])
        ratio = cand["r"] / self.r
        if dist > gate or not 1 / max_change <= ratio <= max_change:
            return None
        return dist / self.gate() + abs(math.log(ratio)) - 0.3 * cand["color"]  # 顏色越像扣越多

    def correct(self, cand):
        noise = MEAS_NOISE_BOX if cand["source"] == "yolo" else MEAS_NOISE
        self.kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * noise
        self.kf.correct(np.array([[cand["x"]], [cand["y"]]], np.float32))
        # 只有 YOLO 框（沒顏色）的大小很粗略（框通常比球大），幾乎不拿來改半徑；
        # 半徑主要靠顏色找到的圓，不然球會越變越大、把旁邊的東西也當成自己
        keep = 0.95 if cand["source"] == "yolo" else RADIUS_SMOOTH
        self.r = keep * self.r + (1 - keep) * cand["r"]
        self.misses, self.hits = 0, self.hits + 1
        self.source, self.conf = cand["source"], cand["conf"]
        from_yolo = cand["source"] in ("yolo", "yolo+cv")
        if from_yolo:
            self.yolo_hits, self.since_yolo = self.yolo_hits + 1, 0
            self.conf_sum += cand["conf"]
        else:
            self.since_yolo += 1
        if cand["color"] >= MIN_COLOR:
            self.colorless = False
        if self.confirmed:
            return
        if not self.need_yolo:
            self.confirmed = self.hits >= MIN_HITS
        elif self.colorless:
            self.confirmed = (self.yolo_hits >= COLORLESS_YOLO_HITS
                              and self.conf_sum / self.yolo_hits >= COLORLESS_MEAN_CONF)
        else:
            self.confirmed = self.yolo_hits >= 1  # 後來被 YOLO + 顏色確認了

    def coast(self):
        """沒找到：卡爾曼不會自己更新，把預測值當成這張的狀態，速度慢慢降下來。"""
        self.kf.statePost = self.kf.statePre.copy()
        self.kf.statePost[2:] *= COAST_DAMP
        self.kf.errorCovPost = self.kf.errorCovPre.copy()
        self.misses += 1
        self.since_yolo += 1
        self.source, self.conf = "predict", 0.0

    def to_dict(self, W, H):
        x, y = self.pos
        r = self.r
        return {
            "id": self.id, "x": x, "y": y, "r": r, "d": 2 * r, "size": 2 * r / W,
            "ex": (x - W / 2) / (W / 2), "ey": (y - H / 2) / (H / 2),
            "box": [x - r, y - r, x + r, y + r],
            "vx": float(self.kf.statePost[2, 0]), "vy": float(self.kf.statePost[3, 0]),
            "source": self.source, "conf": self.conf, "misses": self.misses,
        }


def greedy_match(tracks, cands, cost):
    """把候選配給追蹤中的東西：先配最像的一對，配過的兩邊都不能再用。
    cost(track, cand) 回傳 None = 不可能配、數字越小越像。回傳 {track 的索引: cand 的索引}。"""
    pairs = []
    for ti, t in enumerate(tracks):
        for ci, c in enumerate(cands):
            k = cost(t, c)
            if k is not None:
                pairs.append((k, ti, ci))
    matched, used = {}, set()
    for _, ti, ci in sorted(pairs):
        if ti not in matched and ci not in used:
            matched[ti] = ci
            used.add(ci)
    return matched


# ---------- 很多顆球 ----------

class MultiBallTracker:
    def __init__(self, settings, use_yolo=True):
        self.color = AdaptiveColor(settings)  # 顏色範圍會跟著燈光學
        self.use_yolo = use_yolo
        self.tracks = []
        self.next_id = 1
        self.candidates = []  # 這張畫面看過的 YOLO 候選（給畫面除錯用）
        self.lost = []        # 最近跟丟的球（編號、最後位置、大小、第幾張畫面），用來還編號
        self.frame_no = 0

    def _new_id(self, c):
        """新球：附近最近剛跟丟過一顆差不多大的球 → 還它原本的編號；不然給新編號。"""
        self.lost = [lt for lt in self.lost if self.frame_no - lt["frame"] <= REID_FRAMES]
        close = [lt for lt in self.lost
                 if math.hypot(c["x"] - lt["x"], c["y"] - lt["y"]) <= REID_DIST
                 and 1 / REID_SIZE <= c["r"] / lt["r"] <= REID_SIZE]
        if close:
            lt = min(close, key=lambda lt: math.hypot(c["x"] - lt["x"], c["y"] - lt["y"]))
            self.lost.remove(lt)
            return lt["id"]
        self.next_id += 1
        return self.next_id - 1

    # ---------- 候選：YOLO 的框 → 用顏色修正、驗證 ----------

    def _from_yolo(self, frame, det):
        x1, y1, x2, y2, conf = det
        w, h = x2 - x1, y2 - y1
        pad = 0.15 * max(w, h)  # 框有時候切到球的邊，多看一點
        sub, ox, oy = crop(frame, x1 - pad, y1 - pad, x2 + pad, y2 + pad)
        box_r = (w + h) / 4
        cand = {"x": (x1 + x2) / 2, "y": (y1 + y2) / 2, "r": box_r, "conf": conf,
                "color": 0.0, "source": "yolo", "box": (x1, y1, x2, y2)}
        if sub is None:
            return cand
        mask = make_mask(sub, *self.color.range)
        bx, by = cand["x"] - ox, cand["y"] - oy
        # 框裡可能有好幾個色塊：挑圓心在框內、離框中心近、大小跟框差不多的那個
        circles = [c for c in find_circles(mask, min_radius=max(MIN_RADIUS_PX, 0.3 * box_r))
                   if abs(c["x"] - bx) <= w / 2 and abs(c["y"] - by) <= h / 2]
        if not circles:
            return cand
        best = min(circles, key=lambda c: math.hypot(c["x"] - bx, c["y"] - by) / box_r
                   + abs(math.log(c["r"] / box_r)))
        cx, cy, r = best["x"], best["y"], best["r"]
        cand.update(x=cx + ox, y=cy + oy, r=r, color=color_ratio(mask, cx, cy, r), source="yolo+cv")
        return cand

    # ---------- 候選：只用顏色 ----------

    def _search_color(self, frame, center=None, half=None):
        """回傳所有顏色找到的圓。center=None 表示整張畫面找；否則只找預測位置附近。"""
        if center is None:
            sub, ox, oy = frame, 0, 0
        else:
            sub, ox, oy = crop(frame, center[0] - half, center[1] - half,
                               center[0] + half, center[1] + half)
            if sub is None:
                return []
        mask = make_mask(sub, *self.color.range)
        return [{"x": c["x"] + ox, "y": c["y"] + oy, "r": c["r"], "clean": c["clean"], "conf": 0.0,
                 "color": color_ratio(mask, c["x"], c["y"], c["r"]), "source": "cv"}
                for c in find_circles(mask)]

    def _near_any(self, cand, others):
        """候選是不是跟某顆已經有的球重疊（同一顆球不要開兩個編號）。"""
        return any(math.hypot(cand["x"] - o["x"], cand["y"] - o["y"]) < max(cand["r"], o["r"]) for o in others)

    # ---------- 每張畫面呼叫一次 ----------

    def update(self, frame, detections):
        H, W = frame.shape[:2]
        self.frame_no += 1
        for t in self.tracks:
            t.predict()

        # 1. YOLO 的框 → 修正、驗證。顏色不對的框（燈光變了）也先留著：
        #    它只能配給「預測位置就在那裡」的球，或開待確認的新球，不會憑空變成球
        cands = [self._from_yolo(frame, d) for d in detections]
        for c in cands:
            c["ok"] = c["color"] >= MIN_COLOR or c["conf"] >= YOLO_KEEP_CONF
        self.candidates = cands
        pool = [c for c in cands if c["ok"]]

        # 2. 把 YOLO 候選配給已經在追的球（離預測近、大小像、顏色對的優先）
        pairs = greedy_match(self.tracks, pool, lambda t, c: t.cost(c))
        matched = {self.tracks[ti]: pool[ci] for ti, ci in pairs.items()}
        taken = list(matched.values())

        # 3. YOLO 沒配到（或只配到顏色不對的框）的球 → 在它們的預測位置附近用顏色找，
        #    找到的圓跟那些「顏色不對的框」放在一起，所有這些球「一起」重新配對（不是一顆一顆輪流挑，
        #    不然先挑的球可能搶走別顆球的位置，編號就對調了）
        need = [t for t in self.tracks if not (t in matched and matched[t]["source"] == "yolo+cv")]
        if need:
            good = [c for t, c in matched.items() if t not in need]  # 已經用顏色確認的，別人不能搶
            options = [matched[t] for t in need if t in matched]
            for t in need:
                for c in self._search_color(frame, t.pred, t.gate()):
                    if not self._near_any(c, good) and not any(
                            math.hypot(c["x"] - o["x"], c["y"] - o["y"]) < 3 for o in options):
                        options.append(c)
            for t in need:
                matched.pop(t, None)
            pairs = greedy_match(need, options, lambda t, c: t.cost(c))
            for ti, ci in pairs.items():
                matched[need[ti]] = options[ci]
            taken = list(matched.values())

        # 4. 更新：有配到的用量測修正，沒配到的用預測頂著
        for t in self.tracks:
            if t in matched:
                c = matched[t]
                t.correct(c)
                # 學「現在燈光下」球的顏色：只從 YOLO 看到的、已確認的球學（YOLO 為主，CV 跟著學）
                if t.confirmed and c["source"] in ("yolo", "yolo+cv") and c["conf"] >= LEARN_CONF:
                    self.color.learn(frame, c["x"], c["y"], c["r"])
            else:
                t.coast()

        # 5. 刪掉：太久沒看到、跑出畫面、待確認太久；有 YOLO 時，太久沒被 YOLO 看到（只靠顏色撐著）的也刪
        def alive(t):
            x, y = t.pos
            if not (-t.r <= x <= W + t.r and -t.r <= y <= H + t.r):
                return False
            if not t.confirmed:
                return t.misses <= 1 and t.age <= TENTATIVE_MAX_AGE
            if t.misses > MAX_MISSES:
                return False
            return not (self.use_yolo and t.since_yolo > MAX_NO_YOLO)
        keep = []
        for t in self.tracks:
            if alive(t):
                keep.append(t)
            elif t.confirmed or t.reused_id:  # 記住跟丟的球：等一下在附近又出現就還它編號
                x, y = t.pos
                self.lost.append({"id": t.id, "x": x, "y": y, "r": t.r, "frame": self.frame_no})
        self.tracks = keep

        # 6. 開新球：沒配到任何球的候選
        existing = [{"x": t.pos[0], "y": t.pos[1], "r": t.r} for t in self.tracks] + taken
        new = []  # (候選, 馬上算數嗎)
        if self.use_yolo:
            for c in pool:
                colored = c["source"] == "yolo+cv" and c["color"] >= MIN_COLOR
                if colored:
                    new.append((c, True))           # YOLO 找到 + 顏色對：馬上算數
                elif c["conf"] >= YOLO_START_CONF:
                    new.append((c, False))          # 顏色不對（例如燈光怪）：要被 YOLO 看到好幾次才算數
        else:
            # 沒有 YOLO：整張畫面用顏色找，要連續看到 MIN_HITS 次才算數
            new = [(c, False) for c in self._search_color(frame) if c["clean"] and c["r"] >= START_MIN_RADIUS]
        for c, confirmed in sorted(new, key=lambda n: -n[0]["r"]):
            if any(c is m for m in taken) or self._near_any(c, existing):
                continue
            before = self.next_id
            track = BallTrack(self._new_id(c), c, confirmed, need_yolo=self.use_yolo)
            track.reused_id = self.next_id == before  # 拿的是舊編號（跟丟過的球又回來）
            self.tracks.append(track)
            existing.append(c)

        balls = [t.to_dict(W, H) for t in self.tracks if t.confirmed]
        return sorted(balls, key=lambda b: -b["d"])  # 大（近）的排前面


# ---------- 人 ----------

def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class PersonTracker:
    """人：用框的重疊（IoU）判斷是不是同一個人，給固定編號；框做一點平滑，數字才不會一直跳。"""

    def __init__(self):
        self.tracks = []  # 每個是 dict：id, box, conf, misses
        self.next_id = 1

    def update(self, detections, W, H):
        dets = [{"box": list(d[:4]), "conf": d[4]} for d in detections]

        def cost(t, d):
            overlap = iou(t["box"], d["box"])
            return 1 - overlap if overlap >= PERSON_IOU else None

        pairs = greedy_match(self.tracks, dets, cost)
        for ti, t in enumerate(self.tracks):
            if ti in pairs:
                d = dets[pairs[ti]]
                t["box"] = [PERSON_SMOOTH * a + (1 - PERSON_SMOOTH) * b for a, b in zip(t["box"], d["box"])]
                t["conf"], t["misses"] = d["conf"], 0
            else:
                t["misses"] += 1
        self.tracks = [t for t in self.tracks if t["misses"] <= PERSON_MAX_MISSES]
        used = set(pairs.values())
        for ci, d in enumerate(dets):
            if ci not in used:
                self.tracks.append({"id": self.next_id, "box": d["box"], "conf": d["conf"], "misses": 0})
                self.next_id += 1

        people = []
        for t in self.tracks:
            x1, y1, x2, y2 = t["box"]
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            people.append({"id": t["id"], "box": [x1, y1, x2, y2], "x": cx, "y": cy,
                           "ex": (cx - W / 2) / (W / 2), "ey": (cy - H / 2) / (H / 2),
                           "h": (y2 - y1) / H, "conf": t["conf"], "misses": t["misses"]})
        return sorted(people, key=lambda p: -(p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1]))
