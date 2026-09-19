"""
一句話（whisper 的 transcript）→ 指令。先用關鍵字比對，中英文都可以：

    「追球模式」「ball mode」          → SwitchMode(BALL)
    「語音模式」「手動模式」「manual」  → SwitchMode(VOICE)
    「前進兩秒」「go forward 2 seconds」→ Drive(FORWARD, 2)
    「停」「stop」                      → Drive(STOP)

聽不懂就回傳 None。之後要換成 LLM（LLM_control_motor/agent.py 那套 tool calling）也只要換掉 parse()。
"""

import re

from commands import BACKWARD, BALL, FORWARD, LEFT, RIGHT, STOP, VOICE, Drive, Quit, SwitchMode

# whisper 中文常常輸出簡體字，所以繁簡都放
# 順序有意義：先比對「切換模式」，再比對「停」，最後才是移動（「停止追球」要先被當成停）
MODE_WORDS = {
    BALL: ("追球", "球模式", "追蹤", "追踪", "跟球", "ball", "track", "follow"),
    VOICE: ("語音模式", "语音模式", "手動", "手动", "聲控", "声控", "遙控", "遥控", "voice mode", "manual", "control mode"),
}
STOP_WORDS = ("停", "別動", "别动", "不要動", "不要动", "stop", "halt", "freeze")
QUIT_WORDS = ("結束程式", "结束程序", "關機", "关机", "shut down", "shutdown", "exit program")
DRIVE_WORDS = {
    FORWARD: ("前進", "前进", "往前", "向前", "forward", "ahead", "go straight"),
    BACKWARD: ("後退", "后退", "往後", "往后", "向後", "向后", "倒車", "倒车", "backward", "back up", "reverse", "go back"),
    LEFT: ("左轉", "左转", "往左", "向左", "turn left", "left"),
    RIGHT: ("右轉", "右转", "往右", "向右", "turn right", "right"),
}
CHINESE_NUMBERS = {"半": 0.5, "一": 1, "兩": 2, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}
ENGLISH_NUMBERS = {"half a": 0.5, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _normalize(text):
    # whisper 會加標點、大小寫不一定
    return re.sub(r"[，。！？,!?]|\.(?!\d)", " ", text).lower().strip()   # 「1.5」的小數點要留著


def parse_seconds(text):
    """「兩秒」「2 秒」「1.5 seconds」「three seconds」→ 秒數；沒講就 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec|second)", text)
    if m:
        return float(m.group(1))
    for word, value in CHINESE_NUMBERS.items():
        if f"{word}秒" in text:
            return value
    for word, value in ENGLISH_NUMBERS.items():
        if re.search(rf"\b{word}\s+sec", text):
            return value
    return None


def _has(text, words):
    return any(w in text for w in words)


def parse(text, source="voice"):
    text = _normalize(text)
    if not text:
        return None
    if _has(text, QUIT_WORDS):
        return Quit(source)
    for mode, words in MODE_WORDS.items():
        if _has(text, words) and not _has(text, STOP_WORDS):
            return SwitchMode(mode, source)
    if _has(text, STOP_WORDS):
        return Drive(STOP, source=source)
    for action, words in DRIVE_WORDS.items():
        if _has(text, words):
            return Drive(action, seconds=parse_seconds(text), source=source)
    return None
