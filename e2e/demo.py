"""
Demo 總控：像投影片一樣，n 下一個階段、p 上一個階段。每個階段是原本的程式（子程序），
同一時間只有一個在連 micro:bit（換階段時先停掉舊的、等它停車斷線，才開新的）。

    階段 1  voice  語音叫車子動           robot_control/main.py
    階段 2  chase  車子跟著網球走         ball_chase/chase.py
    階段 3  fall   有人躺在地上就喊救命    e2e/fall_alert.py

tracking server（tennis_tracking/server.py）一開始就在背景開好，整個 demo 都開著，結束時關掉
（已經有一個在跑就直接用、不會關它）。

    .venv/bin/python e2e/demo.py --fake                     # 不連 micro:bit，走一遍流程
    .venv/bin/python e2e/demo.py                            # 真的跑
    .venv/bin/python e2e/demo.py --stage 2                  # 按 n 從第 2 階段開始
    .venv/bin/python e2e/demo.py --voice-args="--bluetooth-mic --llm --allow-motion" \\
                                 --tracker-args="--camera 2" --fall-args="--sound tts/output/救命呀.wav"
    （給子程式的參數要用 --xxx-args="..." 有等號的寫法，不然只有一個參數時 argparse 會當成 demo.py 自己的選項）

按鍵（任何時候）：n 下一個階段、p 上一個階段
    階段中：其他按鍵都照常傳給那個程式（voice 的 w/a/s/d、q；chase 的 q + Enter；fall 的 a、q …）；
            Ctrl-C 只結束目前的階段。chase 的暫停原本是 p → 在 demo 裡改按空白鍵
            fall 階段按 a = 手動觸發警報（馬上喊救命）
    階段之間（程式自己結束了）：n / p、r 重跑、1/2/3 直接跳、q 結束 demo
"""

import argparse
import fcntl
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(__file__).resolve().parent / "logs"
TRACKER_HEALTH = "http://127.0.0.1:8000/health"
TRACKER_START_TIMEOUT = 60.0    # 開相機 + 第一次轉 ncnn 模型可能要幾十秒
STOP_TIMEOUT = 5.0              # 換階段：送 Ctrl-C 後等多久讓它停車、斷藍牙，還沒結束就強制關


@dataclass
class Stage:
    key: str
    title: str
    script: str
    needs_tracker: bool
    fake_flag: bool             # --fake 要不要傳給這支程式（fall 不連車，沒有 --fake）
    how_to_leave: str
    remap: dict = field(default_factory=dict)   # demo 裡按的鍵 → 實際送給程式的


STAGES = [
    Stage("voice", "語音叫車子動", "robot_control/main.py", False, True, "q 或 Ctrl-C"),
    Stage("chase", "車子跟著網球走", "ball_chase/chase.py", True, True, "q + Enter 或 Ctrl-C；空白鍵 暫停／繼續",
          remap={b" ": b"p\n"}),
    Stage("fall", "有人躺在地上就喊救命", "e2e/fall_alert.py", True, False, "q 或 Ctrl-C；👉 a = 手動觸發警報"),
]


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def tracker_up():
    try:
        with urllib.request.urlopen(TRACKER_HEALTH, timeout=0.5) as response:
            return response.status == 200
    except OSError:
        return False


class Tracker:
    """tennis_tracking/server.py：demo 一開始就開在背景，demo 結束時關掉（別人開的不關）。"""

    def __init__(self, extra_args):
        self.extra_args = extra_args
        self.proc = None
        self.log_file = None

    def start(self):
        if tracker_up():
            log("✅ tracking server 已經在跑，直接用")
            return
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / time.strftime("tracker_%Y%m%d_%H%M%S.log")
        self.log_file = path.open("w", encoding="utf-8")
        cmd = [sys.executable, str(ROOT / "tennis_tracking/server.py"), *self.extra_args]
        log(f"📷 在背景開 tracking server（log：{path.relative_to(ROOT)}）...")
        # 自己的 session：階段裡按 Ctrl-C 不會把它一起關掉
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=self.log_file,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + TRACKER_START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"❌ tracking server 啟動失敗（exit {self.proc.returncode}），看 {path}")
            if tracker_up():
                log("✅ tracking server 就緒")
                return
            time.sleep(0.5)
        log(f"⚠️  {TRACKER_START_TIMEOUT:.0f} 秒內沒回應 /health，先繼續；看 {path}")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            log("📷 關掉 tracking server")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log_file:
            self.log_file.close()


class StageProcess:
    """在自己的 pty 裡跑一個階段：程式以為自己在終端機裡（cbreak、Ctrl-C 都照常），
    我們在中間轉送按鍵和輸出，才能攔下 n / p。"""

    def __init__(self, index, stage, args):
        self.index, self.stage = index, stage
        extra = {"voice": args.voice_args, "chase": args.chase_args, "fall": args.fall_args}[stage.key]
        cmd = [sys.executable, str(ROOT / stage.script)]
        if args.fake and stage.fake_flag:
            cmd.append("--fake")
        cmd += shlex.split(extra)
        if stage.needs_tracker and not tracker_up():
            log(f"⚠️  tracking server 沒回應（{TRACKER_HEALTH}）：這個階段會一直等資料，看 e2e/logs/tracker_*.log")
        log("")
        log(f"━━━━━━━━ 階段 {index + 1}/{len(STAGES)}：{stage.title} ━━━━━━━━")
        log(f"$ {shlex.join(cmd)}")
        log(f"   結束：{stage.how_to_leave}  |  換階段：n 下一個、p 上一個")
        self.started = time.monotonic()
        self.code = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                          # 子程序：pty 已經是它的終端機
            os.chdir(ROOT)
            os.environ["PYTHONUNBUFFERED"] = "1"
            os.execv(cmd[0], cmd)
        self.resize()

    def resize(self):
        try:
            size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    def send(self, key):
        os.write(self.fd, self.stage.remap.get(key, key))

    def pump(self):
        """把程式的輸出抄到我們的螢幕；程式結束（pty 關了）回傳 False。"""
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if data:
            os.write(sys.stdout.fileno(), data)
            return True
        self.reap()
        return False

    def reap(self):
        if self.code is None:
            _, status = os.waitpid(self.pid, 0)
            self.code = os.waitstatus_to_exitcode(status)
            os.close(self.fd)
            result = "正常結束" if self.code in (0, -signal.SIGINT, 130) else f"exit {self.code}"
            log(f"\r━━━━━━━━ 階段 {self.index + 1} 結束（{result}，{time.monotonic() - self.started:.0f} 秒）━━━━━━━━")

    def stop(self):
        """跟按 Ctrl-C 一樣讓它自己收尾（停車、斷藍牙）；太久沒結束才強制關。"""
        if self.code is not None:
            return
        for sig, wait in ((signal.SIGINT, STOP_TIMEOUT), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.fd], [], [], 0.1)
                if ready and not self.pump():
                    return
            if sig != signal.SIGKILL:
                log(f"\r⚠️  階段 {self.index + 1} {wait:.0f} 秒內沒結束 → 強制關")
        self.reap()


def show_idle(last, pending):
    log("")
    for i, stage in enumerate(STAGES):
        mark = "▶" if i == pending else ("✓" if i == last else " ")
        log(f"  {mark} {i + 1}. {stage.title:<14} {stage.script}")
    keys = ["n = 跑 ▶"] + (["p = 上一個"] if last else []) + \
        (["r = 重跑剛剛的"] if last is not None else []) + ["1/2/3 = 直接跳", "q = 結束 demo"]
    log("   " + "、".join(keys))


def run(args):
    fd_in = sys.stdin.fileno()
    saved = termios.tcgetattr(fd_in)
    # cbreak（一個鍵一個鍵讀、不回顯）+ 關掉 ISIG：Ctrl-C 變成一般的 \x03 轉給目前的階段，不會關掉 demo.py
    tty.setcbreak(fd_in)
    mode = termios.tcgetattr(fd_in)
    mode[3] &= ~termios.ISIG
    termios.tcsetattr(fd_in, termios.TCSANOW, mode)

    stage = None                       # 正在跑的 StageProcess
    last = None                        # 最後一個跑的階段
    pending = args.stage - 1           # 閒置時按 n 會跑哪一個（None = 已經跑完最後一個）
    signal.signal(signal.SIGWINCH, lambda *_: stage and stage.resize())

    def switch(to):
        nonlocal stage, last
        if stage is not None:
            stage.stop()
        stage, last = StageProcess(to, STAGES[to], args), to

    try:
        show_idle(last, pending)
        while True:
            watch = [fd_in] + ([stage.fd] if stage else [])
            try:
                ready, _, _ = select.select(watch, [], [])
            except InterruptedError:   # SIGWINCH
                continue
            if stage and stage.fd in ready and not stage.pump():
                stage = None
                pending = last + 1 if last + 1 < len(STAGES) else None
                show_idle(last, pending)
            if fd_in not in ready:
                continue
            data = os.read(fd_in, 1024)
            if not data:
                break
            for key in [data] if data.startswith(b"\x1b") else [bytes([b]) for b in data]:   # 方向鍵等整段轉送
                if key in (b"n", b"N"):
                    to = stage.index + 1 if stage else pending
                    if to is None or to >= len(STAGES):
                        log("\r（已經是最後一個階段）")
                    else:
                        switch(to)
                elif key in (b"p", b"P"):
                    current = stage.index if stage else last
                    if not current:
                        log("\r（已經是第一個階段）")
                    else:
                        switch(current - 1)
                elif stage is not None:
                    stage.send(key)
                elif key in (b"r", b"R") and last is not None:
                    switch(last)
                elif key in (b"1", b"2", b"3") and int(key) <= len(STAGES):
                    switch(int(key) - 1)
                elif key in (b"q", b"Q", b"\x03", b"\x04"):
                    return
    finally:
        if stage is not None:
            stage.stop()
        termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)


def main():
    parser = argparse.ArgumentParser(description="Demo 總控：語音 → 追球 → 躺地偵測；n 下一個、p 上一個階段")
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit（傳 --fake 給會連車的階段）")
    parser.add_argument("--stage", type=int, choices=range(1, len(STAGES) + 1), default=1, help="按 n 從第幾階段開始")
    parser.add_argument("--tracker-args", default="", help="給 tennis_tracking/server.py 的參數，例如 --tracker-args=\"--camera 2\"")
    parser.add_argument("--voice-args", default="", help="給 robot_control/main.py 的參數")
    parser.add_argument("--chase-args", default="", help="給 ball_chase/chase.py 的參數")
    parser.add_argument("--fall-args", default="", help="給 e2e/fall_alert.py 的參數")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("要在終端機裡跑（要一個鍵一個鍵讀 n / p）")

    tracker = Tracker(shlex.split(args.tracker_args))
    try:
        tracker.start()
        run(args)
    except KeyboardInterrupt:
        log("")
    finally:
        tracker.stop()
    log("🔚 demo 結束")


if __name__ == "__main__":
    main()
