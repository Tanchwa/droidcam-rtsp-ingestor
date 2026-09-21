"""Frame submission to the assigned llm-d inference pool.

THIS MODULE IS THE JOINT between the handler and the llm-d router, and it
carries the return path with it.

Frames go straight to the pod llm-d assigned this session, bypassing the
gateway -- that is what keeps the session's constant prompt warm in that pod's
prefix cache. The cost is that the gateway never sees the response and so
cannot relay it to the caller: an Envoy ext_proc filter cannot splice one
request's response into another request's stream, so there is no version of
this where the caller's original connection carries the output.

So the handler carries it. Each frame's response is piped, unbuffered, to
``RESULTS_CALLBACK_URL`` -- the frontend replica holding that session's
WebSocket, named by the caller itself and injected here by the gateway hook.
Without a callback the response is drained and discarded, and the session
produces nothing anyone can see.

If your gateway correlates on something else -- a different header, a body
field, a path segment -- change ``_headers``/``_build_payload`` and nothing
else in the handler needs to move.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from .config import HandlerSettings

log = logging.getLogger("cellphone-camera.handler.pool")

#: Header the gateway matches to tie this submission to the caller's session.
SESSION_HEADER = "x-llmd-session-id"
#: Marks the request as handler-originated frame traffic rather than a fresh
#: user request, so the hook does not try to provision a second handler.
ORIGIN_HEADER = "x-llmd-frame-source"
ORIGIN_VALUE = "cellphone-camera-handler"
#: Tags each callback POST with the frame it carries, so the frontend can label
#: token runs even when the model's own response does not echo the index back.
FRAME_INDEX_HEADER = "x-cellphone-camera-frame-index"
#: Marks the final callback POST of a session rather than another frame.
SESSION_END_HEADER = "x-cellphone-camera-session-end"


class PoolClient:
    """Posts frames to the inference pool endpoint llm-d assigned."""

    def __init__(self, settings: HandlerSettings):
        self._settings = settings
        headers = {
            "Content-Type": "application/json",
            SESSION_HEADER: settings.session_id,
            ORIGIN_HEADER: ORIGIN_VALUE,
        }
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout, connect=10.0),
            headers=headers,
        )
        # A separate client for the callback, deliberately NOT carrying the
        # pool's Authorization header -- the frontend is a different party and
        # has no business receiving the inference API key.
        self._callback = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(settings.timeout, connect=10.0),
                headers={SESSION_HEADER: settings.session_id},
            )
            if settings.results_callback_url
            else None
        )

    def _build_payload(self, jpeg: bytes, frame_index: int) -> dict:
        b64 = base64.b64encode(jpeg).decode("ascii")
        return {
            "model": self._settings.model,
            # The gateway relays these tokens to the caller, so keep streaming
            # on even though this process discards them.
            "stream": True,
            # Echoed in the body as well as the header -- some gateway
            # configurations inspect only one of the two.
            "session_id": self._settings.session_id,
            "frame_index": frame_index,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._settings.prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        }

    async def submit_frame(self, jpeg: bytes, frame_index: int) -> Optional[int]:
        """Send one frame to the pool and forward its response to the caller.

        Returns the number of response bytes handled.

        The response is piped to the callback as it arrives rather than being
        buffered here: a frame's tokens should reach the browser while the
        model is still generating them, which is the whole reason the pool is
        asked for a stream in the first place.

        With no callback configured the body is drained and dropped. Draining
        (rather than disconnecting early) matters either way -- an early
        disconnect reads to the server as a client abort mid-generation.
        """
        payload = self._build_payload(jpeg, frame_index)
        forwarded = 0

        async with self._client.stream(
            "POST", self._settings.chat_url, json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"pool {resp.status_code}: {body[:500]}")

            if self._callback is None:
                async for chunk in resp.aiter_bytes():
                    forwarded += len(chunk)
                return forwarded

            # Taken once and shared: httpx refuses a second aiter_bytes() on the
            # same response, so the failure path below has to resume *this*
            # iterator rather than ask for a fresh one.
            stream = resp.aiter_bytes()

            async def _pipe():
                nonlocal forwarded
                async for chunk in stream:
                    forwarded += len(chunk)
                    yield chunk

            try:
                sunk = await self._callback.post(
                    self._settings.results_callback_url,
                    content=_pipe(),
                    headers={
                        "Content-Type": "text/event-stream",
                        FRAME_INDEX_HEADER: str(frame_index),
                    },
                )
            except httpx.HTTPError as exc:
                # The caller has gone away or cannot keep up. That ends this
                # frame, not the session: the worker keeps sampling and the
                # backstops still bound how long it retries.
                log.warning("frame %d: callback failed: %s", frame_index, exc)
                # Finish reading the pool response so the server does not see
                # this as an abort mid-generation.
                try:
                    async for chunk in stream:
                        forwarded += len(chunk)
                except httpx.HTTPError:
                    pass
                return forwarded

        if sunk.status_code >= 400:
            log.warning(
                "frame %d: callback returned %d: %s",
                frame_index,
                sunk.status_code,
                sunk.text[:200],
            )
        return forwarded

    async def end_session(self) -> None:
        """Tell the caller this session is over.

        Nothing else can: the handler exits on a backstop the frontend cannot
        observe, and there is no connection between them to close. Without this
        the browser would sit on a live socket waiting for a frame that is
        never coming.
        """
        if self._callback is None:
            return
        try:
            await self._callback.post(
                self._settings.results_callback_url,
                content=b"data: [DONE]\n\n",
                headers={
                    "Content-Type": "text/event-stream",
                    SESSION_END_HEADER: "1",
                },
            )
        except httpx.HTTPError as exc:
            # Best effort. The frontend times the session out on its own.
            log.warning("could not signal session end: %s", exc)

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._callback is not None:
            await self._callback.aclose()
