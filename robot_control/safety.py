"""
防撞（stub）。所有輪子指令（M,左,右#）送出去之前都會先經過 CollisionGuard.filter()，
兩個模式共用，所以之後只要把真的邏輯寫在這裡就好。

現在：什麼都不擋，原樣回傳。
之後：例如用 tennis_tracking 的「人」的位置／大小、或超音波距離，前面太近就把往前的速度壓成 0。
"""


class CollisionGuard:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def update(self, observation):
        """餵感測資料（相機偵測結果、距離…）；格式等實作時再定。"""
        # TODO: 存下最新的障礙物資訊
        pass

    def filter(self, left, right):
        """輪子速度 (-255~255) → 允許的輪子速度。回傳 (left, right, reason)，reason 是 None 或被擋的原因。"""
        if not self.enabled:
            return left, right, None
        # TODO: 前方有障礙物時擋掉往前（left + right > 0）的部分
        return left, right, None
