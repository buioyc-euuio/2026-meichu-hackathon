# e2e：Demo 總控（像投影片：n 下一個、p 上一個階段）

| 階段 | 做什麼 | 程式 |
|---|---|---|
| 1 | 語音叫車子動 | `robot_control/main.py` |
| 2 | 車子跟著網球走 | `ball_chase/chase.py` |
| 3 | 有人躺在地上 → 喊「救命呀！」 | `e2e/fall_alert.py` |

tracking server（`tennis_tracking/server.py`，階段 2、3 要用）**demo 一開始就在背景開好**，整個 demo 都開著，
階段 2 → 3 不用重開相機；demo 結束時關掉。log 在 `e2e/logs/tracker_*.log`。

每個階段都是原本的程式，用子程序跑；**同一時間只有一個在連 micro:bit**（上一個結束、藍牙斷開，才開下一個）。

## 怎麼跑（在專案最外層）

```bash
.venv/bin/python e2e/demo.py --fake                  # 不連 micro:bit，走一遍流程
.venv/bin/python e2e/demo.py                         # 真的跑
.venv/bin/python e2e/demo.py --tracker-args="--camera 2" \
    --voice-args="--bluetooth-mic --llm --allow-motion" --fall-args="--once"
```

| 按鍵 | 階段中 | 階段之間（程式自己結束了） |
|---|---|---|
| `n` | 停掉這個階段 → 開下一個 | 跑 ▶ 標的那個 |
| `p` | 停掉這個階段 → 開上一個 | 跑上一個 |
| `Ctrl-C` | 只結束這個階段，回到階段之間 | 結束 demo |
| 其他鍵 | 照常傳給那支程式（voice 的 w/a/s/d、空白鍵、q；chase 的 `q` + Enter；fall 的 `a`、`q`） | `r` 重跑、`1`/`2`/`3` 直接跳、`q` 結束 demo |

- 換階段時先送 Ctrl-C 讓舊程式自己收尾（停車、斷藍牙），5 秒內沒結束才強制關，之後才開新的
- `n` / `p` 被 demo 拿去換階段，所以：**chase 的暫停（原本 `p`）在 demo 裡改按空白鍵**；
  voice 按 `v` 打字時字裡不能有 n / p
- **階段 3 手動觸發警報：按 `a`**（不用 Enter，馬上喊救命）。終端機一開始和每一行狀態都會提示
- 每個階段在自己的 pty 裡跑，程式以為自己直接在終端機裡（voice 的 cbreak 鍵盤照常能用）
- 給子程式的參數用 `--voice-args=` / `--chase-args=` / `--fall-args=` / `--tracker-args=`，**要有等號**
  （只有一個參數時，例如 `--voice-args=--no-audio`，沒等號 argparse 會當成 demo.py 自己的選項）
- 已經有 tracking server 在跑就直接用；是 demo.py 開的才會在結束時關掉
- tracking server 啟動失敗（例如相機編號不對）→ demo.py 直接結束，看 `e2e/logs/tracker_*.log`

## 階段 3：躺在地上（`fall_alert.py`）

警報 = **喊「救命呀！」**：`tts/` 產生的 3 倍速語音，預設 `tts/output/救命呀x3.wav`（連喊三次，0.64 秒）；
`--sound tts/output/救命呀.wav` 換成只喊一次（0.21 秒）。沒有這個檔就先跑 `tts/tts.py`（見 `tts/README.md`）。
放一支影片到 `e2e/media/alert.mp4`（或 `--video` 指定）的話會**跟聲音同時播**（全螢幕）；沒有就只喊。
播放用 `ffplay`。

判斷只用 YOLO 的人框（tracking server 的 `people[].box`）：

- 框的 **寬 / 高 ≥ 1.3**（站著的人框是直的，躺著是橫的）
- 框寬 ≥ 畫面寬 20%、YOLO 信心 ≥ 0.35、這張真的看到（`misses == 0`）
- 連續 **1 秒**都這樣（中間漏抓 0.5 秒內不算中斷）→ 喊救命；喊完 10 秒內不再觸發

```bash
.venv/bin/python e2e/fall_alert.py --test-play --windowed   # 不偵測，直接警報一次（先確認喇叭 OK）
.venv/bin/python e2e/fall_alert.py                          # 狀態列會印每個人框的 寬/高，拿來現場調門檻
.venv/bin/python e2e/fall_alert.py --once --hold 1.5        # 播一次就結束、要躺 1.5 秒
```

執行中直接按 **`a` 手動觸發警報**（不用 Enter，馬上播一次；demo 時偵測失靈的備案）、`q` 離開。
門檻在 `fall_alert.py` 最上面（`LYING_ASPECT`、`MIN_WIDTH`、`MIN_CONF`、`LYING_HOLD`、`COOLDOWN`）。

⚠️ 限制：只看框的形狀，**不是**真的姿勢辨識。人正對鏡頭躺著（頭或腳朝鏡頭）框會是直的 → 抓不到；
張開雙臂、坐在地上伸腿也可能被當成躺著。之後要更準可以換 YOLO pose（看肩膀、髖部的連線是不是水平的）。
相機往下的角度會影響框的形狀，請在現場實際躺一次，看狀態列的 寬/高 數值再調 `LYING_ASPECT`。
