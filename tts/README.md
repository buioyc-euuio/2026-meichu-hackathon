# tts：文字轉語音（加速 → 聽起來很急）

用 edge-tts（微軟的台灣中文神經語音）念，再用 ffmpeg `atempo` 加速（預設 **3 倍**，音調不變，不會變成花栗鼠聲），
音調調高、音量放大讓它像在喊。

```bash
uv pip install --python .venv/bin/python edge-tts        # 安裝（只要一次）

.venv/bin/python tts/tts.py                                # 「救命呀！」3 倍速 → tts/output/救命呀.wav
.venv/bin/python tts/tts.py --play                         # 產生完直接播
.venv/bin/python tts/tts.py --repeat 3 --out tts/output/救命呀x3.wav   # 連喊三次
.venv/bin/python tts/tts.py "有人跌倒了" --speed 2          # 別的句子、別的速度
.venv/bin/python tts/tts.py --voice zh-TW-YunJheNeural     # 台灣男聲（.venv/bin/edge-tts --list-voices 看全部）
```

**產生的時候要網路**（edge-tts 是線上服務）；產生好的 wav 放在 `tts/output/`，demo 現場直接播檔案就不用網路。

| 檔案 | 內容 | 長度 |
|---|---|---|
| `output/救命呀.wav` | 救命呀！× 1，3 倍速 | 0.21 秒 |
| `output/救命呀x3.wav` | 救命呀！× 3，3 倍速 | 0.64 秒 |

⚠️ 原本念一次約 0.6 秒，3 倍速只剩 0.2 秒（一個字約 70 ms），可能太快聽不清楚；
聽起來不對就用 `--repeat 3` 或 `--speed 2`。聲音、音調、音量在 `tts.py` 最上面（`VOICE`、`PITCH`、`VOLUME`、`GAIN_DB`）。
