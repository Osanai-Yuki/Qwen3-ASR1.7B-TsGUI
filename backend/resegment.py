"""Re-segment aligned word-level output into readable subtitle units.

Merges individual word timestamps into phrase-level segments by respecting
sentence boundaries, pause gaps, and duration/length constraints. When the
original ASR segment boundaries are available, they guide the merge so that
segment breaks prefer natural sentence boundaries.

Chinese forced alignment emits *per-character* timestamps and the ASR text
often carries no punctuation, so a naive "cut when the limit is hit" break
lands in the middle of words (「研/报」「科/技」). Hard breaks therefore
backtrack over a small window and pick the best-scoring split point (pause
gap, punctuation, filler particles, original ASR boundaries) while refusing
to cut latin/number runs (GDP, NoBazuka, 6.75, 百分之四点六).
"""
import re
from dataclasses import dataclass


@dataclass
class _W:
    word: str
    t0: float
    t1: float


@dataclass
class _Seg:
    text: str
    start: float
    end: float


_BREAK_RE = re.compile(r"[。！？\.\!\?…]{1,3}$")
_COMMA_RE = re.compile(r"[，、,;；：:]$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")
_LATIN_NUM_RE = re.compile(r"[A-Za-z0-9%]")

# Sentence-final particles: a break right after one of these reads naturally
# in unpunctuated Mandarin speech (…呢 / …啊 / …吧).
_FILLER_CHARS = set("呢啊吧嘛呀哦哈啦嘞咯喽哇")
# Particles that must not START a subtitle line (有/的 must not split) and
# dangling function words that read badly at the END of a line.
_BAD_LEAD_CHARS = set("的地得了着呢吧吗嘛呀哦啊嘞哈")
_BAD_TAIL_CHARS = set("的地得把被将和跟与或比让在于对从向当就还也都很更最")
# CJK numeric run: never split inside 四点六 / 六点七五 / 五十万 …
_NUM_CJK = set("0123456789零一二三四五六七八九十百千万亿点两几")
# Multi-char units that must never be cut internally or torn from the number
# that follows them (百分之/四点六).
_GLUE_UNITS = ("百分之", "分之")

# How many trailing words a hard break may backtrack over to find a split.
_BACKTRACK_WORDS = 12
# Minimum score for a soft (early) break once past the soft thresholds.
_SOFT_BREAK_SCORE = 2.0


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


def _forbidden_split(left: str, right: str) -> bool:
    """True when breaking between ``left`` and ``right`` would cut a token.

    Guards latin/number runs (GDP, NoBazuka, 6.75), CJK numeral runs
    (四点六) and glue units (百分之 + number).
    """
    if not left or not right:
        return False
    l, r = left[-1], right[0]
    if _LATIN_NUM_RE.match(l) and _LATIN_NUM_RE.match(r):
        return True
    if l in _NUM_CJK and r in _NUM_CJK:
        return True
    # 百分之 | 四点六 — the unit binds to the number after it.
    for unit in _GLUE_UNITS:
        if left.endswith(unit) and (r in _NUM_CJK):
            return True
        # Also refuse to cut inside the unit itself (百 | 分之).
        ctx_left = left[-(len(unit) - 1):] if len(unit) > 1 else ""
        combined = ctx_left + right[: len(unit) - 1]
        if unit in combined:
            return True
    return False


def resegment_words(
    word_segments: list[dict],
    *,
    max_duration: float | None = None,
    max_chars: int | None = None,
    min_chars: int = 2,
    max_gap: float = 0.8,
    min_duration: float = 1.0,
    original_segments: list[dict] | None = None,
) -> list[dict]:
    if not word_segments:
        return []

    is_cjk = _has_cjk(word_segments[0].get("word", "") or word_segments[0].get("text", ""))
    # CJK subtitles read best short (industry norm ≤ ~20-25 chars/line);
    # latin text needs more room since words are longer.
    if max_duration is None:
        max_duration = 5.5 if is_cjk else 7.0
    if max_chars is None:
        max_chars = 24 if is_cjk else 42
    soft_duration = max_duration * 0.75
    soft_chars = int(max_chars * 0.75)

    boundaries = _build_boundary_set(original_segments)
    joiner = "" if is_cjk else " "

    result: list[_Seg] = []
    cur: list[_W] = []

    def cur_text(words: list[_W]) -> str:
        return joiner.join(w.word for w in words)

    def emit(words: list[_W]) -> None:
        if not words:
            return
        text = cur_text(words).strip()
        if text:
            result.append(_Seg(text=text, start=words[0].t0, end=words[-1].t1))

    def at_boundary(t: float) -> bool:
        if not boundaries:
            return False
        return round(t * 100) in boundaries

    def split_score(left: _W, right: _W) -> float:
        """Quality of a break between two adjacent words; -inf = forbidden."""
        if _forbidden_split(left.word, right.word):
            return float("-inf")
        score = 0.0
        gap = right.t0 - left.t1
        if gap > 0:
            score += min(gap, 1.0) * 5.0  # a pause is the strongest signal
        if _BREAK_RE.search(left.word):
            score += 4.0
        if _COMMA_RE.search(left.word):
            score += 3.0
        if left.word and left.word[-1] in _FILLER_CHARS:
            score += 2.0
        if at_boundary(left.t1):
            score += 1.5
        # Penalties: a line must not start with a trailing particle (有|的)
        # nor end on a dangling function word (…把 / …在) — steer the break
        # elsewhere when there is any alternative.
        if right.word and right.word[0] in _BAD_LEAD_CHARS:
            score -= 2.0
        if left.word and left.word[-1] in _BAD_TAIL_CHARS and left.word[-1] not in _FILLER_CHARS:
            score -= 1.5
        return score

    def best_split(words: list[_W]) -> int:
        """Index j so that ``words[:j+1]`` is emitted and ``words[j+1:]``
        carries over. Scans the trailing backtrack window; prefers the
        highest score, ties resolved towards the latest position.
        Returns -1 when every possible split would cut through a token
        (e.g. the whole window sits inside one long latin word)."""
        last = len(words) - 2  # must leave at least one word in the suffix
        lo = max(0, len(words) - 1 - _BACKTRACK_WORDS)
        best_j = -1
        best_score = float("-inf")
        for j in range(last, lo - 1, -1):
            prefix = words[: j + 1]
            if (prefix[-1].t1 - prefix[0].t0) < min_duration:
                break  # any earlier j only makes the prefix shorter
            if len(cur_text(prefix)) < min_chars:
                break
            s = split_score(words[j], words[j + 1])
            if s > best_score:
                best_score = s
                best_j = j
        if best_j >= 0 and best_score > float("-inf"):
            return best_j
        # Whole window forbidden (inside a long latin/number run): widen the
        # search over the entire segment for the latest legal split.
        for j in range(min(last, lo - 1), -1, -1):
            if not _forbidden_split(words[j].word, words[j + 1].word):
                return j
        return -1

    hard_overflow_chars = max_chars * 2

    for ws in word_segments:
        word = ws.get("word") or ws.get("text", "")
        t0 = ws.get("start", 0.0)
        t1 = ws.get("end", 0.0)
        if not word:
            continue

        if not cur:
            cur = [_W(word, t0, t1)]
            continue

        gap = t0 - cur[-1].t1
        if gap > max_gap:
            emit(cur)
            cur = [_W(word, t0, t1)]
            continue

        cur_dur = cur[-1].t1 - cur[0].t0
        cand_len = len(cur_text(cur)) + len(joiner) + len(word)
        cand_dur = t1 - cur[0].t0
        past_min = cur_dur >= min_duration

        # Soft break: once past the soft thresholds, take any decent split
        # point *before* appending, instead of waiting for the hard limit
        # to force a cut at an arbitrary character.
        if (
            past_min
            and (cand_dur > soft_duration or cand_len > soft_chars)
            and len(cur_text(cur)) >= min_chars
            and split_score(cur[-1], _W(word, t0, t1)) >= _SOFT_BREAK_SCORE
        ):
            emit(cur)
            cur = [_W(word, t0, t1)]
            continue

        cur.append(_W(word, t0, t1))

        # Sentence-final punctuation: flush right away.
        if _BREAK_RE.search(word):
            emit(cur)
            cur = []
            continue

        # Hard limit: backtrack to the best split point in the window so the
        # cut never lands mid-word / mid-number. When even the widened search
        # finds no legal split (one giant latin run), keep accumulating up to
        # a generous overflow before forcing a raw cut.
        if cand_dur > max_duration or (cand_len > max_chars and past_min):
            j = best_split(cur)
            if j < 0:
                if cand_len > hard_overflow_chars:
                    j = len(cur) - 2  # give up: raw cut
                else:
                    continue  # tolerate temporary overflow, retry next word
            emit(cur[: j + 1])
            cur = cur[j + 1:]
            continue

        # Original ASR segment boundary: a natural place to break.
        if at_boundary(t1) and past_min:
            emit(cur)
            cur = []
            continue

        # Clause punctuation with enough content: break.
        if _COMMA_RE.search(word) and cand_len >= min_chars and cand_dur >= 1.2:
            emit(cur)
            cur = []
            continue

    emit(cur)

    merged: list[_Seg] = []
    for seg in result:
        if merged and (len(seg.text) < min_chars or (seg.end - seg.start) < 0.3):
            prev = merged[-1]
            prev.text = prev.text + joiner + seg.text
            prev.end = seg.end
        else:
            merged.append(seg)

    return [{"start": s.start, "end": s.end, "text": s.text} for s in merged]
