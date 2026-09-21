"""The llm-d router gateway contract.

THIS MODULE IS THE JOINT between the frontend and the llm-d router. The trigger
POST does two things, and notably NOT a third:

  1. It carries the cellphone camera address, so the router's gateway hook can
     provision a handler for this session and inject that address into it.
  2. It carries this pod's callback address, so the handler knows where to send
     the session's output. This pod's, not the Service's: each session's
     WebSocket lives in one replica's memory, and results balanced to another
     replica would land somewhere that has never heard of the session.
  3. It does NOT carry the tokens back. It used to be held open as the
     session's response on the theory that the gateway would join the handler's
     output onto it, but nothing can do that join -- the handler posts frames
     straight to the assigned pod, so the gateway never sees the response, and
     an Envoy ext_proc filter cannot splice one request's response into
     another's stream anyway. The trigger is now a short request that returns
     as soon as the handler is provisioned, and results arrive separately at
     ``POST /ingest/{session_id}``.

Note the frontend only ever passes the cellphone camera address along as a value; it
never opens that stream. The handler is the only component that connects to it,
which is why nothing here imports OpenCV.

The frontend never learns the handler's address and never contacts it. If your
gateway wants the stream URL somewhere else (a different header, a body field,
a routing path), change ``_headers``/``_build_trigger_payload`` here and
nothing else in the frontend moves. The handler's mirror image of this contract
lives in ``cellphone_camera_handler/pool.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Iterable, Iterator

import httpx

from .config import Settings

#: Must match ``SESSION_HEADER`` in cellphone_camera_handler/pool.py.
SESSION_HEADER = "x-llmd-session-id"
#: Distinguishes the frontend's provisioning trigger from the handler's frame
#: traffic, so the hook provisions exactly one handler per session.
ORIGIN_HEADER = "x-llmd-frame-source"
ORIGIN_VALUE = "frontend-trigger"
#: Origin value that asks the hook to delete this session's handler. Must match
#: the hook's ``stopOrigin``.
STOP_ORIGIN_VALUE = "frontend-stop"
#: Where the hook reads the cellphone camera address from.
STREAM_URL_HEADER = "x-cellphone-camera-stream-url"
FRAME_INTERVAL_HEADER = "x-cellphone-camera-frame-interval"
#: Where the hook reads this pod's results callback from. The hook appends the
#: session id and injects the result as the handler's RESULTS_CALLBACK_URL.
RESULTS_CALLBACK_HEADER = "x-cellphone-camera-results-callback"


def new_session_id() -> str:
    return f"cellphone-camera-{uuid.uuid4().hex[:12]}"


@dataclass
class FrameStart:
    index: int


@dataclass
class Token:
    index: int
    text: str


@dataclass
class FrameEnd:
    index: int


Event = FrameStart | Token | FrameEnd


class GatewayClient:
    """Opens one streaming session against the llm-d router gateway."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.response_timeout, connect=10.0)
        )

    def _headers(self, session_id: str, stream_url: str, interval: float) -> dict:
        headers = {
            "Content-Type": "application/json",
            SESSION_HEADER: session_id,
            ORIGIN_HEADER: ORIGIN_VALUE,
            # The hook injects these into the handler pod it creates.
            STREAM_URL_HEADER: stream_url,
            FRAME_INTERVAL_HEADER: str(interval),
        }
        # Omitted when this process cannot name itself (no POD_IP outside
        # Kubernetes); the hook then falls back to its configured base URL.
        if self._settings.callback_base:
            headers[RESULTS_CALLBACK_HEADER] = self._settings.callback_base
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    def _build_trigger_payload(
        self, session_id: str, stream_url: str, prompt: str, interval: float
    ) -> dict:
        """The request that provisions the handler and opens the token stream.

        Shaped like an ordinary chat completion so llm-d routes it to a pool
        normally, with the handler's configuration hung off a ``cellphone-camera``
        extension object the hook reads. Everything under ``cellphone-camera`` becomes
        env vars on the handler pod.
        """
        body = {
            "model": self._settings.model,
            "stream": False,
            "session_id": session_id,
            "cellphone-camera": {
                "stream_url": stream_url,
                "prompt": prompt,
                "interval": interval,
            },
            # The prompt is repeated here so the request is still a valid,
            # routable completion even if the hook is not installed. It is
            # capped at one token because nobody reads the answer -- this
            # request exists to be routed and inspected, not to generate.
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,
        }
        # Body fallback for gateways that strip unknown request headers.
        if self._settings.callback_base:
            body["cellphone-camera"]["results_callback"] = self._settings.callback_base
        return body

    async def trigger_session(
        self, session_id: str, stream_url: str, prompt: str, interval: float
    ) -> None:
        """Ask the router to provision a handler for this session.

        Returns as soon as the router has accepted the request -- the handler
        is running by then, and its output will arrive at this pod's
        /ingest/{session_id} rather than on this connection. Raises if the
        router refused, which is how a bad camera URL or an out-of-allowlist
        callback reaches the browser as an error rather than as silence.

        ``stream_url`` is passed through untouched for the hook to consume --
        this method never connects to it.
        """
        payload = self._build_trigger_payload(session_id, stream_url, prompt, interval)
        headers = self._headers(session_id, stream_url, interval)

        resp = await self._client.post(
            self._settings.chat_url, json=payload, headers=headers
        )
        if resp.status_code >= 400:
            body = resp.text
            raise RuntimeError(f"gateway {resp.status_code}: {body[:500]}")

    async def stop_session(self, session_id: str) -> None:
        """Ask the router to delete this session's handler.

        Without this the handler keeps sampling the camera until one of its own
        backstops trips, burning pool capacity on frames nobody will see. Best
        effort: the backstops are still the guarantee, this is just the polite
        path.
        """
        headers = {
            "Content-Type": "application/json",
            SESSION_HEADER: session_id,
            ORIGIN_HEADER: STOP_ORIGIN_VALUE,
        }
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        await self._client.post(
            self._settings.chat_url,
            json={
                "model": self._settings.model,
                "session_id": session_id,
                "messages": [{"role": "user", "content": "stop"}],
                "max_tokens": 1,
            },
            headers=headers,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class FrameDecoder:
    """Turns one frame's SSE body into browser events, a chunk at a time.

    The handler forwards each frame's pool response verbatim, so what arrives
    at /ingest is an ordinary chat-completion stream: a run of token deltas,
    then a finish_reason. One POST is one frame, so frame boundaries come from
    the request itself and this needs none of the cross-frame state machine the
    old single-stream version carried.

    It is stateful because a POST body arrives in arbitrary chunks: the opening
    FrameStart must be emitted once for the frame, not once per chunk, and a
    line can be split across a chunk boundary.
    """

    def __init__(self, frame_index: int):
        self._index = frame_index
        self._opened = False
        self._buffer = ""

    def feed(self, chunk: str) -> Iterator[Event]:
        """Yield the events completed by this chunk, holding any partial line."""
        self._buffer += chunk
        lines, newline, self._buffer = self._buffer.rpartition("\n")
        if not newline:
            return
        yield from self._lines(lines.split("\n"))

    def finish(self) -> Iterator[Event]:
        """Yield whatever the trailing bytes complete, then close the frame."""
        if self._buffer:
            yield from self._lines([self._buffer])
            self._buffer = ""
        if self._opened:
            self._opened = False
            yield FrameEnd(index=self._index)

    def _lines(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            text = delta.get("content")
            if not text:
                continue
            if not self._opened:
                self._opened = True
                yield FrameStart(index=self._index)
            yield Token(index=self._index, text=text)
