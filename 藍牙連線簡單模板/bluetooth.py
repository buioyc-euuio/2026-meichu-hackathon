import asyncio
import tkinter as tk
from bleak import BleakClient, BleakScanner

# micro:bit BLE UART UUID
UART_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

class MacCarController:
    def __init__(self):
        self.client = None
        self.ch1 = 1500  # 前後
        self.ch2 = 1500  # 左右
        self.pressed_keys = set()
        self.running = True

    async def connect(self):
        print("🔍 正在搜尋 micro:bit...")
        devices = await BleakScanner.discover()
        target = None
        for d in devices:
            if d.name and "micro:bit" in d.name.lower():
                target = d
                break
        
        if not target:
            print("❌ 找不到 micro:bit，請確認已開機！")
            return False

        print(f"🔗 找到裝置 {target.name}，正在連線...")
        self.client = BleakClient(target.address)
        await self.client.connect()
        print("✅ 藍牙連線成功！")
        return True

    def update_channels(self):
        ch1 = 1500
        ch2 = 1500

        if 'Up' in self.pressed_keys:
            ch1 = 1800
        elif 'Down' in self.pressed_keys:
            ch1 = 1200

        if 'Left' in self.pressed_keys:
            ch2 = 1200
        elif 'Right' in self.pressed_keys:
            ch2 = 1800

        self.ch1 = ch1
        self.ch2 = ch2

    def on_key_down(self, event):
        if event.keysym in ['Up', 'Down', 'Left', 'Right']:
            if event.keysym not in self.pressed_keys:
                self.pressed_keys.add(event.keysym)
                self.update_channels()

    def on_key_up(self, event):
        if event.keysym in self.pressed_keys:
            self.pressed_keys.remove(event.keysym)
            self.update_channels()

    async def send_loop(self):
        last_cmd = ""
        while self.running and self.client and self.client.is_connected:
            cmd_str = f"SRV{self.ch1:04d}{self.ch2:04d}15001500#"
            await self.client.write_gatt_char(UART_RX_CHAR_UUID, cmd_str.encode('utf-8'))
            if cmd_str != last_cmd:
                print(f"📡 發送指令: {cmd_str}")
                last_cmd = cmd_str
            await asyncio.sleep(0.1)

    async def disconnect(self):
        if self.client:
            await self.client.write_gatt_char(UART_RX_CHAR_UUID, "SRV1500150015001500#".encode('utf-8'))
            await self.client.disconnect()
            print("👋 已安全連線中斷。")

async def main():
    controller = MacCarController()
    if await controller.connect():
        # 建立 Tkinter 控制視窗
        root = tk.Tk()
        root.title("micro:bit 遙控器")
        root.geometry("300x150")
        
        label = tk.Label(root, text="請在此視窗內使用【方向鍵】控制\n關閉視窗或按 Esc 結束", font=("Arial", 14), pad=20)
        label.pack()

        # 綁定按鍵事件
        root.bind("<KeyPress>", controller.on_key_down)
        root.bind("<KeyRelease>", controller.on_key_up)
        root.bind("<Escape>", lambda e: root.destroy())

        # 定義 GUI 關閉時的動作
        def on_close():
            controller.running = False
            root.destroy()
        
        root.protocol("WM_DELETE_WINDOW", on_close)

        # 讓 asyncio 與 tkinter 共存更新
        async def gui_loop():
            try:
                while controller.running and root.winfo_exists():
                    root.update()
                    await asyncio.sleep(0.02)
            except tk.TclError:
                pass
            controller.running = False

        # 同時執行藍牙發送與 GUI 畫面更新
        await asyncio.gather(
            controller.send_loop(),
            gui_loop()
        )

        await controller.disconnect()

if __name__ == "__main__":
    asyncio.run(main())