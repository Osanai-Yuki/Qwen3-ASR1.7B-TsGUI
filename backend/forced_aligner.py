import ctypes
import logging
import os
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger("asr")

ALIGNER_MODEL_NAME = "Qwen3-ForcedAligner-0.6B-Q8_0"
_lib_path: str | None = None
_lib_handle: ctypes.CDLL | None = None
_lib_signatures_set: bool = False


def _resolve_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_lib() -> str | None:
    global _lib_path
    if _lib_path is not None:
        return _lib_path
    project_root = _resolve_app_root()
    candidates = [
        project_root / "bin" / "crispasr" / "crispasr.dll",
        project_root / "bin" / "crispasr" / "whisper.dll",
        project_root / "bin" / "crispasr.dll",
        project_root / "bin" / "whisper.dll",
    ]
    for p in candidates:
        if p.exists():
            _lib_path = str(p)
            return _lib_path
    return None


def _load_lib() -> ctypes.CDLL | None:
    global _lib_handle, _lib_signatures_set
    if _lib_handle is not None:
        return _lib_handle
    lib_path = _resolve_lib()
    if not lib_path:
        return None

    lib_dir = Path(lib_path).parent

    # On Windows, register the DLL directory so dependent libraries
    # (ggml.dll, ggml-base.dll, ggml-cpu.dll) are found alongside crispasr.dll.
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(lib_dir))
        except OSError as e:
            logger.warning("add_dll_directory(%s) failed: %s", lib_dir, e)

    # Preload dependencies in order to avoid Windows loader picking up
    # incompatible versions from neighbouring directories.
    if sys.platform == "win32":
        for dep_name in ("ggml-base.dll", "ggml-cpu.dll", "ggml.dll"):
            dep_path = lib_dir / dep_name
            if dep_path.exists():
                try:
                    ctypes.CDLL(str(dep_path))
                except OSError as e:
                    logger.warning("Failed to preload %s: %s", dep_name, e)

    try:
        lib = ctypes.CDLL(lib_path)
    except OSError as e:
        logger.error("Failed to load %s: %s", lib_path, e)
        return None

    if not hasattr(lib, "crispasr_align_words_abi"):
        logger.error(
            "crispasr_align_words_abi not in %s — rebuild CrispASR 0.4.7+",
            lib_path,
        )
        return None

    lib.crispasr_align_words_abi.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_float), ctypes.c_int32,
        ctypes.c_int64, ctypes.c_int32,
    ]
    lib.crispasr_align_words_abi.restype = ctypes.c_void_p
    lib.crispasr_align_result_n_words.argtypes = [ctypes.c_void_p]
    lib.crispasr_align_result_n_words.restype = ctypes.c_int
    lib.crispasr_align_result_word_text.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.crispasr_align_result_word_text.restype = ctypes.c_char_p
    lib.crispasr_align_result_word_t0.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.crispasr_align_result_word_t0.restype = ctypes.c_int64
    lib.crispasr_align_result_word_t1.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.crispasr_align_result_word_t1.restype = ctypes.c_int64
    lib.crispasr_align_result_free.argtypes = [ctypes.c_void_p]
    lib.crispasr_align_result_free.restype = None

    _lib_handle = lib
    _lib_signatures_set = True
    return lib


@dataclass
class AlignedWord:
    text: str
    start: float
    end: float


def _align_words(
    aligner_model: str,
    transcript: str,
    pcm: np.ndarray,
    *,
    t_offset: float = 0.0,
    n_threads: int = 4,
) -> list[AlignedWord]:
    """Call CrispASR's align_words_abi via ctypes."""
    if not aligner_model or not transcript or pcm is None or len(pcm) == 0:
        return []

    lib = _load_lib()
    if lib is None:
        return []

    pcm_np = np.ascontiguousarray(pcm, dtype=np.float32)
    res = lib.crispasr_align_words_abi(
        aligner_model.encode("utf-8"),
        transcript.encode("utf-8"),
        pcm_np.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        int(len(pcm_np)),
        int(round(t_offset * 100)),
        int(n_threads),
    )
    if not res:
        return []
    try:
        n = lib.crispasr_align_result_n_words(res)
        out: list[AlignedWord] = []
        for i in range(n):
            t = lib.crispasr_align_result_word_text(res, i)
            text = t.decode("utf-8") if t else ""
            out.append(AlignedWord(
                text=text,
                start=lib.crispasr_align_result_word_t0(res, i) / 100.0,
                end=lib.crispasr_align_result_word_t1(res, i) / 100.0,
            ))
        return out
    finally:
        lib.crispasr_align_result_free(res)


class ForcedAligner:
    """Forced alignment via CrispASR C++ runtime (crispasr.dll).

    Calls ``crispasr_align_words_abi`` directly via ctypes — no pip
    dependency on the ``crispasr`` Python package. Runs entirely on CPU.
    """

    def __init__(self, model_path: str, mmproj_path: str | None = None):
        self.model_path = Path(model_path)
        self.mmproj_path = Path(mmproj_path) if mmproj_path else None

    @property
    def available(self) -> bool:
        return self.model_path.exists() and _resolve_lib() is not None

    @property
    def lib_status(self) -> str:
        if not self.model_path.exists():
            return "model_missing"
        if not _resolve_lib():
            return "crispasr_dll_missing"
        if _load_lib() is None:
            return "crispasr_dll_invalid"
        return "ready"

    def _wav_to_pcm(self, wav_path: Path) -> np.ndarray:
        """Read a WAV file and return mono float32 PCM samples normalized to [-1, 1]."""
        with wave.open(str(wav_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)

        if sampwidth == 2:
            dtype = np.int16
            scale = 32768.0
        elif sampwidth == 4:
            dtype = np.int32
            scale = 2147483648.0
        else:
            raise ValueError(f"Unsupported sample width: {sampwidth}")

        pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32) / scale
        if n_channels > 1:
            pcm = pcm.reshape(-1, n_channels).mean(axis=1)
        if framerate != 16000:
            ratio = 16000 / framerate
            pcm = np.interp(
                np.arange(0, len(pcm), ratio),
                np.arange(len(pcm)),
                pcm,
            ).astype(np.float32)
        return pcm

    def align_chunk(
        self,
        wav_path: Path,
        text: str,
        chunk_start: float,
    ) -> list[dict]:
        """Align text to audio for one WAV chunk.

        Returns word-level segments with timestamps offset by chunk_start.
        """
        if not text.strip():
            return []

        if _load_lib() is None:
            logger.warning("CrispASR DLL unavailable — skipping alignment")
            return []

        try:
            pcm = self._wav_to_pcm(wav_path)
        except Exception as e:
            logger.error("Failed to read WAV %s: %s", wav_path, e)
            return []

        try:
            words = _align_words(
                str(self.model_path),
                text,
                pcm,
                t_offset=chunk_start,
                n_threads=4,
            )
        except Exception as e:
            logger.error("CrispASR align_words failed: %s", e)
            return []

        return [
            {"word": w.text, "start": w.start, "end": w.end, "text": w.text}
            for w in words
            if w.text
        ]
