"""Handler configuration, read entirely from the environment.

The handler is provisioned by the llm-d router's gateway hook, so every value
below arrives as an env var on the pod spec -- there is no config file, no API
the handler listens on, and no Kubernetes client. ``STREAM_URL`` (the DroidCam
address the user typed into the UI) and ``POOL_ENDPOINT`` (the inference pool
llm-d assigned the session to) are the two the hook must always inject.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """A required env var is missing or unusable."""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required (injected by the gateway hook)")
    return value


@dataclass(frozen=True)
class HandlerSettings:
    # --- Injected per session by the gateway hook ---
    # Correlates this handler's frame submissions with the caller's open
    # response stream on the gateway. Must match the id the frontend sent.
    session_id: str = field(default_factory=lambda: _required("SESSION_ID"))
    # The DroidCam MJPEG/RTSP address the user entered in the UI.
    stream_url: str = field(default_factory=lambda: _required("STREAM_URL"))
    # The specific inference pool endpoint llm-d assigned this session to.
    pool_endpoint: str = field(default_factory=lambda: _required("POOL_ENDPOINT"))

    prompt: str = field(
        default_factory=lambda: _env("PROMPT", "Describe what you see in this image.")
    )

    # --- Pool request shape ---
    pool_chat_path: str = field(
        default_factory=lambda: _env("POOL_CHAT_PATH", "/v1/chat/completions")
    )
    model: str = field(
        default_factory=lambda: _env("MODEL", "Qwen/Qwen2-VL-7B-Instruct")
    )
    api_key: str = field(default_factory=lambda: _env("API_KEY", ""))
    timeout: float = field(default_factory=lambda: float(_env("TIMEOUT", "60")))

    # --- Frame sampling ---
    frame_interval: float = field(
        default_factory=lambda: float(_env("FRAME_INTERVAL", "2.0"))
    )
    jpeg_quality: int = field(default_factory=lambda: int(_env("JPEG_QUALITY", "80")))
    max_frame_edge: int = field(
        default_factory=lambda: int(_env("MAX_FRAME_EDGE", "1024"))
    )
    stream_open_timeout: float = field(
        default_factory=lambda: float(_env("STREAM_OPEN_TIMEOUT", "15"))
    )

    # --- Backstops, so an orphaned handler always dies ---
    # Wall-clock ceiling on the session. 0 disables (not recommended in-cluster).
    max_session_seconds: float = field(
        default_factory=lambda: float(_env("MAX_SESSION_SECONDS", "300"))
    )
    # Frame ceiling. 0 means "no limit, rely on the clock".
    max_frames: int = field(default_factory=lambda: int(_env("MAX_FRAMES", "0")))
    # Give up if the stream yields no decodable frame for this long.
    idle_timeout: float = field(
        default_factory=lambda: float(_env("IDLE_TIMEOUT", "30"))
    )

    @property
    def chat_url(self) -> str:
        return f"{self.pool_endpoint.rstrip('/')}{self.pool_chat_path}"


def load() -> HandlerSettings:
    """Build settings from the environment, raising ConfigError if unusable."""
    return HandlerSettings()
