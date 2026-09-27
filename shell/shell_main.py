"""Desktop shell for Qwen3-ASR: a WebView2 (Chromium) window hosting the
existing FastAPI backend in-process.

Design (see also backend/main.py middleware):
- dynamic loopback port + one-time session token handed to the page via
  ``?st=<token>``; the backend exchanges it for an HttpOnly cookie, so no
  other local process can call the API even though it is plain HTTP;
- no pywebview ``js_api`` is registered: page JS has no bridge into Python,
  so an XSS can never escalate to code execution;
- navigation guard: anything that is not our loopback origin is kicked out
  to the system browser; new-window requests never open inside the shell;
- WebView2 profile is kept app-private under ``data/webview_profile``;
- graceful degradation: if WebView2 / pywebview is unavailable the app
  falls back to the plain system-browser mode (same behavior as run.py).

Env knobs:
  ASR_SHELL_SMOKE=<seconds>  auto-close the window after N seconds (CI/smoke)
  ASR_SHELL_DEBUG=1          enable the WebView2 devtools / debug mode
  PORT                       fixed port override (default: random free port)
"""
import ctypes
import logging
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Resolve the app root both in dev (repo checkout) and frozen (PyInstaller)
# layouts, mirroring backend.main._resolve_app_root.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))

WINDOW_TITLE = "Qwen3-ASR Transcription"
LOG_DIR = ROOT / "data" / "logs"
PROFILE_DIR = ROOT / "data" / "webview_profile"

logger = logging.getLogger("asr")


def _setup_logging() -> None:
    """File + stderr logging: the packaged shell runs with console=False,
    so without the rotating file there would be no trace of failures."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(
        LOG_DIR / "asr.log", maxBytes=2 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # Only attach a console handler when a real stream exists. In a windowed
    # (console=False) PyInstaller build sys.stderr is None, and a
    # StreamHandler bound to it raises on every emit — including from
    # uvicorn's startup logging — which manifests as a silent hang.
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _ensure_std_streams() -> None:
    """Guarantee sys.stdout/stderr are writable file objects.

    A windowed PyInstaller app has ``sys.stdout is sys.stderr is None``.
    Any library that writes to them during startup (uvicorn, click, a
    subprocess inheriting the handles) can then hang or crash the frozen
    process. Point them at the rotating log dir so every write is safe.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stream = open(LOG_DIR / "stdio.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _acquire_single_instance() -> bool:
    """Named mutex so a second launch can't race the port/queue/profile."""
    ERROR_ALREADY_EXISTS = 183
    ctypes.windll.kernel32.CreateMutexW(None, False, "Qwen3ASR_Shell_Mutex")
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def _alloc_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_up(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _message_box(text: str, title: str = WINDOW_TITLE) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def main() -> None:
    _ensure_std_streams()
    _setup_logging()

    if not _acquire_single_instance():
        logger.warning("Another shell instance is already running; exiting.")
        _message_box("Qwen3-ASR is already running.")
        return

    token = secrets.token_urlsafe(32)
    port = int(os.environ.get("PORT") or _alloc_port())

    # Must be set BEFORE backend.main is imported: the middleware and bind
    # config read these at import time. The shell always binds loopback.
    os.environ["SHELL_MODE"] = "1"
    os.environ["ASR_SHELL_TOKEN"] = token
    os.environ["HOST"] = "127.0.0.1"
    os.environ["PORT"] = str(port)

    logger.info("Shell starting backend on 127.0.0.1:%d", port)
    from backend import main as backend_main  # noqa: E402
    import uvicorn  # noqa: E402

    # log_config=None: do NOT let uvicorn run its own dictConfig. Its default
    # ColourizedFormatter calls sys.stdout.isatty() at init, which crashes in
    # a windowed (console=False) PyInstaller build where sys.stdout is None
    # ("'NoneType' object has no attribute 'isatty'"). With None, uvicorn's
    # loggers just propagate to the root logger already wired to the rotating
    # file handler in _setup_logging().
    config = uvicorn.Config(
        backend_main.app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="uvicorn").start()

    if not _wait_http_up(port):
        logger.error("Backend did not start listening on port %d", port)
        _message_box(
            "Backend failed to start.\nSee data\\logs\\asr.log for details.",
        )
        os._exit(1)

    origin = f"http://127.0.0.1:{port}"
    boot_url = f"{origin}/?st={token}"

    def shutdown() -> None:
        """Ordered teardown: lifespan (queue worker + llama-server) via the
        uvicorn exit flag, plus a direct runner.stop() belt-and-braces so a
        GPU llama-server never outlives the window."""
        logger.info("Shell window closed - shutting down")
        server.should_exit = True
        try:
            backend_main.runner.stop()
        except Exception:
            pass
        time.sleep(1.0)  # give lifespan shutdown a moment to run
        os._exit(0)

    try:
        import webview  # noqa: E402

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        window = webview.create_window(
            WINDOW_TITLE,
            boot_url,
            width=1280,
            height=820,
            min_size=(960, 640),
            # No js_api: the page gets no bridge into Python by design.
        )

        def on_loaded(*_args) -> None:
            """Navigation guard: the shell must only ever render our own
            loopback origin. Anything else is sent to the system browser
            and the shell snaps back home."""
            try:
                url = window.get_current_url() or ""
            except Exception:
                return
            if not url.startswith(origin):
                logger.warning("Blocked in-shell navigation to %s", url)
                window.load_url(origin + "/")
                if url.startswith(("http://", "https://")):
                    webbrowser.open(url)

        window.events.loaded += on_loaded
        window.events.closed += shutdown

        smoke = float(os.environ.get("ASR_SHELL_SMOKE", "0") or 0)
        if smoke > 0:
            def _auto_close():
                time.sleep(smoke)
                logger.info("Smoke mode: closing window after %.0fs", smoke)
                try:
                    window.destroy()
                except Exception:
                    os._exit(0)
            threading.Thread(target=_auto_close, daemon=True).start()

        webview.start(
            debug=os.environ.get("ASR_SHELL_DEBUG") == "1",
            private_mode=False,               # persist the session cookie
            storage_path=str(PROFILE_DIR),    # app-private, not shared w/ Edge
        )
        # webview.start() returns when the last window closes; `closed`
        # handler has already begun teardown, this is only a fallback.
        shutdown()
    except Exception:
        # WebView2 runtime / pywebview unavailable: degrade to the plain
        # system-browser flow so the app remains usable.
        logger.exception(
            "WebView2 shell unavailable - falling back to the system browser",
        )
        webbrowser.open(boot_url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            shutdown()


if __name__ == "__main__":
    main()
