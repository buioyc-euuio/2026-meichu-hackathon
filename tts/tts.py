"""
文字轉語音：用 edge-tts（微軟的中文神經語音）念出來，再用 ffmpeg 加速（預設 3 倍），聽起來很急。

    .venv/bin/python tts/tts.py                          # 「救命呀！」3 倍速 → tts/output/救命呀.wav
    .venv/bin/python tts/tts.py --play                   # 產生完直接播
    .venv/bin/python tts/tts.py "有人跌倒了" --speed 2    # 別的句子、別的速度
    .venv/bin/python tts/tts.py --repeat 3               # 連喊三次
    .venv/bin/python tts/tts.py --voice zh-TW-YunJheNeural   # 男生的聲音（edge-tts --list-voices 看全部）

edge-tts 要網路（只有產生的時候要）：先產生好 wav 放著，demo 現場播檔案就不用網路。
加速用 ffmpeg 的 atempo：音調不變，只是講得快（不會變成花栗鼠的聲音）。
「喊」：音調調高一點、音量拉大（edge-tts 免費版不能選情緒風格）。

安裝（只要一次）：uv pip install --python .venv/bin/python edge-tts
"""

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "output"

# ====== 想改的東西都在這裡 ======
TEXT = "救命呀！"
VOICE = "zh-TW-HsiaoChenNeural"   # 台灣女聲；zh-TW-YunJheNeural 是台灣男聲
SPEED = 3.0                       # 念完之後再加速幾倍（音調不變）
PITCH = "+30Hz"                   # 音調調高 → 聽起來比較像在喊
VOLUME = "+50%"                   # edge-tts 的音量
GAIN_DB = 6                       # ffmpeg 再放大幾 dB（有限幅，不會爆音）
# ================================


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def atempo_chain(speed):
    """atempo 一段最多 2 倍（舊版 ffmpeg）→ 拆成好幾段相乘：3 = 2 × 1.5。"""
    parts = []
    while speed > 2.0:
        parts.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        parts.append(0.5)
        speed /= 0.5
    parts.append(speed)
    return ",".join(f"atempo={p:.6g}" for p in parts)


async def synthesize(text, voice, pitch, volume, path):
    import edge_tts
    await edge_tts.Communicate(text, voice, pitch=pitch, volume=volume).save(str(path))


def make(text, out, voice=VOICE, speed=SPEED, repeat=1, pitch=PITCH, volume=VOLUME, gain_db=GAIN_DB):
    if not shutil.which("ffmpeg"):
        raise SystemExit("❌ 找不到 ffmpeg")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.mp3"
        log(f"🗣️  edge-tts（{voice}）：{text}")
        asyncio.run(synthesize(text, voice, pitch, volume, raw))
        filters = [
            # 去掉頭尾的靜音（加速之後靜音佔比更大，會覺得反應慢）
            "silenceremove=start_periods=1:start_threshold=-45dB",
            "areverse", "silenceremove=start_periods=1:start_threshold=-45dB", "areverse",
            atempo_chain(speed),
            f"volume={gain_db}dB", "alimiter=limit=0.95",
        ]
        if repeat > 1:
            filters.append(f"aloop=loop={repeat - 1}:size=2147483647")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-af", ",".join(filters),
                        "-ar", "44100", "-ac", "1", str(out)], check=True)
    seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(out)], capture_output=True, text=True, check=True).stdout)
    log(f"✅ {out}（{speed:g} 倍速、{seconds:.2f} 秒）")
    return out


def play(path):
    if shutil.which("ffplay"):
        subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)], check=False)
    elif shutil.which("paplay"):
        subprocess.run(["paplay", str(path)], check=False)
    else:
        raise SystemExit("❌ 找不到播放器（ffplay / paplay）")


def main():
    parser = argparse.ArgumentParser(description="文字轉語音，加速讓它聽起來很急")
    parser.add_argument("text", nargs="?", default=TEXT, help=f"要念的句子（預設「{TEXT}」）")
    parser.add_argument("--speed", type=float, default=SPEED, help="加速幾倍（音調不變）")
    parser.add_argument("--voice", default=VOICE, help="edge-tts 的聲音")
    parser.add_argument("--repeat", type=int, default=1, help="連續念幾次")
    parser.add_argument("--out", type=Path, help="輸出檔（預設 tts/output/<句子>.wav）")
    parser.add_argument("--play", action="store_true", help="產生完直接播")
    args = parser.parse_args()
    if args.speed <= 0 or args.repeat < 1:
        parser.error("--speed 要 > 0、--repeat 要 ≥ 1")
    name = "".join(c for c in args.text if c.isalnum()) or "tts"
    out = args.out or OUT_DIR / f"{name}.wav"
    make(args.text, out, voice=args.voice, speed=args.speed, repeat=args.repeat)
    if args.play:
        play(out)


if __name__ == "__main__":
    main()
