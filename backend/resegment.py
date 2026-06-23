"""Re-segment aligned word-level output into readable subtitle units.

Merges individual word timestamps into phrase-level segments by respecting
sentence boundaries, pause gaps, and duration/length constraints. When the
original ASR segment boundaries are available, they guide the merge so that
segment breaks prefer natural sentence boundaries.
"""
import re
from dataclasses import dataclass, field


@dataclass
class _Seg:
    text: str
    start: float
    end: float


_BREAK_RE = re.compile(r"[。！？\.\!\?…]{1,3}$")
_COMMA_RE = re.compile(r"[，、,;；：:]$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def _has_cjk(text: str) -> bool:
    return any(_is_cjk(ch) for ch in text)


def _build_boundary_set(original_segments: list[dict] | None) -> set[int]:
    if not original_segments:
        return set()
    out: set[int] = set()
    for seg in original_segments:
        t = seg.get("end")
        if t is not None:
            idx = round(t * 100)
            for offset in range(-8, 9):
                out.add(idx + offset)
    return out


def resegment_words(
    word_segments: list[dict],
    *,
    max_duration: float = 7.0,
    max_chars: int = 42,
    min_chars: int = 2,
    max_gap: float = 0.8,
    min_duration: float = 1.0,
    original_segments: list[dict] | None = None,
) -> list[dict]:
    if not word_segments:
        return []

    is_cjk = _has_cjk(word_segments[0].get("word", "") or word_segments[0].get("text", ""))
    boundaries = _build_boundary_set(original_segments)

    result: list[_Seg] = []
    cur_text = ""
    cur_start: float | None = None
    cur_end: float = 0.0

    def flush():
        nonlocal cur_text, cur_start, cur_end
        if cur_text.strip() and cur_start is not None:
            result.append(_Seg(text=cur_text.strip(), start=cur_start, end=cur_end))
        cur_text = ""
        cur_start = None

    def at_boundary(t: float) -> bool:
        if not boundaries:
            return False
        return round(t * 100) in boundaries

    for ws in word_segments:
        word = ws.get("word") or ws.get("text", "")
        t0 = ws.get("start", 0.0)
        t1 = ws.get("end", 0.0)

        if not word:
            continue

        if cur_start is None:
            cur_start = t0
            cur_end = t1
            cur_text = word
            continue

        gap = t0 - cur_end
        candidate = cur_text + ("" if is_cjk else " ") + word
        candidate_dur = t1 - cur_start
        candidate_len = len(candidate)

        cur_dur = cur_end - cur_start
        past_min = cur_dur >= min_duration

        should_break = False
        prefer_break = False

        if gap > max_gap:
            should_break = True
        elif candidate_dur > max_duration:
            should_break = True
        elif candidate_len > max_chars and past_min:
            should_break = True

        if not should_break and _BREAK_RE.search(word):
            cur_text = candidate
            cur_end = t1
            flush()
            continue

        if not should_break and at_boundary(t1) and past_min:
            prefer_break = True

        if should_break:
            flush()
            cur_start = t0
            cur_end = t1
            cur_text = word
            continue

        if prefer_break:
            cur_text = candidate
            cur_end = t1
            flush()
            continue

        if _COMMA_RE.search(word) and candidate_len >= min_chars and candidate_dur >= 1.2:
            cur_text = candidate
            cur_end = t1
            flush()
            continue

        cur_text = candidate
        cur_end = t1

    flush()

    merged: list[_Seg] = []
    for seg in result:
        if merged and (len(seg.text) < min_chars or (seg.end - seg.start) < 0.3):
            prev = merged[-1]
            prev.text = prev.text + ("" if is_cjk else " ") + seg.text
            prev.end = seg.end
        else:
            merged.append(seg)

    return [{"start": s.start, "end": s.end, "text": s.text} for s in merged]
