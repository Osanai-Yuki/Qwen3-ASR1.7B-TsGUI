import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class FFmpegMissingError(RuntimeError):
    pass


# Extensions treated as video when ffprobe-based detection is unavailable.
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts",
    ".m4v", ".wmv", ".mpg", ".mpeg", ".3gp", ".vob", ".ogv",
}


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
    """Split audio into mono 16 kHz wav chunks. Returns [(chunk_path, start_seconds), ...].

    Chunks are extracted in parallel via a thread pool — each ffmpeg invocation
    is independent and I/O-bound, so concurrency speeds up the split stage
    without changing the output. Results are returned in chronological order.
    """
    ffmpeg = _resolve_ffmpeg("ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(input_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration")

    # Build the extraction plan first (deterministic order).
    plan: list[tuple[int, float]] = []
    start = 0.0
    idx = 0
    while start < duration:
        plan.append((idx, start))
        start += chunk_seconds
        idx += 1

    def extract(item: tuple[int, float]) -> tuple[Path, float] | None:
        i, s = item
        out_path = out_dir / f"chunk_{i:04d}.wav"
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel", "error",
            "-ss", f"{s:.3f}",
            "-t", f"{chunk_seconds:.3f}",
            "-i", str(input_path),
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        if out_path.stat().st_size > 44:
            return (out_path, s)
        return None

    workers = max(1, min(len(plan), 8))
    chunks: list[tuple[Path, float]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map preserves input order; filter empty results.
        for res in pool.map(extract, plan):
            if res is not None:
                chunks.append(res)

    chunks.sort(key=lambda c: c[1])
    return chunks


def probe_audio_bitrate(input_path: Path) -> int | None:
    """Best-effort audio-stream bitrate in bits/sec (None if unknown/VBR)."""
    ffprobe = _resolve_ffmpeg("ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=bit_rate",
                "-of", "json",
                str(input_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return None
    br = streams[0].get("bit_rate")
    try:
        return int(br) if br else None
    except (TypeError, ValueError):
        return None


def is_video_file(input_path: Path) -> bool:
    """Detect whether a file is a video container.

    Prefers ffprobe (presence of a video stream); falls back to extension.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_type",
                    "-of", "json",
                    str(input_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                streams = (json.loads(result.stdout or "{}").get("streams")) or []
                if streams and streams[0].get("codec_type") == "video":
                    # An album-art-only "video" stream in a pure audio file is
                    # mpeg4-video with a single frame; treat durations <2s as
                    # non-video to avoid misclassifying tagged MP3/FLAC.
                    dur = probe_duration(input_path)
                    if dur and dur < 2.0:
                        return False
                    return True
        except Exception:
            pass
    return input_path.suffix.lower() in _VIDEO_EXTS


def extract_audio_to_mp3(src: Path, dst: Path, target_bitrate: int) -> Path:
    """Extract the audio track of ``src`` into an MP3 file ``dst``.

    ``target_bitrate`` is in bits/sec (e.g. 256000). The video stream is
    dropped (``-vn``); output is stereo 44.1 kHz libmp3lame.
    """
    ffmpeg = _resolve_ffmpeg("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(src),
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-c:a", "libmp3lame",
        "-b:a", str(target_bitrate),
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return dst


def safe_cache_stem(filename: str) -> str:
    """Sanitize a filename into a cache-key stem (alnum/._-, max 80 chars)."""
    stem = Path(filename).stem or "input"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    stem = stem.strip("._") or "input"
    return stem[:80]
