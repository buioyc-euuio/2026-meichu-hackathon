class Mode:
    """一個控制模式。controller 保證同一時間只有一個模式在 enter() 和 exit() 之間（只有它能動馬達）。"""

    name = "?"

    async def enter(self):
        """切換到這個模式時呼叫。"""

    async def exit(self):
        """切換走（或程式結束）時呼叫；要把自己動到的馬達停好。"""

    async def handle(self, command):
        """處理一個指令（commands.Drive…）；回傳 True = 有處理，False = 這個模式不管這種指令。"""
        return False
