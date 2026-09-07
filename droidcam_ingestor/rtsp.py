"""RTSP / DroidCam frame capture.

DroidCam exposes a few stream URLs depending on the app/mode:
  - MJPEG over HTTP:  http://<phone-ip>:4747/video  (or /mjpegfeed)
  - RTSP (DroidCamX):  rtsp://<phone-ip>:4747/...

OpenCV's ``VideoCapture`` handles all of them. RTSP in particular buffers
frames, so a continuous reader thread drains the buffer and keeps only the most
recent frame; the inference loop then always samples "now" rather than a stale
frame from the queue.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import cv2


class FrameGrabber:
    """Background reader that always exposes the latest decoded frame."""

    def __init__(self, stream_url: str, open_timeout: float = 15.0):
        self.stream_url = stream_url
        self.open_timeout = open_timeout
        self._cap: Optional[cv2.VideoCapture] = None
        self._latest = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None

    def start(self) -> None:
        cap = cv2.VideoCapture(self.stream_url)
        # Keep OpenCV's internal buffer tiny so we stay close to real time.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass

        deadline = time.monotonic() + self.open_timeout
        while not cap.isOpened() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not cap.isOpened():
            cap.release()
            raise ConnectionError(f"Could not open stream: {self.stream_url}")

        self._cap = cap
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self._cap is not None
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                # Transient drop or end of stream; back off briefly and retry.
                self._error = "stream read failed"
                time.sleep(0.05)
                continue
            self._error = None
            with self._lock:
                self._latest = frame

    def read_latest(self):
        """Return the most recent frame, or ``None`` if none decoded yet."""
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    @property
    def error(self) -> Optional[str]:
        return self._error

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def encode_jpeg(frame, quality: int = 80, max_edge: int = 1024) -> bytes:
    """Downscale (if needed) and JPEG-encode a BGR frame."""
    if max_edge and max_edge > 0:
        h, w = frame.shape[:2]
        longest = max(h, w)
        if longest > max_edge:
            scale = max_edge / longest
            frame = cv2.resize(
                frame,
                (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_AREA,
            )
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()
