"""列出附近所有藍牙裝置，用來檢查 Mac 看不看得到 micro:bit。"""

import asyncio
from bleak import BleakScanner


async def main():
    print("🔍 掃描 10 秒，請把 micro:bit 靠近電腦 ...\n")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    for device, adv in sorted(found.values(), key=lambda x: -x[1].rssi):
        name = device.name or adv.local_name or "(沒有名稱)"
        mark = "👉" if "micro" in name.lower() else "  "
        print(f"{mark} {name:30} 訊號 {adv.rssi:4}  {device.address}")
    print(f"\n共 {len(found)} 個裝置")


asyncio.run(main())
