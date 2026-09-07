"""Runtime configuration, sourced from environment variables.

The VLM endpoint can live inside the same cluster (e.g.
``http://vlm-service.default.svc.cluster.local:8000``) or behind a public
Envoy API gateway (e.g. ``https://vlm.example.com``). Either way it is a single
env var so the same image runs in both places.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


@dataclass(frozen=True)
class Settings:
    # Base URL of the OpenAI-compatible VLM server. No trailing slash.
    vlm_endpoint: str = _env("VLM_ENDPOINT", "http://localhost:8000")
    # Path appended to the base URL for chat completions.
    vlm_chat_path: str = _env("VLM_CHAT_PATH", "/v1/chat/completions")
    # Model name the server expects in the request body.
    vlm_model: str = _env("VLM_MODEL", "Qwen/Qwen2-VL-7B-Instruct")
    # Optional bearer token (Envoy / gateway auth). Empty means no auth header.
    vlm_api_key: str = _env("VLM_API_KEY", "")
    # Seconds to wait on the VLM before giving up on a single frame.
    vlm_timeout: float = float(_env("VLM_TIMEOUT", "60"))

    # Default seconds between sampled frames (the UI can override this).
    default_interval: float = float(_env("FRAME_INTERVAL", "2.0"))
    # JPEG quality (1-100) for frames sent to the VLM.
    jpeg_quality: int = int(_env("JPEG_QUALITY", "80"))
    # Longest edge (px) a frame is downscaled to before encoding. 0 disables.
    max_frame_edge: int = int(_env("MAX_FRAME_EDGE", "1024"))

    @property
    def chat_url(self) -> str:
        return f"{self.vlm_endpoint.rstrip('/')}{self.vlm_chat_path}"


settings = Settings()
