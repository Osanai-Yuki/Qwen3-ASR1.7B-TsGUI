"""Standalone entrypoint for PyInstaller packaging."""
import logging
import os
import sys
import threading

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("asr")

from backend.main import app


def _open_browser_when_ready(host: str, port: int, timeout: float = 180.0) -> None:
    """Poll /api/readiness in a background thread and open the browser once the
    boot sequence completes (or give up after ``timeout``).

    Polling from a daemon thread keeps this out of the async event loop and
    tolerates the slow first model load without blocking startup. On a boot
    error we leave the browser closed and log the URL so the user can open it
    manually and read the error screen.
    """
    import json
    import time
    import urllib.request
    import webbrowser

    readiness_url = f"http://{host}:{port}/api/readiness"
    app_url = f"http://{host}:{port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(readiness_url, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if data.get("ready"):
                webbrowser.open(app_url)
                logger.info("Opened browser at %s", app_url)
                return
            if data.get("phase") == "error":
                logger.warning(
                    "Backend boot failed; not opening the browser. Open %s "
                    "manually to see the error.", app_url,
                )
                return
        except Exception:
            pass  # server not up yet (binding / first import); retry shortly
        time.sleep(1.0)
    logger.warning(
        "Backend did not become ready in %.0fs; open %s manually.", timeout, app_url,
    )


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    # Auto-open the browser once the backend is ready. Non-blocking; opt out
    # with ASR_NO_BROWSER=1 (e.g. for headless / multi-instance runs).
    if os.environ.get("ASR_NO_BROWSER") != "1":
        threading.Thread(
            target=_open_browser_when_ready, args=(host, port), daemon=True
        ).start()

    import uvicorn
    logger.info("Starting ASR server on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
