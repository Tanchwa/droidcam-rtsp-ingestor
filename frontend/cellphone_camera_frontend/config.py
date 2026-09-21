"""Frontend configuration, sourced from environment variables.

The frontend talks to the llm-d router gateway, and tells it where to send the
results back. It still has no knowledge of inference pools, of which pool a
session landed on, or of the handler pod's address -- all of that is the
router's business. It only has to be able to name *itself*.
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
    # How long the gateway has to answer the trigger. This used to bound a
    # 600-second token stream; it now bounds a short provisioning request that
    # returns as soon as the handler exists, because results arrive separately
    # at /ingest. A session running longer than this is no longer affected.
    response_timeout: float = float(_env("RESPONSE_TIMEOUT", "60"))

    # Defaults offered to the UI; the browser can override both.
    default_prompt: str = _env("PROMPT", "Describe what you see in this image.")
    default_interval: float = float(_env("FRAME_INTERVAL", "2.0"))

    # --- Where results come back ---
    # This pod's own address, from the downward API (see the Deployment). It is
    # this pod specifically and not the Service, because each session's
    # WebSocket lives entirely in one replica's memory: results load-balanced
    # to a different replica would arrive somewhere that has never heard of the
    # session. Empty (running outside Kubernetes) means no callback is
    # advertised and the router falls back to whatever it has configured.
    pod_ip: str = _env("POD_IP", "")
    port: int = int(_env("PORT", "8080"))
    # Overrides the address derived from POD_IP. Set this when the handler
    # reaches this process by some other route -- a tunnel in local
    # development, a Service in a single-replica deployment.
    results_callback_base: str = _env("RESULTS_CALLBACK_BASE", "")

    @property
    def chat_url(self) -> str:
        return f"{self.gateway_endpoint.rstrip('/')}{self.gateway_chat_path}"

    @property
    def callback_base(self) -> str:
        """Base URL the handler should POST this session's results to.

        The router appends the session id, giving the handler a
        RESULTS_CALLBACK_URL that lands on this pod's /ingest/{session_id}.
        """
        if self.results_callback_base:
            return self.results_callback_base.rstrip("/")
        if self.pod_ip:
            return f"http://{self.pod_ip}:{self.port}/ingest"
        return ""


settings = Settings()
