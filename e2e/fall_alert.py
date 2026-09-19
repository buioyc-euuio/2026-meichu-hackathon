"""
第三階段：有人躺在地上 → 喊「救命呀！」（tts/ 產生的 3 倍速語音）；有 MP4 的話同時播影片。

讀 tennis_tracking/server.py 的「人」（YOLO person 的框），用框的形狀判斷：
    站著的人框是直的（高 > 寬），躺著的人框是橫的（寬 > 高）。
    框的寬 / 高 ≥ LYING_ASPECT、而且框夠大（不是遠處的小框）、連續 LYING_HOLD 秒都這樣 → 算「躺著」→ 警報。
    播完等 COOLDOWN 秒才會再觸發（人還躺著也不會一直重播）。

不連 micro:bit、不動馬達；只需要 tracking server 在跑。

    .venv/bin/python tennis_tracking/server.py                 # 1. 追蹤伺服器
    .venv/bin/python e2e/fall_alert.py                         # 2. 開始看，有人躺下就喊救命
    .venv/bin/python e2e/fall_alert.py --sound tts/output/救命呀.wav   # 換聲音（只喊一次的版本）
    .venv/bin/python e2e/fall_alert.py --video 某個.mp4 --once  # 喊 + 播影片，一次就結束（demo 用）
    .venv/bin/python e2e/fall_alert.py --test-play             # 不偵測，直接警報一次（先確認喇叭／播放器 OK）

警報聲：預設 tts/output/救命呀x3.wav（沒有的話先跑 .venv/bin/python tts/tts.py --repeat 3 --out tts/output/救命呀x3.wav）。
影片：e2e/media/alert.mp4 存在就跟聲音同時播（全螢幕）；不存在就只喊。

執行中直接按鍵（不用 Enter）：a 手動觸發警報（馬上播一次，demo 時偵測失靈的備案）、q 離開。
（不用 p：demo.py 的 n / p 是換階段）
"""

import argparse
import asyncio
import shutil
import sys
import termios
import threading
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "camera_control"))

from ball_center import SERVER_URL, STALE_SECONDS, Feed, start_stdin_reader  # noqa: E402
from robot_ble import log  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOUND = ROOT / "tts" / "output" / "救命呀x3.wav"
DEFAULT_VIDEO = ROOT / "e2e" / "media" / "alert.mp4"

# ====== 想改的東西都在這裡 ======
LYING_ASPECT = 1.3       # 框的寬 / 高 至少這樣才算「橫的」（站著通常 < 0.8，蹲／坐約 0.8~1.2）
MIN_WIDTH = 0.20         # 框的寬至少佔畫面寬度這個比例（太小 = 太遠或誤判，不算）
MIN_CONF = 0.35          # YOLO 對這個人的信心至少多少
LYING_HOLD = 1.0         # 連續躺著這麼多秒才觸發（過濾一兩張畫面的誤判、走過去彎腰）
MISS_GRACE = 0.5         # YOLO 偶爾漏抓：這麼久內沒看到「躺著」還不算中斷
COOLDOWN = 10.0          # 播完之後這麼多秒內不再觸發
STATUS_INTERVAL = 0.5
TRIGGER_KEY = "a"        # 手動觸發警報（alert）
# ================================
HINT = f"👉 按 {TRIGGER_KEY} = 手動觸發警報（馬上喊救命）、q = 離開"


def is_lying(person, width):
    """這個人的框看起來是不是躺著：這張真的看到、信心夠、框夠大、而且是橫的。"""
    x1, y1, x2, y2 = person["box"]
    w, h = x2 - x1, y2 - y1
    return (person["misses"] == 0 and person.get("conf", 0) >= MIN_CONF
            and h > 0 and w / h >= LYING_ASPECT and w / width >= MIN_WIDTH)


class LyingDetector:
    """每張畫面丟進來；連續躺著夠久就回傳 True（一次觸發只回傳一次，之後要 reset）。"""

    def __init__(self, hold=LYING_HOLD, grace=MISS_GRACE):
        self.hold, self.grace = hold, grace
        self.since = None          # 從什麼時候開始一直躺著
        self.last_seen = None      # 最後一次看到躺著
        self.last_lying = []       # 最後一次看到躺著的人（觸發時這張可能剛好漏抓，印 log 用這個）

    def update(self, people, width, now):
        lying = [p for p in people if is_lying(p, width)]
        if lying:
            self.last_lying = lying
            if self.since is None or now - self.last_seen > self.grace:
                self.since = now
            self.last_seen = now
        elif self.since is not None and now - self.last_seen > self.grace:
            self.since = None
        return lying, self.held(now) >= self.hold

    def held(self, now):
        return 0.0 if self.since is None else now - self.since

    def reset(self):
        self.since = self.last_seen = None
        self.last_lying = []


def player_command(video, windowed=False):
    """找得到的播放器：ffplay 優先（播完自動關），沒有就用 VLC。"""
    if shutil.which("ffplay"):
        return ["ffplay", "-autoexit", "-loglevel", "error", "-window_title", "alert",
                *([] if windowed else ["-fs"]), str(video)]
    if shutil.which("cvlc"):
        return ["cvlc", "--play-and-exit", "--quiet", *([] if windowed else ["--fullscreen"]), str(video)]
    raise SystemExit("❌ 找不到播放器：請安裝 ffmpeg（ffplay）或 vlc")


def sound_command(sound):
    """只放聲音、不開視窗。"""
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(sound)]
    if shutil.which("paplay"):
        return ["paplay", str(sound)]
    if shutil.which("cvlc"):
        return ["cvlc", "--play-and-exit", "--quiet", str(sound)]
    raise SystemExit("❌ 找不到播放器：請安裝 ffmpeg（ffplay）")


async def run_player(cmd):
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL)
    try:
        await proc.wait()
    finally:
        if proc.returncode is None:      # 被取消（q / Ctrl-C）：播放器也關掉
            proc.terminate()
            await proc.wait()


async def play(sound, video=None, windowed=False):
    """警報：喊救命；有影片就同時播。兩個都播完才回來。"""
    log(f"📢 救命呀！（{sound.name}" + (f" + {video.name}" if video else "") + "）")
    players = [run_player(sound_command(sound))]
    if video:
        players.append(run_player(player_command(video, windowed)))
    await asyncio.gather(*players)
    log("📢 警報結束")


def start_key_reader(loop, queue):
    """終端機：一個鍵一個鍵讀（按 a 馬上觸發，不用 Enter）；不是終端機（被 pipe）就一行一行讀。
    回傳還原終端機的函式。"""
    if not sys.stdin.isatty():
        start_stdin_reader(loop, queue)
        return lambda: None
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)                    # 保留 ISIG：Ctrl-C 照常能用

    def run():
        while True:
            key = sys.stdin.read(1)
            loop.call_soon_threadsafe(queue.put_nowait, key or "q")   # EOF（Ctrl-D）= q
            if not key:
                return
    threading.Thread(target=run, daemon=True).start()
    return lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)


async def run(args):
    sound = Path(args.sound)
    if not sound.is_file():
        raise SystemExit(f"❌ 找不到警報聲 {sound}（先跑 .venv/bin/python tts/tts.py --repeat 3 --out {sound}）")
    if args.video:
        video = Path(args.video)
        if not video.is_file():
            raise SystemExit(f"❌ 找不到影片 {video}")
    else:
        video = DEFAULT_VIDEO if DEFAULT_VIDEO.is_file() else None
    sound_command(sound)                 # 先確認有播放器，不要等到有人躺下才發現
    if video:
        player_command(video)
    if args.test_play:
        await play(sound, video, args.windowed)
        return

    feed = Feed()
    receiver = asyncio.create_task(feed.receive_forever(args.url))
    commands = asyncio.Queue()
    restore_terminal = start_key_reader(asyncio.get_running_loop(), commands)
    detector = LyingDetector(hold=args.hold)
    cooldown_until, last_status, stale_logged = 0.0, 0.0, False
    log(f"👀 看有沒有人躺在地上（框 寬/高 ≥ {LYING_ASPECT}、連續 {args.hold:g} 秒）→ 喊 {sound.name}" + (f" + 播 {video.name}" if video else "（沒有 e2e/media/alert.mp4，只喊不播影片）"))
    log(HINT)
    try:
        while True:
            try:
                await asyncio.wait_for(feed.event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            feed.event.clear()
            manual = False
            while not commands.empty():
                cmd = commands.get_nowait().lower()
                if cmd == "q":
                    return
                if cmd == TRIGGER_KEY:
                    manual = True
                elif cmd.strip():
                    log(HINT)

            now = time.monotonic()
            data = feed.latest
            if data is None or now - feed.received_at > STALE_SECONDS:
                if not stale_logged:
                    log("⚠️  沒收到 tennis_tracking 的資料（server 有開嗎？）")
                    stale_logged = True
                detector.reset()
                data = None
            elif stale_logged:
                log("✅ 又收到 tennis_tracking 的資料")
                stale_logged = False

            lying, triggered = ([], False) if data is None else detector.update(data["people"], data["width"], now)
            if now < cooldown_until:
                triggered = False
            if triggered or manual:
                if manual:
                    log("🚨 手動觸發警報")
                elif triggered:
                    p = detector.last_lying[0]
                    x1, y1, x2, y2 = p["box"]
                    log(f"🚨 有人躺在地上！（人 #{p['id']} 框 寬/高 {(x2 - x1) / (y2 - y1):.2f}、"
                        f"信心 {p.get('conf', 0):.2f}、持續 {detector.held(now):.1f} 秒）")
                await play(sound, video, args.windowed)
                detector.reset()
                cooldown_until = time.monotonic() + args.cooldown
                if args.once:
                    return
                continue

            if data is not None and now - last_status >= STATUS_INTERVAL:
                shapes = ", ".join(f"#{p['id']} {(p['box'][2] - p['box'][0]) / max(p['box'][3] - p['box'][1], 1):.2f}"
                                   for p in data["people"] if p["misses"] == 0) or "沒有人"
                state = f"躺著 {detector.held(now):.1f}s" if lying else "—"
                cool = f"  （冷卻 {cooldown_until - now:.0f}s）" if now < cooldown_until else ""
                log(f"[fall] 人（寬/高）：{shapes}  |  {state}{cool}  |  按 {TRIGGER_KEY} 手動觸發")
                last_status = now
    finally:
        receiver.cancel()
        restore_terminal()


def main():
    parser = argparse.ArgumentParser(description="有人躺在地上就喊救命（讀 tennis_tracking server 的人框）")
    parser.add_argument("--url", default=SERVER_URL, help="tennis_tracking server 的 WebSocket 網址")
    parser.add_argument("--sound", default=str(DEFAULT_SOUND), help="警報聲（tts/tts.py 產生的）")
    parser.add_argument("--video", help="同時播的影片（預設：e2e/media/alert.mp4 有的話就播）")
    parser.add_argument("--hold", type=float, default=LYING_HOLD, help="連續躺著幾秒才觸發")
    parser.add_argument("--cooldown", type=float, default=COOLDOWN, help="播完幾秒內不再觸發")
    parser.add_argument("--once", action="store_true", help="播一次就結束")
    parser.add_argument("--windowed", action="store_true", help="影片不要全螢幕")
    parser.add_argument("--test-play", action="store_true", help="不偵測，直接警報一次")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    log("🔚 結束")


if __name__ == "__main__":
    main()
