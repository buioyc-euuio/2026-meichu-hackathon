import asyncio
import json
from pathlib import Path
import sys
import time
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "camera_control"))

from commands import BALL, STOP, VOICE, Drive, LlmFailure, LlmResult, Quit, SwitchMode, Transcript
from controller import Controller
from llm_planner import (LocalLlmPlanner, MotionPlan, checked_plan, guard_direction_contradiction, request_seconds,
                         unsupported_request, validate_plan)


class PlanValidationTests(unittest.TestCase):
    def test_durations_are_parsed_and_bounded_in_code(self):
        for text, seconds in (("往前", 1), ("前進兩秒", 2), ("後退半秒", 0.5),
                              ("left 1.5 seconds", 1.5), ("前進兩 秒", 2)):
            self.assertEqual(request_seconds(text), seconds)
        for text in ("前進10秒", "前進0秒", "後退-1秒", "走十秒", "走nan秒", "一秒或兩秒"):
            with self.assertRaises(ValueError, msg=text):
                request_seconds(text)

    def test_unsupported_requests_are_not_movement(self):
        for text in ("不要前進", "先前進再後退", "左轉九十度", "向前兩公尺", "走一分鐘", "全速前進"):
            self.assertIsNotNone(unsupported_request(text), text)

    def test_strict_schema_rejects_extra_fields_and_unknown_actions(self):
        valid = validate_plan('{"action":"forward","reason":"向前"}', 1)
        self.assertEqual(valid, MotionPlan("forward", 1, "向前"))
        invalid = [
            '{"action":"fly","reason":"x"}',
            '{"action":"forward","reason":"x","speed":255}',
            '{"action":[],"reason":"x"}',
            '{"action":"forward"}',
            '```json\n{"action":"forward","reason":"x"}\n```',
        ]
        for response in invalid:
            with self.assertRaises(ValueError):
                validate_plan(response, 1)

    def test_execution_boundary_rechecks_limits(self):
        for seconds in (True, -1, 0, 3, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                checked_plan("forward", seconds, "test")
        with self.assertRaises(ValueError):
            checked_plan("stop", 1, "test")

    def test_remote_or_credentialed_endpoint_is_rejected(self):
        for url in ("https://example.com", "http://example.com", "http://user@localhost:1",
                    "http://localhost:1/other", "http://localhost:1?key=x"):
            with self.assertRaises(ValueError):
                LocalLlmPlanner(url)

    def test_homophone_repair_is_allowed_but_explicit_opposites_are_not(self):
        self.assertEqual(guard_direction_contradiction(MotionPlan("right", 1, "修正同音字"), "又轉一秒").action, "right")
        self.assertEqual(guard_direction_contradiction(MotionPlan("forward", 1, "修正同音字"), "錢進一秒").action, "forward")
        self.assertEqual(guard_direction_contradiction(MotionPlan("right", 1, "wrong"), "左轉一秒").action, "none")
        self.assertEqual(guard_direction_contradiction(MotionPlan("left", 1, "ok"), "向左轉一下").action, "left")

    def test_compact_output_keeps_homophone_repair(self):
        planner = LocalLlmPlanner()
        response = {
            "choices": [{"finish_reason": "stop",
                         "message": {"content": '{"action":"right","reason":"repair"}'}}]
        }
        try:
            with patch("llm_planner.requests.post") as post:
                post.return_value.json.return_value = response
                plan = planner._request("又轉一秒", 1)
                sent = post.call_args.kwargs["json"]
            self.assertEqual(plan.action, "right")
            self.assertEqual(plan.reason, "修正 ASR 同音字")
            self.assertEqual(sent["max_tokens"], 64)
            self.assertIn("repair", sent["response_format"]["schema"]["properties"]["reason"]["enum"])
            self.assertEqual(sent["messages"][-1], {"role": "user", "content": "又轉一秒"})
            self.assertIn({"role": "assistant", "content": '{"action": "right", "reason": "repair"}'},
                          sent["messages"])
        finally:
            planner.executor.shutdown(wait=True, cancel_futures=True)

    def test_compact_uncertainty_cannot_authorize_motion(self):
        planner = LocalLlmPlanner()
        try:
            with patch("llm_planner.requests.post") as post:
                post.return_value.json.return_value = {
                    "choices": [{"finish_reason": "stop",
                                 "message": {"content": '{"action":"right","reason":"unclear"}'}}]}
                with self.assertRaises(ValueError):
                    planner._request("轉一下", 1)
        finally:
            planner.executor.shutdown(wait=True, cancel_futures=True)

    def test_verbose_debug_mode_remains_available(self):
        planner = LocalLlmPlanner(compact=False)
        try:
            with patch("llm_planner.requests.post") as post:
                post.return_value.json.return_value = {
                    "choices": [{"finish_reason": "stop",
                                 "message": {"content": '{"action":"right","reason":"修正又為右"}'}}]}
                plan = planner._request("又轉一秒", 1)
                self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 128)
                self.assertEqual(len(post.call_args.kwargs["json"]["messages"]), 2)
            self.assertEqual(plan.reason, "修正又為右")
        finally:
            planner.executor.shutdown(wait=True, cancel_futures=True)


class FakeMode:
    def __init__(self, name):
        self.name, self.commands, self.exits = name, [], 0

    async def enter(self):
        pass

    async def exit(self):
        self.exits += 1

    async def handle(self, command):
        self.commands.append(command)
        return True


class DelayedPlanner:
    def __init__(self, result=None):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.result = result or MotionPlan("forward", 0.1, "test")
        self.calls = []

    async def plan(self, text):
        self.calls.append(text)
        self.started.set()
        await self.release.wait()
        return self.result


class AsyncControlTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, planner):
        voice, ball = FakeMode(VOICE), FakeMode(BALL)
        controller = Controller({VOICE: voice, BALL: ball}, planner=planner)
        controller.mode = voice
        controller.voice_after = 0
        return controller, voice, ball

    async def test_keyboard_stop_does_not_wait_for_llm(self):
        planner = DelayedPlanner()
        controller, voice, _ = self.controller(planner)
        await controller.dispatch(Transcript("前進", "voice", time.monotonic()))
        await planner.started.wait()
        old_revision = controller.plan_revision
        await asyncio.wait_for(controller.dispatch(Drive(STOP, source="keyboard")), timeout=0.1)
        self.assertEqual(voice.commands[-1].action, STOP)
        await controller.dispatch(LlmResult(old_revision, "forward", 1, "late",
                                            "voice", time.monotonic(), time.monotonic()))
        self.assertEqual([c.action for c in voice.commands], [STOP])
        await asyncio.sleep(0)

    async def test_spoken_stop_bypasses_the_llm(self):
        planner = DelayedPlanner()
        controller, voice, _ = self.controller(planner)
        await controller.dispatch(Transcript("停止", "voice", time.monotonic()))
        self.assertEqual(planner.calls, [])
        self.assertEqual(voice.commands[-1].action, STOP)

    async def test_running_http_worker_cannot_deliver_after_stop(self):
        began, release = threading.Event(), threading.Event()

        class ThreadPlanner(LocalLlmPlanner):
            def _request(self, text, seconds):
                began.set()
                if not release.wait(2):
                    raise TimeoutError("Test HTTP worker was not released")
                return MotionPlan("forward", seconds, "late HTTP response")

        planner = ThreadPlanner()
        controller, voice, _ = self.controller(planner)
        try:
            await controller.dispatch(Transcript("前進", "voice", time.monotonic()))
            self.assertTrue(await asyncio.to_thread(began.wait, 1))
            await asyncio.wait_for(controller.dispatch(Drive(STOP, source="keyboard")), timeout=0.1)
            release.set()
            await planner.close()
            await asyncio.sleep(0)
            self.assertTrue(controller.queue.empty())
            self.assertEqual([command.action for command in voice.commands], [STOP])
        finally:
            release.set()
            await planner.close()

    async def test_mode_switch_cancels_old_plans(self):
        planner = DelayedPlanner()
        controller, voice, ball = self.controller(planner)
        await controller.dispatch(Transcript("往回退", "voice", time.monotonic()))
        await planner.started.wait()
        revision = controller.plan_revision
        await controller.dispatch(SwitchMode(BALL, source="keyboard"))
        await controller.dispatch(LlmResult(revision, "backward", 1, "old",
                                            "voice", time.monotonic(), time.monotonic()))
        self.assertEqual(ball.commands, [])
        self.assertEqual(voice.commands, [])
        await asyncio.sleep(0)

    async def test_expired_or_invalid_plan_cannot_move(self):
        controller, voice, _ = self.controller(DelayedPlanner())
        await controller.dispatch(LlmResult(0, "forward", 1, "old", "voice",
                                            time.monotonic() - 20, time.monotonic()))
        self.assertEqual(voice.commands, [])
        await controller.dispatch(LlmResult(controller.plan_revision, "forward", 99, "bad",
                                            "voice", time.monotonic(), time.monotonic()))
        self.assertEqual([command.action for command in voice.commands], [STOP])
        self.assertEqual(controller.llm_failures, 1)

    async def test_planner_failure_stops_and_never_falls_back_to_keywords(self):
        controller, voice, _ = self.controller(DelayedPlanner())
        await controller.dispatch(LlmFailure(0, "server offline"))
        self.assertEqual([command.action for command in voice.commands], [STOP])
        self.assertEqual(controller.llm_failures, 1)

    async def test_plan_is_consumed_only_once(self):
        controller, voice, _ = self.controller(DelayedPlanner())
        result = LlmResult(0, "left", 0.1, "test", "voice",
                           time.monotonic(), time.monotonic())
        await controller.dispatch(result)
        await controller.dispatch(result)
        self.assertEqual([command.action for command in voice.commands], ["left"])

    async def test_file_eof_drains_the_last_plan_without_cancelling_it(self):
        planner = DelayedPlanner()
        controller, voice, _ = self.controller(planner)
        runner = asyncio.create_task(controller.run())
        await asyncio.sleep(0)
        await controller.queue.put(Transcript("前進", "stdin"))
        await planner.started.wait()
        await controller.queue.put(Quit("audio-file-eof"))
        planner.release.set()
        await asyncio.wait_for(runner, timeout=2)
        self.assertEqual([command.action for command in voice.commands], ["forward"])
        self.assertEqual(voice.exits, 1)

    async def test_negated_mode_change_does_not_start_ball_mode(self):
        controller, _, _ = self.controller(DelayedPlanner())
        await controller.dispatch(Transcript("不要追球", "stdin"))
        self.assertEqual(controller.mode.name, VOICE)


if __name__ == "__main__":
    unittest.main()
