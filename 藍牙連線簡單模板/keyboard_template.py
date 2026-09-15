"""
最簡單的 micro:bit 藍牙鍵盤遙控 template

在終端機按方向鍵 → 透過藍牙送出一段文字 → micro:bit 顯示箭頭
空白鍵 = 停止（清空畫面），q = 離開

執行：.venv/bin/python keyboard_template.py
"""

import asyncio
import sys
import termios
import tty

from bleak import BleakClient, BleakScanner

# ====== 想改的東西都在這裡 ======

# 按鍵 → 要送出的訊息（micro:bit 端用 '#' 當結尾來切一筆訊息）
KEY_MESSAGES = {
    "UP":    "U#",
    "DOWN":  "D#",
    "LEFT":  "L#",
    "RIGHT": "R#",
    "SPACE": "S#",
}

DEVICE_NAME = "micro:bit"   # 藍牙名稱包含這段文字就會連

# ================================

UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}  # 終端機方向鍵是 ESC [ A/B/C/D


async def connect():
    print("🔍 搜尋 micro:bit ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: DEVICE_NAME.lower() in (d.name or adv.local_name or "").lower(), timeout=10)
    if device is None:
        sys.exit("❌ 找不到 micro:bit（確認已開機、手機 App 沒連著它）")

    client = BleakClient(device)
    await client.connect()

    # 找出 UART 服務裡「可以寫入」的特徵（micro:bit 的 0002/0003 方向常搞混，直接用找的）
    write_char = next(
        c for c in client.services.get_service(UART_SERVICE).characteristics
        if "write" in c.properties or "write-without-response" in c.properties
    )
    print(f"✅ 已連線 {device.name}")
    return client, write_char


async def send(client, write_char, text):
    await client.write_gatt_char(write_char, text.encode())
    print(f"📡 送出 {text}\r")


def read_key():
    """讀一個按鍵，回傳 'UP' / 'DOWN' / 'LEFT' / 'RIGHT' / 'SPACE' / 'q' 或其他字元。"""
    ch = sys.stdin.read(1)
    if ch == "\x1b":                 # 方向鍵是三個字元：ESC [ X
        sys.stdin.read(1)
        return ARROWS.get(sys.stdin.read(1), "")
    if ch == " ":
        return "SPACE"
    return ch


async def main():
    client, write_char = await connect()
    print("⌨️  方向鍵控制，空白鍵停止，q 離開")

    loop = asyncio.get_running_loop()
    keys = asyncio.Queue()
    loop.add_reader(sys.stdin, lambda: keys.put_nowait(read_key()))

    old_settings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin)         # 不用按 Enter 就能讀到按鍵
    try:
        while client.is_connected:
            key = await keys.get()
            if key == "q":
                break
            if key in KEY_MESSAGES:
                await send(client, write_char, KEY_MESSAGES[key])
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        loop.remove_reader(sys.stdin)
        if client.is_connected:
            await send(client, write_char, KEY_MESSAGES["SPACE"])
            await client.disconnect()
        print("👋 已斷線")


if __name__ == "__main__":
    asyncio.run(main())
