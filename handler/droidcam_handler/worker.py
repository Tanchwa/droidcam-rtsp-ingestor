"""Handler entrypoint: sample the DroidCam stream into the inference pool.

Lifecycle is deliberately dumb, because this whole component is a POC stand-in
for a real RTSP handler:

    start -> open stream -> [grab frame -> submit to pool -> wait] -> exit

It runs until a backstop trips (wall clock, frame count, or an idle stream) or
until Kubernetes sends SIGTERM. There is no inbound control channel -- nothing
can reach this pod -- so the backstops are the only thing guaranteeing it dies.

Exit codes: 0 ran to completion, 1 stream never produced frames / went idle,
2 misconfigured.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from .config import ConfigError, HandlerSettings, load
from .pool import PoolClient
from .rtsp import FrameGrabber, encode_jpeg

log = logging.getLogger("droidcam.handler")


def _describe(settings: HandlerSettings) -> str:
    return (
        f"session={settings.session_id} stream={settings.stream_url} "
        f"pool={settings.chat_url} model={settings.model} "
        f"interval={settings.frame_interval}s "
        f"limits(max_session={settings.max_session_seconds}s "
        f"max_frames={settings.max_frames or 'unlimited'} "
        f"idle={settings.idle_timeout}s)"
    )


async def run(settings: HandlerSettings, stop: asyncio.Event) -> int:
    loop = asyncio.get_running_loop()
    grabber = FrameGrabber(settings.stream_url, settings.stream_open_timeout)

    try:
        await loop.run_in_executor(None, grabber.start)
    except Exception as exc:
        log.error("could not open stream: %s", exc)
        return 1

    pool = PoolClient(settings)
    started = time.monotonic()
    last_frame_at = started
    frames = 0
    exit_code = 0

    try:
        while not stop.is_set():
            now = time.monotonic()

            if settings.max_session_seconds and now - started >= settings.max_session_seconds:
                log.info("max session duration reached after %d frame(s)", frames)
                break
            if settings.max_frames and frames >= settings.max_frames:
                log.info("max frame count (%d) reached", settings.max_frames)
                break

            frame = grabber.read_latest()
            if frame is None:
                if now - last_frame_at >= settings.idle_timeout:
                    log.error(
                        "no frames for %.0fs (%s); giving up",
                        settings.idle_timeout,
                        grabber.error or "stream silent",
                    )
                    exit_code = 1
                    break
                await asyncio.sleep(0.25)
                continue

            last_frame_at = now
            frames += 1
            try:
                jpeg = await loop.run_in_executor(
                    None,
                    encode_jpeg,
                    frame,
                    settings.jpeg_quality,
                    settings.max_frame_edge,
                )
                drained = await pool.submit_frame(jpeg, frames)
                log.info(
                    "frame %d submitted (%d B jpeg, %d B response drained)",
                    frames,
                    len(jpeg),
                    drained or 0,
                )
            except Exception as exc:
                # One bad frame or a transient pool error should not kill the
                # session; the backstops still bound how long we retry.
                log.warning("frame %d failed: %s", frames, exc)

            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.frame_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await pool.aclose()
        await loop.run_in_executor(None, grabber.stop)

    log.info("handler finished: %d frame(s) submitted", frames)
    return exit_code


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    try:
        settings = load()
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(2)

    log.info("starting handler: %s", _describe(settings))

    async def _main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        return await run(settings, stop)

    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
