"""
鍵盤輸入：在終端機直接按鍵（不用按 Enter），不用裝額外套件（termios，只能在 Linux / macOS）。

    w / ↑  前進     s / ↓  後退     a / ←  左轉     d / →  右轉     空白鍵  停
    1  語音模式      2  追球模式      v  打一句話當作語音（測 intent 用）     h  說明     q  離開

stdin 不是終端機（例如 printf "ball mode\\nstop\\n" | python main.py --fake）時，
每一行都當成一句語音（「wait 1.5」= 等 1.5 秒），方便寫腳本測整條 pipeline。
"""

import sys
import termios
import threading
import time
import tty

from commands import BACKWARD, BALL, FORWARD, LEFT, RIGHT, STOP, VOICE, Drive, Quit, SwitchMode, Transcript
from robot_ble import log

HELP = ("⌨️  w/a/s/d 或方向鍵 開車、空白鍵 停 | 1 語音模式、2 追球模式 | "
        "v 打一句話當作語音 | h 說明 | q 離開")

KEYS = {
    "w": Drive(FORWARD, source="keyboard"), "s": Drive(BACKWARD, source="keyboard"),
    "a": Drive(LEFT, source="keyboard"), "d": Drive(RIGHT, source="keyboard"),
    " ": Drive(STOP, source="keyboard"),
    "1": SwitchMode(VOICE, source="keyboard"), "2": SwitchMode(BALL, source="keyboard"),
    "q": Quit("keyboard"), "\x03": Quit("keyboard"), "\x04": Quit("keyboard"),   # Ctrl-C、Ctrl-D
}
ARROWS = {"A": "w", "B": "s", "D": "a", "C": "d"}   # ESC [ A = ↑ …


class KeyboardInput:
    def __init__(self, emit):
        self.emit = emit
        self.saved = None

    def start(self):
        target = self._keys if sys.stdin.isatty() else self._lines
        threading.Thread(target=target, daemon=True).start()
        if sys.stdin.isatty():
            log(HELP)

    def stop(self):
        """把終端機設定還原（一定要呼叫，不然 shell 會一直是 cbreak 模式）。"""
        if self.saved is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)
            self.saved = None

    def _lines(self):
        for line in sys.stdin:
            line = line.strip()
            if line.startswith("wait "):          # 腳本用：「wait 1.5」= 等 1.5 秒再送下一行
                time.sleep(float(line.split()[1]))
            elif line:
                self.emit(Transcript(line, source="stdin"))
        self.emit(Quit("stdin"))

    def _keys(self):
        fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        try:
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":                      # 方向鍵：ESC [ A/B/C/D
                    if sys.stdin.read(1) == "[":
                        ch = ARROWS.get(sys.stdin.read(1), "")
                ch = ch.lower()
                if ch == "v":
                    self._type_sentence(fd)
                elif ch == "h":
                    log(HELP)
                elif ch in KEYS:
                    self.emit(KEYS[ch])
                    if isinstance(KEYS[ch], Quit):
                        return
        except (OSError, ValueError):   # 程式結束時 stdin 被關掉
            pass

    def _type_sentence(self, fd):
        """暫時切回一般模式讀一整行。"""
        termios.tcsetattr(fd, termios.TCSADRAIN, self.saved)
        try:
            sys.stderr.write("🗣️  說（打字）：")
            sys.stderr.flush()
            line = sys.stdin.readline().strip()
        finally:
            tty.setcbreak(fd)
        if line:
            self.emit(Transcript(line, source="keyboard-v"))
