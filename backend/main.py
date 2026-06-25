import asyncio
import io
import os
import shutil
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

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .llamarunner import LlamaRunner
from .forced_aligner import ForcedAligner, ALIGN_MAX_WORKERS
from .aligner_gpu import GPUAligner, GPU_ALIGNER_MODEL_PATH, _gpu_aligner_available
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


def _detect_vram_gb() -> float | None:
    """Best-effort GPU VRAM detection via nvidia-smi (None if unavailable)."""
    try:
        import subprocess as _sp
        r = _sp.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
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
        _gpu_backend_ready = ALIGNER_BACKEND == "gpu" and _gpu_aligner_available()
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
                if d.is_dir() and d.stat().st_mtime < cutoff:
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
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="aligner_warmup_"))
        wav_path = tmp_dir / "warmup.wav"
        wav_path.write_bytes(_make_silent_wav(0.3))
        words = aligner.align_chunk(wav_path, "test", 0.0)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Aligner warmup OK (%d words from silent)", len(words))
        return True
    except Exception as e:
        logger.warning("Aligner warmup failed (non-fatal): %s", e)
        return False


def _boot_sequence():
    skip_llama = os.environ.get("SKIP_LLAMA", "0") == "1"

    try:
        _cleanup_stale_tmp()
    except Exception as e:
        logger.warning("Boot cleanup failed: %s", e)

    if skip_llama:
        _set_boot(phase="asr_skipped", asr_loaded=True, asr_warmup=True,
                  current_model="skipped")
    else:
        _set_boot(phase="asr_loading")
        try:
            _start_asr()
            _set_boot(asr_loaded=True, phase="asr_warmup")
        except Exception as e:
            _set_boot(phase="error", error=f"ASR load failed: {e}")
            return

        try:
            _warmup_asr()
            _set_boot(asr_warmup=True, phase="aligner_check")
        except Exception:
            _set_boot(asr_warmup=True, phase="aligner_check")

    if ALIGNER_BACKEND == "gpu":
        if _is_gpu_backend_ready():
            _set_boot(aligner_loaded=True, aligner_warmup=True, phase="ready", ready=True)
            logger.info("Aligner backend: GPU (qwen-asr, model at %s)", GPU_ALIGNER_MODEL_PATH)
        else:
            logger.warning(
                "ALIGNER_BACKEND=gpu but qwen-asr/model not available; "
                "alignment will be skipped.  Install: pip install qwen-asr torch transformers "
                "&& huggingface-cli download Qwen/Qwen3-ForcedAligner-0.6B --local-dir %s",
                GPU_ALIGNER_MODEL_PATH,
            )
            _set_boot(aligner_loaded=False, aligner_warmup=False, phase="ready", ready=True)
    elif aligner.available:
        _set_boot(phase="aligner_loading", aligner_loaded=True)
        from .forced_aligner import ALIGN_MAX_WORKERS, ALIGN_N_THREADS
        logger.info(
            "Aligner backend: CPU (CrispASR, workers=%d threads=%d)",
            ALIGN_MAX_WORKERS, ALIGN_N_THREADS,
        )
        try:
            ok = _warmup_aligner()
            _set_boot(aligner_warmup=ok, phase="ready", ready=True)
        except Exception:
            _set_boot(aligner_warmup=False, phase="ready", ready=True)
    else:
        _set_boot(aligner_loaded=False, aligner_warmup=False, phase="ready", ready=True)

    logger.info("Boot complete — system ready")


@asynccontextmanager
async def lifespan(app: FastAPI):
    boot_thread = threading.Thread(target=_boot_sequence, daemon=True)
    boot_thread.start()
    yield
    runner.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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


@app.get("/api/models")
async def models_list():
    """List available ASR models and the currently loaded one."""
    return {
        "models": _scan_asr_models(),
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

    # Refuse if a transcription is in flight.
    with job_lock:
        busy = job_state["status"] in ("preparing", "transcribing", "aligning")
    if busy:
        return {"error": "busy", "detail": "a transcription is in progress"}

    _set_boot(switching=True, phase="asr_switching", ready=False, error=None)
    try:
        await asyncio.to_thread(
            _start_asr,
            model_path=match["path"],
            mmproj_path=match["mmproj"],
            ctx_size=int(req.get("ctx_size", CTX_SIZE)),
            kv_quant=req.get("kv_quant", KV_QUANT),
            n_gpu_layers=int(req.get("n_gpu_layers", DEFAULT_NGL)),
            threads=req.get("threads", DEFAULT_THREADS),
            batch_size=req.get("batch_size"),
            ubatch_size=req.get("ubatch_size"),
            flash_attn=bool(req.get("flash_attn", True)),
            mmproj_offload=bool(req.get("mmproj_offload", False)),
            model_name=match["name"],
        )
        _set_boot(switching=False, phase="ready", ready=True, asr_loaded=True)
        return {"ok": True, "current_model": runner.current_model}
    except Exception as e:
        _set_boot(switching=False, phase="error", error=f"Model switch failed: {e}")
        return {"error": "switch_failed", "detail": str(e)}


async def _transcribe_chunk(client: httpx.AsyncClient, chunk_path: Path) -> dict:
    with chunk_path.open("rb") as fh:
        audio_bytes = fh.read()
    resp = await client.post(
        f"{runner.base_url}/v1/audio/transcriptions",
        files={"file": (chunk_path.name, audio_bytes, "audio/wav")},
        data={"response_format": "json"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"llama-server {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    if "error" in data:
        msg = data["error"].get("message", str(data["error"])) if isinstance(data["error"], dict) else str(data["error"])
        raise RuntimeError(msg)
    return data


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
    audio_bytes = await file.read()
    logger.info(f"Received file: {file.filename}, size={len(audio_bytes)} bytes, align={align}")

    set_job("preparing", 5, "Preparing audio...")

    tmp_dir = Path(tempfile.mkdtemp(prefix="asr_"))
    src_suffix = Path(file.filename or "input").suffix or ".bin"
    src_path = tmp_dir / f"input{src_suffix}"
    src_path.write_bytes(audio_bytes)

    stats = {
        "filename": file.filename,
        "file_size": len(audio_bytes),
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
            if is_video_file(src_path):
                stats["source_was_video"] = True
                cache_name = f"{safe_cache_stem(file.filename or 'input')}__{len(audio_bytes)}.mp3"
                mp3_path = AUDIO_CACHE_DIR / cache_name
                if mp3_path.exists() and mp3_path.stat().st_size > 0:
                    logger.info("Reusing cached mp3 for %s: %s", file.filename, cache_name)
                else:
                    set_job("preparing", 7, "Extracting audio (video → MP3)...")
                    orig_br = probe_audio_bitrate(src_path)
                    target = orig_br if (orig_br and orig_br <= MP3_DEFAULT_BITRATE) else MP3_DEFAULT_BITRATE
                    logger.info(
                        "Extracting mp3 from %s: orig_br=%s target=%d",
                        file.filename, orig_br, target,
                    )
                    extract_audio_to_mp3(src_path, mp3_path, target)
                    stats["mp3_bitrate"] = target
                src_path = mp3_path
                stats["audio_cache_name"] = mp3_path.name
        except FFmpegMissingError as e:
            set_job("error", 0, str(e))
            return {"error": "ffmpeg_missing", "detail": str(e)}
        except Exception as e:
            set_job("error", 0, f"Audio extraction failed: {e}")
            return {"error": "extract_failed", "detail": str(e)}

        try:
            duration = probe_duration(src_path)
        except FFmpegMissingError as e:
            set_job("error", 0, str(e))
            return {"error": "ffmpeg_missing", "detail": str(e)}
        except Exception as e:
            set_job("error", 0, f"Probe failed: {e}")
            return {"error": "probe_failed", "detail": str(e)}

        stats["audio_duration"] = round(duration, 3)

        try:
            chunks = split_audio(src_path, tmp_dir / "chunks", CHUNK_SECONDS)
        except Exception as e:
            set_job("error", 0, f"Split failed: {e}")
            return {"error": "split_failed", "detail": str(e)}

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
        t_asr_start = time.monotonic()
        async with httpx.AsyncClient(timeout=600.0) as client:
            for i, (chunk_path, start_offset) in enumerate(chunks, 1):
                progress = 10 + int(70 * (i - 1) / total)
                set_job(
                    "transcribing",
                    progress,
                    f"Transcribing chunk {i}/{total} ({start_offset:.1f}s)...",
                )
                try:
                    data = await _transcribe_chunk(client, chunk_path)
                except httpx.ReadTimeout:
                    if align_pool:
                        align_pool.shutdown(wait=False, cancel_futures=True)
                    set_job("error", 0, f"Chunk {i} timed out")
                    return {"error": "timeout", "detail": f"Chunk {i}/{total} timed out after 600s"}
                except Exception as e:
                    if align_pool:
                        align_pool.shutdown(wait=False, cancel_futures=True)
                    logger.error("Chunk %d failed: %s", i, e)
                    set_job("error", 0, f"Chunk {i} failed: {e}")
                    return {"error": "chunk_failed", "detail": str(e)}

                text = (data.get("text") or "").strip()
                text = clean_asr_text(text)
                segments = data.get("segments") or []
                segments = clean_segments(segments)
                if segments:
                    asr_segments_raw.extend(segments)
                    all_segments.extend(_shift_segments(segments, start_offset))
                if text:
                    all_text_parts.append(text)
                    if do_align_cpu and align_pool is not None:
                        align_futures.append(
                            align_pool.submit(aligner.align_chunk, chunk_path, text, start_offset)
                        )
                    elif gpu_swap:
                        gpu_align_jobs.append((chunk_path, text, start_offset))

        stats["asr_time"] = round(time.monotonic() - t_asr_start, 3)

        merged_text = " ".join(all_text_parts).strip()

        if not merged_text and not all_segments:
            if align_pool:
                align_pool.shutdown(wait=False, cancel_futures=True)
            set_job("error", 0, "No transcription text returned")
            return {"error": "empty_result", "detail": "All chunks returned empty"}

        stats["char_count"] = len(merged_text)
        stats["segment_count"] = len(all_segments)

        # ── Phase 2: Alignment ─────────────────────────────────────────
        if gpu_swap and gpu_align_jobs:
            # ── GPU path: unload ASR, load aligner on GPU, realign, reload ASR
            assert t_align_start is not None
            _gpu_swap_align(
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
                filename=file.filename or "unknown",
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


@app.post("/api/align")
async def align_standalone(
    file: UploadFile = File(...),
    text: str = Form(...),
):
    """Standalone forced alignment endpoint using CrispASR."""
    if not aligner.available:
        return {"error": "aligner_unavailable", "detail": f"ForcedAligner not available: {aligner.lib_status}"}

    audio_bytes = await file.read()
    tmp_dir = Path(tempfile.mkdtemp(prefix="align_"))
    src_suffix = Path(file.filename or "input").suffix or ".bin"
    src_path = tmp_dir / f"input{src_suffix}"
    src_path.write_bytes(audio_bytes)

    try:
        chunks = split_audio(src_path, tmp_dir / "chunks", CHUNK_SECONDS)
        word_segments: list[dict] = []
        for chunk_path, start_offset in chunks:
            words = aligner.align_chunk(chunk_path, text, start_offset)
            word_segments.extend(words)
        return {"words": word_segments, "count": len(word_segments)}
    finally:
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
