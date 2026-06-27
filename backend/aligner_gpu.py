"""GPU-accelerated forced alignment via the official qwen-asr package.

Replaces CrispASR (CPU, per-chunk model reload) with the NAR
Qwen3-ForcedAligner-0.6B model loaded locally and kept in GPU memory
across chunks.  Requires ``torch``, ``transformers``, and ``qwen-asr``
in the environment.
"""
import logging
import os
from pathlib import Path

# Reduce CUDA fragmentation on small-VRAM cards (4 GB) — the PyTorch OOM
# message explicitly recommends this when "reserved but unallocated" memory
# is significant.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

logger = logging.getLogger("asr")

# Reuse the same per-chunk text cap as the CPU backend so a hallucinated
# long transcript can't blow up the attention matrix (a 400-char text on a
# 25 s chunk tried to allocate 16 GiB → OOM on a 4 GB card).
from .forced_aligner import ALIGN_MAX_CHARS

_DEFAULT_GPU_MODEL_DIR = (
    Path(__file__).resolve().parent.parent / "models" / "aligner" / "official"
)
# HuggingFace CLI may nest the repo one level deeper.
if (_DEFAULT_GPU_MODEL_DIR / "Qwen3-ForcedAligner-0.6B" / "config.json").exists():
    _DEFAULT_GPU_MODEL_DIR = _DEFAULT_GPU_MODEL_DIR / "Qwen3-ForcedAligner-0.6B"
GPU_ALIGNER_MODEL_PATH = os.environ.get(
    "GPU_ALIGNER_MODEL_PATH",
    str(_DEFAULT_GPU_MODEL_DIR),
)


def _gpu_aligner_available() -> bool:
    """Check whether the qwen-asr package *and* the local model are present."""
    try:
        import qwen_asr  # noqa: F401
    except ImportError:
        return False
    return Path(GPU_ALIGNER_MODEL_PATH).is_dir()


class GPUAligner:
    """Forced alignment via ``qwen_asr.Qwen3ForcedAligner`` on GPU.

    The model is loaded lazily (on first alignment call) and kept in GPU
    memory until ``close()`` is called.  This design assumes the ASR model
    has been unloaded from the GPU before ``load()`` to avoid OOM on cards
    with ≤4 GB VRAM.
    """

    def __init__(self, model_path: str | None = None):
        self._model_path = Path(model_path or GPU_ALIGNER_MODEL_PATH)
        self._model = None  # lazy
        self._torch = None
        self._Qwen3ForcedAligner = None

    # ── public API ────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return _gpu_aligner_available()

    def load(self) -> None:
        """Import dependencies and load the model onto GPU.

        Idempotent — repeated calls are no-ops.
        """
        if self._model is not None:
            return

        import torch
        from qwen_asr import Qwen3ForcedAligner

        self._torch = torch
        self._Qwen3ForcedAligner = Qwen3ForcedAligner

        logger.info("Loading GPU aligner from %s", self._model_path)
        self._model = Qwen3ForcedAligner.from_pretrained(
            str(self._model_path),
            dtype=torch.bfloat16,
            device_map="cuda:0",
        )
        logger.info("GPU aligner ready")

    def close(self) -> None:
        """Release GPU memory."""
        if self._model is not None:
            del self._model
            self._model = None
        if self._torch is not None:
            self._torch.cuda.empty_cache()
            self._torch = None
        self._Qwen3ForcedAligner = None
        logger.info("GPU aligner released")

    def align_chunk(
        self,
        wav_path: Path,
        text: str,
        chunk_start: float,
    ) -> list[dict]:
        """Align text to one WAV chunk on GPU.

        Returns word-level dicts with ``{word, start, end, text}``.
        An empty result is returned (and logged) on failure.
        """
        if not text.strip():
            return []

        # Truncate hallucinated/over-long text — same guard as the CPU
        # backend. Without it a long text forces a huge attention matrix
        # (16 GiB on a 25 s chunk) and OOMs a 4 GB card.
        if len(text) > ALIGN_MAX_CHARS:
            logger.warning(
                "GPU align: truncating text %d → %d chars for %s",
                len(text), ALIGN_MAX_CHARS, wav_path.name,
            )
            text = text[:ALIGN_MAX_CHARS]

        self.load()
        assert self._model is not None and self._torch is not None

        try:
            # qwen-asr returns List[List[AlignResult]] — one group per audio.
            groups = self._model.align(
                audio=str(wav_path),
                text=text,
                language="auto",
            )
        except Exception as e:
            logger.warning("GPU align failed for %s: %s", wav_path.name, e)
            # Free the failed pass's activations so the next chunk isn't
            # poisoned by accumulated allocations.
            self._torch.cuda.empty_cache()
            return []

        words: list[dict] = []
        for group in (groups or []):
            for item in group:
                t0 = float(getattr(item, "start_time", 0.0))
                t1 = float(getattr(item, "end_time", 0.0))
                w = str(getattr(item, "text", ""))
                if w:
                    words.append({
                        "word": w,
                        "start": chunk_start + t0,
                        "end": chunk_start + t1,
                        "text": w,
                    })
        return words

    def align_many(
        self,
        items: list[tuple[Path, str, float]],
        progress_cb=None,
    ) -> list[list[dict]]:
        """Align many chunks sequentially with the model kept on GPU.

        GPU inference is a single forward pass per chunk (~10-50 ms) so
        there is no need for a thread pool; the model stays loaded for the
        entire batch.
        """
        if not items:
            return []

        total = len(items)
        results: list[list[dict]] = []
        for i, (wav_path, text, chunk_start) in enumerate(items, 1):
            words = self.align_chunk(wav_path, text, chunk_start)
            # Release per-chunk activations to keep VRAM bounded across a long
            # batch — without this, allocations accumulate and OOM the card
            # even though each chunk individually fits.
            if self._torch is not None:
                self._torch.cuda.empty_cache()
            results.append(words)
            if progress_cb is not None:
                progress_cb(i, total)
        return results

    # ── model info ────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        return "Qwen3-ForcedAligner-0.6B (GPU)"
