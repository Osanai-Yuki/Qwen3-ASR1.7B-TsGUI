"""Standalone entrypoint for PyInstaller packaging."""
import logging
import os
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("asr")

from backend.main import app


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    import uvicorn
    logger.info("Starting ASR server on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
