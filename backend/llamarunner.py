import subprocess
import time
import logging
import threading
import httpx
from pathlib import Path

logger = logging.getLogger("asr")


class LlamaRunner:
    """Manages a single llama-server instance with hot-swappable model config.

    Only one model runs at a time on the same port. Call start() with new
    model paths to swap models; the previous process is killed automatically.
    """

    def __init__(
        self,
        bin_path: str,
        host: str = "127.0.0.1",
        port: int = 8080,
    ):
        self.bin_path = Path(bin_path)
        self.host = host
        self.port = port
        self.process: subprocess.Popen | None = None
        self.current_model: str | None = None

        # defaults overridden by start()
        self._model_path: Path = Path()
        self._mmproj_path: Path | None = None
        self._ctx_size: int = 32768
        self._kv_quant: str = "q8_0"
        self._n_gpu_layers: int = 99
        self._threads: int | None = None
        self._batch_size: int | None = None
        self._ubatch_size: int | None = None
        self._flash_attn: bool = True
        self._mmproj_offload: bool = False
        self._extra_args: list[str] = []

        # stderr capture for diagnostics: drained by a background thread so
        # the OS pipe buffer never fills and blocks llama-server on startup.
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def current_config(self) -> dict:
        """Read-only snapshot of the active configuration (for /api/models)."""
        return {
            "model_path": str(self._model_path),
            "mmproj_path": str(self._mmproj_path) if self._mmproj_path else None,
            "ctx_size": self._ctx_size,
            "kv_quant": self._kv_quant,
            "n_gpu_layers": self._n_gpu_layers,
            "threads": self._threads,
            "batch_size": self._batch_size,
            "ubatch_size": self._ubatch_size,
            "flash_attn": self._flash_attn,
            "mmproj_offload": self._mmproj_offload,
        }

    def build_cmd(self) -> list[str]:
        cmd = [
            str(self.bin_path),
            "-m", str(self._model_path),
            "--host", self.host,
            "--port", str(self.port),
            "-ngl", str(self._n_gpu_layers),
            "-c", str(self._ctx_size),
        ]
        if self._flash_attn:
            cmd += ["-fa", "on"]
        if self._threads is not None:
            cmd += ["--threads", str(self._threads)]
        if self._batch_size is not None:
            cmd += ["--batch-size", str(self._batch_size)]
        if self._ubatch_size is not None:
            cmd += ["--ubatch-size", str(self._ubatch_size)]
        if self._mmproj_path:
            cmd += ["--mmproj", str(self._mmproj_path)]
            if not self._mmproj_offload:
                cmd += ["--no-mmproj-offload"]
        if self._kv_quant and self._kv_quant.lower() != "f16":
            cmd += ["--cache-type-k", self._kv_quant, "--cache-type-v", self._kv_quant]
        cmd.extend(self._extra_args)
        return cmd

    def start(
        self,
        model_path: str,
        mmproj_path: str | None = None,
        ctx_size: int = 32768,
        kv_quant: str = "q8_0",
        n_gpu_layers: int = 99,
        threads: int | None = None,
        batch_size: int | None = None,
        ubatch_size: int | None = None,
        flash_attn: bool = True,
        mmproj_offload: bool = False,
        extra_args: list[str] | None = None,
        model_name: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        """Start or restart the server with a new model config.

        If a process is already running, it is stopped first.
        """
        self.stop()
        self._wait_port_free(timeout=15.0)

        self._model_path = Path(model_path)
        self._mmproj_path = Path(mmproj_path) if mmproj_path else None
        self._ctx_size = ctx_size
        self._kv_quant = kv_quant
        self._n_gpu_layers = n_gpu_layers
        self._threads = threads
        self._batch_size = batch_size
        self._ubatch_size = ubatch_size
        self._flash_attn = flash_attn
        self._mmproj_offload = mmproj_offload
        self._extra_args = extra_args or []
        self.current_model = model_name or self._model_path.stem

        with self._stderr_lock:
            self._stderr_lines = []

        logger.info("Starting llama-server with model=%s", self.current_model)
        self.process = subprocess.Popen(
            self.build_cmd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._start_stderr_reader()
        self._wait_until_ready(timeout)
        logger.info("llama-server ready (model=%s)", self.current_model)

    def _start_stderr_reader(self) -> None:
        """Background thread that continuously drains stderr so the pipe
        buffer never fills (which would block llama-server during a slow
        model load and cause a silent timeout)."""
        if not self.process or not self.process.stderr:
            return

        def _read():
            assert self.process is not None
            assert self.process.stderr is not None
            try:
                for raw in iter(self.process.stderr.readline, b""):
                    with self._stderr_lock:
                        self._stderr_lines.append(raw.decode(errors="replace").rstrip())
                        # bound the buffer to avoid unbounded growth
                        if len(self._stderr_lines) > 2000:
                            del self._stderr_lines[:500]
            except Exception:
                pass

        self._reader_thread = threading.Thread(target=_read, daemon=True)
        self._reader_thread.start()

    def _stderr_tail(self, n_chars: int = 2000) -> str:
        with self._stderr_lock:
            text = "\n".join(self._stderr_lines)
        return text[-n_chars:]

    def _wait_until_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=2.0)
                if r.status_code == 200:
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            if self.process and self.process.poll() is not None:
                tail = self._stderr_tail()
                raise RuntimeError(
                    "llama-server exited unexpectedly (code "
                    f"{self.process.returncode}).\n--- stderr tail ---\n{tail}"
                )
            time.sleep(0.5)
        tail = self._stderr_tail()
        raise TimeoutError(
            "llama-server did not become ready in time.\n"
            f"--- stderr tail ---\n{tail}"
        )

    def _wait_port_free(self, timeout: float = 15.0) -> None:
        """After stop(), wait until the port no longer answers so the next
        process can bind without an address-in-use failure."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{self.base_url}/health", timeout=0.5)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                return  # port is free
            time.sleep(0.3)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def is_alive(self) -> bool:
        if not self.process:
            return False
        return self.process.poll() is None
