"""
看 chase.py 的 log：設定、各狀態比例、事件、狀態／輪子變化的時間軸，以及常見問題的提示。

    .venv/bin/python ball_chase/analyze_log.py                        # 最新一份
    .venv/bin/python ball_chase/analyze_log.py ball_chase/logs/xxx.jsonl
    .venv/bin/python ball_chase/analyze_log.py --all                  # 時間軸不省略
"""

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"


def fmt(value, spec):
    return "?" if value is None else format(value, spec)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="log 檔（預設最新一份）")
    parser.add_argument("--all", action="store_true", help="時間軸全部列出（預設最多 120 行）")
    args = parser.parse_args()
    path = Path(args.path) if args.path else max(LOG_DIR.glob("chase_*.jsonl"), default=None,
                                                  key=lambda p: p.stat().st_mtime)
    if path is None or not path.exists():
        sys.exit("找不到 log（ball_chase/logs/chase_*.jsonl）")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    config = next((r for r in records if r["type"] == "config"), {})
    frames = [r for r in records if r["type"] == "frame"]
    events = [r for r in records if r["type"] == "event"]
    print(f"📄 {path.name}")
    if config:
        target = f"目標直徑 {config['target_d']}px" if config.get("target_d") else "目標 = 啟動時第一顆球（見事件）"
        print(f"   micro:bit {config.get('device')}  {target}  fake={config.get('args', {}).get('fake')}"
              f"  max_speed={config.get('args', {}).get('max_speed')}")
    if not frames:
        print("   沒有任何畫面紀錄")
        return
    duration = frames[-1]["t"] - frames[0]["t"]
    states = collections.Counter(("miss" if f["reason"] == "miss" else f["state"]) for f in frames)
    print(f"   {len(frames)} 張、{duration:.1f} 秒（約 {len(frames) / max(duration, 0.01):.1f} fps）")
    print("   狀態：" + "、".join(f"{k} {100 * v / len(frames):.0f}%" for k, v in states.most_common()))

    seen = [f for f in frames if f["ball"] and f["ball"]["misses"] == 0]
    sources = collections.Counter(f["ball"]["source"] for f in seen)
    with_dist = [f for f in frames if f["ball"] and f["ball"]["dist_err"] is not None]
    print(f"   真的看到球 {100 * len(seen) / len(frames):.0f}%（{dict(sources)}）；有距離的 {100 * len(with_dist) / len(frames):.0f}%")
    if with_dist:
        cms = [f["ball"]["dist_cm_est"] for f in with_dist if f["ball"]["dist_cm_est"]]
        if cms:
            print(f"   估計距離 {min(cms)}～{max(cms)} cm（中位數 {statistics.median(cms):.0f}）")

    # 常見問題提示
    moving = [f for f in frames if any(f["want"])]
    flips = sum(1 for a, b in zip(frames, frames[1:])
                if a["want"][0] * b["want"][0] < 0 or a["want"][1] * b["want"][1] < 0)
    stops = sum(1 for a, b in zip(frames, frames[1:]) if any(a["want"]) and not any(b["want"]))
    print(f"   要求動的畫面 {100 * len(moving) / len(frames):.0f}%；從動到停 {stops} 次；輪子方向反轉 {flips} 次")
    tips = []
    if states.get("lost", 0) > 0.3 * len(frames):
        tips.append("lost 很多：球常常沒被看到（光線、太遠、跑出畫面？）")
    if states.get("miss", 0) > 0.3 * len(frames):
        tips.append("miss 很多：YOLO 常漏抓，靠 LOST_GRACE 撐著")
    if flips > max(3, duration / 5):
        tips.append("輪子方向常反轉：可能來回晃（衝過頭），調小 TURN_GAIN／FORWARD_GAIN 或加大 ARRIVE_BAND")
    if len(with_dist) < 0.3 * max(len(seen), 1):
        tips.append("距離常算不出來：球常被畫面邊緣切到或只有 YOLO 框（見 source）")
    for tip in tips:
        print(f"   💡 {tip}")

    if events:
        print("\n📢 事件")
        for e in events:
            print(f"   {e['t']:7.2f}s  {e['message']}")

    print("\n⏱️  時間軸（狀態或輪子改變時；miss = 短暫漏抓、維持原動作）")
    prev, shown = None, 0
    for f in frames:
        state = "miss" if f["reason"] == "miss" else f["state"]
        key = (state, tuple(f["want"]))
        if key == prev:
            continue
        prev = key
        b = f["ball"]
        ball = "沒有球" if b is None else (
            f"角度 {fmt(b['angle_deg'], '+5.1f')}° d {fmt(b['d'], '5.1f')} 中位 {fmt(b['d_median'], '5.1f')} "
            f"誤差 {fmt(b['dist_err'], '+.2f')} 距離×{fmt(b.get('dist_ratio'), '')} {b['source']}")
        print(f"   {f['t']:7.2f}s [{state:>8}] 要 {f['want']} 送 {f['sent']}  {ball}")
        shown += 1
        if shown >= 120 and not args.all:
            print("   …（--all 看全部）")
            break

    summary = next((r for r in records if r["type"] == "summary"), None)
    if summary is None:
        print("\n⚠️  沒有 summary：程式可能不是正常結束的（當掉或被強制關掉）")


if __name__ == "__main__":
    main()
