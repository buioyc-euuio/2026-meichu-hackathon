"""
FastAPI 伺服器：相機 → YOLO + CV 追蹤 → 即時把每張畫面的結果（球和人的框）用 JSON 串流出去。

    .venv/bin/python tennis_tracking/server.py                      # http://127.0.0.1:8000
    .venv/bin/python tennis_tracking/server.py --host 0.0.0.0       # 讓同一個網路的其他裝置也能連
    .venv/bin/python tennis_tracking/server.py --video test.mp4     # 用影片代替相機（會一直重播）

網址：
    GET  /          網頁：看標好框的畫面 + 即時 JSON（除錯用）
    WS   /ws        WebSocket：每張畫面推一個 JSON（給程式用，最即時）
    GET  /stream    Server-Sent Events：一樣的 JSON，`curl -N http://127.0.0.1:8000/stream` 就能看
    GET  /latest    最新一張畫面的 JSON
    GET  /video     標好框的即時影像（MJPEG，瀏覽器直接開；沒人看的時候不會浪費 CPU 去畫）
    GET  /health    伺服器狀態

JSON 格式寫在 pipeline.py 最上面。

用戶端範例：tennis_tracking/client_example.py

注意：伺服器每張畫面都會推（約 30 次／秒）。如果用戶端處理一次要比 1/30 秒久（例如控制馬達、問 LLM），
訊息會在用戶端那邊越積越多、延遲越來越大。解法：用一個背景工作一直收、只留最新的一筆，
主程式要用的時候拿「最新的」就好（client_example.py 就是這樣寫的）。
"""

import argparse
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager

import cv2
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from pipeline import Pipeline
from track_ball import CAMERA_INDEX, camera_arg, open_camera

JPEG_QUALITY = 70


class Hub:
    """存最新一張的結果，有新的就叫醒所有在等的用戶端。只在 asyncio 的執行緒裡用。"""

    def __init__(self):
        self.seq = 0
        self.text = None       # 最新結果（已經轉成 JSON 字串，每張只轉一次）
        self.jpeg = None       # 最新標好框的畫面
        self.video_clients = 0
        self.cond = asyncio.Condition()

    def publish(self, text, jpeg):
        self.seq += 1
        self.text = text
        if jpeg is not None:
            self.jpeg = jpeg
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self.cond:
            self.cond.notify_all()

    async def wait_next(self, seq):
        """等到有比 seq 新的結果；如果已經有了就馬上回傳（跳過中間的，永遠拿最新）。"""
        async with self.cond:
            await self.cond.wait_for(lambda: self.seq > seq)
            return self.seq, self.text, self.jpeg


class Worker(threading.Thread):
    """背景執行緒：一直讀相機、跑 YOLO + 追蹤，把結果交給 Hub。"""

    def __init__(self, args, hub, loop):
        super().__init__(daemon=True)
        self.args, self.hub, self.loop = args, hub, loop
        self.stopping = threading.Event()
        self.status = "starting"
        self.mode = None
        self.pipeline = None

    def open_source(self):
        if self.args.video:
            cap = cv2.VideoCapture(self.args.video)
            if not cap.isOpened():
                raise RuntimeError(f"打不開 {self.args.video}")
            return cap
        return open_camera(self.args.camera)

    def run(self):
        try:
            pipeline = self.pipeline = Pipeline(use_yolo=not self.args.no_yolo, device=self.args.device, conf=self.args.conf)
            self.mode = pipeline.mode
            cap = self.open_source()
        except (Exception, SystemExit) as e:
            self.status = f"error: {e}"
            print(f"❌ {e}")
            return
        # 影片照原本的速度播（不然一下子就跑完），播完從頭再來
        frame_time = 1 / (cap.get(cv2.CAP_PROP_FPS) or 30) if self.args.video else 0
        self.status = "running"
        print(f"🎾 開始追蹤（{self.mode}）")
        try:
            while not self.stopping.is_set():
                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    if self.args.video:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.status = "error: 讀取相機失敗"
                    print("❌ 讀取相機失敗")
                    break
                result = pipeline.process(frame)
                jpeg = None
                if self.hub.video_clients > 0:  # 有人在看 /video 才畫、才壓 JPEG
                    pipeline.draw(frame, result)
                    jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])[1].tobytes()
                text = json.dumps(result, ensure_ascii=False)
                self.loop.call_soon_threadsafe(self.hub.publish, text, jpeg)
                if frame_time:
                    time.sleep(max(0.0, frame_time - (time.monotonic() - t0)))
        finally:
            cap.release()
            if self.status == "running":
                self.status = "stopped"


def create_app(args):
    @asynccontextmanager
    async def lifespan(app):
        app.state.hub = Hub()
        app.state.worker = Worker(args, app.state.hub, asyncio.get_running_loop())
        app.state.worker.start()
        yield
        app.state.worker.stopping.set()
        app.state.worker.join(timeout=3)

    app = FastAPI(title="tennis_tracking", lifespan=lifespan)
    # 讓別的網頁（不同 port 的前端）也能呼叫 /latest
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/health")
    async def health():
        w, hub = app.state.worker, app.state.hub
        info = {"status": w.status, "mode": w.mode, "frames": hub.seq}
        if w.pipeline is not None:  # 現在燈光下學到的網球顏色範圍（HSV），看它有沒有跟著燈光變
            lower, upper = w.pipeline.balls.color.range
            info["ball_color_hsv"] = {"lower": lower, "upper": upper, "samples": w.pipeline.balls.color.samples}
        return info

    @app.get("/latest")
    async def latest():
        text = app.state.hub.text
        if text is None:
            return JSONResponse({"error": "還沒有畫面", "status": app.state.worker.status}, status_code=503)
        return JSONResponse(json.loads(text))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        hub, seq = app.state.hub, app.state.hub.seq
        try:
            while True:
                seq, text, _ = await hub.wait_next(seq)
                await websocket.send_text(text)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/stream")
    async def stream(request: Request):
        hub = app.state.hub

        async def events():
            seq = hub.seq
            while not await request.is_disconnected():
                try:
                    seq, text, _ = await asyncio.wait_for(hub.wait_next(seq), timeout=5)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {text}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/video")
    async def video(request: Request):
        hub = app.state.hub

        async def frames():
            hub.video_clients += 1
            try:
                seq = hub.seq
                while not await request.is_disconnected():
                    try:
                        seq, _, jpeg = await asyncio.wait_for(hub.wait_next(seq), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if jpeg is not None:
                        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            finally:
                hub.video_clients -= 1

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return INDEX_HTML

    return app


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>tennis_tracking</title>
<style>body{font-family:sans-serif;margin:16px;background:#111;color:#eee}
.row{display:flex;gap:16px;flex-wrap:wrap}img{max-width:100%;border:1px solid #444}
pre{background:#222;padding:8px;max-height:480px;overflow:auto;font-size:12px;flex:1;min-width:320px}</style>
</head><body>
<h3>tennis_tracking <span id="info"></span></h3>
<div class="row"><img src="/video" width="640"><pre id="json">connecting...</pre></div>
<script>
const info = document.getElementById("info"), pre = document.getElementById("json");
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = e => {
    const r = JSON.parse(e.data);
    info.textContent = `frame ${r.frame} · ${r.fps} fps · balls ${r.balls.length} · people ${r.people.length}`;
    pre.textContent = JSON.stringify(r, null, 1);
  };
  ws.onclose = () => { info.textContent = "(disconnected, retrying...)"; setTimeout(connect, 1000); };
}
connect();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = 讓同一個網路的其他裝置也能連")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--camera", type=camera_arg, default=CAMERA_INDEX,
                        help="相機編號（0）或路徑（預設 GO 3S 的 /dev/v4l/by-id/ 路徑）")
    parser.add_argument("--video", help="用影片檔代替相機（會一直重播）")
    parser.add_argument("--no-yolo", action="store_true", help="不用 YOLO，只靠顏色 + 前後畫面（不找人）")
    parser.add_argument("--conf", type=float, default=0.05, help="YOLO 找球的信心門檻（預設故意放低）")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu")
    args = parser.parse_args()
    print(f"🌐 http://{args.host}:{args.port}  （Ctrl+C 結束）", flush=True)
    uvicorn.run(create_app(args), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
