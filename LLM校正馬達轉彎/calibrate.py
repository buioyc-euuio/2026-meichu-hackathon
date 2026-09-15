"""
校正程式：跟 LLM 一起反覆測試，找出「轉 90 度要幾秒」「多快的速度剛好」

流程：
  1. LLM 讓車子做一個動作（例如：速度 150 左轉 0.8 秒）
  2. 你觀察後用自然語言回報：「只轉了大概 60 度」「太快了」「剛好 90 度」
  3. LLM 記錄這次結果、調整數值，再試一次
  4. 資料夠了之後，程式用數學算出「幾秒轉幾度」的公式
  5. 產生報告，並把學到的知識寫回 LLM 的 system prompt

執行（在專案最外層）：
    .venv/bin/python LLM校正馬達轉彎/calibrate.py          # 連真車
    .venv/bin/python LLM校正馬達轉彎/calibrate.py --fake   # 不連車，先練習流程

產生的檔案（都在這個資料夾 LLM校正馬達轉彎/）：
    trials.jsonl          每一次嘗試的原始紀錄（每次校正都會累積下去）
    report_日期_時間.md    這次校正的報告 → 交給 Claude 看的就是這份
    calibration.json      校正後的數值，LLM控制馬達實作/robot_bluetooth.py 會讀來當預設值
    knowledge.md          學到的知識，LLM控制馬達實作/agent.py 會自動加進 LLM 的 system prompt

小技巧：在地上用膠帶貼出 0 度、90 度、180 度的線，觀察角度會準很多。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 借用 LLM控制馬達實作/ 裡的 agent.py 和 robot_bluetooth.py（藍牙連線和車子工具）
sys.path.insert(0, str(HERE.parent / "LLM控制馬達實作"))

from google import genai
from google.genai import types

from agent import GEMINI_MODEL  # 匯入 agent 時會順便讀取 .env
from robot_bluetooth import (MAX_MOVE_SECONDS, move_backward, move_forward, robot, stop,
                             turn_left, turn_right)

CAL_DIR = HERE
TRIALS_PATH = CAL_DIR / "trials.jsonl"
CALIBRATION_PATH = CAL_DIR / "calibration.json"
KNOWLEDGE_PATH = CAL_DIR / "knowledge.md"

UNITS = {"turn_left": "度", "turn_right": "度", "move_forward": "公分", "move_backward": "公分"}

# ====== 想改的東西都在這裡 ======
COACH_PROMPT = """你是機器人車的「校正教練」。你要透過反覆實驗，找出讓車子動作準確的數值。
使用者在旁邊觀察車子，會用自然語言回報結果，例如「只轉了 40 度」「太快了」「剛好」。

已知的起點（之前實測）：
- 速度 180、轉 0.5 秒，只轉了大約 40 度。
- 速度 180 前進太快，使用者希望慢一點。

校正目標（依序完成）：
1. 前進速度：找出使用者覺得合適、比 180 慢的前進速度。速度太低時馬達可能推不動，也要記錄。
2. 左轉：選一個固定的轉彎速度，找出轉 90 度要幾秒。
3. 右轉：用同一個轉彎速度，找出轉 90 度要幾秒（左右可能不對稱，要分開測）。
4. 建立公式：左轉和右轉都要在「同一個轉彎速度」下，用至少 3 種不同秒數測試（例如目標 45、90、180 度）。
5. 選做：用決定好的前進速度測 2 種秒數，請使用者量走了幾公分。

每一輪的規則：
- 一次只做一個動作，而且一定要明確指定 seconds 和 speed。
- 做完動作後，問使用者結果（轉彎問幾度、前進問幾公分或速度感覺），然後等使用者回答，不要連續做下一個動作。
- 使用者回答後，先呼叫 log_trial 記錄，再決定下一次的數值。使用者沒給數字時，依描述估計並跟他確認。
- 調整方法：用比例推算，例如 0.5 秒轉 40 度 → 90 度約需 0.5 × 90 ÷ 40 ≈ 1.1 秒。
  同一個動作和速度有 2 筆以上資料後，呼叫 analyze 取得公式來預測。
- 轉彎速度一旦決定就不要再改，否則之前的資料無法一起計算。
- 使用者說「停」就立刻呼叫 stop。
- 誤差在 ±10 度內，或使用者說剛好，就算達成該目標。
- 全部目標完成（或使用者說要結束）時，先跟使用者確認前進速度和轉彎速度，再呼叫 finish_calibration。
- 用繁體中文，回覆簡短，清楚告訴使用者下一步要觀察什麼。"""
# ================================

session_trials = []   # 這次校正的紀錄
state = {"report": None}


# ---------- 數學：用最小平方法算公式 ----------
def fit(trials):
    """依（動作, 速度）分組，算出：實際結果 = rate × 秒數 + offset。"""
    groups = {}
    for t in trials:
        groups.setdefault((t["action"], t["speed"]), []).append(t)

    results = []
    for (action, speed), ts in sorted(groups.items()):
        xs = [t["seconds"] for t in ts]
        ys = [t["observed"] for t in ts]
        n = len(ts)
        reliable = n >= 2 and max(xs) > min(xs)   # 至少兩種不同秒數才畫得出一條線
        if reliable:
            mx, my = sum(xs) / n, sum(ys) / n
            rate = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            offset = my - rate * mx
        else:
            rate, offset = sum(ys) / sum(xs), 0.0
        results.append({"action": action, "speed": speed, "n": n, "reliable": reliable,
                        "rate": rate, "offset": offset, "unit": UNITS[action]})
    return results


def seconds_for(f, target):
    return (target - f["offset"]) / f["rate"]


def minus(offset):
    """把「目標 − 偏移」寫成好讀的樣子：偏移 -9.8 → "+ 9.8"，偏移 3 → "− 3.0"。"""
    return f"+ {-offset}" if offset < 0 else f"− {offset}"


# ---------- 給校正教練 LLM 用的工具 ----------
def log_trial(action: str, seconds: float, speed: int, observed: float, note: str = "") -> str:
    """記錄一次嘗試的結果。每做完一個動作、使用者回報結果後，一定要呼叫一次。

    Args:
        action: 剛才做的動作，必須是 turn_left、turn_right、move_forward、move_backward 其中之一
        seconds: 剛才用的秒數
        speed: 剛才用的速度
        observed: 實際結果；轉彎填「度」，前進後退填「公分」。只是評估速度感覺、沒有量距離時填 0
        note: 使用者的原話或補充，例如「太快」「推不動」「有點打滑」
    """
    if action not in UNITS:
        return f"action 必須是 {list(UNITS)} 其中之一"
    trial = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "action": action, "seconds": float(seconds), "speed": int(float(speed)),
        "observed": float(observed), "unit": UNITS[action], "note": note,
    }
    session_trials.append(trial)
    CAL_DIR.mkdir(exist_ok=True)
    with TRIALS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trial, ensure_ascii=False) + "\n")
    print(f"📝 記錄：{action} {trial['seconds']} 秒、速度 {trial['speed']} → "
          f"{trial['observed']:g} {trial['unit']}  {note}")
    return f"已記錄（這次校正共 {len(session_trials)} 筆）"


def analyze() -> str:
    """用這次校正的紀錄算出公式：實際結果 = 每秒幾度（或公分）× 秒數 + 偏移，並預測轉 90 度要幾秒。
    同一個動作、同一個速度，至少要有 2 種不同秒數的紀錄，公式才可靠。"""
    usable = [t for t in session_trials if t["observed"] > 0]
    results = fit(usable)
    if not results:
        return "還沒有可以計算的紀錄"
    lines = []
    for f in results:
        line = (f"{f['action']} 速度 {f['speed']}：{f['n']} 筆，每秒 {f['rate']:.1f} {f['unit']}，"
                f"偏移 {f['offset']:+.1f}{'' if f['reliable'] else '（只有一種秒數，僅供參考）'}")
        if f["unit"] == "度" and f["rate"] > 0:
            line += f"，轉 90 度約需 {seconds_for(f, 90):.2f} 秒"
        lines.append(line)
    print("📊 " + "\n📊 ".join(lines))
    return "\n".join(lines)


def finish_calibration(move_speed: int, turn_speed: int, summary: str) -> str:
    """校正完成時呼叫：決定之後預設用的速度，寫下學到的經驗，並產生報告和知識檔。
    turn_left 和 turn_right 在 turn_speed 下都必須有至少 2 種不同秒數的紀錄。

    Args:
        move_speed: 之後前進、後退的預設速度（使用者覺得合適的）
        turn_speed: 之後轉彎的預設速度（校正時使用的轉彎速度）
        summary: 用繁體中文條列這次學到的經驗，例如最低能動的速度、左右轉是否不對稱、地板是否打滑
    """
    move_speed, turn_speed = int(float(move_speed)), int(float(turn_speed))
    fits = {(f["action"], f["speed"]): f for f in fit([t for t in session_trials if t["observed"] > 0])}

    missing = [a for a in ("turn_left", "turn_right")
               if not ((a, turn_speed) in fits and fits[(a, turn_speed)]["reliable"]
                       and fits[(a, turn_speed)]["rate"] > 0)]
    if missing:
        return f"資料不足，還不能完成：{missing} 在速度 {turn_speed} 需要至少 2 種不同秒數的紀錄"

    cal = {"updated": datetime.now().isoformat(timespec="seconds"),
           "move_speed": move_speed, "turn_speed": turn_speed}
    for a in ("turn_left", "turn_right"):
        f = fits[(a, turn_speed)]
        cal[a] = {"degrees_per_second": round(f["rate"], 1), "offset_degrees": round(f["offset"], 1),
                  "seconds_for_45": round(seconds_for(f, 45), 2),
                  "seconds_for_90": round(seconds_for(f, 90), 2),
                  "seconds_for_180": round(seconds_for(f, 180), 2)}
    for a in ("move_forward", "move_backward"):
        f = fits.get((a, move_speed))
        if f and f["reliable"] and f["rate"] > 0:
            cal[a] = {"cm_per_second": round(f["rate"], 1), "offset_cm": round(f["offset"], 1)}

    CAL_DIR.mkdir(exist_ok=True)
    CALIBRATION_PATH.write_text(json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
    knowledge = make_knowledge(cal, summary)
    KNOWLEDGE_PATH.write_text(knowledge, encoding="utf-8")
    state["report"] = write_report(cal, summary, knowledge)
    return f"校正完成，已產生報告 {state['report'].name}，並更新 calibration.json 和 knowledge.md"


# ---------- 產生知識檔和報告 ----------
def make_knowledge(cal, summary):
    lines = [
        f"## 校正知識（{cal['updated'][:10]} 實測，由 calibrate.py 自動產生）",
        f"- 前進 / 後退預設速度 {cal['move_speed']}；轉彎預設速度 {cal['turn_speed']}。沒有特別要求就用這些速度。",
        "- 使用者只說「左轉」「右轉」沒有講角度時，就是轉 90 度，直接用下面的秒數。",
    ]
    for a, name in (("turn_left", "左轉"), ("turn_right", "右轉")):
        c = cal[a]
        lines.append(
            f"- {name}（速度 {cal['turn_speed']}）：45 度 ≈ {c['seconds_for_45']} 秒、"
            f"90 度 ≈ {c['seconds_for_90']} 秒、180 度 ≈ {c['seconds_for_180']} 秒。"
            f"其他角度：秒數 = (度數 {minus(c['offset_degrees'])}) ÷ {c['degrees_per_second']}")
    for a, name in (("move_forward", "前進"), ("move_backward", "後退")):
        if a in cal:
            c = cal[a]
            lines.append(f"- {name}（速度 {cal['move_speed']}）："
                         f"秒數 = (公分 {minus(c['offset_cm'])}) ÷ {c['cm_per_second']}")
    lines += [
        f"- 這些公式只在上面的速度成立，換速度角度就不準；單一動作最多 {MAX_MOVE_SECONDS:g} 秒，"
        "超過就分成好幾次呼叫。",
        "- 校正時學到的經驗：",
        summary.strip(),
    ]
    return "\n".join(lines) + "\n"


def write_report(cal=None, summary="", knowledge=""):
    CAL_DIR.mkdir(exist_ok=True)
    path = CAL_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
    out = [
        "# 機器人車校正報告",
        "",
        f"- 時間：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{GEMINI_MODEL}",
        f"- 模式：{'假機器人' if robot.fake else '實體車'}",
        f"- 狀態：{'✅ 完成' if cal else '⚠️ 未完成（中途結束）'}",
        "",
        "## 每一次嘗試",
        "",
        "| # | 動作 | 秒數 | 速度 | 實際結果 | 備註 |",
        "|---|---|---|---|---|---|",
    ]
    for i, t in enumerate(session_trials, 1):
        out.append(f"| {i} | {t['action']} | {t['seconds']} | {t['speed']} | "
                   f"{t['observed']:g} {t['unit']} | {t['note']} |")

    out += ["", "## 計算出的公式（實際結果 = 每秒 × 秒數 + 偏移）", "",
            "| 動作 | 速度 | 筆數 | 每秒 | 偏移 | 可靠 |", "|---|---|---|---|---|---|"]
    for f in fit([t for t in session_trials if t["observed"] > 0]):
        out.append(f"| {f['action']} | {f['speed']} | {f['n']} | {f['rate']:.1f} {f['unit']} | "
                   f"{f['offset']:+.1f} | {'是' if f['reliable'] else '否（只有一種秒數）'} |")

    if cal:
        out += ["", "## 寫入 calibration.json 的數值", "", "```json",
                json.dumps(cal, ensure_ascii=False, indent=2), "```",
                "", "## LLM 的經驗總結", "", summary.strip(),
                "", "## 寫回 system prompt 的知識（knowledge.md）", "", knowledge]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


# ---------- 主程式 ----------
def main():
    robot.fake = "--fake" in sys.argv
    robot.connect()

    client = genai.Client()
    session = client.chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=COACH_PROMPT,
            tools=[move_forward, move_backward, turn_left, turn_right, stop,
                   log_trial, analyze, finish_calibration],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=20),
        ),
    )

    print("🎯 校正模式：照 LLM 的指示觀察車子，用中文回報結果；輸入 q 結束\n")
    try:
        text = "開始校正。先簡短說明流程，然後做第一個測試。"
        while True:
            print(f"AI：{session.send_message(text).text}\n")
            if state["report"]:
                break
            text = input("你：").strip()
            if text.lower() in ("q", "quit", "exit"):
                break
            if not text:
                text = "（使用者沒有輸入，請再問一次剛才的問題）"
    except (KeyboardInterrupt, EOFError):
        print("\n⛔ 已中止")
    finally:
        robot.disconnect()
        if not state["report"] and session_trials:
            state["report"] = write_report()   # 中途結束也保留報告

    if state["report"]:
        print(f"\n📄 報告：{state['report']}")
        if KNOWLEDGE_PATH.exists() and CALIBRATION_PATH.exists():
            print("🧠 已更新 calibration.json 和 knowledge.md，下次執行 agent.py 會自動使用")
        print("👉 把報告檔案交給 Claude 檢查")


if __name__ == "__main__":
    main()
