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
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .llamarunner import LlamaRunner
from .forced_aligner import ForcedAligner
from .audio_chunk import split_audio, probe_duration, FFmpegMissingError
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

CTX_SIZE = int(os.environ.get("CTX_SIZE", "32768"))
CHUNK_SECONDS = float(os.environ.get("CHUNK_SECONDS", "25"))
KV_QUANT = os.environ.get("KV_QUANT", "q8_0")

MODELS_DIR = PROJECT_ROOT / "models"
DATA_DIR = PROJECT_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

ASR_MODEL = str(MODELS_DIR / "asr" / "Qwen3-ASR-1.7B-Q8_0.gguf")
ASR_MMPROJ = str(MODELS_DIR / "asr" / "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf")

runner = LlamaRunner(
    bin_path=str(PROJECT_ROOT / "bin" / "llama-server.exe"),
    host="127.0.0.1",
    port=8080,
)

aligner = ForcedAligner(
    model_path=str(MODELS_DIR / "aligner" / "qwen3-forced-aligner-0.6b-q8_0.gguf"),
    mmproj_path=str(MODELS_DIR / "asr" / "mmproj-Qwen3-ForcedAligner-0.6B-Q8_0.gguf"),
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


def _start_asr():
    runner.start(
        model_path=ASR_MODEL,
        mmproj_path=ASR_MMPROJ,
        ctx_size=CTX_SIZE,
        kv_quant=KV_QUANT,
        n_gpu_layers=99,
        model_name="Qwen3-ASR-1.7B-Q8_0",
    )


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
        _set_boot(phase="asr_skipped", asr_loaded=True, asr_warmup=True)
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

    if aligner.available:
        _set_boot(phase="aligner_loading", aligner_loaded=True)
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
        "aligner_available": aligner.available,
        "aligner_status": aligner.lib_status,
    }


@app.get("/api/readiness")
async def readiness():
    with boot_lock:
        return dict(boot_state)


@app.get("/api/status")
async def status():
    with job_lock:
        return dict(job_state)


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
        "model": "Qwen3-ASR-1.7B-Q8_0",
        "aligner_model": None,
    }

    try:
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
        chunk_infos: list[tuple[Path, float, str]] = []
        asr_segments_raw: list[dict] = []
        total = len(chunks)

        # ── Phase 1: ASR ───────────────────────────────────────────────
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
                    set_job("error", 0, f"Chunk {i} timed out")
                    return {"error": "timeout", "detail": f"Chunk {i}/{total} timed out after 600s"}
                except Exception as e:
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
                    chunk_infos.append((chunk_path, start_offset, text))

        stats["asr_time"] = round(time.monotonic() - t_asr_start, 3)

        merged_text = " ".join(all_text_parts).strip()

        if not merged_text and not all_segments:
            set_job("error", 0, "No transcription text returned")
            return {"error": "empty_result", "detail": "All chunks returned empty"}

        stats["char_count"] = len(merged_text)
        stats["segment_count"] = len(all_segments)

        # ── Phase 2: Forced Alignment via CrispASR (CPU, no swap) ──────
        if align and aligner.available and chunk_infos:
            t_align_start = time.monotonic()
            set_job("aligning", 85, "Running forced alignment (CrispASR)...")
            try:
                word_segments: list[dict] = []
                for i, (chunk_path, start_offset, text) in enumerate(chunk_infos, 1):
                    set_job(
                        "aligning",
                        85 + int(10 * i / len(chunk_infos)),
                        f"Aligning chunk {i}/{len(chunk_infos)}...",
                    )
                    words = aligner.align_chunk(chunk_path, text, start_offset)
                    word_segments.extend(words)

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
        elif align and not aligner.available:
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


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
