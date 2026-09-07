"""Console entrypoint: run the FastAPI app with uvicorn."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "droidcam_ingestor.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        reload=bool(os.environ.get("RELOAD")),
    )


if __name__ == "__main__":
    main()
