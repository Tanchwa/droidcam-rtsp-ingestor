"""FastAPI application: static frontend + inference WebSocket.

    Browser --ws--> this server --POST(SSE)--> llm-d router gateway
                                                    | hook provisions
                                                    v
                              DroidCam --> handler --> inference pool

The browser sends the DroidCam URL + prompt over the WebSocket. This server
passes that URL along in one request to the gateway, which both provisions the
handler for the session and carries the model's tokens back.

This process never connects to the DroidCam URL -- it only relays it. Frame
capture happens entirely in the handler workload, so there is no OpenCV here,
and this process never contacts the handler either.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .gateway import FrameEnd, FrameStart, GatewayClient, Token, new_session_id

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="DroidCam -> llm-d VLM Frontend")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "gateway_endpoint": settings.chat_url}


@app.get("/api/defaults")
async def defaults() -> dict:
    return {
        "prompt": settings.default_prompt,
        "interval": settings.default_interval,
        "model": settings.model,
    }


async def _send(ws: WebSocket, **payload) -> None:
    await ws.send_text(json.dumps(payload))


async def _relay_session(
    ws: WebSocket,
    gw: GatewayClient,
    session_id: str,
    stream_url: str,
    prompt: str,
    interval: float,
) -> None:
    """Pump gateway events onto the browser WebSocket until the response ends."""
    try:
        async for event in gw.stream_session(session_id, stream_url, prompt, interval):
            if isinstance(event, FrameStart):
                await _send(ws, type="frame_start", index=event.index)
            elif isinstance(event, Token):
                await _send(ws, type="token", index=event.index, text=event.text)
            elif isinstance(event, FrameEnd):
                await _send(ws, type="frame_end", index=event.index)
        await _send(ws, type="status", message="session ended")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _send(ws, type="error", message=str(exc))


@app.websocket("/ws/inference")
async def inference_ws(ws: WebSocket) -> None:
    await ws.accept()
    gw: GatewayClient | None = None
    relay: asyncio.Task | None = None
    session_id: str | None = None

    async def teardown() -> None:
        """Close the gateway token response. Note this does NOT stop the handler.

        Nothing can reach the handler pod, so it keeps sampling the DroidCam
        stream until one of its own backstops (MAX_SESSION_SECONDS /
        MAX_FRAMES / IDLE_TIMEOUT) trips. Closing here only stops tokens
        reaching this browser.
        """
        nonlocal gw, relay
        if relay and not relay.done():
            relay.cancel()
            try:
                await relay
            except asyncio.CancelledError:
                pass
        relay = None
        if gw:
            await gw.aclose()
            gw = None

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            action = msg.get("action")

            if action == "start":
                if relay and not relay.done():
                    await _send(ws, type="status", message="already running")
                    continue

                stream_url = (msg.get("stream_url") or "").strip()
                if not stream_url:
                    await _send(ws, type="error", message="stream_url is required")
                    continue
                prompt = (msg.get("prompt") or settings.default_prompt).strip()
                interval = float(msg.get("interval") or settings.default_interval)

                session_id = new_session_id()
                gw = GatewayClient(settings)
                await _send(
                    ws,
                    type="status",
                    message=(
                        f"session {session_id}: asking gateway to provision a "
                        f"handler for {stream_url} …"
                    ),
                    session_id=session_id,
                )
                relay = asyncio.create_task(
                    _relay_session(ws, gw, session_id, stream_url, prompt, interval)
                )

            elif action == "stop":
                await teardown()
                await _send(
                    ws,
                    type="status",
                    message=(
                        "stopped receiving tokens "
                        "(handler exits on its own backstop)"
                    ),
                )

            else:
                await _send(ws, type="error", message=f"unknown action: {action!r}")

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await teardown()
