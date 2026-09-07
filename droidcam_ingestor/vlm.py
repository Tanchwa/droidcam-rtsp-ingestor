"""Client for an OpenAI-compatible VLM inference server.

Targets the ``/v1/chat/completions`` contract exposed by vLLM, SGLang,
LMDeploy, etc. A frame is sent as a base64 ``data:`` image URL alongside a text
prompt, and the model's reply is streamed back token by token (SSE).

The request body is deliberately isolated in ``_build_payload`` so a
non-OpenAI server can be supported by editing one function.
"""

from __future__ import annotations

import base64
import json
from typing import AsyncIterator

import httpx

from .config import Settings


class VLMClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        headers = {"Content-Type": "application/json"}
        if settings.vlm_api_key:
            headers["Authorization"] = f"Bearer {settings.vlm_api_key}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.vlm_timeout, connect=10.0),
            headers=headers,
        )

    def _build_payload(self, prompt: str, jpeg: bytes, stream: bool) -> dict:
        b64 = base64.b64encode(jpeg).decode("ascii")
        return {
            "model": self._settings.vlm_model,
            "stream": stream,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                    ],
                }
            ],
        }

    async def stream_inference(
        self, prompt: str, jpeg: bytes
    ) -> AsyncIterator[str]:
        """Yield response text chunks for one frame as they arrive."""
        payload = self._build_payload(prompt, jpeg, stream=True)
        async with self._client.stream(
            "POST", self._settings.chat_url, json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"VLM {resp.status_code}: {body[:500]}")
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
                delta = obj.get("choices", [{}])[0].get("delta", {})
                chunk = delta.get("content")
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()
