"""
模式二：相機追球（stub）。

真的追球邏輯在 camera_control/ball_center.py（之後還會加防撞），這裡先只放「接口」：
進入模式時開一個背景工作，離開時取消它、停馬達、相機回正。現在背景工作什麼都不做。

之後接上 camera_control 大概是這樣（_run 裡面）：

    from ball_center import CameraController, Feed, control_loop
    controller = CameraController(self.robot, self.args)       # args 要有 threshold_x、kp… 見 ball_center.main()
    await controller.center(time.monotonic())
    feed = Feed()
    receiver = asyncio.create_task(feed.receive_forever(self.args.url))
    try:
        await control_loop(controller, feed, self.ball_commands, on_output)   # 放 "q" 進 ball_commands 就會結束
    finally:
        receiver.cancel()

注意：
    - 共用同一個 RobotBLE（self.robot），不要在模式裡再連一次藍牙
    - 要開車（M 指令）就用 self.driver，才會經過防撞（safety.py）；
      偵測結果可以用 self.guard.update(out) 餵給防撞
    - 不要在 exit() 裡斷線，只要停馬達（robot 由 main.py 管）
"""

import asyncio

from modes.base import Mode
from robot_ble import log

PAN_CENTER = 90


class BallMode(Mode):
    name = "ball"

    def __init__(self, robot, driver, guard, args=None):
        self.robot = robot     # camera_control/robot_ble.RobotBLE
        self.driver = driver   # drive.WheelDriver（開車要經過防撞）
        self.guard = guard     # safety.CollisionGuard
        self.args = args       # 給 CameraController 的設定（之後接上時用）
        self.task = None

    async def enter(self):
        log("🎾 追球模式：相機會自動對準網球（說「語音模式」或「停」回到語音模式）")
        self.task = asyncio.create_task(self._run())

    async def exit(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self.driver.stop()                     # 輪子、相機上下都停（S#）
        await self.robot.send(f"P,{PAN_CENTER}#", reliable=True)   # 相機回正

    async def _run(self):
        # TODO: 換成 camera_control 的追球（見最上面的說明）
        log("   （stub）追球還沒接上 camera_control，先什麼都不做")
        while True:
            await asyncio.sleep(1)
