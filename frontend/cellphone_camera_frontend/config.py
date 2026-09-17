"""Frontend configuration, sourced from environment variables.

The frontend only ever talks to one thing: the llm-d router gateway. It has no
knowledge of inference pools, of which pool a session landed on, or of the
handler pod's address -- all of that is the router's business.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


@dataclass(frozen=True)
class Settings:
    # Base URL of the llm-d router gateway. No trailing slash.
    gateway_endpoint: str = _env("GATEWAY_ENDPOINT", "http://localhost:8000")
    # Path appended to the base URL for chat completions.
    gateway_chat_path: str = _env("GATEWAY_CHAT_PATH", "/v1/chat/completions")
    # Model name, used for routing to an inference pool.
    model: str = _env("MODEL", "Qwen/Qwen2-VL-7B-Instruct")
    # Optional bearer token if the gateway requires auth.
    api_key: str = _env("API_KEY", "")
    # How long the token response to our POST may stay open. This bounds the
    # gateway response only -- the frontend never opens the cellphone camera stream
    # itself. Keep it comfortably above the handler's MAX_SESSION_SECONDS, or
    # tokens stop reaching the browser while the handler is still working.
    response_timeout: float = float(_env("RESPONSE_TIMEOUT", "600"))

    # Defaults offered to the UI; the browser can override both.
    default_prompt: str = _env("PROMPT", "Describe what you see in this image.")
    default_interval: float = float(_env("FRAME_INTERVAL", "2.0"))

    @property
    def chat_url(self) -> str:
        return f"{self.gateway_endpoint.rstrip('/')}{self.gateway_chat_path}"


settings = Settings()
