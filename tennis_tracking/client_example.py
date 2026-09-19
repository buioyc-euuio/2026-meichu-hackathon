"""
連到 server.py，即時拿到球和人的位置。先開 server，再跑這支：

    .venv/bin/python tennis_tracking/server.py
    .venv/bin/python tennis_tracking/client_example.py

重點：背景一直收、只留最新的一筆（latest）。主程式就算一次要處理很久（控制馬達、問 LLM），
拿到的永遠是最新的畫面，不會越積越多、延遲越來越大。
"""

import argparse
import asyncio
import json

import websockets

latest = None  # 最新一張畫面的結果


async def receive_forever(url):
    """背景工作：一直收，只留最新的；斷線就一秒後重連。"""
    global latest
    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"✅ 連上 {url}")
                async for message in ws:
                    latest = json.loads(message)
        except (OSError, websockets.ConnectionClosed):
            print("⚠️  連不上 server，一秒後重試 ...")
            await asyncio.sleep(1)


async def main(url):
    asyncio.create_task(receive_forever(url))
    while True:
        await asyncio.sleep(0.5)  # 假裝主程式在忙（例如控制馬達），每 0.5 秒才看一次
        if latest is None:
            continue
        balls = [(b["id"], round(b["x"]), round(b["y"]), round(b["d"])) for b in latest["balls"]]
        people = [(p["id"], round(p["x"]), round(p["y"]), p["h"]) for p in latest["people"]]
        print(f"frame {latest['frame']}  球 (id, x, y, 直徑) {balls}  人 (id, x, y, h) {people}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    asyncio.run(main(parser.parse_args().url))
