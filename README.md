# 2026 梅竹黑客松 — LLM 控制 micro:bit 機器人車

打一句「往前走兩秒再右轉」，LLM 決定要呼叫哪些工具，指令透過藍牙送到 micro:bit，車子就動了。
這個 repo 收了從「鍵盤遙控」到「LLM tool calling ＋ 馬達校正」的完整過程。
https://youtube.com/shorts/D9MfTdmBM3Q?si=4X9HD2Bdnaq6EYTw
## 資料夾

| 資料夾 | 內容 |
|---|---|
| `藍牙連線簡單模板/` | 最小可動的藍牙範例：鍵盤遙控、V7RC 車、tkinter 介面 |
| `LLM控制microbit模板/` | LLM tool calling 的乾淨模板（Ollama 本地模型或 Gemini 都可） |
| `LLM_control_motor/` | 實際跑的版本：輪子 ＋ 相機工具、對話介面、測試腳本 |
| `LLM校正馬達轉彎/` | 跟 LLM 一起把「左轉」校正成剛好 90 度，產生 `calibration.json` 和 `knowledge.md` |
| `tennis_tracking/` | 相機追蹤多顆網球和多個人（YOLO + 傳統 CV，跑在 GPU），FastAPI 即時串流 JSON |

每個資料夾都有自己的 README，寫了該怎麼跑。

## 開始

```bash
python3 -m venv .venv
.venv/bin/pip install bleak python-dotenv google-genai ollama

cp .env.example .env      # 然後把 GEMINI_API_KEY 填進去
```

micro:bit 端的程式是各資料夾裡的 `.js`，貼到 [MakeCode](https://makecode.microbit.org/) 編譯後燒錄，
藍牙設定要選 **No Pairing Required**。

```bash
.venv/bin/python LLM_control_motor/agent.py --gemini          # 真的車子
.venv/bin/python LLM_control_motor/agent.py --gemini --fake   # 不連車，只測對話
```

## 需要的東西

micro:bit v2、馬達驅動板和兩顆馬達、一台裝了 Python 3.10+ 的電腦（藍牙用 `bleak`）、
一把 Gemini API key（或本機跑 Ollama）。

`.env` 有 API key，已經在 `.gitignore` 裡，不會被推上來。
