import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / "asr/server.py"
available = all(importlib.util.find_spec(name) is not None
                for name in ("sherpa_onnx", "soxr", "websockets"))
if available:
    spec = importlib.util.spec_from_file_location("robot_asr_server", path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)


@unittest.skipUnless(available, "Run ASR protocol tests using work/ironenv")
class ConfidenceTests(unittest.TestCase):
    def result(self, probability=0.1, logprob=-0.2):
        return {"text": "speech", "segments": [
            {"no_speech_prob": probability, "avg_logprob": logprob}]}

    def test_accepts_confident_text(self):
        text, reason, confidence = server.select_transcript(self.result())
        self.assertEqual(text, "speech")
        self.assertIsNone(reason)

    def test_rejects_noise_and_low_confidence(self):
        self.assertEqual(server.select_transcript(self.result(probability=0.9))[1], "no_speech")
        self.assertEqual(server.select_transcript(self.result(logprob=-2))[1], "low_confidence")

    def test_empty_audio_is_explicitly_rejected(self):
        self.assertEqual(server.select_transcript({"text": "", "segments": []})[1], "empty")

    def test_missing_or_invalid_confidence_is_an_error(self):
        for result in ({"text": "speech"}, self.result(probability=float("nan")),
                       self.result(logprob=None)):
            with self.assertRaises(ValueError):
                server.select_transcript(result)


@unittest.skipUnless(available, "Run ASR protocol tests using work/ironenv")
class SessionCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_stream_close_is_cancellation(self):
        class ClosedStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        with self.assertRaises(server.AudioClientStopped):
            await server.AudioSession.receive(SimpleNamespace(ws=ClosedStream()))

    async def check_session(self, failure, expected_level):
        class Socket:
            def __init__(self):
                self.messages = []

            async def recv(self):
                return json.dumps({"type": "start"})

            async def send(self, message):
                self.messages.append(json.loads(message))

        class Session:
            def __init__(self, *args):
                pass

            async def receive(self):
                raise failure

            async def decode(self):
                await asyncio.Future()

        service = server.Service(None)
        websocket = Socket()
        try:
            with patch.object(server, "AudioSession", Session):
                with self.assertLogs("robot-asr", level=expected_level) as logs:
                    await service.handle(websocket)
            self.assertFalse(service.active)
            return websocket.messages, logs.output
        finally:
            service.executor.shutdown(wait=True, cancel_futures=True)

    async def test_normal_close_releases_session_without_error_response(self):
        messages, logs = await self.check_session(server.AudioClientStopped(), "INFO")
        self.assertEqual([m["type"] for m in messages], ["ready"])
        self.assertFalse(any("ERROR" in line for line in logs))

    async def test_invalid_audio_still_reports_an_error(self):
        messages, logs = await self.check_session(ValueError("invalid PCM"), "ERROR")
        self.assertEqual([m["type"] for m in messages], ["ready", "error"])
        self.assertEqual(messages[-1]["message"], "invalid PCM")
        self.assertTrue(any("Audio session failed" in line for line in logs))


@unittest.skipUnless(available, "Run ASR protocol tests using work/ironenv")
class SpeechBoundaryTests(unittest.TestCase):
    def session(self, previous_end=0, history_head=0):
        class Vad:
            def __init__(self):
                self.segments = [SimpleNamespace(start=16000, samples=[0.0] * 4000)]

            def empty(self):
                return not self.segments

            @property
            def front(self):
                return self.segments[0]

            def pop(self):
                self.segments.pop(0)

        history = server.sherpa_onnx.CircularBuffer(32000)
        history.push(server.np.arange(24000, dtype=server.np.float32))
        if history_head:
            history.pop(history_head)
        return SimpleNamespace(vad=Vad(), history=history, last_segment_end=previous_end,
                               jobs=asyncio.Queue(maxsize=2))

    def test_preroll_preserves_actual_audio_before_vad_start(self):
        session = self.session()
        server.AudioSession.drain_segments(session)
        pcm, start, end, speech_end, queued_at = session.jobs.get_nowait()
        self.assertEqual(start, 0)
        self.assertEqual(end, 1.45)
        self.assertEqual(len(pcm), 23200)
        self.assertEqual(pcm[0], 0)
        self.assertEqual(pcm[-1], 23199)
        self.assertEqual(speech_end, 1.25)
        self.assertGreater(queued_at, 0)

    def test_preroll_does_not_replay_previous_utterances(self):
        session = self.session(previous_end=15000)
        server.AudioSession.drain_segments(session)
        pcm, start, _, _, _ = session.jobs.get_nowait()
        self.assertEqual(start, 15000 / server.RATE)
        self.assertEqual(pcm[0], 15000)
        self.assertEqual(session.last_segment_end, 23200)

    def test_history_boundary_is_respected(self):
        session = self.session(history_head=10000)
        server.AudioSession.drain_segments(session)
        pcm, _, _, _, _ = session.jobs.get_nowait()
        self.assertEqual(pcm[0], 10000)


@unittest.skipUnless(available, "Run ASR protocol tests using work/ironenv")
class AsrTimingTests(unittest.TestCase):
    def test_queue_encoder_and_decoder_are_measured_separately(self):
        result = {"text": "stop", "segments": [{"no_speech_prob": 0.01, "avg_logprob": -0.1}]}
        with tempfile.TemporaryDirectory() as temporary:
            recognizer = SimpleNamespace(
                process=SimpleNamespace(poll=lambda: None),
                encoder=SimpleNamespace(encode=lambda pcm: server.np.zeros((1500, 1280), dtype=server.np.float32)),
                injection=Path(temporary) / "encoder.f32", port=1, decoder_log="unused",
                wav_bytes=lambda pcm: b"test",
            )
            response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)
            with patch.object(server.requests, "post", return_value=response):
                with patch.object(server.time, "monotonic", side_effect=[10.0, 11.5, 12.0]):
                    actual = server.Recognizer.recognize(recognizer, server.np.zeros(16000), queued_at=9.0)
        self.assertEqual(actual["queue_s"], 1.0)
        self.assertEqual(actual["encoder_s"], 1.5)
        self.assertEqual(actual["decoder_s"], 0.5)
        self.assertEqual(actual["processing_s"], 2.0)


if __name__ == "__main__":
    unittest.main()
