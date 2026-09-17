"""The llm-d router gateway contract.

THIS MODULE IS THE JOINT between the frontend and the llm-d router. One POST
does double duty:

  1. It carries the DroidCam address, so the router's gateway hook can
     provision a handler for this session and inject that address into it.
  2. It stays open as the session's token response, so tokens the handler
     generates -- by submitting frames to the pool tagged with the same
     ``SESSION_HEADER`` -- come back down this connection to the browser.

Note the frontend only ever passes the DroidCam address along as a value; it
never opens that stream. The handler is the only component that connects to it,
which is why nothing here imports OpenCV.

The frontend never learns the handler's address and never contacts it. If your
gateway wants the stream URL somewhere else (a different header, a body field,
a routing path), change ``_headers``/``_build_trigger_payload`` here and
nothing else in the frontend moves. The handler's mirror image of this contract
lives in ``droidcam_handler/pool.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from .config import Settings

#: Must match ``SESSION_HEADER`` in droidcam_handler/pool.py.
SESSION_HEADER = "x-llmd-session-id"
#: Distinguishes the frontend's provisioning trigger from the handler's frame
#: traffic, so the hook provisions exactly one handler per session.
ORIGIN_HEADER = "x-llmd-frame-source"
ORIGIN_VALUE = "frontend-trigger"
#: Where the hook reads the DroidCam address from.
STREAM_URL_HEADER = "x-droidcam-stream-url"
FRAME_INTERVAL_HEADER = "x-droidcam-frame-interval"


def new_session_id() -> str:
    return f"droidcam-{uuid.uuid4().hex[:12]}"


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
            "Accept": "text/event-stream",
            SESSION_HEADER: session_id,
            ORIGIN_HEADER: ORIGIN_VALUE,
            # The hook injects these into the handler pod it creates.
            STREAM_URL_HEADER: stream_url,
            FRAME_INTERVAL_HEADER: str(interval),
        }
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    def _build_trigger_payload(
        self, session_id: str, stream_url: str, prompt: str, interval: float
    ) -> dict:
        """The request that provisions the handler and opens the token stream.

        Shaped like an ordinary chat completion so llm-d routes it to a pool
        normally, with the handler's configuration hung off a ``droidcam``
        extension object the hook reads. Everything under ``droidcam`` becomes
        env vars on the handler pod.
        """
        return {
            "model": self._settings.model,
            "stream": True,
            "session_id": session_id,
            "droidcam": {
                "stream_url": stream_url,
                "prompt": prompt,
                "interval": interval,
            },
            # The prompt is repeated here so the request is still a valid,
            # routable completion even if the hook is not installed.
            "messages": [{"role": "user", "content": prompt}],
        }

    @staticmethod
    def _frame_index(obj: dict, fallback: int) -> int:
        """Pull the handler's frame index out of a chunk, if it survived.

        The handler tags each submission with ``frame_index``; whether the
        gateway echoes it back depends on how the join is implemented, so fall
        back to counting completions locally.
        """
        for key in ("frame_index", "frame"):
            value = obj.get(key)
            if isinstance(value, int):
                return value
        return fallback

    async def stream_session(
        self, session_id: str, stream_url: str, prompt: str, interval: float
    ) -> AsyncIterator[Event]:
        """Yield frame/token events for a whole session until the gateway ends it.

        ``stream_url`` is passed through untouched for the hook to consume --
        this method never connects to it.
        """
        payload = self._build_trigger_payload(session_id, stream_url, prompt, interval)
        headers = self._headers(session_id, stream_url, interval)

        # Frames arrive as a sequence of completions on one stream: a run of
        # token deltas, then a finish_reason, then the next frame's run.
        counted = 0
        open_index: Optional[int] = None

        async with self._client.stream(
            "POST", self._settings.chat_url, json=payload, headers=headers
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"gateway {resp.status_code}: {body[:500]}")

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = obj.get("choices") or [{}]
                choice = choices[0]
                delta = choice.get("delta") or {}
                text = delta.get("content")
                finished = choice.get("finish_reason")

                if text:
                    if open_index is None:
                        counted += 1
                        open_index = self._frame_index(obj, counted)
                        yield FrameStart(index=open_index)
                    yield Token(index=open_index, text=text)

                if finished and open_index is not None:
                    yield FrameEnd(index=open_index)
                    open_index = None

        if open_index is not None:
            yield FrameEnd(index=open_index)

    async def aclose(self) -> None:
        await self._client.aclose()
