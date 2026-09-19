"""麥克風或測試 WAV -> 本機 NPU Whisper -> 現有 controller queue。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
import traceback
import wave

from commands import Quit, Transcript
from robot_ble import log


@dataclass
class AudioConfig:
    url: str = "ws://127.0.0.1:18082"
    device: str | int | None = None
    channels: int = 1
    sample_rate: int | None = None
    wav_file: Path | None = None
    wake_word: str = "小狗"
    pipewire_source: str | None = None


def speech_timing(event, capture_start, now):
    end = event.get("speech_end_s", event.get("end_s"))
    if capture_start is None or type(end) not in (int, float) or not math.isfinite(end) or end < 0:
        raise ValueError("ASR 語音時間戳錯誤")
    captured_at = capture_start + end
    fields = ("processing_s", "encoder_s", "decoder_s", "queue_s")
    if any(field in event for field in fields[1:]) and not all(field in event for field in fields):
        raise ValueError("ASR timing 欄位不完整")
    if all(field in event for field in fields):
        if any(type(event[field]) not in (int, float) or not math.isfinite(event[field])
               or event[field] < 0 for field in fields):
            raise ValueError("ASR timing 欄位錯誤")
        total = max(0.0, now - captured_at)
        other = max(0.0, total - event["processing_s"] - event["queue_s"])
        log(f"⏱ 出字 {total:.2f}s：斷句/傳輸約 {other:.2f}s，排隊 {event['queue_s']:.2f}s，"
            f"encoder {event['encoder_s']:.2f}s，decoder {event['decoder_s']:.2f}s")
    return captured_at


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as error:
        raise RuntimeError("麥克風需要 sounddevice 和 libportaudio2；見 robot_control/README.md") from error
    return sounddevice


def list_microphones():
    sounddevice = _sounddevice()
    found = False
    for index, device in enumerate(sounddevice.query_devices()):
        if device["max_input_channels"] > 0:
            found = True
            print(f"{index}: {device['name']} "
                  f"({int(device['default_samplerate'])} Hz, {device['max_input_channels']} channels)")
    if not found:
        raise RuntimeError("沒有可用麥克風；接上後再執行 --list-mics")


class AudioInput:
    def __init__(self, emit, enabled=True, config=None):
        self.emit, self.enabled = emit, enabled
        self.config = config or AudioConfig()
        self.executor = None
        self.future = None
        self.loop = self.task = None
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.blocks = queue.Queue(maxsize=32)
        self.capture_error = None
        self.capture_start = None
        self.input_rate = None
        self.input_channels = self.config.channels
        self._sd = None
        self.pipewire = None
        self.first_pipewire_block = None

    def prepare(self):
        if not self.enabled:
            return
        if self.config.pipewire_source is not None:
            if self.config.wav_file is not None or self.config.device is not None:
                raise ValueError("PipeWire 輸入不能同時指定 ALSA 裝置或 WAV")
            from inputs.pipewire import graph, resolve_source
            target = resolve_source(self.config.pipewire_source, graph())
            from inputs.pipewire import PipeWireCapture
            self.input_rate = self.config.sample_rate or 16000
            self.pipewire = PipeWireCapture(target, self.input_rate, self.input_channels)
            log(f"🎤 PipeWire 指定來源：{target}")
        elif self.config.wav_file is not None:
            with wave.open(str(self.config.wav_file), "rb") as source:
                if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
                    raise ValueError("--audio-file 必須是 PCM16 WAV")
                self.input_rate, self.input_channels = source.getframerate(), source.getnchannels()
        else:
            self._sd = _sounddevice()
            try:
                info = self._sd.query_devices(self.config.device, "input")
                self.input_rate = self.config.sample_rate or int(info["default_samplerate"])
                self._sd.check_input_settings(device=self.config.device, channels=self.input_channels,
                                              dtype="int16", samplerate=self.input_rate)
            except self._sd.PortAudioError as error:
                raise RuntimeError("麥克風不可用；先執行 --list-mics，再用 --audio-device 選擇") from error
        if self.input_channels not in (1, 2) or not 8000 <= self.input_rate <= 96000:
            raise ValueError("音訊必須為 1/2 channels、8000-96000 Hz")

    async def start(self):
        if not self.enabled:
            return
        self.prepare()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robot-audio")
        self.future = self.executor.submit(lambda: asyncio.run(self._session()))
        self.future.add_done_callback(self._finished)
        if not await asyncio.to_thread(self.ready.wait, 15):
            raise TimeoutError("NPU ASR 啟動逾時；確認本機 18082 服務已啟動")
        if self.future.done() and self.future.exception() is not None:
            raise RuntimeError("無法啟動 NPU 語音輸入") from self.future.exception()
        log(f"🎤 語音輸入已就緒（NPU Whisper，喚醒詞：{self.config.wake_word or '關閉'}）")

    def _finished(self, future):
        self.ready.set()
        if self.stopping.is_set():
            return
        error = future.exception()
        if error is not None:
            log(f"❌ 語音輸入中止：{error}")
            traceback.print_exception(error)
            self.emit(Quit(source="audio-error"))
        elif self.config.wav_file is not None:
            self.emit(Quit(source="audio-file-eof"))
        else:
            log("❌ 語音串流意外結束")
            self.emit(Quit(source="audio-stream-ended"))

    def _capture(self, data, frames, timing, status):
        if status:
            self.capture_error = RuntimeError(f"麥克風資料遺失：{status}")
            return
        if self.capture_start is None:
            self.capture_start = time.monotonic() - frames / self.input_rate
        try:
            self.blocks.put_nowait(bytes(data))
        except queue.Full:
            self.capture_error = RuntimeError("麥克風緩衝區已滿；為避免過期指令，停止語音輸入")

    async def _send(self, websocket):
        if self.pipewire is not None:
            await asyncio.wait_for(websocket.send(self.first_pipewire_block), timeout=2)
            while True:
                block = await self.pipewire.read()
                await asyncio.wait_for(websocket.send(block), timeout=2)
        if self.config.wav_file is not None:
            self.capture_start = time.monotonic()
            frames_sent = 0
            with wave.open(str(self.config.wav_file), "rb") as source:
                while data := source.readframes(max(1, int(self.input_rate * 0.032))):
                    frames_sent += len(data) // (2 * self.input_channels)
                    await asyncio.sleep(max(0, self.capture_start + frames_sent / self.input_rate - time.monotonic()))
                    await asyncio.wait_for(websocket.send(data), timeout=2)
            await websocket.send(json.dumps({"type": "end"}))
            return
        last_audio = time.monotonic()
        while True:
            if self.capture_error is not None:
                raise self.capture_error
            try:
                data = await asyncio.to_thread(self.blocks.get, True, 0.1)
            except queue.Empty:
                if time.monotonic() - last_audio > 2:
                    raise RuntimeError("麥克風超過 2 秒沒有提供音訊，停止語音輸入")
                continue
            if self.capture_error is not None:
                raise self.capture_error
            last_audio = time.monotonic()
            await asyncio.wait_for(websocket.send(data), timeout=2)

    async def _receive(self, websocket):
        async for raw in websocket:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("ASR 回傳格式錯誤")
            if event.get("type") == "error":
                raise RuntimeError(event.get("message", "ASR error"))
            if event.get("type") == "complete":
                if self.config.wav_file is None:
                    raise RuntimeError("即時 ASR 意外結束")
                return
            if event.get("type") == "rejected":
                speech_timing(event, self.capture_start, time.monotonic())
                log(f"🎤 忽略語音片段：{event.get('reason')} "
                    f"(no_speech={event.get('no_speech_prob')})")
                continue
            if event.get("type") != "transcript" or not isinstance(event.get("text"), str):
                raise ValueError("ASR 回傳未知訊息")
            captured_at = speech_timing(event, self.capture_start, time.monotonic())
            self.on_text(event["text"], captured_at=captured_at)
        raise RuntimeError("NPU ASR 連線中斷")

    async def _exchange(self, websocket):
        tasks = [asyncio.create_task(self._send(websocket)),
                 asyncio.create_task(self._receive(websocket))]
        if self.pipewire is not None:
            tasks.append(asyncio.create_task(self.pipewire.watch()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _session(self):
        from websockets.asyncio.client import connect
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        async with connect(self.config.url, open_timeout=5, max_size=65536) as websocket:
            await websocket.send(json.dumps({"type": "start", "format": "s16le",
                                            "sample_rate": self.input_rate, "channels": self.input_channels}))
            ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if ready.get("type") != "ready" or ready.get("backend") != "breeze-npu-gemm-cpu-sdpa":
                raise RuntimeError(f"NPU ASR 未就緒：{ready}")
            if self.pipewire is not None:
                try:
                    self.first_pipewire_block = await self.pipewire.start()
                    self.capture_start = time.monotonic() - 512 / self.input_rate
                    self.ready.set()
                    await self._exchange(websocket)
                finally:
                    await self.pipewire.close()
            elif self.config.wav_file is not None:
                self.ready.set()
                await self._exchange(websocket)
            else:
                with self._sd.RawInputStream(
                    device=self.config.device, samplerate=self.input_rate,
                    channels=self.input_channels, dtype="int16",
                    blocksize=0, latency="high", callback=self._capture,
                ):
                    self.ready.set()
                    await self._exchange(websocket)

    def on_text(self, text, captured_at=None):
        text = text.strip()
        if not text:
            return
        log(f"🎤 {text}")
        compact = re.sub(r"[\s，。！？,.!?]", "", text)
        stop = compact.lower() in {"停", "停止", "停下", "停下來", "停下来", "別動", "别动", "stop", "halt"}
        if self.config.wake_word and not stop:
            prefix = re.match(rf"^\s*{re.escape(self.config.wake_word)}[\s，。,:：]*", text)
            if prefix is None:
                log(f"   （未以「{self.config.wake_word}」開頭，不執行指令）")
                return
            text = text[prefix.end():].strip()
        command_text = re.sub(r"[\s，。！？,.!?]", "", text).lower()
        stop = command_text in {"停", "停止", "停下", "停下來", "停下来", "別動", "别动", "stop", "halt"}
        if text.lower().startswith(("不要", "別", "别", "不准", "先別", "先别", "don't", "do not")) and not stop:
            log("   （否定句不執行移動／切換；要停下請直接說「停止」）")
            return
        if text:
            self.emit(Transcript(text, source="voice", captured_at=captured_at))

    async def stop(self):
        self.stopping.set()
        if self.future is None:
            return
        if self.loop is not None and not self.loop.is_closed() and self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        try:
            await asyncio.wrap_future(self.future)
        except asyncio.CancelledError:
            pass
        finally:
            await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
