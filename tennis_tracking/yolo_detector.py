"""
YOLO 找球和人：同一次推論同時找 COCO 資料集裡的「sports ball」和「person」，不會變慢。
第一次用會自動下載模型到 tennis_tracking/models/。

預設用 GPU：這台是 AMD 內顯（Radeon 860M），沒有 NVIDIA 的 CUDA，
所以把模型轉成 ncnn 格式、透過 Vulkan 在 GPU 上跑（第一次會自動轉，幾秒鐘）。
找不到 GPU 就自動改用 CPU。

需要先裝（用 CPU 版的 torch，比較小；ncnn 負責 GPU）：
    uv pip install --python .venv/bin/python torch torchvision --index-url https://download.pytorch.org/whl/cpu
    uv pip install --python .venv/bin/python ultralytics ncnn pnnx
"""

import os
import shutil
from pathlib import Path

YOLO_MODEL = "yolo11n.pt"   # n 最小最快；想更準可以換 yolo11s.pt（GPU 上約 30 fps）
YOLO_CONF = 0.05            # 故意放低：錄影裡信心 0.01～0.05 的框有 90% 真的是球；抓錯的交給 tracking_helper 用顏色淘汰
YOLO_IMGSZ = 640
SPORTS_BALL = 32            # COCO 資料集裡「sports ball」的類別編號
PERSON = 0                  # COCO 資料集裡「person」的類別編號
PERSON_CONF = 0.4           # 人沒有顏色可以驗證，門檻不能像球那麼低，不然會亂框

MODEL_DIR = Path(__file__).resolve().parent / "models"


def find_vulkan_gpu():
    """回傳第一張真的 GPU 的編號；沒有就回傳 None（llvmpipe 是用 CPU 模擬的，不算）。"""
    try:
        import ncnn
    except ImportError:
        return None, "沒裝 ncnn"
    for i in range(ncnn.get_gpu_count()):
        name = ncnn.get_gpu_info(i).device_name()
        if "llvmpipe" not in name:
            return i, name
    return None, "找不到 Vulkan GPU"


class YoloBallDetector:
    def __init__(self, model_name=YOLO_MODEL, conf=YOLO_CONF, device="gpu"):
        if device == "gpu":
            # GPU 在算的時候，CPU 的執行緒預設會「原地空轉」等結果，白白吃掉 7 顆核心；
            # 改成睡著等：速度一樣，CPU 用量從約 700% 降到約 50%（要在載入 torch / ncnn 之前設定）
            os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
            os.environ.setdefault("GOMP_SPINCOUNT", "0")
        try:
            from ultralytics import YOLO
        except ImportError:
            raise SystemExit("❌ 還沒裝 ultralytics，指令在 yolo_detector.py 最上面；或加 --no-yolo 只用顏色")
        pt = MODEL_DIR / model_name
        if not pt.exists():
            print(f"⬇️  下載 {model_name} ...")
            YOLO(model_name)  # ultralytics 會下載到目前資料夾，再搬進 models/
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(model_name, pt)

        path, self.device = pt, "cpu"
        if device == "gpu":
            gpu, name = find_vulkan_gpu()
            if gpu is None:
                print(f"⚠️  {name}，YOLO 改用 CPU")
            else:
                path = MODEL_DIR / f"{pt.stem}_ncnn_model"
                if not path.exists():
                    print(f"🔧 第一次用 GPU：把 {model_name} 轉成 ncnn 格式 ...")
                    YOLO(str(pt)).export(format="ncnn", imgsz=YOLO_IMGSZ)  # 存在 .pt 旁邊
                self.device = f"vulkan:{gpu}"
                import torch
                torch.set_num_threads(2)  # 模型在 GPU 上跑，CPU 只做前後處理，2 條執行緒就夠
                print(f"🚀 YOLO 用 GPU：{name}")
        if self.device == "cpu":
            print("🐢 YOLO 用 CPU")
        self.model = YOLO(str(path), task="detect")
        self.conf = conf

    def __call__(self, frame):
        """回傳 (balls, people)，各是 [(x1, y1, x2, y2, conf), ...]。"""
        result = self.model.predict(frame, conf=self.conf, classes=[SPORTS_BALL, PERSON], imgsz=YOLO_IMGSZ,
                                    device=self.device, verbose=False)[0]
        boxes = result.boxes
        balls, people = [], []
        for xyxy, conf, cls in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist()):
            box = (*map(float, xyxy), float(conf))
            if int(cls) == SPORTS_BALL:
                balls.append(box)
            elif conf >= PERSON_CONF:
                people.append(box)
        return balls, people
