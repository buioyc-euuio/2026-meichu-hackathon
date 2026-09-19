"""
延遲紀錄：印出每一句話的時間花在哪裡，方便 debug「為什麼車子很慢才動」。

時間線（每一句話）：
    你按 Enter ──LLM 思考──▶ 呼叫工具 ──工具執行（含藍牙寫入、動作本身的秒數）──▶ LLM 思考 ──▶ ... ──▶ 回覆文字

印出來的樣子：
    ⏱ [+ 1.85s] LLM 思考 1.85s → 呼叫 turn_left
    ⏱ [+ 2.97s] turn_left 執行完 1.12s（藍牙寫入 12 次，平均 38ms，最慢 95ms）
    ⏱ [+ 4.60s] LLM 思考 1.63s → 回覆文字
    ⏱ 總計 4.60s ＝ LLM 3.48s（2 次）＋ 工具 1.12s；第一個動作在 +1.85s 才送出

    LLM 思考很久   → 模型太慢 / 網路慢 / 一句話來回太多次（每呼叫一次工具就多一次來回）
    藍牙寫入很慢   → 藍牙訊號差、micro:bit 太遠；寫入超過 0.5 秒 micro:bit 會以為斷線而停車
"""

import functools
import time

SLOW_WRITE_SECONDS = 0.15   # 單次藍牙寫入超過這個時間就特別警告


class Latency:
    def __init__(self):
        self.enabled = False   # agent.py 會打開；其他程式預設不印
        self.start()

    def start(self):
        """每一句話開始時呼叫：歸零計時器。"""
        self.t0 = self.last = time.monotonic()
        self.llm = []          # 每次 LLM 思考的秒數
        self.tools = []        # (工具名稱, 秒數)
        self.writes = []       # 每次藍牙寫入的秒數
        self.first_action = None

    def log(self, text):
        if self.enabled:
            print(f"   ⏱ [+{time.monotonic() - self.t0:5.2f}s] {text}")

    def llm_done(self, what):
        """LLM 回來了（要呼叫工具或回覆文字）：記下從上一個事件到現在花的思考時間。"""
        now = time.monotonic()
        if now - self.last < 0.05:   # LLM 一次回覆裡包含好幾個工具呼叫，不算新的一次來回
            self.log(f"（同一次 LLM 回覆）→ {what}")
        else:
            self.llm.append(now - self.last)
            self.log(f"LLM 思考 {now - self.last:.2f}s → {what}")
        self.last = now

    def bluetooth_write(self, seconds, text):
        self.writes.append(seconds)
        if self.first_action is None:
            self.first_action = time.monotonic() - self.t0
        if seconds > SLOW_WRITE_SECONDS:
            self.log(f"⚠️ 藍牙寫入 {text} 花了 {seconds * 1000:.0f}ms（很慢）")

    def summary(self):
        if not self.enabled:
            return
        total = time.monotonic() - self.t0
        tools = sum(s for _, s in self.tools)
        first = "沒有送出任何指令" if self.first_action is None else \
            f"第一個指令在 +{self.first_action:.2f}s 才送出"
        print(f"   ⏱ 總計 {total:.2f}s ＝ LLM {sum(self.llm):.2f}s（{len(self.llm)} 次）"
              f"＋ 工具 {tools:.2f}s；{first}")
        if self.writes:
            print(f"   ⏱ 藍牙寫入 {len(self.writes)} 次，共 {sum(self.writes):.2f}s，"
                  f"平均 {sum(self.writes) / len(self.writes) * 1000:.0f}ms，"
                  f"最慢 {max(self.writes) * 1000:.0f}ms")


latency = Latency()


def timed(func):
    """包住工具函式：記錄 LLM 思考多久才呼叫它、它自己執行多久。
    functools.wraps 會保留名稱、參數和說明文字，LLM 看到的工具跟原本一樣。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        latency.llm_done(f"呼叫 {func.__name__}")
        start = time.monotonic()
        n = len(latency.writes)
        try:
            return func(*args, **kwargs)
        finally:
            end = time.monotonic()
            latency.tools.append((func.__name__, end - start))
            w = latency.writes[n:]
            info = (f"（藍牙寫入 {len(w)} 次，平均 {sum(w) / len(w) * 1000:.0f}ms，"
                    f"最慢 {max(w) * 1000:.0f}ms）") if w else ""
            latency.log(f"{func.__name__} 執行完 {end - start:.2f}s{info}")
            latency.last = end
    return wrapper
