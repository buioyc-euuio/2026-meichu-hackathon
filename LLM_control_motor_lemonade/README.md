# LLM 控制馬達實作（Lemonade 本地版）

跟 `../LLM_control_motor/` 一樣，但把 Gemini Flash 換成本機的 [Lemonade Server](https://lemonade-server.ai/docs/api/openai/)，
走 OpenAI 相容 API（`http://localhost:13305/v1`），不用 API key、不用網路。

## 檔案

| 檔案 | 用途 |
|---|---|
| `agent.py` | 打字跟本地 LLM 對話來控制車子（OpenAI client → Lemonade） |
| `run_tests.py` | 測試 LLM 有沒有用對工具 |
| `robot_bluetooth.py` | 藍牙連線 ＋ 工具函式（跟原版相同） |
| `microbit_llm_motor.js` | micro:bit 程式（跟原版相同） |

## 準備

1. 啟動 Lemonade Server，下載一個有 **tool-calling** 標籤的模型：
   ```bash
   curl http://localhost:13305/v1/models   # 看 labels 裡有沒有 "tool-calling"
   ```
2. 安裝套件：
   ```bash
   cd LLM_control_motor_lemonade
   uv venv --python 3.12 && uv pip install bleak openai python-dotenv
   ```
3.（可選）在專案最外層 `.env` 設定：
   ```
   LEMONADE_BASE_URL=http://localhost:13305/v1
   LEMONADE_MODEL=gemma-4-26B-A4B-it-qat-q4_0-gguf-Q4_0   # 不填就自動挑第一個支援 tool-calling 的
   LEMONADE_API_KEY=                                      # 伺服器有設 key 才需要
   ```

## 指令（在專案最外層執行）

```bash
LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/agent.py          # 真的車子
LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/agent.py --fake   # 不連車子，只測對話

LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/run_tests.py          # 假機器人跑全部
LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/run_tests.py --real   # 連真車
LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/run_tests.py 7 11     # 只跑第 7、11 題
LLM_control_motor_lemonade/.venv/bin/python LLM_control_motor_lemonade/run_tests.py --list   # 列出測試句子
```

## 跟 Gemini 版差在哪

- Gemini SDK 會自動讀 Python 函式產生工具說明、自動執行工具；OpenAI API 不會，
  所以 `agent.py` 的 `function_to_tool()` 從「型別、預設值、docstring 的 `Args:`」產生 JSON schema，
  `make_chat()` 自己跑「呼叫模型 → 執行工具 → 回傳結果」的迴圈。
- 改工具時照舊只改 `robot_bluetooth.py`，說明文字保持 `Args:` 格式即可。
- 校正檔 `../LLM校正馬達轉彎/calibration.json`、`knowledge.md` 一樣會自動讀取。
