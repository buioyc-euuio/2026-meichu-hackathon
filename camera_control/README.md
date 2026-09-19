# camera_control：相機自動對準網球

從 `tennis_tracking/server.py` 的 WebSocket 拿球和人的位置，球心離畫面中心超過**門檻（像素）**就轉相機，把球拉回中間：

- **看畫面直接跟**：每張畫面看球在哪就決定（`--frames N` 可改成看 N 張取平均再決定，比較穩但比較慢）
- 左右：伺服馬達 `P,角度#`，一次送目標角度，**送完等 0.2 秒**讓畫面反應過來再決定下一步（太快會轉過頭來回）；每步 2～5 度，**防護：任何一個指令最多只轉 5 度**，回正也是一步一步轉
- 上下：直流馬達 `Z,速度#`，**不等**：實測轉速跟畫面移動很線性，每張畫面直接依誤差設轉速（誤差越大越快，往上 85～130、往下 40～80），球回到門檻內就停；往上從靜止起步先衝一下
- ⚠️ **這台車 `Z` 負數才是相機往上**（跟 micro:bit 程式的註解相反），`ball_center.py` 的 `TILT_UP_SIGN` 已處理
- 上下是直流馬達，micro:bit 不知道它的位置、極限和中心（沒有編碼器／限位開關），所以全部看畫面判斷

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
| `--kp` | 每次修正誤差的幾成（0~1，預設 0.8） |
| `--frames` | 每次看幾張畫面再決定（預設 1 = 每張都直接跟） |
| `--device-name` | micro:bit 名稱，逗號分開依優先順序（預設 `vapup,zutog`：先找 vapup，找不到才連 zutog） |
| `--invert-pan` / `--invert-tilt` | 轉的方向相反時用 |
| `--no-tilt` | 只控制左右 |
| `--fake` | 不連藍牙 |
| `--json` | stdout 每張畫面印一行 JSON |
| `--quiet` | 不印每個藍牙指令 |
| `--url` | server 網址，預設 `ws://127.0.0.1:8000/ws` |

執行中在終端機打指令 + Enter：`t 40`（兩軸門檻）、`tx 30`、`ty 50`、`kp 0.8`、`n 1`、`p`（暫停／繼續）、`c`（回正）、`s`（看設定）、`q`（離開）。

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

看「球動了，相機怎麼跟著動」：偵測結果、門檻框、誤差、送出的控制指令都畫在畫面上；門檻、gain、每次看幾張（frames）用視窗上的滑桿即時調。
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
| 洋紅色 × | 上一次決定用的位置（幾張平均 + 速度預估） |
| 黃色箭頭 | 相機現在轉的方向（左右箭頭越長 = 這一步轉越多度） |
| 下方面板 | 狀態、左右角度量表（左邊 = 相機看左邊）、上下 Z 值、門檻、gain、frames |

實機模式的綠圈、紫框（球、人）是 server 畫的；模擬模式由 test_view 自己畫。

按鍵：`q`／Esc 離開、`p` 暫停／繼續、`c` 回正；模擬模式另有 `a` 球自動繞圈、`+`／`-` 繞圈速度、`r` 球回中間、滑鼠左鍵拖球。

模擬的相機參數（每度幾像素、延遲、上下馬達速度和推不動的範圍、fps）用的是實機量測值（`test_view.py` 最上面的 `SIM_*`）。

## JSON 格式

```json
{
  "frame": 174, "time": 1789799424.6, "width": 640, "height": 480, "fps": 30.4,
  "target": {"id": 1, "x": 500, "y": 100, "r": 20, "d": 40, "size": 0.06, "ex": 0.56, "ey": -0.58,
             "box": [480, 80, 520, 120], "vx": 0, "vy": 0, "source": "yolo+cv", "conf": 0.6, "misses": 0,
             "dx_px": 180.0, "dy_px": -140.0, "dx_avg": 175.2, "dy_avg": -138.0,
             "centered_x": false, "centered_y": false},
  "balls":  [ ...所有球（欄位同 tennis_tracking）... ],
  "people": [ ...所有人（欄位同 tennis_tracking）... ],
  "control": {"state": "tracking", "phase": "move", "locked_id": 1, "pan": 82, "pan_step": -5.0, "tilt_speed": -115,
              "threshold_x": 30, "threshold_y": 30,
              "kp": 0.8, "frames": 1}
}
```

- `target`：正在對準的球（沒有球時是 `null`）。`dx_px`／`dy_px` = 球心離畫面中心幾個像素（右、下為正），`dx_avg`／`dy_avg` = 上一次決定用的位置（幾張平均 + 速度預估），`centered_x/y` = 有沒有在門檻內
- 選球：一直跟同一個 id；那顆被 tracker 刪掉才換成目前最大（最近）的球
- `control.state`：`tracking`（控制中）、`predict`（這張沒看到，用預測位置，不動）、`lost`（沒有球，不動）、`paused`、`no_data`（超過 0.5 秒沒收到 server 資料，上下馬達停）
- `phase`：`observe`（收集畫面中）／`move`（在轉或等畫面穩定）；`pan` = 目前伺服角度；`pan_step` = 這一步轉了幾度（正 = 往左）；`tilt_speed` = 送出的 Z 值（這台車負 = 往上、正 = 往下、0 = 停）

## 實機量測（2026-09-19，用相機畫面位移量的）

| 項目 | 數值 | 用在哪 |
|---|---|---|
| 左右 1 度 = 畫面幾像素 | 約 13.5 px（水平視角約 47°） | `PAN_PX_PER_DEG` |
| 左右延遲（送指令 → 畫面動） | 約 0.12 秒 | 左右送完角度等 `PAN_SETTLE` = 0.2 秒 |
| 上下延遲 | 約 0.15～0.22 秒；送 `S#` 後還會滑 12～35 px | `TILT_LATENCY` |
| 往上速度 | 50 推不動（從靜止 75～80 也常推不動）、90 ≈ 180 px/s、110 ≈ 250 px/s | `TILT_*_UP` |
| 往下速度 | 50 ≈ 135 px/s、75 ≈ 225 px/s、110 ≈ 400 px/s（有重力幫忙） | `TILT_*_DOWN` |
| 藍牙寫入 | 等回應：中位數 30 ms、最慢 130 ms；不等回應：約 1 ms | `robot_ble.py` 預設不等回應 |
| 相機實際 fps | 約 20 | |

這些數值是用「真相機 + 真馬達 + 虛擬球」的閉環測試量的。

## 調整

- 門檻太小相機會在中心附近來回晃：伺服只能整數度轉，1 度 ≈ 畫面上 13.5 px，左右門檻建議 ≥ 20 px
- 相機衝過頭、來回晃 → 調小 `--kp`；跟不上 → 調大
- 偵測很抖、相機被雜訊騙去動 → `--frames` 調大（例如 3）
- 左右在對準附近來回 → `PAN_SETTLE` 調大（實測 0.15 會來回、0.2 不會）
- 一步最多轉幾度、等畫面穩定多久、球在動時往前預估多久，在 `ball_center.py` 最上面（`PAN_MAX_STEP`、`PAN_SETTLE`、`LEAD`）
- 上下轉速範圍在同一個地方（`TILT_MIN_SPEED_UP`、`TILT_MAX_SPEED_UP`、`TILT_MIN_SPEED_DOWN`、`TILT_MAX_SPEED_DOWN`）
- 電池電量會影響上下馬達推不推得動：電量低時往上容易卡住，`TILT_MIN_SPEED_UP` 要調高
