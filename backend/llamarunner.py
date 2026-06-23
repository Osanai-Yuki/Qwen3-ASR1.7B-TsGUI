import subprocess
import time
import logging
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
        self._extra_args: list[str] = []

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def build_cmd(self) -> list[str]:
        cmd = [
            str(self.bin_path),
            "-m", str(self._model_path),
            "--host", self.host,
            "--port", str(self.port),
            "-ngl", str(self._n_gpu_layers),
            "-c", str(self._ctx_size),
            "-fa", "on",
        ]
        if self._mmproj_path:
            cmd += ["--mmproj", str(self._mmproj_path), "--no-mmproj-offload"]
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
        extra_args: list[str] | None = None,
        model_name: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        """Start or restart the server with a new model config.

        If a process is already running, it is stopped first.
        """
        self.stop()

        self._model_path = Path(model_path)
        self._mmproj_path = Path(mmproj_path) if mmproj_path else None
        self._ctx_size = ctx_size
        self._kv_quant = kv_quant
        self._n_gpu_layers = n_gpu_layers
        self._extra_args = extra_args or []
        self.current_model = model_name or self._model_path.stem

        logger.info("Starting llama-server with model=%s", self.current_model)
        self.process = subprocess.Popen(
            self.build_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._wait_until_ready(timeout)
        logger.info("llama-server ready (model=%s)", self.current_model)

    def _wait_until_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=2.0)
                if r.status_code == 200:
                    return
            except (httpx.ConnectError, httpx.ReadTimeout):
                pass
            if self.process and self.process.poll() is not None:
                stderr = b""
                if self.process.stderr:
                    try:
                        stderr = self.process.stderr.read() or b""
                    except Exception:
                        pass
                raise RuntimeError(
                    "llama-server exited unexpectedly: "
                    + stderr.decode(errors="replace")[-2000:]
                )
            time.sleep(0.5)
        raise TimeoutError("llama-server did not become ready in time")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        self.current_model = None

    def is_alive(self) -> bool:
        if not self.process:
            return False
        return self.process.poll() is None
