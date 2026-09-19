"""
把整條 pipeline 串起來：所有輸入（鍵盤、語音）都丟指令進同一個 queue，這裡一個一個處理。

    麥克風 ─whisper─▶ Transcript ─intent.parse─┐
    鍵盤 ────────────────────────────────────┼─▶ queue ─▶ Controller ─▶ 目前的模式 ─▶ RobotBLE ─▶ micro:bit
                                               │               │
                                   SwitchMode ─┘               └─ 切換模式：舊模式 exit()（停馬達）→ 新模式 enter()

規則：
    - 任何模式都聽得懂「切換模式」「停」「結束」
    - 追球模式裡說「停」＝ 馬上停下來並回到語音模式（最安全）
    - 追球模式裡的開車指令（前進、w…）不理它
"""

import asyncio
import math
import time
import traceback

from commands import BALL, STOP, VOICE, Drive, LlmFailure, LlmResult, Quit, SwitchMode, Transcript
from intent import parse
from robot_ble import log

IGNORED_LOG_INTERVAL = 1.0   # 鍵盤按住會一直重複，被忽略的訊息最多每秒印一次


class Controller:
    def __init__(self, modes, start_mode=VOICE, max_audio_age=10.0, planner=None):
        self.modes = modes            # {"voice": VoiceMode, "ball": BallMode}
        self.start_mode = start_mode
        self.mode = None
        self.queue = asyncio.Queue()
        self.loop = None
        self.last_ignored_log = 0.0
        self.voice_after = time.monotonic()
        self.max_audio_age = max_audio_age
        self.planner = planner
        self.plan_revision = 0
        self.plan_task = None
        self.drain_task = None
        self.last_motion_until = 0.0
        self.llm_failures = 0

    def cancel_plan(self):
        self.plan_revision += 1
        task, self.plan_task = self.plan_task, None
        if task is not None:
            task.cancel()
        return task

    def fresh(self, source, captured_at):
        if source != "voice" or captured_at is None:
            return True
        now = time.monotonic()
        return (math.isfinite(captured_at) and self.voice_after <= captured_at <= now + 0.5
                and now - captured_at <= self.max_audio_age)

    def submit_plan(self, transcript):
        self.cancel_plan()
        revision = self.plan_revision
        submitted = time.monotonic()

        async def plan():
            result = await self.planner.plan(transcript.text)
            await self.queue.put(LlmResult(
                revision, result.action, result.seconds, result.reason,
                transcript.source, transcript.captured_at, submitted, time.monotonic(),
            ))

        def finished(task):
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                log(f"❌ LLM 判斷失敗：{error}")
                traceback.print_exception(error)
                self.queue.put_nowait(LlmFailure(revision, str(error)))

        self.plan_task = asyncio.create_task(plan())
        self.plan_task.add_done_callback(finished)
        log(f"🧠 交給本機 LLM：{transcript.text}")

    async def drain_recording(self):
        # WAV 只允許 fake；讓最後一個已提出的意圖與限時動作完成再退出。
        if self.plan_task is not None:
            await asyncio.gather(self.plan_task, return_exceptions=True)
        await self.queue.join()
        await asyncio.sleep(max(0, self.last_motion_until - time.monotonic()) + 0.1)
        await self.queue.put(Quit("audio-file-drained"))

    def emit(self, command):
        """給輸入端用：可以從任何執行緒呼叫。"""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, command)

    async def switch(self, name, source="?"):
        self.cancel_plan()
        if self.mode is not None and self.mode.name == name:
            log(f"（已經是 {name} 模式）")
            return
        if self.mode is not None:
            await self.mode.exit()
        log(f"🔀 切換到 {name} 模式（{source}）")
        self.mode = self.modes[name]
        await self.mode.enter()

    async def run(self):
        """處理指令直到收到 Quit；結束時讓目前的模式停好馬達。"""
        self.voice_after = time.monotonic()
        await self.switch(self.start_mode, "start")
        try:
            while True:
                command = await self.queue.get()
                try:
                    outcome = await self.dispatch(command)
                finally:
                    self.queue.task_done()
                if outcome == "quit":
                    return
        finally:
            pending = self.cancel_plan()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            if self.drain_task is not None:
                self.drain_task.cancel()
                await asyncio.gather(self.drain_task, return_exceptions=True)
            if self.mode is not None:
                await self.mode.exit()

    async def dispatch(self, command):
        if isinstance(command, (LlmResult, LlmFailure)):
            if command.revision != self.plan_revision or self.mode.name != VOICE:
                log("🛑 丟棄已取消或模式已變更的 LLM 結果")
                return None
            self.plan_task = None
            self.plan_revision += 1
            if isinstance(command, LlmFailure):
                self.llm_failures += 1
                self.last_motion_until = 0.0
                await self.mode.handle(Drive(STOP, source="llm-error"))
                return None
            now = time.monotonic()
            completed = command.completed_at if command.completed_at is not None else now
            timing = (f"⏱ LLM 排隊/判斷 {completed - command.submitted_at:.2f}s，"
                      f"控制器等待 {max(0.0, now - completed):.2f}s")
            if command.source == "voice" and command.captured_at is not None:
                timing += f"，語音末端→判斷 {now - command.captured_at:.2f}s"
            log(timing)
            if (not self.fresh(command.source, command.captured_at)
                    or time.monotonic() - command.submitted_at > self.max_audio_age):
                log("🛑 LLM 結果已過期，不執行")
                return None
            from llm_planner import checked_plan
            try:
                plan = checked_plan(command.action, command.seconds, command.reason)
            except ValueError as error:
                self.llm_failures += 1
                self.last_motion_until = 0.0
                log(f"🛑 拒絕不安全的 LLM 結果：{error}")
                await self.mode.handle(Drive(STOP, source="llm-error"))
                return None
            log(f"🧠 LLM 判斷：{plan.action}（{plan.reason}）")
            if plan.action != "none":
                self.last_motion_until = time.monotonic() + plan.seconds
                await self.mode.handle(Drive(plan.action, plan.seconds, source="llm"))
            return None

        if isinstance(command, Transcript):
            if not self.fresh(command.source, command.captured_at):
                log("🛑 捨棄過期語音，或手動操作之前錄到的語音")
                return None
            parsed = parse(command.text, command.source)
            if self.planner is not None:
                from llm_planner import negated_request
                immediate_stop = isinstance(parsed, Drive) and parsed.action == STOP
                if negated_request(command.text) and not immediate_stop:
                    self.cancel_plan()
                    log("🛑 否定語句不執行移動或切換模式")
                    return None
                if not isinstance(parsed, (Quit, SwitchMode)) and not immediate_stop:
                    if self.mode.name == VOICE:
                        self.submit_plan(command)
                    else:
                        log("🛑 追球模式不接受 LLM 行走指令")
                    return None
            log(f"🗣️  「{command.text}」→ {parsed if parsed else '聽不懂，忽略'}")
            if parsed is None:
                return None
            command = parsed

        if isinstance(command, Quit) and command.source == "audio-file-eof" and self.planner is not None:
            if self.drain_task is None:
                self.drain_task = asyncio.create_task(self.drain_recording())
            return None
        if isinstance(command, (Drive, SwitchMode)) and command.source != "voice":
            self.voice_after = time.monotonic()
        if isinstance(command, (Drive, SwitchMode, Quit)):
            self.cancel_plan()
            if not isinstance(command, Drive) or command.action == STOP:
                self.last_motion_until = 0.0

        if isinstance(command, Quit):
            log(f"👋 離開（{command.source}）")
            return "quit"
        if isinstance(command, SwitchMode):
            await self.switch(command.mode, command.source)
            return None
        if isinstance(command, Drive) and command.action == STOP and self.mode.name == BALL:
            await self.switch(VOICE, f"{command.source} 說停")
            return None
        if not await self.mode.handle(command):
            now = time.monotonic()
            if now - self.last_ignored_log >= IGNORED_LOG_INTERVAL:
                log(f"   {self.mode.name} 模式不處理 {command}（切到語音模式才能開車）")
                self.last_ignored_log = now
        return None
