"""
用 iMac 藍牙 (BLE) 控制 micro:bit V7RC 自走車。

micro:bit 端：使用 V7RC 擴充積木 (https://github.com/lioujj/pxt-V7RC)，
它會開啟藍牙 UART，讀到 '#' 為一筆訊息，格式為：
    SRV + ch1(4位) + ch2(4位) + ch3(4位) + ch4(4位) + '#'
    例如 "SRV1500150015001500#"，數值 1000~2000，1500 為中立。
依照你的積木：ch1 > 1700 前進、< 1300 後退；ch2 > 1700 右轉、< 1300 左轉。

用法（在程式裡呼叫函式）：
    import asyncio
    from v7rc_car import Car

    async def main():
        async with Car() as car:
            await car.forward(1.0)   # 前進 1 秒
            await car.right(0.5)     # 右轉 0.5 秒
            await car.stop()

    asyncio.run(main())
"""

import asyncio
from bleak import BleakClient, BleakScanner

UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

NEUTRAL = 1500


def _clamp(v):
    return max(1000, min(2000, int(v)))


class Car:
    def __init__(self, name_hint="micro:bit", send_interval=0.1, scan_timeout=10.0):
        self.name_hint = name_hint.lower()
        self.send_interval = send_interval  # 持續重送的間隔（秒）
        self.scan_timeout = scan_timeout
        self.client = None
        self._write_char = None
        self._channels = [NEUTRAL] * 4
        self._sender = None

    # ---------- 連線 ----------
    async def connect(self):
        print("🔍 搜尋 micro:bit ...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, adv: bool(d.name and self.name_hint in d.name.lower())
            or UART_SERVICE_UUID in [u.lower() for u in adv.service_uuids],
            timeout=self.scan_timeout,
        )
        if device is None:
            raise RuntimeError("找不到 micro:bit：請確認已燒錄 V7RC 程式、已開機，且手機 V7RC App 沒有連著它")

        print(f"🔗 連線到 {device.name} ...")
        self.client = BleakClient(device)
        await self.client.connect()

        # micro:bit 的 UART 兩個特徵 (…0002 / …0003) 在不同文件裡方向寫法相反，
        # 所以直接找「可以寫入」的那一個，避免寫錯。
        service = self.client.services.get_service(UART_SERVICE_UUID)
        if service is None:
            await self.client.disconnect()
            raise RuntimeError("此裝置沒有藍牙 UART 服務：請確認積木程式有使用 V7RC 擴充")
        for ch in service.characteristics:
            if "write" in ch.properties or "write-without-response" in ch.properties:
                self._write_char = ch
                break
        if self._write_char is None:
            await self.client.disconnect()
            raise RuntimeError("找不到可寫入的 UART 特徵")

        print(f"✅ 已連線（寫入特徵 {self._write_char.uuid}）")
        self._sender = asyncio.create_task(self._send_loop())

    async def disconnect(self):
        if self._sender:
            self._sender.cancel()
            self._sender = None
        if self.client and self.client.is_connected:
            self._channels = [NEUTRAL] * 4
            await self._send_now()
            await self.client.disconnect()
        print("👋 已中斷連線")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    # ---------- 底層傳送 ----------
    def _message(self):
        return "SRV" + "".join(f"{c:04d}" for c in self._channels) + "#"

    async def _send_now(self):
        await self.client.write_gatt_char(self._write_char, self._message().encode("ascii"))

    async def _send_loop(self):
        last = None
        while self.client and self.client.is_connected:
            msg = self._message()
            if msg != last:
                print(f"📡 {msg}")
                last = msg
            try:
                await self._send_now()
            except Exception as e:
                print(f"⚠️ 傳送失敗：{e}")
                return
            await asyncio.sleep(self.send_interval)

    # ---------- 給你呼叫的控制函式 ----------
    def set_channels(self, ch1=NEUTRAL, ch2=NEUTRAL, ch3=NEUTRAL, ch4=NEUTRAL):
        """直接設定四個頻道 (1000~2000)，會在背景持續送出直到改變。"""
        self._channels = [_clamp(ch1), _clamp(ch2), _clamp(ch3), _clamp(ch4)]

    async def drive(self, ch1, ch2, seconds=None):
        """設定頻道；給 seconds 的話跑完會自動停車。"""
        self.set_channels(ch1, ch2)
        await self._send_now()  # 立刻送一次，不等下一輪
        if seconds is not None:
            await asyncio.sleep(seconds)
            await self.stop()

    async def forward(self, seconds=None, power=300):
        await self.drive(NEUTRAL + power, NEUTRAL, seconds)

    async def backward(self, seconds=None, power=300):
        await self.drive(NEUTRAL - power, NEUTRAL, seconds)

    async def left(self, seconds=None, power=300):
        await self.drive(NEUTRAL, NEUTRAL - power, seconds)

    async def right(self, seconds=None, power=300):
        await self.drive(NEUTRAL, NEUTRAL + power, seconds)

    async def stop(self):
        self.set_channels()
        await self._send_now()


# ---------- 範例：電腦自己開車 ----------
async def demo():
    async with Car() as car:
        await car.forward(1.5)
        await asyncio.sleep(0.5)
        await car.right(1.0)
        await asyncio.sleep(0.5)
        await car.backward(1.5)
        await asyncio.sleep(0.5)
        await car.left(1.0)


if __name__ == "__main__":
    asyncio.run(demo())
