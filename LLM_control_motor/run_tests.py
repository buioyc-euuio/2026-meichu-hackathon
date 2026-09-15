"""
自動測試：LLM 能不能用正確的工具控制機器人

執行（在專案最外層）：
    .venv/bin/python LLM控制馬達實作/run_tests.py --gemini         # 假機器人：不連藍牙、不會動、不等秒數
    .venv/bin/python LLM控制馬達實作/run_tests.py --gemini --real  # 實體測試：連 micro:bit，馬達真的會動
    .venv/bin/python LLM控制馬達實作/run_tests.py                  # 改測本地 llama
    .venv/bin/python LLM控制馬達實作/run_tests.py --list           # 只列出所有情境，可手動貼到 agent.py 測
    .venv/bin/python LLM控制馬達實作/run_tests.py --gemini 7 11    # 只跑第 7、11 題

實體測試（--real）：
    每題開始前會等你按 Enter（先把車子擺好、周圍淨空），輸入 s 跳過、q 結束。
    每題做完會問你「實際動作對不對」，同時統計「工具呼叫正確」和「實體動作正確」。
    建議第一次先把車子架高（輪子懸空）測試方向。
"""

import sys

from agent import make_chat
from robot_bluetooth import robot

MOVES = {"move_forward", "move_backward", "turn_left", "turn_right", "drive"}


# ---------- 檢查用的小工具 ----------
def names(calls):
    return [n for n, _ in calls]


def first(calls, name):
    return next((a for n, a in calls if n == name), None)


def pans(calls):
    return [float(a["angle"]) for n, a in calls if n == "camera_pan"]


def in_order(calls, expected):
    """expected 裡的工具有沒有依照順序出現（中間可以夾別的）。"""
    it = iter(names(calls))
    return all(any(n == e for n in it) for e in expected)


def near(value, target, tol=0.01):
    return abs(float(value) - target) <= tol


# ---------- 測試情境 ----------
SCENARIOS = [
    # 基本動作
    (["往前走"],
     "呼叫 move_forward",
     lambda c: names(c)[:1] == ["move_forward"]),
    (["用慢速往後退 2 秒"],
     "move_backward，seconds=2，speed 比預設 180 小",
     lambda c: first(c, "move_backward") is not None
     and near(first(c, "move_backward")["seconds"], 2)
     and float(first(c, "move_backward")["speed"]) < 180),
    (["車子向右轉"],
     "turn_right，不能左轉",
     lambda c: "turn_right" in names(c) and "turn_left" not in names(c)),
    (["停！"],
     "只呼叫 stop",
     lambda c: names(c) == ["stop"]),

    # 相機
    (["鏡頭看最左邊"],
     "camera_pan(180)",
     lambda c: pans(c)[-1:] == [180]),
    (["把鏡頭轉回正前方"],
     "camera_pan(90)",
     lambda c: pans(c)[-1:] == [90]),
    (["鏡頭往上抬一點點"],
     "camera_up，而且輪子不動",
     lambda c: "camera_up" in names(c) and not MOVES & set(names(c))),
    (["相機往右轉，但車子不要動"],
     "camera_pan 角度 < 90（0 是最右），輪子不動（分辨「車子轉」和「鏡頭轉」）",
     lambda c: pans(c) and pans(c)[-1] < 90 and not MOVES & set(names(c))),

    # 組合與順序
    (["先往前走 1 秒，再原地左轉，最後把鏡頭轉向右邊"],
     "依序 move_forward → turn_left → camera_pan(<90)",
     lambda c: in_order(c, ["move_forward", "turn_left", "camera_pan"]) and pans(c)[-1] < 90),
    (["往前 1 秒，停 2 秒，再後退 1 秒"],
     "依序 move_forward → wait(2) → move_backward",
     lambda c: in_order(c, ["move_forward", "wait", "move_backward"])
     and near(first(c, "wait")["seconds"], 2)),
    (["鏡頭往下看，然後全速往前衝 1 秒"],
     "camera_down → move_forward(speed≥250)",
     lambda c: in_order(c, ["camera_down", "move_forward"])
     and float(first(c, "move_forward")["speed"]) >= 250),

    # 需要理解的指令
    (["一邊往前走一邊慢慢往左彎，持續 2 秒"],
     "drive，左右都前進且右輪比左輪快",
     lambda c: first(c, "drive") is not None
     and 0 <= float(first(c, "drive")["left_speed"]) < float(first(c, "drive")["right_speed"])),
    (["幫我用鏡頭左右掃視一下環境，看完回到正前方"],
     "camera_pan 至少 3 次：有看左（>90）、有看右（<90）、最後回到 90",
     lambda c: len(pans(c)) >= 3 and min(pans(c)) < 90 < max(pans(c)) and pans(c)[-1] == 90),
    (["走一個正方形"],
     "前進 ≥4 次，同方向轉彎 ≥3 次",
     lambda c: names(c).count("move_forward") >= 4
     and max(names(c).count("turn_left"), names(c).count("turn_right")) >= 3),
    (["左邊好像有東西，轉過去看看"],
     "往左：turn_left 或 camera_pan(>90)，不能往右",
     lambda c: ("turn_left" in names(c) or any(p > 90 for p in pans(c)))
     and "turn_right" not in names(c) and not any(p < 90 for p in pans(c))),

    # 多輪對話（記得上一句）
    (["把鏡頭轉到 45 度", "鏡頭再往右轉 30 度"],
     "最後 camera_pan(15)（往右是減角度）",
     lambda c: pans(c)[-1:] == [15]),

    # 不該動的情況
    (["你好，你是誰？"],
     "不呼叫任何工具",
     lambda c: names(c) in ([], ["get_status"])),
    (["幫我把車燈打開"],
     "沒有這個功能，不呼叫移動工具",
     lambda c: not MOVES & set(names(c)) and "camera_pan" not in names(c)),
    (["鏡頭轉到 270 度"],
     "超出範圍：不能送出 >180 的角度，或直接說明做不到",
     lambda c: all(p <= 180 for p in pans(c))),
]


def main():
    use_gemini = "--gemini" in sys.argv
    picked = [int(a) for a in sys.argv[1:] if a.isdigit()]

    if "--list" in sys.argv:
        for i, (prompts, expect, _) in enumerate(SCENARIOS, 1):
            print(f"{i:2}. {' → '.join(prompts)}\n    預期：{expect}")
        return

    real = "--real" in sys.argv
    robot.fake = not real
    robot.fast = not real   # 實體測試要真的等秒數，馬達才會轉
    results = []            # (題號, prompts, 工具呼叫正確, 實體動作正確 True/False/None)

    if real:
        print("⚠️  實體測試：馬達會真的動！請確認周圍淨空，第一次建議把車子架高。")
        robot.connect()

    try:
        for i, (prompts, expect, check) in enumerate(SCENARIOS, 1):
            if picked and i not in picked:
                continue
            print(f"\n━━━━ 第 {i} 題 ━━━━  預期：{expect}")

            if real:
                answer = input(f"   「{' → '.join(prompts)}」  Enter 開始 / s 跳過 / q 結束：").strip().lower()
                if answer == "q":
                    break
                if answer == "s":
                    continue
                robot.send("S#")
                robot.send("P,90#")   # 每題開始前：停車、相機回正
                robot.sleep(0.5)

            robot.calls.clear()
            robot.pan_angle = 90
            chat = make_chat(use_gemini)   # 每題都是新對話
            try:
                for p in prompts:
                    print(f"你：{p}")
                    print(f"AI：{chat(p)}")
                ok = bool(check(robot.calls))
            except Exception as e:
                print(f"⚠️ 發生錯誤：{e}")
                ok = False
            finally:
                if real:
                    robot.send("S#")   # 不管成功或出錯，都先停車
            print("✅ 工具呼叫正確" if ok else "❌ 工具呼叫不正確")

            physical = None
            if real:
                seen = input("   實際動作對嗎？ y 對 / n 不對 / Enter 不確定：").strip().lower()
                physical = {"y": True, "n": False}.get(seen)
            results.append((i, prompts, ok, physical))
    except KeyboardInterrupt:
        print("\n⛔ 已中止")
    finally:
        if real:
            robot.disconnect()   # 會先送 S# 停車再斷線

    passed = sum(ok for _, _, ok, _ in results)
    print(f"\n════ 工具呼叫：{passed} / {len(results)} 正確 ════")
    if real:
        judged = [r for r in results if r[3] is not None]
        print(f"════ 實體動作：{sum(r[3] for r in judged)} / {len(judged)} 正確（你有判斷的題目）════")
    for i, prompts, ok, physical in results:
        marks = []
        if not ok:
            marks.append("工具呼叫錯")
        if physical is False:
            marks.append("實體動作錯")
        if marks:
            print(f"❌ 第 {i} 題（{'、'.join(marks)}）：{' → '.join(prompts)}")


if __name__ == "__main__":
    main()
