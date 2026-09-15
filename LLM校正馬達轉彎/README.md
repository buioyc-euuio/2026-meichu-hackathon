# LLM 校正馬達轉彎

跟 LLM 一起反覆測試，讓「左轉」剛好 90 度、前進速度合適，並把學到的數值寫回控制程式。
藍牙和車子工具借用 `../LLM控制馬達實作/`，micro:bit 程式也用那裡的 `microbit_llm_motor.js`。

## 指令（在專案最外層 `2026梅竹黑客松/` 執行）

```bash
.venv/bin/python LLM校正馬達轉彎/calibrate.py          # 連真車
.venv/bin/python LLM校正馬達轉彎/calibrate.py --fake   # 不連車，先練習流程
```

## 流程

1. LLM 讓車子做一個動作（例如速度 150 左轉 0.8 秒）
2. 你用中文回報：「只轉了 60 度」「太快了」「剛好」
3. LLM 記錄、調整數值再試；左右轉各測 3 種秒數
4. 程式用最小平方法算出「秒數 → 度數」公式，產生報告
5. 輸入 `q` 可隨時結束（中途結束也會留下報告）

小技巧：地上用膠帶貼出 0、90、180 度的線，觀察會準很多。

## 產生的檔案（都在這個資料夾）

| 檔案 | 用途 |
|---|---|
| `report_日期_時間.md` | 校正報告，**交給 Claude 檢查** |
| `calibration.json` | 預設速度、轉 90 度的秒數 → `robot_bluetooth.py` 當工具預設值 |
| `knowledge.md` | 換算公式和經驗 → `agent.py` 自動加進 LLM 的 system prompt |
| `trials.jsonl` | 每次嘗試的原始紀錄（會一直累積） |

想重新校正，直接再跑一次，會覆蓋 `calibration.json` 和 `knowledge.md`。
想回到未校正狀態，刪掉這兩個檔案即可。
