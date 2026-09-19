import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "camera_control"))

from commands import Drive, Quit, Transcript, VOICE
from controller import Controller
from inputs.audio import AudioConfig, AudioInput, speech_timing
from websockets.asyncio.server import serve


class VoiceGateTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.audio = AudioInput(self.events.append)

    def test_ambient_speech_cannot_drive(self):
        self.audio.on_text("我們等等再前進兩秒", captured_at=10)
        self.assertEqual(self.events, [])

    def test_wake_word_enters_existing_queue(self):
        self.audio.on_text("小狗，前進兩秒。", captured_at=10)
        self.assertEqual(self.events, [Transcript("前進兩秒。", "voice", 10)])

    def test_stop_does_not_require_wake_word(self):
        self.audio.on_text("停止。", captured_at=10)
        self.assertEqual(self.events[0].text, "停止。")

    def test_negated_movement_is_not_executed(self):
        self.audio.on_text("小狗不要前進", captured_at=10)
        self.assertEqual(self.events, [])

    def test_prefixed_dont_move_is_still_a_stop(self):
        self.audio.on_text("小狗，別動。", captured_at=10)
        self.assertEqual(self.events[0].text, "別動。")

    def test_capture_overflow_becomes_an_explicit_failure(self):
        self.audio.input_rate = 16000
        for _ in range(33):
            self.audio._capture(b"\x00\x00" * 512, 512, None, None)
        self.assertIsInstance(self.audio.capture_error, RuntimeError)
        self.assertEqual(self.audio.blocks.qsize(), 32)


class SpeechTimingTests(unittest.TestCase):
    def test_real_speech_end_is_used_instead_of_postroll_end(self):
        event = {"end_s": 1.2, "speech_end_s": 1.0, "processing_s": 2.0,
                 "encoder_s": 1.5, "decoder_s": 0.5, "queue_s": 0.2}
        with patch("inputs.audio.log") as output:
            captured = speech_timing(event, capture_start=100.0, now=104.0)
        self.assertEqual(captured, 101.0)
        self.assertIn("encoder 1.50s", output.call_args.args[0])

    def test_older_response_remains_compatible(self):
        with patch("inputs.audio.log") as output:
            self.assertEqual(speech_timing({"end_s": 1, "processing_s": 2}, 100, 103), 101)
        output.assert_not_called()

    def test_invalid_or_partial_timing_is_not_silently_accepted(self):
        for event in ({"end_s": float("nan")},
                      {"end_s": 1, "encoder_s": 2},
                      {"end_s": 1, "processing_s": 2, "encoder_s": -1, "decoder_s": 3, "queue_s": 0}):
            with self.assertRaises(ValueError):
                speech_timing(event, 100, 104)


class DummyMode:
    name = VOICE

    def __init__(self):
        self.commands = []

    async def handle(self, command):
        self.commands.append(command)
        return True


class ControllerAgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_audio_does_not_restart_after_keyboard_stop(self):
        mode = DummyMode()
        controller = Controller({VOICE: mode})
        controller.mode = mode
        with patch("controller.time.monotonic", return_value=100):
            await controller.dispatch(Drive("stop", source="keyboard"))
        with patch("controller.time.monotonic", return_value=102):
            await controller.dispatch(Transcript("前進兩秒", "voice", 99))
            await controller.dispatch(Transcript("前進兩秒", "voice", 101))
        self.assertEqual([c.action for c in mode.commands], ["stop", "forward"])

    async def test_stale_and_invalid_voice_times_are_rejected(self):
        mode = DummyMode()
        controller = Controller({VOICE: mode})
        controller.mode = mode
        controller.voice_after = 0
        with patch("controller.time.monotonic", return_value=100):
            for timestamp in (80, 105, float("nan")):
                await controller.dispatch(Transcript("前進", "voice", timestamp))
            await controller.dispatch(Transcript("前進", source="stdin"))
        self.assertEqual(len(mode.commands), 1)
        self.assertEqual(mode.commands[0].source, "stdin")


class AudioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wav = Path(self.temp.name) / "input.wav"
        with wave.open(str(self.wav), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(48000)
            target.writeframes(b"\x00\x00" * 4800)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_recording_to_transcript_to_eof(self):
        received = []

        async def handler(ws):
            config = json.loads(await ws.recv())
            self.assertEqual(config["sample_rate"], 48000)
            await ws.send(json.dumps({"type": "ready", "backend": "breeze-npu-gemm-cpu-sdpa"}))
            async for message in ws:
                if isinstance(message, bytes):
                    received.append(message)
                else:
                    self.assertEqual(json.loads(message), {"type": "end"})
                    await ws.send(json.dumps({"type": "transcript", "text": "小狗前進兩秒",
                                              "end_s": 0.1}))
                    await ws.send(json.dumps({"type": "complete"}))
                    return

        events = []
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            audio = AudioInput(events.append, config=AudioConfig(
                url=f"ws://127.0.0.1:{port}", wav_file=self.wav))
            try:
                await audio.start()
                await asyncio.wait_for(asyncio.wrap_future(audio.future), timeout=3)
            finally:
                await audio.stop()
        self.assertEqual(sum(map(len, received)), 9600)
        transcripts = [e for e in events if isinstance(e, Transcript)]
        self.assertEqual([e.text for e in transcripts], ["前進兩秒"])
        self.assertIsNotNone(transcripts[0].captured_at)
        self.assertTrue(any(isinstance(e, Quit) and e.source == "audio-file-eof" for e in events))

    async def test_wrong_backend_fails_instead_of_falling_back(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"type": "ready", "backend": "cpu-fallback"}))

        events = []
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            audio = AudioInput(events.append, config=AudioConfig(
                url=f"ws://127.0.0.1:{port}", wav_file=self.wav))
            with self.assertRaises(RuntimeError):
                await audio.start()
            with self.assertRaises(RuntimeError):
                await audio.stop()
        self.assertTrue(any(isinstance(e, Quit) and e.source == "audio-error" for e in events))
        self.assertFalse(any(isinstance(e, Transcript) for e in events))

    async def test_disabled_audio_needs_no_microphone_or_service(self):
        audio = AudioInput(lambda event: self.fail("Unexpected event"), enabled=False)
        with patch("inputs.audio._sounddevice", side_effect=AssertionError("must not import audio")):
            await audio.start()
            await audio.stop()

    async def test_invalid_wav_is_rejected_before_connect(self):
        with wave.open(str(self.wav), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(1)
            target.setframerate(16000)
            target.writeframes(b"\x00" * 512)
        audio = AudioInput(lambda event: None, config=AudioConfig(wav_file=self.wav))
        with self.assertRaisesRegex(ValueError, "PCM16"):
            await audio.start()
        self.assertIsNone(audio.future)

    async def test_microphone_worker_can_stop_before_first_sample(self):
        stream_options = {}

        class Stream:
            def __init__(self, **kwargs):
                stream_options.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class SoundDevice:
            RawInputStream = Stream

        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"type": "ready", "backend": "breeze-npu-gemm-cpu-sdpa"}))
            async for message in ws:
                self.fail("A fake microphone must not produce samples")

        events = []
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            audio = AudioInput(events.append, config=AudioConfig(url=f"ws://127.0.0.1:{port}"))
            audio.input_rate = 16000
            audio._sd = SoundDevice()
            with patch.object(audio, "prepare"):
                await audio.start()
            await asyncio.wait_for(audio.stop(), timeout=2)
        self.assertTrue(audio.future.done())
        self.assertEqual(events, [])
        self.assertEqual(stream_options["blocksize"], 0)
        self.assertEqual(stream_options["latency"], "high")


if __name__ == "__main__":
    unittest.main()
