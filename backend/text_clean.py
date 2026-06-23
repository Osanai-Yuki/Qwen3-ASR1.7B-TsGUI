"""Post-process ASR text to remove training template artifacts.

The Qwen3-ASR model occasionally leaks training-time prompt template
fragments into its output, e.g. ``language Chinese<asr_text>...`` or
``<|im_start|>...``. CJK output may also contain spurious spaces between
every character because the tokenizer/template adds them.

This module strips such artifacts and normalises whitespace.
"""
import re
import unicodedata


_TAG_PATTERNS = [
    re.compile(r"<\|[^|]*?\|>"),
    re.compile(r"<asr[_a-z]*>", re.IGNORECASE),
    re.compile(r"</asr[_a-z]*>", re.IGNORECASE),
    re.compile(r"<\s*asr_text\s*>", re.IGNORECASE),
    re.compile(r"</\s*asr_text\s*>", re.IGNORECASE),
    re.compile(r"<\s*lang(?:uage)?\s*>", re.IGNORECASE),
    re.compile(r"</\s*lang(?:uage)?\s*>", re.IGNORECASE),
    re.compile(r"\[BEGIN_AUDIO\]"),
    re.compile(r"\[END_AUDIO\]"),
]

_HALLUCINATION_PATTERNS = [
    re.compile(
        r'(?:ERROR:\s*)?Cannot read\s+"image\.png"\s*'
        r"(?:\(this model does not support image input\)\.?\s*)?"
        r"(?:Inform the user\.?\s*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r'(?:ERROR:\s*)?Cannot read\s+"image\.png"[^.\n]*?\.?\s*',
        re.IGNORECASE,
    ),
]

_LANG_NAMES = (
    r"chinese|english|japanese|korean|spanish|french|german|russian|"
    r"portuguese|italian|arabic|hindi|cantonese|mandarin|"
    r"\u4e2d\u6587|\u65e5\u8bed|\u82f1\u8bed|\u97e9\u8bed|\u7ca4\u8bed"
)

_PREFIX_RE = re.compile(
    rf"^\s*(?:language\s*[:\uff1a]?\s*)?(?:{_LANG_NAMES})\s*[,\uff0c:\uff1a\-]?\s*",
    re.IGNORECASE,
)

_LEADING_LANG_TAG_RE = re.compile(
    rf"^\s*language\s*[:\uff1a]?\s*(?:{_LANG_NAMES})?\s*",
    re.IGNORECASE,
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def _collapse_cjk_spaces(text: str) -> str:
    """Remove spaces inserted between adjacent CJK characters.

    Keeps spaces between Latin words, and between Latin and CJK.
    """
    out_chars: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == " " and out_chars:
            j = i + 1
            while j < n and text[j] == " ":
                j += 1
            if j < n and _is_cjk(text[j]) and _is_cjk(out_chars[-1]):
                i = j
                continue
            else:
                out_chars.append(" ")
                i = j
                continue
        out_chars.append(ch)
        i += 1
    return "".join(out_chars)


def _strip_tags(text: str) -> str:
    for pat in _TAG_PATTERNS:
        text = pat.sub("", text)
    for pat in _HALLUCINATION_PATTERNS:
        text = pat.sub("", text)
    return text


def _strip_lang_prefix(text: str) -> str:
    text = _LEADING_LANG_TAG_RE.sub("", text)
    text = _PREFIX_RE.sub("", text)
    return text


def _normalize_ws(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def clean_asr_text(text: str) -> str:
    """Remove template artifacts and normalise whitespace from ASR output."""
    if not text:
        return ""
    text = _strip_tags(text)
    text = _strip_lang_prefix(text)
    text = _collapse_cjk_spaces(text)
    text = _normalize_ws(text)
    return text


def clean_segments(segments: list[dict]) -> list[dict]:
    """Apply ``clean_asr_text`` to each segment's text in-place-style copy."""
    out: list[dict] = []
    for s in segments or []:
        ns = dict(s)
        if "text" in ns and isinstance(ns["text"], str):
            cleaned = clean_asr_text(ns["text"])
            if not cleaned:
                continue
            ns["text"] = cleaned
        out.append(ns)
    return out
