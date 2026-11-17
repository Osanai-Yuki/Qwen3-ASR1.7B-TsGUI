"""Server-side transcript export formats.

Pure port of frontend/src/utils/subtitle.ts + format.ts so the workbench
auto-export produces byte-identical files to the manual browser export.
Keep the two in sync: frontend/tests/exportPlan.test.mjs and
backend/test_export_formats.py assert the same rules.
"""
import json
import math
import re

FORMATS = ("srt", "vtt", "ass", "txt", "json")
TIMED_FORMATS = ("srt", "vtt", "ass")

_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _round_ms(seconds: float) -> int:
    # JS Math.round rounds halves up; Python round() is banker's rounding.
    return int(math.floor(seconds * 1000 + 0.5))


def format_timestamp(seconds: float) -> str:
    ms = _round_ms(seconds)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, millis = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"


def format_ass_time(seconds: float) -> str:
    cs = int(math.floor(seconds * 100 + 0.5))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _normalize_subtitle_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = _C0.sub("", text)
    return re.sub(r"\n{2,}", "\n", text)


def _escape_vtt_text(text: str) -> str:
    return _normalize_subtitle_text(text).replace("&", "&amp;").replace("<", "&lt;")


def _escape_ass_text(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    text = re.sub(r"[\r\n]+", r"\\N", text)
    return _C0.sub("", text)


def to_srt(segments: list[dict]) -> str:
    cues = []
    for i, seg in enumerate(segments, 1):
        start = format_timestamp(seg["start"]).replace(".", ",")
        end = format_timestamp(seg["end"]).replace(".", ",")
        cues.append(f"{i}\n{start} --> {end}\n{_normalize_subtitle_text(seg['text'])}")
    return "\n\n".join(cues)


def to_vtt(segments: list[dict]) -> str:
    body = "\n\n".join(
        f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n"
        f"{_escape_vtt_text(seg['text'])}"
        for seg in segments
    )
    return f"WEBVTT\n\n{body}"


_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Inter,72,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def to_ass(segments: list[dict]) -> str:
    events = "\n".join(
        f"Dialogue: 0,{format_ass_time(seg['start'])},{format_ass_time(seg['end'])},"
        f"Default,,0,0,0,,{_escape_ass_text(seg['text'])}"
        for seg in segments
    )
    return _ASS_HEADER + events + "\n"


def to_txt(segments: list[dict]) -> str:
    return "\n".join(seg["text"] for seg in segments)


def to_json(text: str, segments: list[dict], stats: dict | None) -> str:
    return json.dumps({"text": text, "segments": segments, "stats": stats},
                      ensure_ascii=False, indent=2)


def subtitle_cues(text: str, segments: list[dict], stats: dict | None) -> tuple[list[dict], bool, bool]:
    """(cues, coarse, duration_known) — same honesty rules as the frontend."""
    if segments:
        return segments, False, True
    if not text.strip():
        return [], False, False
    duration = (stats or {}).get("audio_duration")
    known = isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0
    if not known:
        return [], True, False
    return [{"start": 0, "end": duration, "text": text.strip()}], True, True


_NO_TIMING_NOTICE = (
    "没有生成有效时间范围：本次转写没有分段定位时间戳，音频时长也未知。\n"
    "以下为完整转写文字；需要时间轴时请使用 TXT 或 JSON 格式。\n"
    "\n"
)


def build_subtitle(fmt: str, text: str, segments: list[dict], stats: dict | None) -> str:
    cues, coarse, duration_known = subtitle_cues(text, segments, stats)
    if fmt in TIMED_FORMATS and coarse and not duration_known:
        return _NO_TIMING_NOTICE + text.strip()
    if fmt == "srt":
        return to_srt(cues)
    if fmt == "vtt":
        return to_vtt(cues)
    if fmt == "ass":
        return to_ass(cues)
    if fmt == "txt":
        return to_txt(cues)
    if fmt == "json":
        return to_json(text, segments, stats)
    raise ValueError(f"unknown format: {fmt}")