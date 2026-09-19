# camera_control：相機自動對準網球

從 `tennis_tracking/server.py` 的 WebSocket 拿球和人的位置，球心離畫面中心超過**門檻（像素）**就轉相機，把球拉回中間：

- 左右：伺服馬達 `P,角度#`（轉速跟誤差成正比，有加減速限制）
- 上下：直流馬達 `Z,速度#`（超過門檻越多轉越快；往上、往下分開設轉速；有軟體限位和「連續轉 2 秒就停」兩道保險）
- ⚠️ **這台車 `Z` 負數才是相機往上**（跟 micro:bit 程式的註解相反），`ball_center.py` 的 `TILT_UP_SIGN` 已處理
- ⚠️ 啟動前先把相機上下扶到平衡點（大概正前方）：上下沒有位置回報，軟體限位是從「啟動時的位置」算起

```
tennis_tracking/server.py ──WebSocket /ws──▶ ball_center.py ──藍牙──▶ micro:bit
   （相機 + YOLO + CV）                       （只留最新一筆）          （microbit_llm_motor.js）
```

## 怎麼跑

```bash
.venv/bin/python tennis_tracking/server.py                          # 1. 先開追蹤伺服器
.venv/bin/python camera_control/ball_center.py --fake               # 2. 不連車，只印指令（先測這個）
.venv/bin/python camera_control/ball_center.py                      #    連 micro:bit，真的轉
.venv/bin/python camera_control/ball_center.py --threshold 40       # 門檻 40 px
.venv/bin/python camera_control/ball_center.py --threshold-x 30 --threshold-y 50
.venv/bin/python camera_control/ball_center.py --json > log.jsonl   # 每張畫面一行 JSON（訊息在 stderr）
```

看畫面：瀏覽器開 `http://127.0.0.1:8000/video`。

| 參數 | 意思 |
|---|---|
| `--threshold N` | 門檻（像素），左右上下一樣，預設 30 |
| `--threshold-x` / `--threshold-y` | 分開設左右／上下門檻 |
| `--kp` | 左右的 Kp：球在最邊邊時每秒轉幾度（預設 110） |
| `--smooth` | EMA 平滑的 alpha（0~1，預設 0.5；越小越平滑但越慢） |
| `--invert-pan` / `--invert-tilt` | 轉的方向相反時用 |
| `--no-tilt` | 只控制左右 |
| `--fake` | 不連藍牙 |
| `--json` | stdout 每張畫面印一行 JSON |
| `--quiet` | 不印每個藍牙指令 |
| `--url` | server 網址，預設 `ws://127.0.0.1:8000/ws` |

執行中在終端機打指令 + Enter：`t 40`（兩軸門檻）、`tx 30`、`ty 50`、`kp 110`、`sm 0.5`、`p`（暫停／繼續）、`c`（回正）、`s`（看設定）、`q`（離開）。

## 馬達測試（`motor_test.py`）

左右、上下各轉約 60 度再回來，最後回到中間：左 → 中 → 右 → 中 → 上 → 中 → 下 → 中。

```bash
.venv/bin/python camera_control/motor_test.py --fake        # 不連車，只印指令
.venv/bin/python camera_control/motor_test.py               # 自動跑完（約 13 秒）
.venv/bin/python camera_control/motor_test.py --step        # 每步按 Enter 才繼續
.venv/bin/python camera_control/motor_test.py --only tilt --degrees 20 --step   # 上下先小角度試
```

- 左右是伺服馬達，角度準確（150 = 左 60°、30 = 右 60°），會慢慢轉過去
- 上下是直流馬達，只能控制「轉幾秒」，角度是用實測速度估的（往上 110、往下 75，60° 各約 3.6 秒）：
  看實際轉了幾度，照比例改 `--up-seconds`／`--down-seconds`（轉了 40° → 秒數 × 1.5）
- ⚠️ 上下沒有限位開關，60° 可能超過機構能轉的範圍：先扶到平衡點，第一次用 `--degrees 20 --step`

## 測試視窗（`test_view.py`）

看「球動了，相機怎麼跟著動」：偵測結果、門檻框、誤差、送出的控制指令都畫在畫面上；門檻、Kp、平滑程度（smooth %）用視窗上的滑桿即時調。
控制邏輯直接共用 `ball_center.py`，調好的數字可以直接拿去用。

```bash
.venv/bin/python camera_control/test_view.py --sim          # 模擬：不用相機、不用車，滑鼠拖球
.venv/bin/python camera_control/test_view.py --fake         # 實機畫面（要先開 server），不連車
.venv/bin/python camera_control/test_view.py                # 實機畫面 + 真的轉
.venv/bin/python camera_control/test_view.py --sim --headless 5   # 沒有螢幕：跑 5 秒存 test_snapshot.jpg
```

| 畫面上 | 意思 |
|---|---|
| 白色十字 | 畫面中心 |
| 門檻框 | 球心在框內就不動（綠 = 置中、橘 = 還沒） |
| 青色粗圈 + 線 | 正在對準的球、`dx`／`dy` |
| 洋紅色 × | EMA 平滑後的球位置（控制用的是這個） |
| 黃色箭頭 | 相機現在轉的方向（左右箭頭越長轉越快） |
| 下方面板 | 狀態、左右角度量表（左邊 = 相機看左邊）、上下馬達速度（`BLOCKED` = 轉太久被擋）、門檻、Kp |

實機模式的綠圈、紫框（球、人）是 server 畫的；模擬模式由 test_view 自己畫。

按鍵：`q`／Esc 離開、`p` 暫停／繼續、`c` 回正（也會解除上下的 BLOCKED）；模擬模式另有 `a` 球自動繞圈、`+`／`-` 繞圈速度、`r` 球回中間、滑鼠左鍵拖球。

模擬的相機參數（每度幾像素、延遲、上下馬達速度和推不動的範圍、fps）用的是實機量測值（`test_view.py` 最上面的 `SIM_*`）。

## JSON 格式

```json
{
  "frame": 174, "time": 1789799424.6, "width": 640, "height": 480, "fps": 30.4,
  "target": {"id": 1, "x": 500, "y": 100, "r": 20, "d": 40, "size": 0.06, "ex": 0.56, "ey": -0.58,
             "box": [480, 80, 520, 120], "vx": 0, "vy": 0, "source": "yolo+cv", "conf": 0.6, "misses": 0,
             "dx_px": 180.0, "dy_px": -140.0, "dx_smooth": 175.2, "dy_smooth": -138.0, "dx_pred": 160.4, "dy_pred": -120.0,
             "centered_x": false, "centered_y": false},
  "balls":  [ ...所有球（欄位同 tennis_tracking）... ],
  "people": [ ...所有人（欄位同 tennis_tracking）... ],
  "control": {"state": "tracking", "locked_id": 1, "pan": 76, "pan_speed": -42.5, "tilt_speed": -95,
              "tilt_blocked": 0, "tilt_pos_px": 40, "threshold_x": 30, "threshold_y": 30, "kp": 110.0, "smooth": 0.5}
}
```

- `target`：正在對準的球（沒有球時是 `null`）。`dx_px`／`dy_px` = 球心離畫面中心幾個像素（右、下為正），`dx_smooth`／`dy_smooth` = EMA 平滑後的，`dx_pred`／`dy_pred` = 再加上延遲補償的預測值（控制用這個），`centered_x/y` = 有沒有在門檻內
- 選球：一直跟同一個 id；那顆被 tracker 刪掉才換成目前最大（最近）的球
- `control.state`：`tracking`（控制中）、`predict`（這張沒看到，用預測位置，不動）、`lost`（沒有球，不動）、`paused`、`no_data`（超過 0.5 秒沒收到 server 資料，上下馬達停）
- `pan` = 目前伺服角度；`pan_speed` = 左右轉速（度/秒，正 = 往左）；`tilt_speed` = 送出的 Z 值（這台車負 = 往上、正 = 往下、0 = 停）；`tilt_blocked` = 因為轉太久被擋住的方向；`tilt_pos_px` = 依指令估計的上下位置（畫面像素，+ = 偏上，超過 ±`TILT_LIMIT_PX` 就不再往那邊轉）

## 實機量測（2026-09-19，用相機畫面位移量的）

| 項目 | 數值 | 用在哪 |
|---|---|---|
| 左右 1 度 = 畫面幾像素 | 約 13.5 px（水平視角約 47°） | `PAN_PX_PER_DEG` |
| 左右延遲（送指令 → 畫面動） | 約 0.12 秒 | `PAN_LATENCY` |
| 上下延遲 | 約 0.15～0.22 秒；送 `S#` 後還會滑 12～35 px | `TILT_LATENCY` |
| 往上速度 | 50 推不動（從靜止 75～80 也常推不動）、90 ≈ 180 px/s、110 ≈ 250 px/s | `TILT_*_UP` |
| 往下速度 | 50 ≈ 135 px/s、75 ≈ 225 px/s、110 ≈ 400 px/s（有重力幫忙） | `TILT_*_DOWN` |
| 藍牙寫入 | 等回應：中位數 30 ms、最慢 130 ms；不等回應：約 1 ms | `robot_ble.py` 預設不等回應 |
| 相機實際 fps | 約 20 | |

用「假球」閉環測試調參數：真相機 + 真馬達，球是虛擬的（固定在世界裡，用相機畫面的位移算出它在畫面哪裡）。
Kp 110 時左右 200 px 的步階約 0.4～0.9 秒穩定在門檻內、過衝多半 < 40 px；上下 100 px 約 0.7～0.9 秒、過衝 < 17 px、不會來回抖。
Kp 140 偶爾會過衝到 130 px，所以預設用 110。

## 調整

- 門檻太小相機會在中心附近來回晃：伺服只能整數度轉，1 度 ≈ 畫面上 13.5 px，左右門檻建議 ≥ 20 px
- 左右晃 → 調小 `--kp`；跟不上 → 調大
- 平滑機制（為什麼不抖）：
  1. **EMA**：球的位置先平滑（`--smooth`），濾掉偵測雜訊
  2. **遲滯**：超過門檻才開始轉，回到「門檻 × 0.5」以內才停，不會在門檻邊緣一直開關
  3. **速度控制 + 加減速限制**：轉速跟誤差成正比，起步、停下都是漸進的（`PAN_ACCEL`、`PAN_MAX_SPEED`）
  4. **延遲補償**：記住最近送出的指令，把「已經送出、畫面還沒反應」的移動先算進去（`PAN_LATENCY`、`TILT_LATENCY`），
     這是不會一直轉過頭的關鍵
  5. **上下往上起步衝一下**：從靜止往上先用 `TILT_KICK_SPEED_UP` 衝 0.12 秒，突破靜摩擦 + 重力
- 注意：`--smooth` 不是越小越好。平滑本身會帶來延遲，延遲越大越容易轉過頭來回晃；
  模擬測試（延遲 0.15 秒、雜訊 3 px）：smooth 0.25 比 0.5 更晃。偵測很抖才調小，相機來回晃先調小 `--kp`
- 上下轉速、限位、最長轉動時間在 `ball_center.py` 最上面（`TILT_MIN_SPEED_UP`、`TILT_MAX_SPEED_DOWN`、`TILT_LIMIT_PX`、`TILT_MAX_SECONDS` 等）
- 電池電量會影響上下馬達推不推得動：電量低時往上容易卡住，最低轉速要調高
