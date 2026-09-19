"""
約 20 秒的求救廣播（中文）：一句一句用 edge-tts 念，每句自己的音調、音量，句子之間停頓，全部 1.3 倍速。
第三階段（e2e/fall_alert.py）的警報聲。

    .venv/bin/python tts/help_me.py              # → tts/output/求救廣播.wav
    .venv/bin/python tts/help_me.py --play       # 產生完直接播
    .venv/bin/python tts/help_me.py --speed 1.5  # 換速度

改台詞、語氣、停頓：改下面的 SCRIPT。edge-tts 免費版不能選情緒風格，「生動」靠每句不同的音調、音量和停頓。
產生的時候要網路；產生好的 wav 放著，demo 現場播檔案就不用網路。
"""

import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tts import OUT_DIR, VOICE, atempo_chain, log, play, synthesize  # noqa: E402

# ====== 想改的東西都在這裡 ======
SPEED = 1.3              # 全部句子的速度（音調不變）；停頓不加速
# (台詞, 音調 Hz, 音量 dB, 念完停幾秒)
#   音調越高越像在尖叫；音量是跟其他句比（最後整段會再拉到一樣大、有限幅）
SCRIPT = [
    ("救命呀！", +45, 3, 0.3),                               # 突然發現，最大聲
    ("救命！救命！救命啊！", +40, 2, 0.1),                     # 短促連續，很慌
    ("有人倒在地上了！", +30, 1, 0.1),                         # 說明狀況
    ("他一動也不動！叫他也沒有反應！", +35, 1, 0.4),             # 越說越害怕
    ("有沒有人啊？快來人啊！", +40, 2, 0.1),                    # 對四周大喊
    ("拜託！誰來幫幫忙！", +45, 1, 0.1),                       # 焦急、帶哭腔
    ("那邊的大哥、大姐，拜託過來一下！", +30, 2, 0.3),           # 直接點名路人
    ("快打一一九！叫救護車！", +20, 3, 0.1),                   # 堅定、下指令
    ("有沒有人會 CPR？", +35, 1, 0.1),                        # 急著找人
    ("誰去拿 AED！快一點！", +40, 2, 0.4),                     # 催促
    ("撐著點！救護車馬上就來了！", +10, 0, 0.2),                # 轉向傷者，安撫但很急
    ("救命啊！快來人啊！", +50, 3, 0.0),                       # 最後再喊一次，最大聲收尾
]
SAMPLE_RATE = 44100
# ================================


def run_ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def make(out, voice=VOICE, speed=SPEED):
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, (text, pitch, gain, pause) in enumerate(SCRIPT, 1):
            raw, part = tmp / f"{i}.mp3", tmp / f"{i}.wav"
            log(f"🗣️  {i:2}/{len(SCRIPT)} {text}")
            asyncio.run(synthesize(text, voice, f"{pitch:+d}Hz", "+50%", raw))
            trim = "silenceremove=start_periods=1:start_threshold=-45dB"
            run_ffmpeg("-i", str(raw), "-af", f"{trim},areverse,{trim},areverse,{atempo_chain(speed)},volume={gain}dB,"
                       f"apad=pad_dur={pause}", "-ar", str(SAMPLE_RATE), "-ac", "1", str(part))
            parts.append(part)
        inputs = [arg for p in parts for arg in ("-i", str(p))]
        joined = "".join(f"[{i}:a]" for i in range(len(parts)))
        # 接起來 → 整段拉到一樣響（loudnorm）→ 限幅，不爆音
        run_ffmpeg(*inputs, "-filter_complex", f"{joined}concat=n={len(parts)}:v=0:a=1,loudnorm=I=-12:TP=-1,"
                   f"alimiter=limit=0.95[out]", "-map", "[out]", "-ar", str(SAMPLE_RATE), "-ac", "1", str(out))
    seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(out)], capture_output=True, text=True, check=True).stdout)
    log(f"✅ {out}（{len(SCRIPT)} 句、{speed:g} 倍速、{seconds:.1f} 秒）")
    return out


def main():
    parser = argparse.ArgumentParser(description="約 20 秒的中文求救廣播")
    parser.add_argument("--speed", type=float, default=SPEED, help="速度幾倍（音調不變）")
    parser.add_argument("--voice", default=VOICE, help="edge-tts 的聲音")
    parser.add_argument("--out", type=Path, default=OUT_DIR / "求救廣播.wav", help="輸出檔")
    parser.add_argument("--play", action="store_true", help="產生完直接播")
    args = parser.parse_args()
    if args.speed <= 0:
        parser.error("--speed 要 > 0")
    make(args.out, voice=args.voice, speed=args.speed)
    if args.play:
        play(args.out)


if __name__ == "__main__":
    main()
