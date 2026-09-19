"""Loopback PCM stream -> Silero VAD -> existing NPU Breeze encoder -> CPU decoder."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import numpy as np
import requests
import sherpa_onnx
import soxr
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

LOG = logging.getLogger("robot-asr")
RATE = 16000
WINDOW = 512
PRE_ROLL = int(1.5 * RATE)
POST_ROLL = int(0.2 * RATE)
HISTORY_SAMPLES = 30 * RATE
BACKEND = "breeze-npu-gemm-cpu-sdpa"


class AudioClientStopped(Exception):
    """A normal WebSocket close cancels live speech instead of flushing commands."""


def select_transcript(result):
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise ValueError("Decoder returned an invalid transcript response")
    text = result["text"].strip()
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise ValueError("Decoder did not return verbose_json confidence fields")
    if not text or not segments:
        return "", "empty", {}
    probabilities, logprobs = [], []
    for segment in segments:
        probability = segment.get("no_speech_prob")
        logprob = segment.get("avg_logprob")
        if (not isinstance(probability, (int, float)) or not math.isfinite(probability)
                or not 0 <= probability <= 1
                or not isinstance(logprob, (int, float)) or not math.isfinite(logprob)):
            raise ValueError("Decoder confidence values are missing or non-finite")
        probabilities.append(probability)
        logprobs.append(logprob)
    confidence = {"no_speech_prob": max(probabilities), "avg_logprob": min(logprobs)}
    if confidence["no_speech_prob"] > 0.6:
        return "", "no_speech", confidence
    if confidence["avg_logprob"] < -1.0:
        return "", "low_confidence", confidence
    return text, None, confidence


class Recognizer:
    def __init__(self, work: Path, decoder_port: int, state_dir: Path):
        self.work = work
        self.port = decoder_port
        self.process = None
        self.log = None
        self.scratch = tempfile.TemporaryDirectory(prefix="pn54-robot-asr-")
        self.injection = Path(self.scratch.name) / "encoder.f32"
        self.decoder_log = state_dir / "whisper-server.log"
        self.encoder = None

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))
        self.log = self.decoder_log.open("w", encoding="utf-8")
        import os
        env = dict(os.environ, WHISPER_ENC_INJECT=str(self.injection))
        self.process = subprocess.Popen([
            str(self.work / "whisper.cpp/build/bin/whisper-server"),
            "-m", str(self.work / "models/whisper/ggml-breeze-asr-25-q8_0.bin"),
            "-l", "zh", "-ng", "-t", "8", "-bs", "1", "-bo", "1", "-nt", "-sns",
            "--host", "127.0.0.1", "--port", str(self.port),
        ], stdout=self.log, stderr=subprocess.STDOUT, env=env)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"CPU decoder exited; inspect {self.decoder_log}")
            try:
                response = requests.get(f"http://127.0.0.1:{self.port}/health", timeout=2)
            except requests.ConnectionError:
                time.sleep(0.5)
                continue
            if response.status_code == 200:
                break
            if response.status_code != 503:
                response.raise_for_status()
            time.sleep(0.5)
        else:
            raise TimeoutError(f"CPU decoder startup timed out; inspect {self.decoder_log}")
        sys.path.insert(0, str(self.work / "breeze_npu"))
        from live_meeting import NpuEncoder, wav_bytes
        self.wav_bytes = wav_bytes
        self.encoder = NpuEncoder(attn="cpu")
        for _ in range(3):
            self.encoder.encode(np.zeros(RATE, dtype=np.float32))
        LOG.info("NPU encoder warmed; CPU decoder PID=%d", self.process.pid)

    def recognize(self, pcm, queued_at=None):
        if self.process.poll() is not None:
            raise RuntimeError(f"CPU decoder exited; inspect {self.decoder_log}")
        began = time.monotonic()
        encoded = self.encoder.encode(pcm)
        if encoded.shape != (1500, 1280) or not np.isfinite(encoded).all():
            raise RuntimeError("NPU encoder returned invalid output")
        encoded_at = time.monotonic()
        encoded.tofile(self.injection)
        response = requests.post(
            f"http://127.0.0.1:{self.port}/inference",
            files={"file": ("speech.wav", self.wav_bytes(pcm), "audio/wav")},
            data={"response_format": "verbose_json", "temperature": "0",
                  "prompt": "", "no_speech_thold": "0.6"},
            timeout=(3, 45),
        )
        response.raise_for_status()
        text, reason, confidence = select_transcript(response.json())
        finished_at = time.monotonic()
        return {"text": text, "reason": reason, **confidence,
                "processing_s": finished_at - began,
                "encoder_s": encoded_at - began,
                "decoder_s": finished_at - encoded_at,
                "queue_s": max(0.0, began - queued_at) if queued_at is not None else 0.0,
                "backend": BACKEND}

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                LOG.error("CPU decoder PID=%d did not stop; terminating owned process", self.process.pid)
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
        self.scratch.cleanup()


class AudioSession:
    def __init__(self, websocket, recognizer, executor, config):
        self.ws, self.recognizer, self.executor = websocket, recognizer, executor
        self.input_rate = config.get("sample_rate")
        self.channels = config.get("channels")
        if (config.get("type") != "start" or config.get("format") != "s16le"
                or not isinstance(self.input_rate, int) or not 8000 <= self.input_rate <= 96000
                or self.channels not in (1, 2)):
            raise ValueError("Expected start with s16le, 8000-96000 Hz, and 1 or 2 channels")
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(recognizer.work / "models/diar/silero_vad.onnx")
        vad_config.silero_vad.min_silence_duration = 0.5
        vad_config.silero_vad.min_speech_duration = 0.25
        vad_config.silero_vad.max_speech_duration = 8
        vad_config.sample_rate = RATE
        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
        self.resampler = (soxr.ResampleStream(self.input_rate, RATE, 1, dtype="float32")
                          if self.input_rate != RATE else None)
        self.pending = np.empty(0, dtype=np.float32)
        self.jobs = asyncio.Queue(maxsize=2)
        self.history = sherpa_onnx.CircularBuffer(HISTORY_SAMPLES)
        self.last_segment_end = 0

    def drain_segments(self):
        while not self.vad.empty():
            segment = self.vad.front
            detected_start = int(segment.start)
            speech_end = detected_start + len(segment.samples)
            if speech_end > self.history.tail:
                raise RuntimeError("VAD segment extends beyond received audio")
            end = min(int(self.history.tail), speech_end + POST_ROLL)
            # VAD may start after a soft wake word; retain real preceding audio, not invented text.
            start = max(int(self.history.head), self.last_segment_end, detected_start - PRE_ROLL)
            if end > self.history.tail or start >= end:
                raise RuntimeError("VAD segment is outside the retained audio history")
            pcm = np.asarray(self.history.get(start, end - start), dtype=np.float32)
            self.vad.pop()
            if self.jobs.full():
                raise RuntimeError("Speech backlog exceeded two utterances; pause and restart audio")
            self.jobs.put_nowait((pcm, start / RATE, end / RATE, speech_end / RATE,
                                 time.monotonic()))
            self.last_segment_end = end

    def feed(self, samples):
        if len(samples) > HISTORY_SAMPLES:
            raise ValueError("Audio block exceeds history capacity")
        overflow = max(0, self.history.size + len(samples) - HISTORY_SAMPLES)
        if overflow:
            self.history.pop(overflow)
        self.history.push(samples)
        self.pending = np.concatenate((self.pending, samples))
        count = len(self.pending) // WINDOW
        for index in range(count):
            self.vad.accept_waveform(self.pending[index * WINDOW:(index + 1) * WINDOW])
            self.drain_segments()
        self.pending = self.pending[count * WINDOW:].copy()

    async def receive(self):
        async for message in self.ws:
            if isinstance(message, str):
                if json.loads(message) != {"type": "end"}:
                    raise ValueError("Unknown audio control message")
                if self.resampler is not None:
                    self.feed(self.resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True))
                if len(self.pending):
                    self.feed(np.zeros(WINDOW - len(self.pending), dtype=np.float32))
                self.vad.flush()
                self.drain_segments()
                await self.jobs.put(None)
                return
            if not message or len(message) % (2 * self.channels) or len(message) > 65536:
                raise ValueError("Invalid PCM audio block")
            samples = np.frombuffer(message, dtype="<i2").astype(np.float32) / 32768
            if self.channels == 2:
                samples = samples.reshape(-1, 2).mean(axis=1)
            if self.resampler is not None:
                samples = self.resampler.resample_chunk(samples, last=False)
            self.feed(samples)
        raise AudioClientStopped()

    async def decode(self):
        loop = asyncio.get_running_loop()
        while True:
            job = await self.jobs.get()
            if job is None:
                await self.ws.send(json.dumps({"type": "complete"}))
                return
            pcm, start, end, speech_end, queued_at = job
            result = await loop.run_in_executor(self.executor, self.recognizer.recognize, pcm, queued_at)
            result.update(type="transcript" if result["text"] else "rejected",
                          start_s=start, end_s=end, speech_end_s=speech_end)
            LOG.info("ASR timing: audio=%.2fs queue=%.3fs encoder=%.3fs decoder=%.3fs accepted=%s",
                     len(pcm) / RATE, result["queue_s"], result["encoder_s"],
                     result["decoder_s"], bool(result["text"]))
            await self.ws.send(json.dumps(result, ensure_ascii=False, allow_nan=False))


class Service:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="npu-asr")
        self.active = False

    def health(self, connection, request):
        if request.path != "/health":
            return None
        alive = self.recognizer.process.poll() is None
        return connection.respond(
            HTTPStatus.OK if alive else HTTPStatus.SERVICE_UNAVAILABLE,
            json.dumps({"status": "ready" if alive else "decoder_failed",
                        "backend": BACKEND, "decoder": "cpu", "busy": self.active}),
        )

    async def handle(self, websocket):
        if self.active:
            await websocket.send(json.dumps({"type": "error", "message": "ASR already has an audio client"}))
            return
        self.active = True
        tasks = []
        try:
            config = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if not isinstance(config, dict):
                raise ValueError("Audio configuration must be an object")
            session = AudioSession(websocket, self.recognizer, self.executor, config)
            await websocket.send(json.dumps({"type": "ready", "backend": BACKEND, "sample_rate": RATE}))
            tasks = [asyncio.create_task(session.receive()), asyncio.create_task(session.decode())]
            await asyncio.gather(*tasks)
        except (AudioClientStopped, ConnectionClosedOK):
            LOG.info("Audio client stopped; unfinished speech and pending results discarded")
        except ConnectionClosed as error:
            LOG.warning("Audio transport disconnected: %s; pending results discarded", error)
        except (ValueError, RuntimeError, TimeoutError, requests.RequestException) as error:
            LOG.exception("Audio session failed")
            try:
                await websocket.send(json.dumps({"type": "error", "message": str(error)}))
            except ConnectionClosed:
                LOG.warning("Client closed before the error could be delivered")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.active = False


async def run(args):
    args.state_dir.mkdir(parents=True, exist_ok=True)
    recognizer = Recognizer(args.work, args.decoder_port, args.state_dir)
    service = None
    try:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, stop.set)
        recognizer.start()
        service = Service(recognizer)
        async with serve(service.handle, "127.0.0.1", args.port,
                         max_size=65536, max_queue=8, process_request=service.health):
            LOG.info("READY ws://127.0.0.1:%d (NPU encoder, CPU decoder)", args.port)
            while not stop.is_set():
                if recognizer.process.poll() is not None:
                    raise RuntimeError(f"CPU decoder exited; inspect {recognizer.decoder_log}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except TimeoutError:
                    continue
    finally:
        if service is not None:
            service.executor.shutdown(wait=True, cancel_futures=True)
        recognizer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=Path.home() / "work")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--decoder-port", type=int, default=18081)
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/state/pn54-robot-asr")
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(args.state_dir / "asr.log", maxBytes=1024 * 1024,
                                backupCount=2, encoding="utf-8"),
        ],
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
