# robot_control：機器人總控制（兩個模式，用語音切換）

| 模式 | 做什麼 | 狀態 |
|---|---|---|
| `voice` 語音模式 | 說「小狗前進兩秒」「小狗左轉」「停止」開車；鍵盤 w/a/s/d 也可以 | 已接本機 NPU Whisper；可用錄音與 `--fake` 驗證，麥克風需依現場裝置測試 |
| `ball` 追球模式 | 相機自動對準網球（`camera_control`） | 🚧 stub：之後從 `camera_control` import，再加防撞 |

麥克風切換模式：說「小狗追球模式」／「小狗語音模式」；鍵盤 `1`、`2` 或打字不需喚醒詞。

```
麥克風 ─PCM/WebSocket─▶ 本機 NPU Whisper ─▶ Transcript ─intent.parse─┐
鍵盤 ────────────────────────────────────┼─▶ queue ─▶ Controller ─▶ 目前的模式 ─▶ RobotBLE ─▶ micro:bit
                                           │                              │
                                 SwitchMode┘                   開車都走 WheelDriver ─▶ 防撞（safety.py）
```

- 同一時間只有一個模式能動馬達：切換時舊模式 `exit()`（停馬達、相機回正）→ 新模式 `enter()`
- 兩個模式都一直在聽：追球模式裡只有「切換模式」「停」「結束程式」有作用，開車指令不理
- 追球模式裡說「停」＝ 馬上停下並回到語音模式
- 藍牙只連一次，共用 `camera_control/robot_ble.py`（斷線自動重連）

## 怎麼跑（在專案最外層）

```bash
.venv/bin/python robot_control/main.py --fake --no-audio   # 先測鍵盤，不連 micro:bit、不開麥克風
.venv/bin/python robot_control/main.py --list-mics         # 列麥克風，不連 micro:bit
.venv/bin/python robot_control/main.py --fake --audio-device 4  # 先確認辨識正確，再考慮連實機
.venv/bin/python robot_control/main.py                     # 連 micro:bit
.venv/bin/python robot_control/main.py --mode ball         # 一開始就是追球模式
printf '前進兩秒\nwait 1\n追球模式\n停\n' | .venv/bin/python robot_control/main.py --fake --no-audio
```

| 參數 | 意思 |
|---|---|
| `--mode voice/ball` | 一開始的模式（預設 voice） |
| `--fake` | 不連藍牙 |
| `--verbose` | 印每一個藍牙指令（包含每 0.1 秒的 keep-alive）；預設只印有變的 |
| `--no-audio` | 不開麥克風 |
| `--no-guard` | 關掉防撞 |
| `--device-name` | micro:bit 藍牙名稱（預設跟 `camera_control` 一樣是 `vapup`） |
| `--list-mics` | 只列輸入裝置；接上新麥克風後重查編號 |
| `--audio-device` | 麥克風編號或名稱；未指定時使用系統預設輸入 |
| `--audio-channels` / `--audio-rate` | 預設單聲道、裝置原生取樣率；可選雙聲道，服務端混成 mono 並重取樣為 16 kHz |
| `--asr-url` | 預設 `ws://127.0.0.1:18082` |
| `--audio-wake-word` | 預設「小狗」；移動／切換模式須以此開頭，單獨「停止」不需要 |
| `--audio-max-age` | 語音有效期，預設 10 秒；鍵盤操作前錄到的遲到語音也會被丟棄 |
| `--audio-file` | 以 1x 回放 PCM16 WAV，**只允許 `--fake`**；播完與辨識完成後自動退出 |
| `--llm` | 用本機 iGPU LLM 修正 ASR 與分類方向；需明確選 `--fake` 或 `--allow-motion` |
| `--llm-verbose-reason` | 除錯／比較時產生完整理由；預設使用短標籤以減少生成時間 |

## 本機 NPU Whisper

ASR 與機器人分開執行，避免把 Jerry 的相機環境和 n0ball 的 IRON 環境混在一起：

```text
Jerry: sounddevice PCM16（16/44.1/48 kHz、mono/stereo）
  -> 127.0.0.1:18082 WebSocket
n0ball: 重取樣 -> Silero VAD -> Breeze NPU GEMM + CPU SDPA
  -> whisper-server CPU decoder -> 信心過濾 -> 文字
Jerry: 喚醒詞 -> Transcript queue -> intent -> 目前模式
```

## 藍牙 ASR -> iGPU LLM -> 動作控制

新路徑是選用功能；不加 `--llm` 時仍保留原來的關鍵字解析與鍵盤控制。

```text
藍牙耳機（PipeWire，指定來源）
  -> 本機 Silero VAD + Breeze NPU encoder / CPU decoder
  -> 喚醒詞、語音時間戳
  -> Qwen3-4B / llama.cpp Vulkan（iGPU）
  -> 驗證白名單與秒數
  -> 原本的 WheelDriver / RobotBLE / micro:bit
```

LLM 先修正合理的 ASR 同音錯字，再判斷意圖。例如「又轉一秒」可以修正成「右轉一秒」；
不能要求原始逐字稿一定包含「右」，否則就失去 LLM 的用途。
真正缺少方向的「轉一下」仍不動，明確的「左轉」也不能任意改成右轉。
終端機會同時顯示原始逐字稿、LLM 判斷理由和實際排程的動作。
預設理由使用 `direct/repair/unclear/unsupported` 短標籤，轉成中文顯示；
不是移除 LLM，也不是退回方向關鍵字比對。模型若同時提出移動與不確定／不支援標籤，
會報錯並停止，不執行矛盾的輸出。

### 延遲與量測

終端機會印出每句的時間拆分：

- `出字`：從 VAD 估計的語音末端，到收到 ASR 結果；不含使用者整句說話的時間。
- `斷句/傳輸約`：出字時間扣除 ASR 排隊與處理時間的餘額，是估計值，不是獨立 VAD profiler。
- `排隊`：切句完成後等待 ASR worker 的時間。
- `encoder`：NPU GEMM 加上 CPU 特徵、attention 與資料整理，不是純 NPU kernel 時間。
- `decoder`：encoder 輸出寫入、HTTP 及 CPU 解碼的合計，不是純 CPU kernel 時間。
- `LLM 排隊/判斷`、`控制器等待`、`語音末端→判斷`：分別顯示送出 LLM 後的等待、
  結果回到控制器的等待，以及整體決策延遲。語音停止仍繞過 LLM。

ASR 回應增加 `speech_end_s`（不含 post-roll）、`queue_s`、`encoder_s`、`decoder_s`；
保留原有 `end_s` 與 `processing_s`。新客戶端仍可接舊服務，但沒有完整拆分就不顯示拆分值。
過期檢查改採語音末端而非加上 post-roll 的末端，不延長指令有效期。
VAD 的 1.5 秒 pre-roll 是取回已收到的聲音，**不是多等 1.5 秒**。

2026-09-19 PN54，同一份合成中文錄音、真實 NPU ASR／iGPU LLM、全程 `--fake`：

| 比較 | 完整理由 | 短標籤 |
|---|---:|---:|
| 暖機後 LLM 平均判斷（19 次實際 HTTP 模型呼叫） | 1.54 秒 | 0.55 秒 |
| 四個移動指令的語音末端到判斷平均（含首句 prompt cache 載入） | 6.48 秒 | 5.58 秒 |

短標籤通過 22 個方向／同音修正／拒絕案例，其中 3 個由程式直接拒絕；
完整理由模式在新增的「請又轉半秒」案例仍拒絕移動，保留為除錯選項，不能假定兩種
prompt 的語意完全相同。修改 schema、理由長度或 few-shot 範例後，必須重跑真實模型
與原始 ASR 錯字錄音；只有 mock 通過不足以證明同音修正沒有退步。

端到端出字仍約 4.8 秒，是目前主要瓶頸；首句或排隊時可能更慢。這不是即時控制保證，
也不是新一次現場藍牙／實體行走驗收。現成 FastFlowLM Whisper Turbo 的同音訊
純 ASR 比較約 2.19 秒（Breeze 約 3.88 秒），但將「停止」辨成「緊張」、右轉句辨成
「小狗又转疫苗」，且 `verbose_json` 沒有信心欄位，因此**沒有替換正式 Breeze**。

### 啟動服務

ASR 沿用下節的 `127.0.0.1:18082`。iGPU LLM 使用現成的 4B 權重，不下載新模型，
不干涉其他 Lemonade/Gemma 服務。以 n0ball 執行：

```bash
bash ~/work/robot_llm/run_server.sh
curl -fsS http://127.0.0.1:18083/health
```

啟動器來源為 `llm/run_server.sh`，模型 alias 是 `robot-igpu`。
請先查 health，已在跑時不要再開一份。所有語音／LLM 流量只留在本機，沒有雲端後備。

### 先驗證，不動馬達

在專案根目錄、jerry 的終端機執行：

```bash
.venv/bin/python robot_control/main.py --list-pipewire-mics
.venv/bin/python robot_control/main.py --fake --bluetooth-mic --llm
```

`--bluetooth-mic` 只接受唯一的藍牙錄音來源；多支耳機時，改用列出的 node.name：

```bash
.venv/bin/python robot_control/main.py --fake --llm \
  --pipewire-source bluez_input.XX_XX_XX_XX_XX_XX.0
```

藍牙必須有通話錄音模式（目前驗證過 mSBC）；不會改動其他音源或自行切換耳機 profile。
程式會驗證錄音程序實際連到指定來源，來源消失／改變就停止，不悄悄改錄 Webcam。
這條 PipeWire 路徑可與瀏覽器／桌面音量表共用輸入，不再直接搶用 ALSA 硬體。

一次說一句，例如「小狗，前進一秒」「小狗，後退一秒」「小狗，向左轉一秒」
「小狗，向右轉一秒」。看到辨識與判斷結果後再說下一句；「停止」不需喚醒詞。

### 動作與停止規則

- LLM 只輸出 `forward/backward/left/right/stop/none` 和理由，不直接取得 BLE 或任意工具。
- 一次一個動作；未指定時間是 1 秒，明確秒數只接受 **0.1 到 2 秒**，不偷偷截短。
- 馬達速度仍由既有 WheelDriver 設定，不讓模型指定 PWM；距離、角度、多步路徑與調速要求不執行。
- LLM 以非阻塞工作處理。**鍵盤停止／模式切換不等 LLM**，較晚回來的舊結果會被丟棄。
- 文字已辨識成「停止」後直接走停止路徑；但 ASR 本身仍需數秒，語音不能取代實體急停。
- JSON 無效、動作超限、LLM timeout／斷線會明確報錯並停止，不退回關鍵字亂猜動作。
- 仍保留 ASR 積壓與過期語音保護；這不是可無限連講的對話系統。
- VAD 前保留 1.5 秒真實音訊、後保留 0.2 秒，避免剪掉柔聲的喚醒詞／字尾；
  不重播上一句，也不憑空補上「小狗」文字。

### 實機授權

`--llm` 必須明確選 `--fake` 或 `--allow-motion`，否則拒絕啟動。
確認方向、秒數、停止方式都正確，且沒有其他程式同時控制車子後，才可由操作者執行：

```bash
.venv/bin/python robot_control/main.py --bluetooth-mic --llm --allow-motion
```

**這會真的連 micro:bit 並可能移動。防撞仍是 stub，只能在有人監督的安全空間測試。**
本次自動驗證只用 fake，沒有替操作者完成實體行走／碰撞驗收。

### 回歸測試

```bash
.venv/bin/python -m unittest discover -s robot_control/tests -p 'test_*.py' -v
.venv/bin/python robot_control/main.py --fake --llm --audio-file /path/to/commands.wav
```

錄音測試僅可用 `--fake`；EOF 會等最後的 LLM 意圖與限時假動作結束再退出。
ASR 服務端測試需在 IRON 環境另跑，主環境缺少 NPU/ASR 套件時會明確標示跳過。
與舊 `LLM_control_motor_lemonade/agent.py` 相同，使用本機 OpenAI 相容 API；
但不沿用其同步多工具執行迴圈，以免模型等待阻塞停止指令。

### 1. ASR 服務（以 n0ball 執行）

這台 PN54 的部署副本在 `~/work/robot_asr/`；來源是本專案的 `asr/server.py`。
不修改既有 Breeze encoder，也不使用舊 `live_run.sh` 的全域 `pkill`。

```bash
source ~/work/prof/env.sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
python ~/work/robot_asr/server.py
# 另一個終端：
curl -fsS http://127.0.0.1:18082/health
```

看到 `status: ready` 才開輸入端。服務只綁 loopback，自己管理一個
`127.0.0.1:18081` CPU decoder；連接埠被占用會報錯，不終止其他服務。
`Ctrl+C`／SIGTERM／SIGHUP 會清理自己啟動的 decoder，不留孤立程序。
正常停止麥克風會取消尚未完成的語音，不在停止後補送舊指令，也不誤報服務故障。
服務 log 在 `~/.local/state/pn54-robot-asr/asr.log`（輪替保存），decoder log 在
`~/.local/state/pn54-robot-asr/whisper-server.log`。SSH 斷線不代表服務必定停止；
先查 `/health`，不要直接再開一份搶相同連接埠。

IRON 環境額外依賴見 `asr/requirements.txt`（websockets、soxr）。
機器人環境依賴見 `requirements-audio.txt`；Linux 另外需要 `libportaudio2`。

### 2. 接麥克風（以 jerry 在專案根目錄執行）

```bash
.venv/bin/python robot_control/main.py --list-mics
.venv/bin/python robot_control/main.py --fake --audio-device 4
```

`4` 只是範例，**換 USB 裝置後請重查**。可改用名稱，例如
`--audio-device "Webcam C170"`。先確認終端機會印出辨識文字及正確的指令，
不要在尚未確認裝置／方向／停止機制前拿掉 `--fake`。

- 3.5 mm 插孔用 ALC256 Analog；ALSA 的 Capture Source 要選 `Headset Mic`，不是 `Internal Mic`。
- GNOME 聲音設定的輸入音量表會占用 ALSA 擷取裝置，使 `--list-mics` 暫時列不出它。
  關閉該設定頁後再列裝置／啟動控制器；不要因此改選別的麥克風或沿用舊編號。
  PipeWire 可以共用音訊，但目前主控的 sounddevice 路徑仍是 ALSA。
- 實體擷取讓 PortAudio 選原生 block size，使用 `latency="high"`。這台 ALC256 的
  預設低延遲約 5.8 ms，搭配固定 32 ms block 實測會 input overflow；高延遲約
  34.8 ms、原生 block 的同條件測試沒有 overflow。不要用忽略 overflow 來掩蓋丟音訊。
- 預設說「小狗前進兩秒」「小狗追球模式」「停止」。
- 普通聊天只顯示文字，不執行指令；否定的移動語句不執行。
- 短句仍需約數秒辨識，**語音停止不是緊急停止**；保留鍵盤空白鍵／實體停止方式。
- 鍵盤停止或切換後，先前錄到、現在才辨識好的語音不會恢復舊動作。
- 缺麥克風／ASR 服務時，會在藍牙連線前失敗。執行中資料遺失、堆積、
  麥克風停止供應資料、服務斷線等會報錯並退出控制器，不靜默改用 CPU ASR。
- VAD 的最大語音長度設定為 8 秒，但不是實際片段長度或出字延遲的硬上限；
  解碼使用 no-speech probability／平均 log probability 過濾。
  每次說完等辨識完成再下下一個命令，避免排隊。
- 一次只接受一個音訊用戶端；ASR 不送到雲端、不保存原始麥克風錄音。
- `ball` 與防撞仍是 stub；接上語音不代表它們已完成。

### 不接麥克風的測試

```bash
.venv/bin/python robot_control/main.py --fake --audio-file /path/to/pcm16.wav
# 一般錄音沒有「小狗」時，只在 fake 測試中可關掉喚醒詞觀察 queue：
.venv/bin/python robot_control/main.py --fake --audio-file /path/to/pcm16.wav --audio-wake-word=
.venv/bin/python -m unittest discover -s robot_control/tests -p test_audio.py -v
# 信心過濾測試使用已裝 ASR 依賴的 IRON interpreter：
# ~/work/ironenv/bin/python -m unittest discover -s robot_control/tests -p test_asr_protocol.py -v
```

鍵盤（直接按，不用 Enter）：`w/a/s/d` 或方向鍵開車（按著才動，放開約 0.6 秒後停）、空白鍵停、
`1` 語音模式、`2` 追球模式、`v` 打一句話當作語音、`h` 說明、`q` 離開。
stdin 不是終端機時，每一行都當成一句語音（`wait 1.5` = 等 1.5 秒）。

## 語音指令（`intent.py`，關鍵字比對，繁簡中文、英文都可以）

| 說 | 結果 |
|---|---|
| 追球模式、追蹤、ball mode、track | 切到追球模式 |
| 語音模式、手動、遙控、voice mode、manual | 切到語音模式 |
| 前進／後退／左轉／右轉（＋「兩秒」「1.5 秒」「three seconds」） | 開車，沒講秒數就 1 秒，最多 5 秒 |
| 停、stop | 全部停止（追球模式裡還會切回語音模式） |
| 結束程式、shut down | 離開 |

之後要換成 LLM（`LLM_control_motor` 那套 tool calling），只要換掉 `intent.parse()`。

## 檔案

| 檔案 | 用途 |
|---|---|
| `main.py` | 進入點：連藍牙、建模式、開輸入、跑 controller |
| `controller.py` | 指令 queue、切換模式、把指令交給目前的模式 |
| `commands.py` | 指令種類：`SwitchMode`、`Drive`、`Transcript`、`Quit` |
| `intent.py` | 一句話 → 指令 |
| `drive.py` | 輪子（`M,左,右#`）+ keep-alive + 經過防撞；速度在最上面 |
| `safety.py` | 🚧 防撞 stub（`CollisionGuard.filter()` 現在原樣放行） |
| `modes/voice_mode.py` | 語音模式 |
| `modes/ball_mode.py` | 🚧 追球模式 stub，檔案開頭寫了怎麼接 `camera_control/ball_center.py` |
| `inputs/audio.py` | 麥克風／WAV -> 本機 NPU ASR；喚醒詞、緩衝與錯誤處理 |
| `asr/server.py` | Silero VAD、NPU encoder、CPU decoder 與單一串流服務 |
| `inputs/keyboard.py` | 鍵盤（termios，不用裝套件） |

## 待做

1. 接上實際麥克風，驗證輸入裝置、辨識與喚醒詞；目前先用錄音／fake 驗證
2. `modes/ball_mode.py`：`_run()` 換成 `ball_center` 的 `CameraController` + `Feed` + `control_loop`
3. `safety.py`：防撞，所有 `M` 指令都會經過它
