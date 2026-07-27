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


def resolve_ffmpeg(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FFmpegMissingError(
            f"{name} not found on PATH. Install ffmpeg and ensure it is on PATH."
        )
    return path


def probe_duration(input_path: Path) -> float:
    ffprobe = resolve_ffmpeg("ffprobe")
    try:
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
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        # A crafted/hung media file must not hang the server indefinitely.
        return 0.0
    data = json.loads(result.stdout or "{}")
    return float(data.get("format", {}).get("duration", 0.0))


def split_audio(
    input_path: Path,
    out_dir: Path,
    chunk_seconds: float,
    overlap: float = 0.0,
) -> list[tuple[Path, float, float]]:
    """Split audio into mono 16 kHz wav chunks.

    Returns ``[(chunk_path, start_offset, prev_end), ...]`` where:
    - ``start_offset`` is the chunk's absolute start in the source audio,
    - ``prev_end`` is the absolute time from which this chunk is *responsible*
      for output (the previous chunk's ``start_offset``; 0.0 for the first).

    With ``overlap > 0`` adjacent chunks overlap by that many seconds (each
    chunk is still ``chunk_seconds`` long, but the step is
    ``chunk_seconds - overlap``).  The overlap lets the ASR see context across
    the boundary; callers drop segments whose timestamps fall in the overlap
    (before ``prev_end``) to avoid duplicating content.  ``overlap=0``
    reproduces the original contiguous split.

    Extraction runs in a thread pool — each ffmpeg invocation is independent
    and I/O-bound.  Results are returned in chronological order.
    """
    ffmpeg = resolve_ffmpeg("ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(input_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration")

    # Clamp overlap so the step stays positive and meaningful.
    overlap = max(0.0, min(overlap, chunk_seconds * 0.4))
    step = chunk_seconds - overlap
    # Reject a degenerate chunk_seconds that would explode the plan (e.g. an
    # env-set CHUNK_SECONDS=0.01 → hundreds of thousands of chunks). 1.0s is
    # well below any useful chunk size and keeps the plan bounded.
    if chunk_seconds < 1.0 or step <= 0.0:
        raise ValueError(
            f"invalid chunk_seconds={chunk_seconds} overlap={overlap}: "
            "step must stay positive (chunk_seconds >= 1.0)"
        )

    # Build the extraction plan: (idx, start_offset, prev_end).
    plan: list[tuple[int, float, float]] = []
    start = 0.0
    prev_end = 0.0
    idx = 0
    MAX_PLAN = 6000  # hard cap: a 25s step needs ~8h of audio to reach this
    while start < duration:
        plan.append((idx, start, prev_end))
        prev_end = start  # next chunk is responsible from where this one began
        start += step
        idx += 1
        if len(plan) > MAX_PLAN:
            raise ValueError(
                f"audio too long to split: would exceed {MAX_PLAN} chunks "
                f"(duration={duration:.0f}s, step={step:.3f}s)"
            )

    def extract(item: tuple[int, float, float]) -> tuple[Path, float, float] | None:
        i, s, pe = item
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
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        if out_path.stat().st_size > 44:
            return (out_path, s, pe)
        return None

    workers = max(1, min(len(plan), 8))
    chunks: list[tuple[Path, float, float]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map preserves input order; filter empty results.
        for res in pool.map(extract, plan):
            if res is not None:
                chunks.append(res)

    chunks.sort(key=lambda c: c[1])
    return chunks


def probe_audio_bitrate(input_path: Path) -> int | None:
    """Best-effort audio-stream bitrate in bits/sec (None if unknown/VBR)."""
    ffprobe = resolve_ffmpeg("ffprobe")
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
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
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
    ffmpeg = resolve_ffmpeg("ffmpeg")
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
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    return dst


def safe_cache_stem(filename: str) -> str:
    """Sanitize a filename into a cache-key stem (alnum/._-, max 80 chars)."""
    stem = Path(filename).stem or "input"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    stem = stem.strip("._") or "input"
    return stem[:80]
