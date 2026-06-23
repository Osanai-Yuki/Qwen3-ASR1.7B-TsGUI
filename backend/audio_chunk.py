import json
import shutil
import subprocess
from pathlib import Path


class FFmpegMissingError(RuntimeError):
    pass


def _resolve_ffmpeg(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FFmpegMissingError(
            f"{name} not found on PATH. Install ffmpeg and ensure it is on PATH."
        )
    return path


def probe_duration(input_path: Path) -> float:
    ffprobe = _resolve_ffmpeg("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout or "{}")
    return float(data.get("format", {}).get("duration", 0.0))


def split_audio(input_path: Path, out_dir: Path, chunk_seconds: float) -> list[tuple[Path, float]]:
    """Split audio into mono 16 kHz wav chunks. Returns [(chunk_path, start_seconds), ...]."""
    ffmpeg = _resolve_ffmpeg("ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(input_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration")

    chunks: list[tuple[Path, float]] = []
    start = 0.0
    idx = 0
    while start < duration:
        out_path = out_dir / f"chunk_{idx:04d}.wav"
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-t", f"{chunk_seconds:.3f}",
            "-i", str(input_path),
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        if out_path.stat().st_size > 44:
            chunks.append((out_path, start))
        start += chunk_seconds
        idx += 1
    return chunks
