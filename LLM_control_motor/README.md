# LLM 控制馬達實作

用自然語言控制 micro:bit 機器人車：LLM 呼叫工具 → 藍牙 → micro:bit 控制輪子和相機。

## 檔案

| 檔案 | 用途 |
|---|---|
| `microbit_llm_motor.js` | micro:bit 程式（貼到 MakeCode，要選 No Pairing Required） |
| `robot_bluetooth.py` | 藍牙連線，加上給 LLM 用的工具 |
| `agent.py` | 打字跟 LLM 對話來控制車子 |
| `run_tests.py` | 測試 LLM 有沒有用對工具 |
| `dashboard.html` | 輪速儀表板：用 Chrome 打開，直接輸入左右輪速度和秒數，按 Enter 用藍牙送給 micro:bit |
| `../.env` | 放 `GEMINI_API_KEY`（在專案最外層） |

## 指令（在專案最外層 `2026梅竹黑客松/` 執行）

**控制車子**
```bash
.venv/bin/python LLM_control_motor/agent.py --gemini          # Gemini + 真的車子
.venv/bin/python LLM_control_motor/agent.py --gemini --fake   # 不連車子，只測對話
.venv/bin/python LLM_control_motor/agent.py                   # 改用本地 llama
```

**測試**
```bash
.venv/bin/python LLM_control_motor/run_tests.py --gemini          # 假機器人，快速跑完全部
.venv/bin/python LLM_control_motor/run_tests.py --gemini --real   # 連真車，馬達會動
.venv/bin/python LLM_control_motor/run_tests.py --gemini 7 11     # 只跑第 7、11 題
.venv/bin/python LLM_control_motor/run_tests.py --list            # 列出所有測試句子
```
`--real`：每題開始前按 Enter，輸入 `s` 跳過、`q` 結束，做完回答 y/n 判斷實際動作。第一次建議把車子架高。

**校正（讓「左轉」剛好 90 度、速度合適）** → 程式在 `../LLM校正馬達轉彎/`，說明見那裡的 README
```bash
.venv/bin/python LLM校正馬達轉彎/calibrate.py
```
校正完成後，`agent.py` 和 `run_tests.py` 會自動讀取那個資料夾的 `calibration.json`（工具預設值）和 `knowledge.md`（加進 prompt）。

**找不到 micro:bit 時**
```bash
.venv/bin/python LLM控制microbit模板/scan_devices.py
```

## 藍牙指令

| 指令 | 意思 |
|---|---|
| `M,左輪,右輪#` | -255～255，正數前進、負數後退 |
| `Z,速度#` | 相機上下，正數往上、負數往下 |
| `P,角度#` | 相機左右，**0 最右、90 正前方、180 最左** |
| `T,角度#` | 尾巴轉到指定角度（0～180，90 中間），會中斷搖尾巴 |
| `W,次數,毫秒,幅度#` | 在背景搖尾巴：來回「次數」下，每擺一邊停「毫秒」，以 90 為中心左右各擺「幅度」度 |
| `S#` | 全部停止（尾巴也回到中間） |

micro:bit 0.5 秒沒收到指令會自動停車；動作中電腦每 0.1 秒重送一次。
搖尾巴不受這個機制影響，會在背景自己搖完，所以可以一邊搖尾巴一邊轉圈。尾巴伺服馬達接 P13。

## 常改的地方

- `robot_bluetooth.py` 最上面：預設速度、單次最長秒數、藍牙名稱
- `turn_left` / `turn_right` 是弧線轉彎（外側輪 `TURN_SPEED`、內側輪 `TURN_INNER_SPEED`）；`inner_speed` 設負數才是原地轉。跑過校正會自動改用校正的數值
- 輪子方向相反：改 micro:bit 程式裡的 `left_wheel_forward` / `right_wheel_forward`（0、1 互換）
