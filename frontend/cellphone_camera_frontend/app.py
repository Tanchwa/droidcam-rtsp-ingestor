"""FastAPI application: static frontend + inference WebSocket.

    Browser --ws--> this server --POST--> llm-d router gateway
                       ^                       | hook provisions
                       |                       v
                       +---- POST /ingest ---- handler --> inference pool
                                                  ^            |
                                            cellphone camera <-+

The browser sends the cellphone camera URL + prompt over the WebSocket. This
server passes that URL to the gateway in a short trigger request, which
provisions a handler for the session and tells it where to send results: this
pod's ``/ingest/{session_id}``, named by POD_IP.

Results come back out of band, not on the trigger's response. The handler posts
frames straight to the pod llm-d assigned -- that is what keeps the session's
prompt warm in that pod's prefix cache -- so the gateway never sees the model's
response and cannot relay it onto the caller's connection.

That makes the callback address pod-specific rather than the Service: a session
lives entirely in one replica's memory, so results sent to the Service would be
balanced to a replica holding no socket for it.

This process never connects to the cellphone camera URL -- it only relays it.
Frame capture happens entirely in the handler workload, so there is no OpenCV
here, and this process never contacts the handler either; the handler contacts
it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .gateway import (
    FrameDecoder,
    FrameEnd,
    FrameStart,
    GatewayClient,
    Token,
    new_session_id,
)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Cellphone Camera -> llm-d VLM Frontend")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

#: Header the handler tags each callback POST with; see handler/pool.py.
FRAME_INDEX_HEADER = "x-cellphone-camera-frame-index"
SESSION_END_HEADER = "x-cellphone-camera-session-end"

#: Sessions this replica is holding a WebSocket for, by session id.
#:
#: In-memory and therefore replica-local, which is exactly why the trigger
#: advertises this pod's own address rather than the Service: a callback that
#: reached a different replica would find nothing here to deliver to. A single
#: process owns the whole dict, and asyncio gives it one thread, so no lock.
_sessions: dict[str, asyncio.Queue] = {}

#: Bounds how far a session's results can run ahead of a browser that is not
#: keeping up. Beyond this the handler's POST blocks, which is the backpressure
#: we want: it slows the handler rather than growing this process without limit.
_QUEUE_MAX = 256


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "gateway_endpoint": settings.chat_url,
        # Empty here means this pod cannot name itself (no POD_IP) and is
        # relying on the router's configured fallback -- which, with more than
        # one replica, delivers results to the wrong pod about half the time.
        "results_callback": settings.callback_base,
        "active_sessions": len(_sessions),
    }


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
    """Provision the handler, then pump its results onto the browser socket.

    The queue is registered before the trigger goes out, not after: the handler
    can be running and posting results before the trigger call has even
    returned, and a result that arrives for an unregistered session is dropped.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _sessions[session_id] = queue
    try:
        await gw.trigger_session(session_id, stream_url, prompt, interval)
        await _send(
            ws,
            type="status",
            message="handler provisioned; waiting for the first frame …",
            session_id=session_id,
        )

        while True:
            event = await queue.get()
            if event is None:  # the handler signalled the session is over
                break
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
    finally:
        _sessions.pop(session_id, None)


@app.post("/ingest/{session_id}")
async def ingest(session_id: str, request: Request) -> JSONResponse:
    """Receive one frame's model output from the handler.

    This is the return path the whole system is built around: the handler was
    told to POST here by the router, which got the address from this pod's own
    trigger. Nothing else reaches this route.

    A session this replica does not own is a 404 rather than a silent 200 --
    the handler logs it, and it is the visible symptom of results being sent to
    the Service instead of to the pod holding the socket.
    """
    queue = _sessions.get(session_id)
    if queue is None:
        return JSONResponse(
            {"error": f"no session {session_id} on this replica"}, status_code=404
        )

    if request.headers.get(SESSION_END_HEADER):
        await queue.put(None)
        return JSONResponse({"status": "ended"})

    try:
        frame_index = int(request.headers.get(FRAME_INDEX_HEADER, "0"))
    except ValueError:
        frame_index = 0

    # Decoded as the body arrives, so tokens reach the browser while the model
    # is still generating them. Awaiting put() is what applies backpressure to
    # the handler when the browser falls behind.
    decoder = FrameDecoder(frame_index)
    async for chunk in request.stream():
        for event in decoder.feed(chunk.decode("utf-8", "replace")):
            await queue.put(event)
    for event in decoder.finish():
        await queue.put(event)

    return JSONResponse({"status": "ok"})


@app.websocket("/ws/inference")
async def inference_ws(ws: WebSocket) -> None:
    await ws.accept()
    gw: GatewayClient | None = None
    relay: asyncio.Task | None = None
    session_id: str | None = None

    async def teardown() -> None:
        """Stop the relay and ask the router to delete the handler.

        Nothing can reach the handler pod directly -- it has no listener -- so
        the stop goes through the router, whose hook deletes the Job. That is
        best effort: if it fails, the handler still dies on one of its own
        backstops (MAX_SESSION_SECONDS / MAX_FRAMES / IDLE_TIMEOUT), which is
        what actually guarantees it does not run forever.
        """
        nonlocal gw, relay, session_id
        if relay and not relay.done():
            relay.cancel()
            try:
                await relay
            except asyncio.CancelledError:
                pass
        relay = None
        if gw:
            if session_id:
                try:
                    await gw.stop_session(session_id)
                except Exception:
                    # The browser is already gone in the common case; there is
                    # nobody to report this to and the backstops still apply.
                    pass
            await gw.aclose()
            gw = None
        if session_id:
            _sessions.pop(session_id, None)
            session_id = None

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
                    message="stopped; handler released",
                )

            else:
                await _send(ws, type="error", message=f"unknown action: {action!r}")

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await teardown()
