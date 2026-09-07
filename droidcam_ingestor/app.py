"""FastAPI application: static frontend + inference WebSocket.

Frontend  ──ws──►  this server  ──http──►  VLM inference server
                       │
                       └── OpenCV pulls frames from the DroidCam RTSP/MJPEG URL

The browser sends the DroidCam URL + prompt over the WebSocket; the server
samples frames on an interval, forwards each to the VLM, and streams the
model's reply back so responses appear live.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .rtsp import FrameGrabber, encode_jpeg
from .vlm import VLMClient

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="DroidCam RTSP → VLM Ingestor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "vlm_endpoint": settings.chat_url}


async def _send(ws: WebSocket, **payload) -> None:
    await ws.send_text(json.dumps(payload))


async def _run_inference_loop(
    ws: WebSocket,
    grabber: FrameGrabber,
    vlm: VLMClient,
    prompt: str,
    interval: float,
    stop: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    index = 0
    while not stop.is_set():
        frame = grabber.read_latest()
        if frame is None:
            if grabber.error:
                await _send(ws, type="status", message=f"waiting for frames ({grabber.error})")
            await asyncio.sleep(0.25)
            continue

        index += 1
        try:
            jpeg = await loop.run_in_executor(
                None,
                encode_jpeg,
                frame,
                settings.jpeg_quality,
                settings.max_frame_edge,
            )
            await _send(ws, type="frame_start", index=index)
            async for chunk in vlm.stream_inference(prompt, jpeg):
                await _send(ws, type="token", index=index, text=chunk)
            await _send(ws, type="frame_end", index=index)
        except Exception as exc:  # surface VLM/encode errors to the UI, keep going
            await _send(ws, type="error", message=str(exc))

        # Wait out the sampling interval, but stay responsive to stop.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@app.websocket("/ws/inference")
async def inference_ws(ws: WebSocket) -> None:
    await ws.accept()
    grabber: FrameGrabber | None = None
    vlm: VLMClient | None = None
    stop = asyncio.Event()
    loop_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            action = msg.get("action")

            if action == "start":
                if loop_task and not loop_task.done():
                    await _send(ws, type="status", message="already running")
                    continue

                stream_url = (msg.get("stream_url") or "").strip()
                if not stream_url:
                    await _send(ws, type="error", message="stream_url is required")
                    continue
                prompt = (msg.get("prompt") or "Describe what you see in this image.").strip()
                interval = float(msg.get("interval") or settings.default_interval)

                await _send(ws, type="status", message=f"connecting to {stream_url} …")
                grabber = FrameGrabber(stream_url)
                try:
                    await loop.run_in_executor(None, grabber.start)
                except Exception as exc:
                    await _send(ws, type="error", message=f"stream error: {exc}")
                    grabber = None
                    continue

                vlm = VLMClient(settings)
                stop = asyncio.Event()
                await _send(
                    ws,
                    type="status",
                    message=f"streaming; VLM={settings.chat_url} model={settings.vlm_model}",
                )
                loop_task = asyncio.create_task(
                    _run_inference_loop(ws, grabber, vlm, prompt, interval, stop)
                )

            elif action == "stop":
                stop.set()
                if loop_task:
                    await loop_task
                    loop_task = None
                if grabber:
                    await loop.run_in_executor(None, grabber.stop)
                    grabber = None
                if vlm:
                    await vlm.aclose()
                    vlm = None
                await _send(ws, type="status", message="stopped")

            else:
                await _send(ws, type="error", message=f"unknown action: {action!r}")

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stop.set()
        if loop_task and not loop_task.done():
            loop_task.cancel()
        if grabber:
            await loop.run_in_executor(None, grabber.stop)
        if vlm:
            await vlm.aclose()
