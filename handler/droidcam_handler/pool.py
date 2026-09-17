"""Frame submission to the assigned llm-d inference pool.

THIS MODULE IS THE JOINT between the handler and the llm-d router. The handler
does not receive the model's output -- the caller (frontend) is already holding
an open streaming response on the gateway, and the gateway joins this
submission's output onto that stream by matching ``SESSION_HEADER``. So the
only thing that matters here is that each frame goes out tagged with the
session id the frontend sent.

If your gateway correlates on something else -- a different header, a body
field, a path segment -- change ``_headers``/``_build_payload`` and nothing
else in the handler needs to move.
"""

from __future__ import annotations

import base64
from typing import Optional

import httpx

from .config import HandlerSettings

#: Header the gateway matches to attach this output to the caller's stream.
SESSION_HEADER = "x-llmd-session-id"
#: Marks the request as handler-originated frame traffic rather than a fresh
#: user request, so the hook does not try to provision a second handler.
ORIGIN_HEADER = "x-llmd-frame-source"
ORIGIN_VALUE = "droidcam-handler"


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
        """Send one frame to the pool. Returns bytes of response drained.

        The response body is drained and thrown away: the tokens are already
        on their way to the frontend via the gateway. Draining (rather than
        disconnecting early) keeps the gateway from seeing a client abort
        mid-generation, which would truncate the caller's stream.
        """
        payload = self._build_payload(jpeg, frame_index)
        drained = 0
        async with self._client.stream(
            "POST", self._settings.chat_url, json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"pool {resp.status_code}: {body[:500]}")
            async for chunk in resp.aiter_bytes():
                drained += len(chunk)
        return drained

    async def aclose(self) -> None:
        await self._client.aclose()
