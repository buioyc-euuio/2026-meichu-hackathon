"""
機器人總控制：兩個模式，用語音（或鍵盤）切換。

    語音模式（voice）：說「前進兩秒」「左轉」「停」開車；鍵盤 w/a/s/d 也可以
    追球模式（ball）  ：相機自動對準網球（camera_control，現在是 stub）

    .venv/bin/python robot_control/main.py --fake --no-audio # 不連 micro:bit，先測鍵盤
    .venv/bin/python robot_control/main.py --list-mics       # 只列麥克風，不連車
    .venv/bin/python robot_control/main.py                   # 連 micro:bit
    .venv/bin/python robot_control/main.py --mode ball       # 一開始就是追球模式
    printf '前進兩秒\\n追球模式\\n停\\n' | .venv/bin/python robot_control/main.py --fake --no-audio

切換模式：說「追球模式」／「語音模式」（英文 ball mode / voice mode 也可以），或鍵盤 1、2。
"""

import argparse
import asyncio
import math
import sys
from pathlib import Path

# 藍牙連線共用 camera_control/robot_ble.py（之後追球也會從 camera_control import）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "camera_control"))

from commands import MODES, VOICE                   # noqa: E402
from controller import Controller                   # noqa: E402
from drive import WheelDriver                       # noqa: E402
from inputs.audio import AudioConfig, AudioInput, list_microphones  # noqa: E402
from inputs.keyboard import KeyboardInput           # noqa: E402
from modes.ball_mode import BallMode                # noqa: E402
from modes.voice_mode import VoiceMode              # noqa: E402
from robot_ble import DEVICE_NAME, RobotBLE, log    # noqa: E402
from safety import CollisionGuard                   # noqa: E402


async def run(args):
    robot = RobotBLE(args.device_name, fake=args.fake, quiet=not args.verbose)
    guard = CollisionGuard(enabled=not args.no_guard)
    driver = WheelDriver(robot, guard)
    planner = None
    if args.llm:
        from llm_planner import LocalLlmPlanner
        planner = LocalLlmPlanner(args.llm_url, args.llm_model, compact=not args.llm_verbose_reason)

    modes = {
        "voice": VoiceMode(driver),
        "ball": BallMode(robot, driver, guard, args),
    }
    controller = Controller(modes, start_mode=args.mode, max_audio_age=args.audio_max_age,
                            planner=planner)
    controller.loop = asyncio.get_running_loop()

    keyboard = None if args.audio_file else KeyboardInput(controller.emit)
    device = args.audio_device
    if device is not None and device.isdecimal():
        device = int(device)
    audio = AudioInput(controller.emit, enabled=not args.no_audio, config=AudioConfig(
        url=args.asr_url, device=device, channels=args.audio_channels,
        sample_rate=args.audio_rate, wav_file=args.audio_file, wake_word=args.audio_wake_word,
        pipewire_source=args.pipewire_source,
    ))
    driver_started = False
    try:
        if planner is not None:
            await planner.check_ready()
            log(f"🧠 本機 LLM 已就緒：{args.llm_model}；動作最多 2 秒，停止不等 LLM")
            if not args.fake:
                log("⚠️ 已授權實機移動；防撞仍未完成，僅限有人監督的安全空間")
        # 缺麥克風或 ASR 服務時，在藍牙連線／馬達工作啟動前就失敗。
        await audio.start()
        await robot.connect()
        driver.start()
        driver_started = True
        if keyboard is not None:
            keyboard.start()
        await controller.run()
        if args.audio_file and controller.llm_failures:
            raise RuntimeError("錄音測試中有 LLM 失敗／不安全輸出，沒有當作成功執行")
    finally:
        if keyboard is not None:
            keyboard.stop()
        try:
            await audio.stop()
        finally:
            try:
                if driver_started:
                    await driver.close()
            finally:
                try:
                    await robot.disconnect()
                finally:
                    if planner is not None:
                        await planner.close()


def main():
    parser = argparse.ArgumentParser(description="機器人總控制：語音模式 / 追球模式")
    parser.add_argument("--mode", choices=MODES, default=VOICE, help="一開始的模式")
    parser.add_argument("--fake", action="store_true", help="不連 micro:bit，只印出指令")
    parser.add_argument("--verbose", action="store_true", help="印出每一個藍牙指令（包含 keep-alive）")
    parser.add_argument("--no-audio", action="store_true", help="不開麥克風")
    parser.add_argument("--no-guard", action="store_true", help="關掉防撞")
    parser.add_argument("--device-name", default=DEVICE_NAME, help="micro:bit 的藍牙名稱（包含這段就連）")
    parser.add_argument("--list-mics", action="store_true", help="只列出麥克風，不連藍牙")
    parser.add_argument("--asr-url", default="ws://127.0.0.1:18082", help="本機 NPU Whisper 服務")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--audio-device", help="ALSA 麥克風編號或名稱")
    source.add_argument("--pipewire-source", help="PipeWire node.name，不追隨其他預設來源")
    source.add_argument("--bluetooth-mic", dest="pipewire_source", action="store_const",
                        const="bluetooth", help="選取唯一的藍牙通話麥克風")
    parser.add_argument("--list-pipewire-mics", action="store_true", help="列出 PipeWire／藍牙錄音來源")
    parser.add_argument("--audio-channels", type=int, choices=(1, 2), default=1)
    parser.add_argument("--audio-rate", type=int, help="輸入取樣率；預設裝置取樣率，服務端轉成 16 kHz")
    parser.add_argument("--audio-wake-word", default="小狗", help="移動／切換模式的喚醒詞；單獨說停止不需要")
    parser.add_argument("--audio-max-age", type=float, default=10.0, help="超過此秒數的語音不執行")
    parser.add_argument("--audio-file", type=Path, help="以 1x 回放 PCM16 WAV，僅允許搭配 --fake")
    parser.add_argument("--llm", action="store_true", help="用本機 iGPU LLM 判斷單一步驟的行走方向")
    parser.add_argument("--llm-url", default="http://127.0.0.1:18083")
    parser.add_argument("--llm-model", default="robot-igpu")
    parser.add_argument("--llm-verbose-reason", action="store_true",
                        help="除錯時要求完整 LLM 理由；預設只產生短標籤以降低延遲")
    parser.add_argument("--allow-motion", action="store_true", help="明確授權 LLM 模式連接實機馬達")
    args = parser.parse_args()
    if not math.isfinite(args.audio_max_age) or args.audio_max_age <= 0:
        parser.error("--audio-max-age 必須大於 0")
    if args.audio_file and (not args.fake or args.no_audio):
        parser.error("--audio-file 必須搭配 --fake，且不能搭配 --no-audio")
    if args.list_mics:
        list_microphones()
        return
    if args.list_pipewire_mics:
        from inputs.pipewire import list_microphones
        list_microphones()
        return
    if args.audio_file and args.pipewire_source is not None:
        parser.error("--audio-file 與 PipeWire 輸入不能同時使用")
    if args.llm and not (args.fake or args.allow_motion):
        parser.error("LLM 模式請明確選 --fake，或在安全空間確認後使用 --allow-motion")
    if args.allow_motion and (args.fake or not args.llm):
        parser.error("--allow-motion 只用於 --llm，且不能與 --fake 同時使用")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    log("🔚 結束")


if __name__ == "__main__":
    main()
