import json
import math
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class FFmpegMissingError(RuntimeError):
    pass


# ffmpeg silencedetect output: [silence_start at 1.234] / [silence_end at 5.678]
_SILENCE_RE = re.compile(r"\[silence_(start|end)\s+at\s+([\d.]+)\]")


def _detect_silence(
    input_path: Path,
    noise_db: float = -30,
    min_dur: float = 0.5,
) -> list[tuple[float, float]]:
    """Return [(silence_start, silence_end), ...] via ffmpeg silencedetect.

    Silences that run to EOF are capped at ``float("inf")`` so the caller can
    bound them against the known duration. Runs off the event loop (subprocess
    + ffmpeg); raises on failure so the caller can fall back to fixed chunking.
    """
    ffmpeg = resolve_ffmpeg("ffmpeg")
    cmd = [
        ffmpeg, "-i", str(input_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    intervals: list[tuple[float, float]] = []
    current_start: float | None = None
    for line in result.stderr.splitlines():
        m = _SILENCE_RE.search(line)
        if not m:
            continue
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            current_start = val
        elif kind == "end" and current_start is not None:
            intervals.append((current_start, val))
            current_start = None
    if current_start is not None:
        # Silence runs to EOF; cap sentinel at inf so the caller bounds it.
        intervals.append((current_start, float("inf")))
    return intervals


def _vad_plan(
    input_path: Path,
    duration: float,
    chunk_seconds: float,
    *,
    max_chunk_seconds: float | None = None,
    long_silence: float = 1.0,
    noise_db: float = -30,
    min_silence_dur: float = 0.5,
) -> list[tuple[float, float]]:
    """Compute silence-based chunk boundaries: [(start, end), ...].

    Groups speech segments (the complement of silence within [0, duration])
    into chunks of roughly ``chunk_seconds``, splitting at silence boundaries.
    A silence longer than ``long_silence`` is a natural break; a chunk whose
    span would exceed ``max_chunk_seconds`` (default 1.5x chunk_seconds) is
    closed early. Speech segments with no silence to split them (longer than
    max_chunk_seconds) are split at fixed intervals as a fallback.

    Pure planning: no WAV extraction, so the caller can get the chunk count
    up front for progress reporting.
    """
    if chunk_seconds < 1.0:
        raise ValueError(f"invalid chunk_seconds={chunk_seconds}")
    if max_chunk_seconds is None:
        max_chunk_seconds = chunk_seconds * 1.5

    silence = _detect_silence(input_path, noise_db, min_silence_dur)
    # Bound silence intervals to the known duration.
    silence = [(s, min(e, duration)) for s, e in silence if s < duration]

    # Speech segments = complement of silence within [0, duration].
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silence:
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        speech.append((cursor, duration))
    if not speech:
        return []

    # Group speech segments into chunks.
    groups: list[tuple[float, float]] = []
    g_start = speech[0][0]
    g_end = speech[0][1]
    for seg_start, seg_end in speech[1:]:
        gap = seg_start - g_end
        span = seg_end - g_start
        if span > max_chunk_seconds:
            # Would exceed the size cap: close here, start a new chunk.
            groups.append((g_start, g_end))
            g_start = seg_start
            g_end = seg_end
        elif gap > long_silence:
            # Long silence: a natural break between chunks.
            groups.append((g_start, g_end))
            g_start = seg_start
            g_end = seg_end
        else:
            # Absorb the segment (and the short silence before it).
            g_end = seg_end
    groups.append((g_start, g_end))

    # Fallback: split any group still too long (long speech, no silence).
    plan: list[tuple[float, float]] = []
    for c_start, c_end in groups:
        c_dur = c_end - c_start
        if c_dur <= max_chunk_seconds:
            plan.append((c_start, c_end))
            continue
        n = max(1, math.ceil(c_dur / chunk_seconds))
        sub = c_dur / n
        for i in range(n):
            s = c_start + i * sub
            e = s + sub if i < n - 1 else c_end
            plan.append((s, e))
    return plan


def iter_vad_chunks(
    input_path: Path,
    out_dir: Path,
    chunk_seconds: float,
    overlap: float = 0.0,
    *,
    duration: float | None = None,
    plan: list[tuple[float, float]] | None = None,
    max_chunk_seconds: float | None = None,
    long_silence: float = 1.0,
    noise_db: float = -30,
    min_silence_dur: float = 0.5,
) -> Iterator[tuple[Path, float, float]]:
    """Yield ``(chunk_path, start_offset, prev_end)`` for silence-based chunks.

    Like ``iter_chunks`` but splits at silence boundaries instead of on a fixed
    grid, so speech is never cut mid-sentence. Chunks are non-overlapping and
    separated by silence, so ``prev_end`` is the previous chunk's end (0.0 for
    the first) — the same ``(chunk_path, start_offset, prev_end)`` contract as
    ``iter_chunks``. Pass a pre-computed ``plan`` (from ``_vad_plan``) so the
    caller can know the chunk count before streaming extraction.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if duration is None:
        duration = probe_duration(input_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration")
    if plan is None:
        plan = _vad_plan(
            input_path, duration, chunk_seconds,
            max_chunk_seconds=max_chunk_seconds,
            long_silence=long_silence,
            noise_db=noise_db,
            min_silence_dur=min_silence_dur,
        )

    MAX_PLAN = 6000
    prev_end = 0.0
    idx = 0
    for c_start, c_end in plan:
        if idx > MAX_PLAN:
            raise ValueError(
                f"audio too long to split: would exceed {MAX_PLAN} VAD chunks "
                f"(duration={duration:.0f}s)"
            )
        c_dur = c_end - c_start
        if c_dur <= 0:
            continue
        out_path = out_dir / f"vadchunk_{idx:04d}.wav"
        extract_slice(input_path, out_path, c_start, c_dur)
        if out_path.stat().st_size > 44:
            yield (out_path, c_start, prev_end)
        prev_end = c_end
        idx += 1


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
