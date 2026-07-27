import asyncio
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import logging
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .llamarunner import LlamaRunner
from .forced_aligner import ForcedAligner, ALIGN_MAX_WORKERS
from .aligner_gpu import GPUAligner, GPU_ALIGNER_MODEL_PATH, gpu_aligner_available
from .audio_chunk import (
    split_audio,
    probe_duration,
    probe_audio_bitrate,
    is_video_file,
    extract_audio_to_mp3,
    safe_cache_stem,
    FFmpegMissingError,
)
from .resegment import resegment_words
from .text_clean import clean_asr_text, clean_segments
from .history import HistoryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("asr")


def _resolve_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _resolve_app_root()

MODELS_DIR = PROJECT_ROOT / "models"
ASR_MODELS_DIR = MODELS_DIR / "asr"
DATA_DIR = PROJECT_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
# Persistently cached MP3s extracted from uploaded video sources, kept for
# reuse across runs until the user clears them via /api/audio-cache.
AUDIO_CACHE_DIR = DATA_DIR / "audio_cache"
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
MP3_DEFAULT_BITRATE = 256_000

# ── Network / request hardening ────────────────────────────────────────────
# The app is a single-user *local* tool: the SPA is served same-origin by this
# process, so cross-origin access is never legitimately needed. These knobs
# feed the loopback-only middleware below and the upload/text caps.
_BIND_HOST = os.environ.get("HOST", "127.0.0.1")
_BIND_PORT = int(os.environ.get("PORT", "8000"))
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_BIND_IS_LOOPBACK = _BIND_HOST in _LOOPBACK_HOSTS
# Opt-in Host allowlist for non-loopback (LAN) binds. By default a non-loopback
# bind (e.g. HOST=0.0.0.0 to share on a trusted LAN) does NOT restrict Host:
# LAN clients send the machine's LAN IP/hostname, which can't be predicted, and
# drive-by browser attacks are already blocked by the Origin + Sec-Fetch-Site
# checks below. Setting ASR_ALLOWED_HOSTS=a,b restricts a LAN share to those
# Hosts (plus loopback) - useful to lock a share to one hostname.
_ALLOWED_HOSTS_EXTRA = {
    h.strip().lower() for h in os.environ.get("ASR_ALLOWED_HOSTS", "").split(",") if h.strip()
}

# Hard ceilings on request bodies, to bound memory use under hostile input
# (a drive-by web page or a misconfigured LAN exposure). 2 GB comfortably
# covers long audio/video while preventing the unbounded `await file.read()`
# OOM that previously let a single request allocate the whole upload in RAM.
MAX_UPLOAD_BYTES = int(os.environ.get("ASR_MAX_UPLOAD_BYTES", str(2 * 1024 ** 3)))
# /api/align text field cap — python-multipart buffers the whole field, so an
# unbounded text could exhaust RAM independently of the audio.
MAX_ALIGN_TEXT_CHARS = int(os.environ.get("ASR_MAX_ALIGN_TEXT_CHARS", "50000"))
# Hard cap on accepted audio duration (seconds) before chunking, so a crafted
# or accidental 24h upload can't spawn thousands of ffmpeg chunks.
MAX_AUDIO_DURATION = float(os.environ.get("ASR_MAX_AUDIO_DURATION", str(12 * 3600)))
# Audio-cache total size ceiling; oldest entries are evicted when exceeded.
AUDIO_CACHE_MAX_BYTES = int(os.environ.get("ASR_AUDIO_CACHE_MAX_BYTES", str(4 * 1024 ** 3)))
AUDIO_CACHE_MAX_ENTRIES = int(os.environ.get("ASR_AUDIO_CACHE_MAX_ENTRIES", "200"))

# Allowed upload extensions.  Anything else is treated as an opaque binary blob
# so ffmpeg can still attempt content-based probing, but we avoid creating
# misleading temp files with attacker-chosen extensions.
_ALLOWED_UPLOAD_SUFFIXES = {
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".mov",
    ".avi", ".webm", ".flv", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts",
    ".3gp", ".vob", ".ogv",
}


def _safe_upload_suffix(filename: str | None) -> str:
    suffix = Path(filename or "input").suffix.lower()
    return suffix if suffix in _ALLOWED_UPLOAD_SUFFIXES else ".bin"


def _safe_filename(filename: str | None) -> str:
    """Sanitize a user-provided filename for logging/persistence.

    Keeps word characters (Unicode-aware), spaces, dots, dashes and underscores;
    replaces path separators, control characters and other specials with '_'.
    """
    if not filename:
        return "unknown"
    safe = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", filename)
    safe = safe.replace("\\", "_").replace("/", "_")
    safe = safe.strip("._ ") or "unknown"
    return safe[:120]


def _origin_host(origin: str) -> str:
    try:
        return (urlsplit(origin).hostname or "").lower()
    except Exception:
        return ""


# Allowed llama-server KV-cache quantizations (passed to --cache-type-k/-v).
_KV_QUANT_ALLOWLIST = {"q4_0", "q4_1", "q5_0", "q5_1", "q8_0", "f16"}


def _opt_int(value, lo: int, hi: int) -> int | None:
    """Coerce an optional tuning parameter to a clamped int, or None."""
    if value is None:
        return None
    iv = int(value)
    if not (lo <= iv <= hi):
        raise ValueError("out of range")
    return iv


class _LocalOnlyMiddleware:
    """Pure-ASGI guard that blocks non-local browser access.

    Two checks, both essential because CORS alone cannot stop a browser from
    *sending* a simple cross-origin request (it only gates reading the
    response):

    1. Host header must be loopback when the server is loopback-bound. This
       defeats DNS rebinding, where a remote domain is repointed at 127.0.0.1
       so the browser treats its requests as same-origin (no Origin header,
       no preflight) and reads responses freely.
    2. If an Origin header is present it must be loopback or match the
       request's own Host (i.e. same-origin). A cross-origin page always sends
       Origin, so this blocks drive-by exfiltration of history/transcripts and
       CSRF-style DELETE/POST even when the body would otherwise be processed.

    Implemented as raw ASGI (not BaseHTTPMiddleware) so FileResponse streaming
    for cached audio is not buffered.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", ())
        }
        # Parse Host with IPv6 support: prefixing "//" lets urlsplit extract the
        # hostname from "[::1]:8000" -> "::1" (a naive split(':') yields "[").
        host_name = _origin_host(f"//{headers.get('host', '')}")
        if _BIND_IS_LOOPBACK:
            # Loopback bind: only loopback Hosts (DNS-rebinding defense - a
            # remote domain repointed at 127.0.0.1 must not be read same-origin).
            if host_name not in _LOOPBACK_HOSTS:
                await self._reject(send, "host not allowed")
                return
        elif _ALLOWED_HOSTS_EXTRA:
            # Opt-in: restrict a LAN share to the configured Hosts (+ loopback).
            if host_name not in _LOOPBACK_HOSTS and host_name not in _ALLOWED_HOSTS_EXTRA:
                await self._reject(send, "host not allowed")
                return
        # Non-loopback without ASR_ALLOWED_HOSTS: allow any Host (intentional LAN
        # exposure); Origin + Sec-Fetch-Site below still block drive-by browsers.
        origin = headers.get("origin")
        if origin:
            ohost = _origin_host(origin)
            if ohost not in _LOOPBACK_HOSTS and ohost != host_name:
                await self._reject(send, "origin not allowed")
                return
        # Sec-Fetch-Site: browsers send this on every subresource request,
        # including <audio>/<img> elements that omit Origin. cross-site means a
        # different site embedded this URL - block drive-by reads of history and
        # cached audio. Absent for non-browser clients (curl, ffmpeg) -> allow.
        sfs = headers.get("sec-fetch-site")
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            await self._reject(send, "cross-site request blocked")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, detail: str) -> None:
        body = json.dumps({"error": "forbidden", "detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class _UploadTooLarge(Exception):
    """Raised by the streaming uploader when a body exceeds the cap."""


async def _stream_upload_to(file: UploadFile, dest: Path, max_bytes: int) -> int:
    """Stream an UploadFile to ``dest`` in fixed chunks, returning the byte
    size and aborting with _UploadTooLarge if ``max_bytes`` is exceeded.

    Replaces the unbounded ``await file.read()`` pattern: the body is never
    held as a single in-memory bytes object, so a multi-GB upload cannot OOM
    the process. Content-Length (when present) is checked up front for an
    early rejection; the running counter also guards against a missing/lying
    Content-Length.
    """
    declared = file.size
    if declared is not None and declared > max_bytes:
        raise _UploadTooLarge()
    written = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await file.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise _UploadTooLarge()
            fh.write(chunk)
    return written


def _evict_audio_cache() -> None:
    """Prune AUDIO_CACHE_DIR to fit the size/entry ceilings (LRU by mtime)."""
    try:
        entries = []
        for p in AUDIO_CACHE_DIR.glob("*.mp3"):
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        if not entries:
            return
        entries.sort(key=lambda e: e[0])  # oldest first
        total = sum(e[1] for e in entries)
        while entries and (total > AUDIO_CACHE_MAX_BYTES or len(entries) > AUDIO_CACHE_MAX_ENTRIES):
            _t, size, path = entries.pop(0)
            try:
                path.unlink()
                total -= size
            except OSError:
                pass
    except Exception as e:
        logger.warning("Audio-cache eviction failed: %s", e)


def _claim_job() -> bool:
    """Atomically claim the single job slot. Returns True if acquired.

    Also refuses while a model hot-swap is in progress (``boot_state.switching``)
    so transcription and model switching never run concurrently - both touch the
    llama-server subprocess and would corrupt each other's state.
    """
    with job_lock:
        if job_state["status"] in ("preparing", "transcribing", "aligning"):
            return False
        if boot_state.get("switching"):
            return False
        if not boot_state.get("ready"):
            return False
        job_state["status"] = "preparing"
        job_state["progress"] = 0
        job_state["message"] = ""
        job_state["result"] = None
        # A brand-new job never inherits a cancel requested by the previous one.
        global _cancel_requested
        _cancel_requested = False
        return True


def _detect_vram_gb() -> float | None:
    """Best-effort GPU VRAM detection via nvidia-smi (None if unavailable)."""
    try:
        import subprocess as _sp
        r = _sp.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip().splitlines()[0]) / 1024.0
    except Exception:
        pass
    return None


def _default_tuning() -> dict:
    """Pick ctx/kv/ngl defaults adaptive to available VRAM.

    Tunable via env (VRAM_GB, CTX_SIZE, KV_QUANT, NGL) which override.
    """
    vram = os.environ.get("VRAM_GB")
    vram_gb = float(vram) if vram else (_detect_vram_gb() or 8.0)

    if vram_gb <= 4.5:
        presets = {"ctx_size": 16384, "kv_quant": "q4_0", "n_gpu_layers": 99}
    elif vram_gb <= 8.5:
        presets = {"ctx_size": 32768, "kv_quant": "q8_0", "n_gpu_layers": 99}
    else:
        presets = {"ctx_size": 32768, "kv_quant": "f16", "n_gpu_layers": 99}

    ctx = int(os.environ.get("CTX_SIZE", str(presets["ctx_size"])))
    kv = os.environ.get("KV_QUANT", presets["kv_quant"])
    ngl = int(os.environ.get("NGL", str(presets["n_gpu_layers"])))
    threads = os.environ.get("THREADS")
    threads = int(threads) if threads else None
    return {
        "ctx_size": ctx,
        "kv_quant": kv,
        "n_gpu_layers": ngl,
        "threads": threads,
    }


TUNING = _default_tuning()
CTX_SIZE = TUNING["ctx_size"]
KV_QUANT = TUNING["kv_quant"]
DEFAULT_NGL = TUNING["n_gpu_layers"]
DEFAULT_THREADS = TUNING["threads"]
CHUNK_SECONDS = float(os.environ.get("CHUNK_SECONDS", "25"))
# Overlap between adjacent chunks (seconds). Lets the ASR see context across a
# boundary so words straddling the cut are recognized correctly; the overlapped
# portion is dropped during absorption to avoid duplication. 0 = contiguous.
CHUNK_OVERLAP = float(os.environ.get("CHUNK_OVERLAP", "1.5"))

# Aligner backend: "cpu" (CrispASR via ctypes, no extra deps) or
# "gpu" (official qwen-asr, unloads ASR from VRAM first then loads aligner).
ALIGNER_BACKEND = os.environ.get("ALIGNER_BACKEND", "gpu").lower()
if ALIGNER_BACKEND not in ("cpu", "gpu"):
    ALIGNER_BACKEND = "cpu"

# Lazy — evaluated at boot time so that env vars set after import still work.
_gpu_backend_ready: bool | None = None  # None = not yet checked


def _is_gpu_backend_ready() -> bool:
    global _gpu_backend_ready
    if _gpu_backend_ready is None:
        _gpu_backend_ready = ALIGNER_BACKEND == "gpu" and gpu_aligner_available()
    return _gpu_backend_ready

ASR_MODEL = str(ASR_MODELS_DIR / "Qwen3-ASR-1.7B-Q8_0.gguf")
ASR_MMPROJ = str(ASR_MODELS_DIR / "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf")

runner = LlamaRunner(
    bin_path=str(PROJECT_ROOT / "bin" / "llama-server.exe"),
    host="127.0.0.1",
    port=8080,
)

# ForcedAligner runs on CPU via CrispASR (no mmproj — that field is unused by
# the C align API, and the referenced forced-aligner mmproj does not ship).
aligner = ForcedAligner(
    model_path=str(MODELS_DIR / "aligner" / "qwen3-forced-aligner-0.6b-q8_0.gguf"),
)

STATIC_DIR = PROJECT_ROOT / "frontend" / "dist"

history_store = HistoryStore(HISTORY_DIR)


def _cleanup_stale_tmp(max_age_minutes: int = 30) -> int:
    """Remove leftover ``asr_*`` and ``align_*`` temp directories older than threshold."""
    import time as _t
    tmp_root = Path(tempfile.gettempdir())
    cutoff = _t.time() - max_age_minutes * 60
    removed = 0
    for prefix in ("asr_", "align_", "aligner_warmup_"):
        for d in tmp_root.glob(f"{prefix}*"):
            try:
                # Use lstat and reject symlinks to avoid TOCTOU path traversal:
                # an attacker replacing the directory with a symlink between the
                # stat and the rmtree could cause deletion outside the temp root.
                st = d.lstat()
                if stat.S_ISLNK(st.st_mode):
                    continue
                if stat.S_ISDIR(st.st_mode) and st.st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
    if removed:
        logger.info("Cleaned %d stale temp dirs", removed)
    return removed

job_state = {
    "status": "idle",
    "progress": 0,
    "message": "",
    "result": None,
}
job_lock = threading.Lock()

# Cancel flag: set by POST /api/abort, checked between chunks in the ASR loop.
# Guarded by job_lock so the check-and-clear in _claim_job is atomic with
# claiming the slot - a fresh job never inherits a cancel from the previous one.
_cancel_requested = False


def _is_cancel_requested() -> bool:
    with job_lock:
        return _cancel_requested


def _request_cancel() -> None:
    global _cancel_requested
    with job_lock:
        _cancel_requested = True


boot_state = {
    "phase": "starting",
    "asr_loaded": False,
    "asr_warmup": False,
    "aligner_loaded": False,
    "aligner_warmup": False,
    "ready": False,
    "error": None,
    "current_model": None,
    "switching": False,
    "ffmpeg_ok": None,
}
boot_lock = threading.Lock()


def _set_boot(**kwargs):
    with boot_lock:
        boot_state.update(kwargs)


def set_job(status, progress=0, message="", result=None):
    with job_lock:
        job_state["status"] = status
        job_state["progress"] = progress
        job_state["message"] = message
        job_state["result"] = result


def _make_silent_wav(seconds: float = 0.3, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(int(seconds * sr))))
    return buf.getvalue()


def _scan_asr_models() -> list[dict]:
    """Scan models/asr/ for main ASR models (non-mmproj .gguf) and pair each
    with its best matching mmproj projector (prefer Q8_0, fall back to bf16)."""
    if not ASR_MODELS_DIR.exists():
        return []
    all_gguf = sorted(ASR_MODELS_DIR.glob("*.gguf"))
    mmproj_files = [p for p in all_gguf if p.name.lower().startswith("mmproj")]
    main_files = [p for p in all_gguf if not p.name.lower().startswith("mmproj")]

    models: list[dict] = []
    for mp in main_files:
        stem = mp.name
        # candidates: same base name with mmproj- prefix, prefer Q8_0 over bf16
        base = stem  # e.g. Qwen3-ASR-1.7B-Q8_0.gguf
        pairs = [p for p in mmproj_files if base[:-5] in p.name]
        pairs.sort(key=lambda p: (0 if "q8_0" in p.name.lower() else 1, p.name))
        mmproj = str(pairs[0]) if pairs else None
        # path/mmproj are INTERNAL (used by /api/models/switch to reload the
        # model). The /api/models LIST endpoint strips them before responding so
        # absolute install paths never reach the client.
        models.append({
            "name": mp.stem,
            "path": str(mp),
            "size": mp.stat().st_size,
            "mmproj": mmproj,
        })
    return models


def _start_asr(
    model_path: str = ASR_MODEL,
    mmproj_path: str | None = ASR_MMPROJ,
    ctx_size: int = CTX_SIZE,
    kv_quant: str = KV_QUANT,
    n_gpu_layers: int = DEFAULT_NGL,
    threads: int | None = DEFAULT_THREADS,
    batch_size: int | None = None,
    ubatch_size: int | None = None,
    flash_attn: bool = True,
    mmproj_offload: bool = False,
    model_name: str | None = None,
    timeout: float = 240.0,
) -> None:
    """Start/swap the ASR model. Shared by boot sequence and /api/models/switch."""
    runner.start(
        model_path=model_path,
        mmproj_path=mmproj_path,
        ctx_size=ctx_size,
        kv_quant=kv_quant,
        n_gpu_layers=n_gpu_layers,
        threads=threads,
        batch_size=batch_size,
        ubatch_size=ubatch_size,
        flash_attn=flash_attn,
        mmproj_offload=mmproj_offload,
        model_name=model_name,
        timeout=timeout,
    )
    _set_boot(current_model=runner.current_model)


def _warmup_asr():
    try:
        wav_bytes = _make_silent_wav(0.3)
        r = httpx.post(
            f"{runner.base_url}/v1/audio/transcriptions",
            files={"file": ("warmup.wav", wav_bytes, "audio/wav")},
            data={"response_format": "json"},
            timeout=60.0,
        )
        if r.status_code == 200:
            logger.info("ASR warmup OK")
            return True
        logger.warning("ASR warmup returned %d: %s", r.status_code, r.text[:200])
        return True
    except Exception as e:
        logger.warning("ASR warmup failed (non-fatal): %s", e)
        return True


def _warmup_aligner() -> bool:
    if not aligner.available:
        return False
    tmp_dir = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="aligner_warmup_"))
        wav_path = tmp_dir / "warmup.wav"
        wav_path.write_bytes(_make_silent_wav(0.3))
        words = aligner.align_chunk(wav_path, "test", 0.0)
        logger.info("Aligner warmup OK (%d words from silent)", len(words))
        return True
    except Exception as e:
        logger.warning("Aligner warmup failed (non-fatal): %s", e)
        return False
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _boot_sequence():
    skip_llama = os.environ.get("SKIP_LLAMA", "0") == "1"

    # Stale-temp cleanup only reclaims disk space and nothing downstream
    # depends on it — run it fire-and-forget so it never blocks the queue head.
    def _cleanup_in_background():
        try:
            _cleanup_stale_tmp()
        except Exception as e:
            logger.warning("Boot cleanup failed: %s", e)

    threading.Thread(target=_cleanup_in_background, daemon=True).start()

    # Block transcription/model-switch requests until the boot sequence is
    # complete.  This prevents a switch from racing with startup ASR load.
    _set_boot(phase="starting", switching=True)

    # ── Pre-flight checks ─ fail in milliseconds with a clear message instead
    # of spawning llama-server and waiting out its stderr/timeout path. Only
    # relative file names are exposed (never absolute install paths).
    _set_boot(ffmpeg_ok=shutil.which("ffmpeg") is not None)
    if not skip_llama:
        required = {
            "bin/llama-server.exe": runner.bin_path,
            f"models/asr/{Path(ASR_MODEL).name}": Path(ASR_MODEL),
            f"models/asr/{Path(ASR_MMPROJ).name}": Path(ASR_MMPROJ),
        }
        missing = [name for name, p in required.items() if not p.is_file()]
        if missing:
            logger.error("Boot pre-flight failed; missing: %s", ", ".join(missing))
            _set_boot(phase="error",
                      error="missing required files: " + ", ".join(missing),
                      switching=False)
            return

    # CPU aligner warmup is pure CPU work — overlap it with the GPU model
    # load instead of running serially after it. Result is joined below once
    # ASR is up. The GPU backend probes lazily and needs no warmup here.
    warmup_thread: threading.Thread | None = None
    warmup_result = {"ok": False}
    if ALIGNER_BACKEND != "gpu" and aligner.available:
        def _warmup_in_background():
            try:
                warmup_result["ok"] = _warmup_aligner()
            except Exception:
                warmup_result["ok"] = False

        warmup_thread = threading.Thread(target=_warmup_in_background, daemon=True)
        warmup_thread.start()

    if skip_llama:
        _set_boot(phase="asr_skipped", asr_loaded=True, asr_warmup=True,
                  current_model="skipped")
    else:
        _set_boot(phase="asr_loading")
        try:
            _start_asr()
            _set_boot(asr_loaded=True, phase="asr_warmup")
        except Exception:
            logger.exception("ASR load failed during boot")
            # Keep the raw stderr out of boot_state — /api/readiness returns it
            # verbatim and it contains model paths, GPU info, and the command line.
            _set_boot(phase="error", error="ASR load failed; see server logs",
                      switching=False)
            return

        try:
            _warmup_asr()
            _set_boot(asr_warmup=True, phase="aligner_check")
        except Exception:
            _set_boot(asr_warmup=True, phase="aligner_check")

    if ALIGNER_BACKEND == "gpu":
        if _is_gpu_backend_ready():
            _set_boot(aligner_loaded=True, aligner_warmup=True, phase="ready",
                      ready=True, switching=False)
            logger.info("Aligner backend: GPU (qwen-asr, model at %s)", GPU_ALIGNER_MODEL_PATH)
        else:
            logger.warning(
                "ALIGNER_BACKEND=gpu but qwen-asr/model not available; "
                "alignment will be skipped.  Install: pip install qwen-asr torch transformers "
                "&& huggingface-cli download Qwen/Qwen3-ForcedAligner-0.6B --local-dir %s",
                GPU_ALIGNER_MODEL_PATH,
            )
            _set_boot(aligner_loaded=False, aligner_warmup=False, phase="ready",
                      ready=True, switching=False)
    elif aligner.available:
        _set_boot(phase="aligner_loading", aligner_loaded=True)
        from .forced_aligner import ALIGN_MAX_WORKERS, ALIGN_N_THREADS
        logger.info(
            "Aligner backend: CPU (CrispASR, workers=%d threads=%d)",
            ALIGN_MAX_WORKERS, ALIGN_N_THREADS,
        )
        # Warmup was started in parallel with the ASR load above — just join.
        if warmup_thread is not None:
            warmup_thread.join(timeout=120)
        _set_boot(aligner_warmup=warmup_result["ok"], phase="ready", ready=True,
                  switching=False)
    else:
        _set_boot(aligner_loaded=False, aligner_warmup=False, phase="ready",
                  ready=True, switching=False)

    logger.info("Boot complete — system ready")


@asynccontextmanager
async def lifespan(app: FastAPI):
    boot_thread = threading.Thread(target=_boot_sequence, daemon=True)
    boot_thread.start()
    yield
    runner.stop()


app = FastAPI(lifespan=lifespan)
# The SPA is served same-origin by this app, so CORS is unnecessary. The
# loopback-only guard (Host + Origin checks) replaces the previous wildcard
# CORS policy, which let any website read transcripts/history, delete data,
# and trigger transcription on a user's machine. See _LocalOnlyMiddleware.
app.add_middleware(_LocalOnlyMiddleware)

if not _BIND_IS_LOOPBACK:
    logger.warning(
        "⚠️  Binding to non-loopback host %s:%s with NO authentication — every "
        "device that can reach this machine can read/delete history and consume "
        "GPU. Bind to 127.0.0.1 unless you intentionally share on a trusted LAN.",
        _BIND_HOST, _BIND_PORT,
    )


@app.get("/api/health")
async def health():
    return {
        "backend": True,
        "llama_server": runner.is_alive(),
        "current_model": runner.current_model,
        "aligner_available": aligner.available or _is_gpu_backend_ready(),
        "aligner_status": (
            f"gpu:qwen-asr" if _is_gpu_backend_ready()
            else f"cpu:crispasr" if aligner.available
            else aligner.lib_status
        ),
        "aligner_backend": ALIGNER_BACKEND,
    }


@app.get("/api/readiness")
async def readiness():
    with boot_lock:
        return dict(boot_state)


@app.get("/api/status")
async def status():
    with job_lock:
        return dict(job_state)


@app.post("/api/abort")
async def abort_job():
    """Request cancellation of the in-flight transcription.

    Sets a flag checked between chunks; the ASR loop winds down at the next
    chunk boundary and frees the slot. Idempotent - returns ok when idle too,
    so the client can fire-and-forget on cancel without racing the status.
    """
    _request_cancel()
    return {"ok": True}


@app.get("/api/models")
async def models_list():
    """List available ASR models and the currently loaded one."""
    # Strip absolute paths (internal-only fields) before exposing to the client.
    safe_models = [
        {"name": m["name"], "size": m["size"], "has_mmproj": bool(m["mmproj"])}
        for m in _scan_asr_models()
    ]
    return {
        "models": safe_models,
        "current_model": runner.current_model,
        "tuning": {
            "ctx_size": CTX_SIZE,
            "kv_quant": KV_QUANT,
            "n_gpu_layers": DEFAULT_NGL,
            "threads": DEFAULT_THREADS,
        },
    }


@app.post("/api/models/switch")
async def models_switch(req: dict = Body(default={})):
    """Hot-swap the running ASR model. Runs in a worker thread so the long
    blocking load does not stall the event loop."""
    req = req or {}
    model_name = req.get("model")
    if not model_name:
        return {"error": "bad_request", "detail": "model is required"}

    models = _scan_asr_models()
    match = next((m for m in models if m["name"] == model_name), None)
    if not match:
        return {"error": "not_found", "detail": f"unknown model {model_name}"}

    # Refuse if a transcription or another model switch is in flight.
    # Claim the switching slot atomically so no concurrent job can sneak in
    # between the check and the actual subprocess restart.
    with job_lock:
        busy = job_state["status"] in ("preparing", "transcribing", "aligning")
        switching = boot_state.get("switching")
        not_ready = not boot_state.get("ready")
        if busy:
            return {"error": "busy", "detail": "a transcription is in progress"}
        if switching:
            return {"error": "busy", "detail": "a model switch is already in progress"}
        if not_ready:
            return {"error": "busy", "detail": "backend is not ready"}
        _set_boot(switching=True, phase="asr_switching", ready=False, error=None)

    # ── Validate user-controlled tuning params before touching the subprocess.
    # These flow into llama-server argv; an unbounded ctx_size forces a
    # multi-GB KV-cache allocation (OOM) and a bad kv_quant leaves ASR
    # unloaded. Allowlist/clamp rather than passing values straight through.
    try:
        kv_quant = str(req.get("kv_quant", KV_QUANT)).lower()
        if kv_quant not in _KV_QUANT_ALLOWLIST:
            raise ValueError("invalid kv_quant")
        ctx_size = int(req.get("ctx_size", CTX_SIZE))
        if not (512 <= ctx_size <= 131072):
            raise ValueError("ctx_size out of range")
        n_gpu_layers = int(req.get("n_gpu_layers", DEFAULT_NGL))
        if not (0 <= n_gpu_layers <= 999):
            raise ValueError("n_gpu_layers out of range")
        threads = req.get("threads", DEFAULT_THREADS)
        if threads is not None:
            threads = int(threads)
            if not (1 <= threads <= 1024):
                raise ValueError("threads out of range")
        batch_size = _opt_int(req.get("batch_size"), 1, 8192)
        ubatch_size = _opt_int(req.get("ubatch_size"), 1, 8192)
    except (TypeError, ValueError) as e:
        _set_boot(switching=False, phase="ready", ready=True)
        return {"error": "bad_request", "detail": str(e)}
    try:
        await asyncio.to_thread(
            _start_asr,
            model_path=match["path"],
            mmproj_path=match["mmproj"],
            ctx_size=ctx_size,
            kv_quant=kv_quant,
            n_gpu_layers=n_gpu_layers,
            threads=threads,
            batch_size=batch_size,
            ubatch_size=ubatch_size,
            flash_attn=bool(req.get("flash_attn", True)),
            mmproj_offload=bool(req.get("mmproj_offload", False)),
            model_name=match["name"],
        )
        _set_boot(switching=False, phase="ready", ready=True, asr_loaded=True)
        return {"ok": True, "current_model": runner.current_model}
    except Exception:
        logger.exception("Model switch failed")
        # Don't echo the exception (it embeds llama-server stderr with model
        # paths / GPU info) — keep the detail server-side only.
        _set_boot(switching=False, phase="error", error="Model switch failed; see server logs")
        return {"error": "switch_failed", "detail": "model failed to load; see server logs"}


async def _transcribe_chunk(
    client: httpx.AsyncClient,
    chunk_path: Path,
    initial_prompt: str | None = None,
) -> dict:
    with chunk_path.open("rb") as fh:
        audio_bytes = fh.read()
    data: dict[str, str] = {"response_format": "json"}
    # Greedy decoding eliminates sampling noise (lowers WER at zero cost).
    data["temperature"] = str(ASR_TEMPERATURE)
    if ASR_LANGUAGE:
        data["language"] = ASR_LANGUAGE
    # Feed the previous chunk's text as context so the model doesn't lose
    # continuity across a chunk boundary — directly attacks boundary errors.
    if initial_prompt:
        data["initial_prompt"] = initial_prompt[:ASR_PROMPT_MAX_CHARS]
    resp = await client.post(
        f"{runner.base_url}/v1/audio/transcriptions",
        files={"file": (chunk_path.name, audio_bytes, "audio/wav")},
        data=data,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"llama-server {resp.status_code}: {resp.text[:500]}")
    rdata = resp.json()
    if "error" in rdata:
        msg = rdata["error"].get("message", str(rdata["error"])) if isinstance(rdata["error"], dict) else str(rdata["error"])
        raise RuntimeError(msg)
    return rdata


# Max ASR retries before giving up on a chunk (sampling randomness means a
# template-leakage 500 usually succeeds on retry).
ASR_MAX_RETRIES = int(os.environ.get("ASR_MAX_RETRIES", "2"))
# Re-split a persistently-failing chunk into halves before giving up.
ASR_RESPLIT = os.environ.get("ASR_RESPLIT", "1") == "1"
# Decode params (WER/CER levers).
ASR_TEMPERATURE = float(os.environ.get("ASR_TEMPERATURE", "0"))
ASR_LANGUAGE = os.environ.get("ASR_LANGUAGE", "").strip()
ASR_PROMPT_MAX_CHARS = int(os.environ.get("ASR_PROMPT_MAX_CHARS", "200"))


async def _transcribe_chunk_retry(
    client: httpx.AsyncClient,
    chunk_path: Path,
    initial_prompt: str | None = None,
    max_retries: int = ASR_MAX_RETRIES,
) -> dict:
    """Transcribe a chunk with bounded retries on failure."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _transcribe_chunk(client, chunk_path, initial_prompt)
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    assert last_err is not None
    raise last_err


def _resplit_chunk(chunk_path: Path, out_dir: Path) -> list[tuple[Path, float]]:
    """Split a failing chunk WAV into two halves for a second attempt.

    Re-cutting at a different boundary often sidesteps the content that
    triggered the ASR parse failure. Returns [(half_path, local_offset), ...]
    where local_offset is 0.0 for the first half and half-duration for the
    second — callers add this to the chunk's absolute start_offset.
    """
    dur = probe_duration(chunk_path)
    if dur <= 1.0:
        return []  # too short to split meaningfully
    half = dur / 2.0
    out_dir.mkdir(parents=True, exist_ok=True)
    halves: list[tuple[Path, float]] = []
    ffmpeg = None
    try:
        from .audio_chunk import resolve_ffmpeg
        ffmpeg = resolve_ffmpeg("ffmpeg")
    except Exception:
        return []

    for i, start in enumerate((0.0, half)):
        out = out_dir / f"{chunk_path.stem}_h{i}.wav"
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{half:.3f}",
            "-i", str(chunk_path),
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(out),
        ]
        try:
            import subprocess
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            if out.stat().st_size > 44:
                halves.append((out, start))
        except Exception:
            pass
    return halves


def _gpu_swap_align(
    jobs: list[tuple[Path, str, float]],
    asr_segments_raw: list[dict],
    *,
    all_segments: list[dict],
    stats: dict,
    t_align_start: float,
) -> None:
    """GPU alignment path: unload ASR, load qwen-asr aligner, align, reload ASR.

    This swaps the GPU occupant — llama-server is stopped to free VRAM,
    then the official Qwen3-ForcedAligner is loaded locally and kept warm
    for all alignment jobs.  After alignment the aligner is released and
    llama-server restarted so the system is ready for the next request.

    On any failure the ASR model is *always* reloaded (best-effort) and
    the ASR segments are left intact.
    """
    # 1. Unload ASR from GPU.
    set_job("aligning", 80, "Switching GPU — unloading ASR...")
    logger.info("GPU swap: stopping llama-server to free VRAM")
    runner.stop()

    gpu_aligner: GPUAligner | None = None
    try:
        # 2. Load GPU aligner.
        set_job("aligning", 83, "Loading GPU forced aligner...")
        gpu_aligner = GPUAligner(GPU_ALIGNER_MODEL_PATH)
        if not gpu_aligner.available:
            raise RuntimeError(f"GPU aligner model not found at {GPU_ALIGNER_MODEL_PATH}")
        gpu_aligner.load()

        # 3. Run alignment (NAR forward pass per chunk, model stays on GPU).
        set_job("aligning", 85, "Running GPU forced alignment...")
        total_jobs = len(jobs)

        def _progress(done: int, total: int) -> None:
            set_job(
                "aligning",
                85 + int(8 * done / total),
                f"GPU align chunk {done}/{total}...",
            )

        per_chunk = gpu_aligner.align_many(jobs, progress_cb=_progress)
        word_segments: list[dict] = []
        for words in per_chunk:
            word_segments.extend(words)

        if word_segments:
            resegmented = resegment_words(
                word_segments,
                original_segments=asr_segments_raw,
            )
            all_segments.clear()
            all_segments.extend(resegmented if resegmented else word_segments)
            stats["word_count"] = len(word_segments)
            stats["aligner_used"] = True
            stats["aligner_model"] = "Qwen3-ForcedAligner-0.6B (GPU)"

        logger.info("GPU alignment complete: %d words", len(word_segments))
    except Exception as e:
        logger.warning("GPU align failed, keeping ASR timestamps: %s", e)
    finally:
        # 4. Release aligner from VRAM.
        if gpu_aligner is not None:
            gpu_aligner.close()
            gpu_aligner = None

        # 5. Reload ASR onto GPU (best-effort — if this fails the next
        #    transcription will start a fresh server via _boot_sequence
        #    or the user can restart the app).
        set_job("aligning", 93, "Reloading ASR model...")
        try:
            _start_asr()
            logger.info("ASR model reloaded after GPU swap")
        except Exception as e:
            logger.error("Failed to reload ASR after GPU swap: %s", e)
            set_job("aligning", 93, "ASR reload failed — restart required")

        stats["align_time"] = round(time.monotonic() - t_align_start, 3)


def _shift_segments(segments: list[dict], offset: float) -> list[dict]:
    shifted = []
    for s in segments or []:
        ns = dict(s)
        if "start" in ns and ns["start"] is not None:
            ns["start"] = float(ns["start"]) + offset
        if "end" in ns and ns["end"] is not None:
            ns["end"] = float(ns["end"]) + offset
        shifted.append(ns)
    return shifted


@app.post("/api/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    align: bool = Form(False),
):
    t_total_start = time.monotonic()

    # Single-slot concurrency guard: only one heavy job at a time. Without
    # this, concurrent requests each load a full upload and spawn their own
    # ffmpeg/ASR pipeline, multiplying the per-request footprint and thrashing
    # the shared llama-server. _claim_job atomically checks-and-sets under
    # job_lock to avoid the TOCTOU a naive check-then-proceed would leave.
    if not _claim_job():
        return JSONResponse(
            {"error": "busy", "detail": "a transcription is already in progress"},
            status_code=409,
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="asr_"))
    src_suffix = _safe_upload_suffix(file.filename)
    src_path = tmp_dir / f"input{src_suffix}"

    # Stream the upload straight to disk in 1 MiB chunks — the body is never
    # held as a single in-memory bytes object, so a large upload cannot OOM
    # the process. Oversize uploads are rejected with 413 before any work.
    try:
        upload_size = await _stream_upload_to(file, src_path, MAX_UPLOAD_BYTES)
    except _UploadTooLarge:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        set_job("idle")
        return JSONResponse(
            {"error": "payload_too_large",
             "detail": f"upload exceeds {MAX_UPLOAD_BYTES} bytes"},
            status_code=413,
        )
    safe_filename = _safe_filename(file.filename)
    logger.info("Received file: %s, size=%d bytes, align=%s",
                safe_filename, upload_size, align)

    set_job("preparing", 5, "Preparing audio...")

    stats = {
        "filename": safe_filename,
        "file_size": upload_size,
        "audio_duration": 0.0,
        "chunk_count": 0,
        "chunk_seconds": CHUNK_SECONDS,
        "asr_time": 0.0,
        "align_time": 0.0,
        "total_time": 0.0,
        "rtf": 0.0,
        "char_count": 0,
        "segment_count": 0,
        "word_count": 0,
        "aligner_used": False,
        "model": runner.current_model or "Qwen3-ASR-1.7B-Q8_0",
        "aligner_model": None,
        "source_was_video": False,
        "audio_cache_name": None,
        "mp3_bitrate": None,
    }

    # Initialized here so the `finally` below can safely reference it even on
    # early-return paths that occur before the alignment pipeline is set up.
    align_pool: ThreadPoolExecutor | None = None

    try:
        # ── Video handling: extract (or reuse) a cached MP3 ───────────
        try:
            if await asyncio.to_thread(is_video_file, src_path):
                stats["source_was_video"] = True
                cache_name = f"{safe_cache_stem(file.filename or 'input')}__{upload_size}.mp3"
                mp3_path = AUDIO_CACHE_DIR / cache_name
                if mp3_path.exists() and mp3_path.stat().st_size > 0:
                    logger.info("Reusing cached mp3 for %s: %s", file.filename, cache_name)
                else:
                    set_job("preparing", 7, "Extracting audio (video → MP3)...")
                    orig_br = await asyncio.to_thread(probe_audio_bitrate, src_path)
                    target = orig_br if (orig_br and orig_br <= MP3_DEFAULT_BITRATE) else MP3_DEFAULT_BITRATE
                    logger.info(
                        "Extracting mp3 from %s: orig_br=%s target=%d",
                        file.filename, orig_br, target,
                    )
                    await asyncio.to_thread(extract_audio_to_mp3, src_path, mp3_path, target)
                    _evict_audio_cache()
                    stats["mp3_bitrate"] = target
                src_path = mp3_path
                stats["audio_cache_name"] = mp3_path.name
        except FFmpegMissingError:
            logger.warning("ffmpeg missing during audio extraction")
            set_job("error", 0, "ffmpeg not available")
            return {"error": "ffmpeg_missing", "detail": "ffmpeg not found on PATH"}
        except Exception:
            # Don't echo str(e): subprocess.CalledProcessError embeds the full
            # command line with absolute temp paths (and the Windows username).
            logger.exception("Audio extraction failed")
            # A timed-out/failed extract can leave a partial MP3 in the persistent
            # cache; delete it so a retry re-extracts instead of silently
            # transcribing the truncated file. (mp3_path may be unbound if the
            # failure happened before it was assigned - guard with NameError.)
            try:
                mp3_path.unlink(missing_ok=True)
            except (NameError, OSError):
                pass
            set_job("error", 0, "Audio extraction failed")
            return {"error": "extract_failed", "detail": "audio extraction failed"}

        try:
            duration = await asyncio.to_thread(probe_duration, src_path)
        except FFmpegMissingError:
            logger.warning("ffmpeg missing during probe")
            set_job("error", 0, "ffmpeg not available")
            return {"error": "ffmpeg_missing", "detail": "ffmpeg not found on PATH"}
        except Exception:
            logger.exception("Probe failed")
            set_job("error", 0, "Audio probe failed")
            return {"error": "probe_failed", "detail": "could not probe audio duration"}

        if duration <= 0:
            set_job("error", 0, "No audio content")
            return {"error": "empty_audio", "detail": "audio has no detectable duration"}
        if duration > MAX_AUDIO_DURATION:
            set_job("error", 0, "Audio too long")
            return {"error": "too_long",
                    "detail": f"audio duration {duration:.0f}s exceeds limit {MAX_AUDIO_DURATION:.0f}s"}

        stats["audio_duration"] = round(duration, 3)

        try:
            chunks = await asyncio.to_thread(
                split_audio, src_path, tmp_dir / "chunks", CHUNK_SECONDS, CHUNK_OVERLAP,
            )
        except Exception:
            logger.exception("Split failed")
            set_job("error", 0, "Audio split failed")
            return {"error": "split_failed", "detail": "audio splitting failed"}

        if not chunks:
            set_job("error", 0, "No audio content")
            return {"error": "empty_audio", "detail": "Audio split produced no chunks"}

        stats["chunk_count"] = len(chunks)
        logger.info("Audio duration: %.2fs, chunks=%d, align=%s", duration, len(chunks), align)

        all_segments: list[dict] = []
        all_text_parts: list[str] = []
        asr_segments_raw: list[dict] = []
        total = len(chunks)

        # ── Alignment pipeline setup ───────────────────────────────────
        # CPU: CrispASR via ctypes, parallelized across chunks (pipelined
        #   with ASR to overlap GPU/CPU work).
        # GPU: official qwen-asr, loaded AFTER ASR finishes (llama-server
        #   is stopped to free VRAM) and kept in GPU memory for all chunks.
        gpu_swap = ALIGNER_BACKEND == "gpu" and align and _is_gpu_backend_ready()
        do_align_cpu = align and aligner.available and not gpu_swap
        align_pool = (
            ThreadPoolExecutor(max_workers=ALIGN_MAX_WORKERS) if do_align_cpu else None
        )
        align_futures: list = []  # CPU: Future[list[dict]]; GPU: unused
        # GPU backend defers submission until Phase 2 — collect jobs here.
        gpu_align_jobs: list[tuple[Path, str, float]] = []
        t_align_start = time.monotonic() if (do_align_cpu or gpu_swap) else None

        # ── Phase 1: ASR (+ pipelined alignment submission, CPU only) ──
        # Per-chunk failure handling is three-tier:
        #   1. retry the same chunk (ASR sampling randomness → 500s usually
        #      succeed on retry),
        #   2. re-split the chunk into halves and try each (sidesteps the
        #      content that triggered the parse failure),
        #   3. give up — record the failure and skip (keeps the rest of the
        #      transcription intact).
        t_asr_start = time.monotonic()
        chunk_failures: list[str] = []  # error messages per failed chunk
        cancelled = False  # set True by /api/abort, checked between chunks
        resplit_dir = tmp_dir / "resplit"
        # Running context for initial_prompt: the tail of the last successful
        # chunk's text, fed to the next chunk to preserve cross-boundary continuity.
        last_chunk_text = ""

        def _absorb(
            data: dict, wav_path: Path, start_offset: float, prev_end: float,
        ) -> str:
            """Fold one successful ASR result into accumulators + align queue.

            ``prev_end`` is the absolute time from which this chunk is
            responsible for output. Segments whose (shifted) start falls before
            ``prev_end`` lie in the overlap with the previous chunk and are
            dropped to avoid duplicating content. Returns the chunk's cleaned
            text (used to seed the next chunk's initial_prompt).
            """
            raw_segments = data.get("segments") or []
            raw_segments = clean_segments(raw_segments)
            # Raw (unshifted) segments feed resegment_words later.
            if raw_segments:
                asr_segments_raw.extend(raw_segments)

            kept: list[dict] = []
            for seg in raw_segments:
                shifted = _shift_segments([seg], start_offset)[0]
                # Drop segments entirely in the overlap region (before prev_end).
                # A straddler whose end crosses prev_end is trimmed to prev_end
                # so its content is kept without duplicating the previous chunk.
                if shifted.get("start", 0.0) < prev_end - 1e-3:
                    if shifted.get("end", 0.0) > prev_end:
                        shifted["start"] = prev_end
                        kept.append(shifted)
                    continue
                kept.append(shifted)

            if kept:
                all_segments.extend(kept)

            # Build text from kept segments when available (consistent with
            # the dedup'd segments), else fall back to the ASR text field.
            if kept:
                text = " ".join(s.get("text", "") for s in kept).strip()
            else:
                text = (data.get("text") or "").strip()
            text = clean_asr_text(text)

            if text:
                all_text_parts.append(text)
                if do_align_cpu and align_pool is not None:
                    align_futures.append(
                        align_pool.submit(aligner.align_chunk, wav_path, text, start_offset)
                    )
                elif gpu_swap:
                    gpu_align_jobs.append((wav_path, text, start_offset))
            return text

        async with httpx.AsyncClient(timeout=600.0) as client:
            for i, entry in enumerate(chunks, 1):
                # chunks are (chunk_path, start_offset, prev_end)
                chunk_path, start_offset, prev_end = entry
                # Cooperative cancel: /api/abort sets the flag; wind down at the
                # next chunk boundary so partial work is preserved, not wasted.
                if _is_cancel_requested():
                    logger.info("Transcription cancelled at chunk %d/%d", i, total)
                    cancelled = True
                    break
                progress = 10 + int(70 * (i - 1) / total)
                set_job(
                    "transcribing",
                    progress,
                    f"Transcribing chunk {i}/{total} ({start_offset:.1f}s)...",
                )
                prompt = last_chunk_text or None

                # ── Tier 1: retry ─────────────────────────────────────────
                try:
                    data = await _transcribe_chunk_retry(client, chunk_path, prompt)
                    last_chunk_text = _absorb(data, chunk_path, start_offset, prev_end)
                    continue
                except Exception as e:
                    tier1_err = str(e)
                    logger.warning("Chunk %d/%d failed after retries: %s", i, total, tier1_err)

                # ── Tier 2: re-split into halves, try each ────────────────
                if ASR_RESPLIT:
                    # Run off the event loop: _resplit_chunk calls probe_duration
                    # + ffmpeg, which would otherwise stall /api/status and
                    # /api/abort while a failing chunk is being re-cut.
                    halves = await asyncio.to_thread(_resplit_chunk, chunk_path, resplit_dir)
                    if halves:
                        logger.info("Chunk %d/%d: re-splitting into %d halves", i, total, len(halves))
                        half_ok = 0
                        half_texts: list[str] = []
                        for half_path, local_off in halves:
                            try:
                                h_data = await _transcribe_chunk_retry(client, half_path, prompt)
                                ht = _absorb(h_data, half_path, start_offset + local_off, start_offset + local_off)
                                half_texts.append(ht)
                                half_ok += 1
                            except Exception as he:
                                logger.warning(
                                    "Chunk %d/%d half (off %.1fs) failed: %s",
                                    i, total, local_off, he,
                                )
                        if half_ok > 0:
                            last_chunk_text = " ".join(t for t in half_texts if t)
                            continue  # at least one half succeeded
                # ── Tier 3: give up, skip ─────────────────────────────────
                logger.warning("Chunk %d/%d skipped after all recovery attempts", i, total)
                # Sanitized summary only — the full tier1_err (which embeds the
                # raw llama-server response body) is logged server-side above
                # but must not be persisted to history or returned to clients.
                chunk_failures.append(f"chunk {i} (@{start_offset:.0f}s): asr_error")
                # Don't carry a failed chunk's text forward as context.

        stats["asr_time"] = round(time.monotonic() - t_asr_start, 3)

        merged_text = " ".join(all_text_parts).strip()

        if cancelled:
            # User cancelled mid-ASR: keep the partial transcript so work isn't
            # lost, skip the alignment phase, and release the slot. The finally
            # block won't override "cancelled" (not in its busy-status tuple).
            if align_pool:
                align_pool.shutdown(wait=False, cancel_futures=True)
            stats["cancelled"] = True
            stats["char_count"] = len(merged_text)
            stats["segment_count"] = len(all_segments)
            stats["total_time"] = round(time.monotonic() - t_total_start, 3)
            if duration > 0:
                stats["rtf"] = round(stats["total_time"] / duration, 3)
            set_job("cancelled", 90, "Cancelled by user")
            history_id = None
            if merged_text or all_segments:
                try:
                    record = history_store.save(
                        filename=safe_filename,
                        text=merged_text,
                        segments=all_segments,
                        stats=stats,
                        align_used=False,
                        audio_cache_name=stats.get("audio_cache_name"),
                    )
                    history_id = record["id"]
                except Exception as e:
                    logger.warning("Failed to persist cancelled history: %s", e)
            logger.info("Transcription cancelled; kept %d chars", len(merged_text))
            return {
                "text": merged_text,
                "segments": all_segments,
                "stats": stats,
                "history_id": history_id,
                "cancelled": True,
            }

        if not merged_text and not all_segments:
            if align_pool:
                align_pool.shutdown(wait=False, cancel_futures=True)
            detail = "All chunks returned empty"
            if chunk_failures:
                detail += f" ({len(chunk_failures)} failed: {chunk_failures[0]})"
            set_job("error", 0, "No transcription text returned")
            return {"error": "empty_result", "detail": detail}

        if chunk_failures:
            logger.warning(
                "Transcription completed with %d/%d chunk failures: %s",
                len(chunk_failures), total, "; ".join(chunk_failures[:3]),
            )
            stats["chunk_failures"] = len(chunk_failures)
            stats["chunk_failure_details"] = chunk_failures

        stats["char_count"] = len(merged_text)
        stats["segment_count"] = len(all_segments)

        # ── Phase 2: Alignment ─────────────────────────────────────────
        if gpu_swap and gpu_align_jobs:
            # ── GPU path: unload ASR, load aligner on GPU, realign, reload ASR
            assert t_align_start is not None
            # Run off the event loop: this blocks for seconds (model load +
            # inference + ASR reload); without to_thread it stalls /api/status
            # polls so the UI freezes mid-alignment. Mutates all_segments/stats
            # in place - safe because we await the result before touching them.
            await asyncio.to_thread(
                _gpu_swap_align,
                gpu_align_jobs, asr_segments_raw,
                all_segments=all_segments, stats=stats, t_align_start=t_align_start,
            )
        elif do_align_cpu and align_futures:
            # ── CPU path: drain CrispASR futures (most already completed) ─
            assert align_pool is not None and t_align_start is not None
            set_job("aligning", 85, "Finalizing forced alignment (CrispASR)...")
            total_align = len(align_futures)
            try:
                word_segments: list[dict] = []
                for done_i, fut in enumerate(align_futures, 1):
                    words = fut.result()
                    word_segments.extend(words)
                    set_job(
                        "aligning",
                        85 + int(10 * done_i / total_align),
                        f"Aligning chunk {done_i}/{total_align}...",
                    )

                if word_segments:
                    resegmented = resegment_words(
                        word_segments,
                        original_segments=asr_segments_raw,
                    )
                    all_segments = resegmented if resegmented else word_segments
                    stats["word_count"] = len(word_segments)
                    stats["aligner_used"] = True
                    stats["aligner_model"] = aligner.model_path.stem
                stats["align_time"] = round(time.monotonic() - t_align_start, 3)
                logger.info("Alignment complete: %d words", len(word_segments))
            except Exception as e:
                stats["align_time"] = round(time.monotonic() - t_align_start, 3)
                logger.warning("Forced alignment failed, keeping ASR timestamps: %s", e)
            finally:
                align_pool.shutdown(wait=False)
        elif align and not aligner.available and not gpu_swap:
            logger.info("Alignment requested but ForcedAligner not available (%s) — skipping", aligner.lib_status)

        if not stats["word_count"]:
            stats["word_count"] = len(merged_text.split())

        stats["segment_count"] = len(all_segments)
        stats["total_time"] = round(time.monotonic() - t_total_start, 3)
        if duration > 0:
            stats["rtf"] = round(stats["total_time"] / duration, 3)

        set_job("done", 100, "Complete")
        logger.info(
            "Transcription complete: %d segments, %d chars, RTF=%.3f, asr=%.1fs, align=%.1fs",
            len(all_segments), len(merged_text), stats["rtf"],
            stats["asr_time"], stats["align_time"],
        )
        try:
            record = history_store.save(
                filename=safe_filename,
                text=merged_text,
                segments=all_segments,
                stats=stats,
                align_used=stats.get("aligner_used", False),
                audio_cache_name=stats.get("audio_cache_name"),
            )
            history_id = record["id"]
        except Exception as e:
            logger.warning("Failed to persist history: %s", e)
            history_id = None
        return {
            "text": merged_text,
            "segments": all_segments,
            "stats": stats,
            "history_id": history_id,
        }
    finally:
        # Defensive: ensure the alignment pool is always released, even on
        # early-return / exception paths that bypass the drain block.
        if align_pool is not None:
            align_pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # If we exited via an uncaught exception (status never reached
        # done/error), release the single job slot so later requests aren't
        # permanently rejected as "busy".
        with job_lock:
            if job_state["status"] in ("preparing", "transcribing", "aligning"):
                job_state["status"] = "error"
                job_state["message"] = "Transcription failed unexpectedly"


@app.post("/api/align")
async def align_standalone(
    file: UploadFile = File(...),
    text: str = Form(...),
):
    """Standalone forced alignment endpoint using CrispASR."""
    if not aligner.available:
        return {"error": "aligner_unavailable", "detail": "ForcedAligner not available"}

    # Bound the text field — python-multipart buffers the whole thing, so an
    # unbounded text could exhaust RAM independently of the audio upload.
    if len(text) > MAX_ALIGN_TEXT_CHARS:
        return JSONResponse(
            {"error": "bad_request", "detail": f"text exceeds {MAX_ALIGN_TEXT_CHARS} chars"},
            status_code=422,
        )

    # Share the single job slot so align can't run alongside a transcription.
    if not _claim_job():
        return JSONResponse(
            {"error": "busy", "detail": "a job is already in progress"},
            status_code=409,
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="align_"))
    src_suffix = _safe_upload_suffix(file.filename)
    src_path = tmp_dir / f"input{src_suffix}"
    try:
        try:
            await _stream_upload_to(file, src_path, MAX_UPLOAD_BYTES)
        except _UploadTooLarge:
            return JSONResponse(
                {"error": "payload_too_large",
                 "detail": f"upload exceeds {MAX_UPLOAD_BYTES} bytes"},
                status_code=413,
            )

        set_job("aligning", 0, "Forced alignment...")
        try:
            chunks = await asyncio.to_thread(
                split_audio, src_path, tmp_dir / "chunks", CHUNK_SECONDS,
            )
            word_segments: list[dict] = []
            for chunk_path, start_offset, _prev_end in chunks:
                words = await asyncio.to_thread(
                    aligner.align_chunk, chunk_path, text, start_offset,
                )
                word_segments.extend(words)
            return {"words": word_segments, "count": len(word_segments)}
        except Exception:
            logger.exception("Standalone alignment failed")
            return {"error": "align_failed", "detail": "alignment failed"}
    finally:
        set_job("idle")
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/history")
async def history_list():
    return {"items": history_store.list(limit=200)}


@app.get("/api/history/{hid}")
async def history_get(hid: str):
    rec = history_store.get(hid)
    if not rec:
        return {"error": "not_found"}
    return rec


@app.delete("/api/history/{hid}")
async def history_delete(hid: str):
    ok = history_store.delete(hid)
    return {"deleted": ok}


@app.delete("/api/history")
async def history_clear():
    n = history_store.clear()
    return {"cleared": n}


@app.get("/api/audio-cache")
async def audio_cache_list():
    """List cached converted-audio MP3s with aggregate size."""
    items = []
    total = 0
    for p in sorted(AUDIO_CACHE_DIR.glob("*.mp3"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        items.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return {"count": len(items), "size_bytes": total, "items": items}


@app.delete("/api/audio-cache")
async def audio_cache_clear():
    """Delete all cached converted-audio MP3s."""
    cleared = 0
    bytes_freed = 0
    for p in AUDIO_CACHE_DIR.glob("*.mp3"):
        try:
            sz = p.stat().st_size
            p.unlink()
            cleared += 1
            bytes_freed += sz
        except Exception:
            pass
    logger.info("Cleared audio cache: %d files, %d bytes", cleared, bytes_freed)
    return {"cleared": cleared, "bytes_freed": bytes_freed}


@app.get("/api/audio-cache/{name}")
async def audio_cache_get(name: str):
    """Serve a cached converted-audio MP3 for playback."""
    # Defend against path traversal: only the basename, must exist in cache dir.
    safe_name = Path(name).name
    if safe_name != name:
        return JSONResponse({"error": "bad_request", "detail": "invalid name"}, status_code=400)
    target = AUDIO_CACHE_DIR / safe_name
    if not target.is_file() or target.suffix.lower() != ".mp3":
        return JSONResponse({"error": "not_found", "detail": "no such cached audio"}, status_code=404)
    return FileResponse(str(target), media_type="audio/mpeg", filename=safe_name)


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
