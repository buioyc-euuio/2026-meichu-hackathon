# ball_chase：車子追網球，維持跟一開始一樣

讀 `tennis_tracking/server.py` 的輸出。啟動後看到球，**先原地轉把球轉到畫面中間（recenter），再記下那時的大小和位置當基準**，之後一直維持：
球變小 → 往前；變大 → 往後；偏離基準位置（≈ 畫面中間）→ 轉回來。不用量距離。相機鎖住不動（左右 90 度、上下不動）。

```
tennis_tracking/server.py ──WebSocket /ws──▶ chase.py（狀態機）──▶ chassis.py（M 指令 + keep-alive + 加減速）──藍牙──▶ micro:bit
```

## 怎麼跑

```bash
.venv/bin/python tennis_tracking/server.py              # 1. 追蹤伺服器（相機編號不是 0 就加 --camera N）
.venv/bin/python ball_chase/chase.py                    # 2. 球放在想維持的距離 → 自動轉到中間、抓基準 → 開始追
.venv/bin/python ball_chase/chase.py --fake             #    不連車，只看算出來的輪子指令
.venv/bin/python ball_chase/chase.py --max-speed 120    #    限速
.venv/bin/python ball_chase/chase.py --view            #    開視窗 debug：基準、現在的球、狀態、輪子轉速
```

- **基準**：看到球 → 一段一段原地轉，直到球在畫面中間 ±3° → 停著收集 10 張可以量的畫面（真的看到、沒被邊緣切到），
  取中位數當目標直徑和位置；收集到一半球又偏掉就重新對準、重新收集
- 執行中打 `r` + Enter **重新抓基準**（一樣先轉到中間）、`p` 暫停（停車）、`s` 顯示設定、`q` 離開
- `--view` 視窗：黃色虛線圓 = 基準（球應該在的位置和大小）、淡黃圓 = 「到了」的範圍、黃線 = 基準方位、
  灰線 = 不修方向的範圍、紅線 = 超過就原地轉；綠圈 = 現在的球（橘 = 大小不能用）；
  下方是狀態、距離比例條、左右輪（淡 = 要的、實心 = 正在送的）。視窗按 `r` 重新抓基準、`p` 暫停、`q` 離開
- 想用固定距離：`calibrate.py` 校正後加 `--use-settings`（方位 = 正前方），或 `--target-d 直徑px`

## Log（每次執行自動存）

`ball_chase/logs/chase_日期_時間.jsonl`，一行一筆 JSON：

- `config`（第一行）：參數、校正值、命令列選項、連到哪片 micro:bit
- `frame`（每張畫面）：球的位置／直徑／來源、角度、直徑中位數、距離誤差、估計公分、狀態、原因、
  `want`（程式要的輪子轉速）、`sent`（加減速後實際在送的轉速）、畫面裡的人
- `event`：斷線、server 沒資料、終端機指令
- `summary`（最後一行）：秒數、張數、各狀態次數（**沒有這行 = 程式不是正常結束的**）

```bash
.venv/bin/python ball_chase/analyze_log.py              # 最新一份：設定、狀態比例、事件、時間軸、常見問題提示
.venv/bin/python ball_chase/analyze_log.py ball_chase/logs/chase_xxx.jsonl --all
```

不想存就加 `--no-log`。

## 距離怎麼判斷（不算公分）

跟基準的直徑比：`距離 / 基準距離 ≈ 基準直徑 / 現在直徑`（log 裡的 `dist_ratio`）。
相機高度、往下的角度、焦距都一樣所以會抵消；球是球體，從哪個角度看都是圓。**追球途中不要動相機角度。**

## 狀態

| 狀態 | 條件 | 輪子 |
|---|---|---|
| `lost` | 連續 0.3 秒沒真的看到球（YOLO 常隔一張漏抓，短暫漏抓時維持原本的動作） | 停 |
| `recenter` | 還沒有基準：把球轉到畫面中間 | 一段一段原地轉 |
| `reference` | 對準中間了，收集基準 | 不動 |
| `align` | 球偏離基準方位超過 15°（停著時超過 6°） | **一段一段原地轉**：轉一小段 → 停 → 等 0.35 秒畫面穩定 → 再看（原因欄 `spin 0.20s` / `settle`） |
| `approach` | 太遠 | 邊走邊修方向，越遠越快 |
| `arrived` | 大小在基準 ±12% 內（到了之後要差 25% 才重新走） | 停（只修方向） |
| `backoff` | 比基準近 25% 以上 | 慢慢後退 |
| `person` | 加 `--person-stop` 時：畫面裡有人很近（框高 > 畫面 90%）。預設關，因為拿球站在前面的人本來就很近 | 停 |

執行中在終端機打 `p` 暫停（停車）、`s` 顯示設定、`q` 離開。

## 參數（`chase.py` 最上面）

- `TURN_SIGN`：轉彎方向（vapup 那台實測 M 指令左右是反的 → -1；轉錯邊就改成 1）
- `REF_FRAMES`：抓基準用幾張；`RECENTER_DEG`：抓基準前要轉到離中間幾度以內

- `MIN_WHEEL`：推得動車子的最低轉速（**還沒實測**，先設 100）
- `FORWARD_GAIN`、`MAX_FORWARD`：越遠走越快、最快多少；`BACK_SPEED`：後退轉速
- 原地轉：`TURN_SPEED`（轉速）、`TURN_RATE`（每秒幾度的起始值，執行時看畫面自動修正，log 的 `turn_rate`）、
  `TURN_PULSE_GAIN`（每段只轉算出來角度的幾成）、`TURN_SETTLE`（轉完等多久）、`TURN_START_DEG`／`ALIGN_DEG`
  - 還是轉過頭 → `TURN_PULSE_GAIN` 調小、`TURN_SETTLE` 調大；轉太慢 → `TURN_PULSE_GAIN` 調大
  - ⚠️ `TURN_SETTLE` 不要低於 0.15（相機延遲）：模擬 0.10 會一直左右來回、抓不到基準；0.20 最快又不會來回
- 邊走邊修方向：`TURN_GAIN`（太大會左右晃）、`HEADING_DEADBAND`
- `ARRIVE_BAND`／`LEAVE_BAND`／`BACKOFF_BAND`：到了、重新走、後退的門檻
- `chassis.py` 的 `ACCEL`：加速時轉速每秒最多增加多少；**減速、停車、換方向都馬上**（慢慢減速會轉過頭）
