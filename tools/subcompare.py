#!/usr/bin/env python3
"""Subtitle comparison tool — WER / CER / timestamp-drift analysis.

Compares a hypothesis subtitle file against a ground-truth reference.
Supports SRT and VTT.  Zero external dependencies (stdlib only).

Usage:
    python tools/subcompare.py reference.srt hypothesis.srt
    python tools/subcompare.py --html report.html ref.vtt hyp.vtt
    python tools/subcompare.py --tui ref.srt hyp.srt   (default: TUI table)
"""
import argparse
import html
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


# Data structures

@dataclass
class Segment:
    idx: int
    start: float
    end: float
    text: str


@dataclass
class SegmentPair:
    ref: Segment
    hyp: Segment
    wer: float
    cer: float
    start_drift: float
    end_drift: float


# Subtitle parsers

MAX_SUBTITLE_BYTES = 50 * 1024 * 1024  # 50 MiB

# A cue timestamp is [HH:]MM:SS.mmm; WebVTT allows the hours to be dropped and
# either separator is seen in the wild.
_TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{1,3}"
_TIMING_RE = re.compile(rf"({_TS})\s*-->\s*({_TS})")


def _read(path: Path) -> str:
    """Read text, auto-detecting encoding from BOM or trying common fallbacks."""
    size = path.stat().st_size
    if size > MAX_SUBTITLE_BYTES:
        raise ValueError(
            f"{path} is too large ({size} bytes; max {MAX_SUBTITLE_BYTES})"
        )
    raw = path.read_bytes()
    # BOM-based detection.
    if raw[:3] == b'\xef\xbb\xbf':
        text = raw[3:].decode("utf-8")
    elif raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = raw.decode("utf-16")
    else:
        # Try UTF-8 first (most common for SRT/VTT).
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            # Fall back to encodings common for subtitle files.
            for enc in ("gbk", "gb2312", "big5", "shift_jis", "cp1252"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")
    # Block splitting keys on blank lines, so CRLF (Windows editors write it,
    # and so does this app's own ASS/VTT output path) must not defeat it.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_srt(text: str) -> list[Segment]:
    """Parse SRT, tolerating blank lines and varying separators."""
    segs: list[Segment] = []
    # Split on double-newline blocks.
    blocks = re.split(r'\n{2,}', text.strip())
    idx = 0
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        # Find the timing line: "HH:MM:SS,mmm --> HH:MM:SS,mmm"
        timing_line = None
        timing_idx = None
        for i, line in enumerate(lines):
            m = _TIMING_RE.search(line)
            if m:
                timing_line = m
                timing_idx = i
                break
        if timing_line is None:
            continue
        start = _parse_timestamp(timing_line.group(1))
        end = _parse_timestamp(timing_line.group(2))
        text_lines = lines[timing_idx + 1:]
        text = ' '.join(t.strip() for t in text_lines if t.strip())
        if not text:
            continue
        segs.append(Segment(idx=idx, start=start, end=end, text=text))
        idx += 1
    return segs


def parse_vtt(text: str) -> list[Segment]:
    """Parse WebVTT.

    Cue blocks are blank-line separated exactly like SRT and may carry a cue
    identifier line, so after dropping the WEBVTT header the SRT block walk does
    the job. Header settings and NOTE blocks have no timing line and are skipped
    by it.
    """
    body = text.strip()
    if body.upper().startswith("WEBVT"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    return parse_srt(body)


def _parse_timestamp(s: str) -> float:
    """'HH:MM:SS.mmm', 'HH:MM:SS,mmm' or VTT's 'MM:SS.mmm' → seconds."""
    m = re.match(r'(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})', s.strip())
    if not m:
        return 0.0
    h = int(m.group(1)) if m.group(1) else 0
    mi, sec, ms = int(m.group(2)), int(m.group(3)), m.group(4)
    return h * 3600 + mi * 60 + sec + int(ms.ljust(3, '0')) / 1000.0


def parse_ass(text: str) -> list[Segment]:
    """Parse ASS/SSA subtitles."""
    segs: list[Segment] = []
    idx = 0
    in_events = False
    format_seen = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and not in_events:
            if '[Events]' in stripped or '[events]' in stripped:
                in_events = True
            continue
        if not in_events:
            continue
        if stripped.startswith('Format:') and not format_seen:
            format_seen = True
            continue
        if not stripped.startswith('Dialogue:'):
            continue
        # Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        rest = stripped[len('Dialogue:'):].strip()
        parts = rest.split(',', 9)  # max 10 fields
        if len(parts) < 10:
            continue
        start = _parse_ass_time(parts[1])
        end = _parse_ass_time(parts[2])
        raw_text = parts[9]
        # Remove ASS override tags: {\i1}, {\pos(10,20)}, etc.
        clean = re.sub(r'\{[^}]*\}', '', raw_text)
        # Convert \N and \n to actual newlines, then collapse whitespace.
        clean = clean.replace('\\N', '\n').replace('\\n', ' ')
        clean = re.sub(r'\s+', ' ', clean).strip()
        if not clean:
            continue
        segs.append(Segment(idx=idx, start=start, end=end, text=clean))
        idx += 1
    return segs


def _parse_ass_time(s: str) -> float:
    """ASS time 'H:MM:SS.cc' or 'MM:SS.cc' → seconds."""
    m = re.match(r'(\d+):(\d{2}):(\d{2})\.(\d+)', s.strip()) or \
        re.match(r'(\d{2}):(\d{2})\.(\d+)', s.strip())
    if not m:
        return 0.0
    groups = m.groups()
    if len(groups) == 4:
        h, mi, sec, cs = int(groups[0]), int(groups[1]), int(groups[2]), int(groups[3])
    else:
        h, mi, sec, cs = 0, int(groups[0]), int(groups[1]), int(groups[2])
    return h * 3600 + mi * 60 + sec + cs / 100.0


def parse_file(path: Path) -> list[Segment]:
    text = _read(path)
    suffix = path.suffix.lower()
    if suffix == '.vtt':
        return parse_vtt(text)
    if suffix in ('.ass', '.ssa'):
        return parse_ass(text)
    return parse_srt(text)  # default to SRT


# Text metrics

def _levenshtein(ref: list, hyp: list) -> float:
    """Standard Levenshtein distance on a token list."""
    n, m = len(ref), len(hyp)
    if n == 0 and m == 0:
        return 0.0
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[m]


def wer(ref_text: str, hyp_text: str) -> float:
    """Word Error Rate."""
    ref_words = _tokenize(ref_text)
    hyp_words = _tokenize(hyp_text)
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    return _levenshtein(ref_words, hyp_words) / len(ref_words)


def cer(ref_text: str, hyp_text: str) -> float:
    """Character Error Rate (NFKC-normalised, whitespace-collapsed)."""
    ref_chars = list(_normalize(ref_text))
    hyp_chars = list(_normalize(hyp_text))
    if not ref_chars:
        return 0.0 if not hyp_chars else 1.0
    return _levenshtein(ref_chars, hyp_chars) / len(ref_chars)


def _tokenize(text: str) -> list[str]:
    """Split on whitespace after normalising."""
    return _normalize(text).split()


def _normalize(text: str) -> str:
    """NFKC + collapse whitespace."""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


# Alignment

def align_segments(
    ref_segs: list[Segment],
    hyp_segs: list[Segment],
    min_iou: float = 0.0,
) -> list[tuple[Segment, Segment]]:
    """Greedy time-overlap alignment: for each ref, pick the hyp with max IoU.

    Unmatched references count as deletions (empty hypothesis text).
    Unmatched hypotheses count as insertions (empty reference text) — handled
    by iterating both directions and merging.
    """
    # For every (ref, hyp) pair compute temporal IoU.
    pairs: list[tuple[float, int, int]] = []
    for ri, r in enumerate(ref_segs):
        for hi, h in enumerate(hyp_segs):
            overlap_start = max(r.start, h.start)
            overlap_end = min(r.end, h.end)
            overlap = max(0.0, overlap_end - overlap_start)
            union = max(r.end, h.end) - min(r.start, h.start)
            iou = overlap / union if union > 0 else 0.0
            if iou >= min_iou:
                pairs.append((iou, ri, hi))
    pairs.sort(reverse=True)

    used_ref: set[int] = set()
    used_hyp: set[int] = set()
    aligned: list[tuple[int, int]] = []
    for iou, ri, hi in pairs:
        if ri in used_ref or hi in used_hyp:
            continue
        used_ref.add(ri)
        used_hyp.add(hi)
        aligned.append((ri, hi))
    aligned.sort()

    # Collect unmatched refs (deletions).
    unmatched_ref = [i for i in range(len(ref_segs)) if i not in used_ref]

    result: list[tuple[Segment, Segment]] = []
    ri_ptr = 0
    for ri, hi in aligned:
        # Insert unmatched refs before this pair.
        while ri_ptr < ri:
            result.append((ref_segs[ri_ptr], Segment(idx=-1, start=0, end=0, text="")))
            ri_ptr += 1
        result.append((ref_segs[ri], hyp_segs[hi]))
        ri_ptr = ri + 1
    # Trailing unmatched refs.
    while ri_ptr < len(ref_segs):
        result.append((ref_segs[ri_ptr], Segment(idx=-1, start=0, end=0, text="")))
        ri_ptr += 1
    return result


# Analysis

def compute_metrics(pairs: list[tuple[Segment, Segment]]) -> dict:
    """Aggregate WER / CER / timestamp drift across all pairs."""
    if not pairs:
        return {"wer": 0.0, "cer": 0.0, "start_drift_mean": 0.0,
                "end_drift_mean": 0.0, "n": 0}

    total_wer = 0.0
    total_cer = 0.0
    start_drifts: list[float] = []
    end_drifts: list[float] = []
    ref_word_count = 0
    ref_char_count = 0
    pair_details: list[SegmentPair] = []

    for ref, hyp in pairs:
        w = wer(ref.text, hyp.text)
        c = cer(ref.text, hyp.text)
        sd = abs(ref.start - hyp.start) if hyp.text else 0.0
        ed = abs(ref.end - hyp.end) if hyp.text else 0.0
        total_wer += w
        total_cer += c
        ref_word_count += len(_tokenize(ref.text)) if ref.text else 0
        ref_char_count += len(list(_normalize(ref.text))) if ref.text else 0
        if hyp.text:
            start_drifts.append(sd)
            end_drifts.append(ed)
        pair_details.append(SegmentPair(
            ref=ref, hyp=hyp, wer=w, cer=c,
            start_drift=sd, end_drift=ed,
        ))

    n = len(pairs)
    return {
        "wer": total_wer / n * 100,
        "cer": total_cer / n * 100,
        "wer_weighted": total_wer / max(ref_word_count, 1) * 100,
        "cer_weighted": total_cer / max(ref_char_count, 1) * 100,
        "start_drift_mean": sum(start_drifts) / max(len(start_drifts), 1),
        "end_drift_mean": sum(end_drifts) / max(len(end_drifts), 1),
        "start_drift_max": max(start_drifts) if start_drifts else 0.0,
        "end_drift_max": max(end_drifts) if end_drifts else 0.0,
        "n": n,
        "n_matched": len(start_drifts),
        "n_deletions": sum(1 for _, h in pairs if not h.text),
        "pairs": pair_details,
    }


# TUI output

def tui_report(metrics: dict, ref_name: str, hyp_name: str) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("  Subtitle Comparison Report")
    lines.append(f"  Reference : {ref_name}")
    lines.append(f"  Hypothesis: {hyp_name}")
    lines.append("=" * 60)
    lines.append("")

    # Summary table.
    summary_rows = [
        ("Segments (ref)", str(metrics["n"])),
        ("Matched pairs", str(metrics["n_matched"])),
        ("Deletions (missing in hyp)", str(metrics["n_deletions"])),
        ("WER (per-segment avg)", f"{metrics['wer']:.1f}%"),
        ("WER (word-weighted)", f"{metrics['wer_weighted']:.1f}%"),
        ("CER (per-segment avg)", f"{metrics['cer']:.1f}%"),
        ("CER (char-weighted)", f"{metrics['cer_weighted']:.1f}%"),
        ("Start drift (mean)", f"{metrics['start_drift_mean']*1000:.0f} ms"),
        ("Start drift (max)", f"{metrics['start_drift_max']*1000:.0f} ms"),
        ("End drift (mean)", f"{metrics['end_drift_mean']*1000:.0f} ms"),
        ("End drift (max)", f"{metrics['end_drift_max']*1000:.0f} ms"),
    ]
    label_w = max(len(r[0]) for r in summary_rows)
    for label, val in summary_rows:
        lines.append(f"  {label:<{label_w}}  {val}")

    lines.append("")
    lines.append("=" * 60)
    lines.append("  Per-segment detail")
    lines.append("=" * 60)

    # Per-segment table.
    hdr = f"  {'#':>3}  {'WER%':>6} {'CER%':>6} {'stDrift':>8} {'endDrift':>9}  {'Ref text':<30} {'Hyp text':<30}"
    lines.append(hdr)
    lines.append("  " + "-" * 76)
    for i, p in enumerate(metrics["pairs"][:50]):
        ref_t = p.ref.text[:28] + ".." if len(p.ref.text) > 28 else p.ref.text
        hyp_t = p.hyp.text[:28] + ".." if len(p.hyp.text) > 28 else p.hyp.text
        lines.append(
            f"  {i+1:>3}  {p.wer*100:>6.1f} {p.cer*100:>6.1f} "
            f"{p.start_drift*1000:>7.0f}ms {p.end_drift*1000:>8.0f}ms  "
            f"{ref_t:<30} {hyp_t:<30}"
        )
    if len(metrics["pairs"]) > 50:
        lines.append(f"  ... ({len(metrics['pairs']) - 50} more segments)")

    return "\n".join(lines)


# HTML report

def html_report(metrics: dict, ref_name: str, hyp_name: str) -> str:
    """Side-by-side diff HTML with timestamp drift chart."""
    rows_html = []
    max_drift = max(
        metrics["start_drift_max"], metrics["end_drift_max"], 0.001
    )
    for i, p in enumerate(metrics["pairs"]):
        ref_t = html.escape(p.ref.text)
        hyp_t = html.escape(p.hyp.text)
        wer_cls = "ok" if p.wer < 0.3 else ("warn" if p.wer < 0.6 else "bad")
        st_d = p.start_drift * 1000
        ed_d = p.end_drift * 1000
        rows_html.append({
            "idx": i + 1,
            "wer": f"{p.wer * 100:.1f}",
            "cer": f"{p.cer * 100:.1f}",
            "st_drift": f"{st_d:.0f}",
            "ed_drift": f"{ed_d:.0f}",
            "ref": ref_t or "(deleted)",
            "hyp": hyp_t or "(missing)",
            "wer_cls": wer_cls,
        })

    chart_data = json.dumps([
        {"x": i, "st": p.start_drift * 1000, "ed": p.end_drift * 1000}
        for i, p in enumerate(metrics["pairs"]) if p.hyp.text
    ])

    summary = {
        "WER (avg)": f"{metrics['wer']:.1f}%",
        "WER (weighted)": f"{metrics['wer_weighted']:.1f}%",
        "CER (avg)": f"{metrics['cer']:.1f}%",
        "CER (weighted)": f"{metrics['cer_weighted']:.1f}%",
        "Start drift (mean)": f"{metrics['start_drift_mean']*1000:.0f} ms",
        "Start drift (max)": f"{metrics['start_drift_max']*1000:.0f} ms",
        "End drift (mean)": f"{metrics['end_drift_mean']*1000:.0f} ms",
        "End drift (max)": f"{metrics['end_drift_max']*1000:.0f} ms",
        "Segments (ref)": str(metrics["n"]),
        "Matched": str(metrics["n_matched"]),
        "Deletions": str(metrics["n_deletions"]),
    }

    summary_html = "".join(
        f'<tr><td class="label">{k}</td><td class="val">{v}</td></tr>'
        for k, v in summary.items()
    )

    rows_html_joined = "".join(
        f'<tr class="{r["wer_cls"]}">'
        f'<td class="idx">{r["idx"]}</td>'
        f'<td class="metric">{r["wer"]}</td>'
        f'<td class="metric">{r["cer"]}</td>'
        f'<td class="metric">{r["st_drift"]}ms</td>'
        f'<td class="metric">{r["ed_drift"]}ms</td>'
        f'<td class="text ref">{r["ref"]}</td>'
        f'<td class="text hyp">{r["hyp"]}</td>'
        f'</tr>'
        for r in rows_html
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Subtitle Comparison</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0a;color:#e0e0e0;font-family:'Geist',ui-sans-serif,sans-serif;
     padding:2rem;max-width:1200px;margin:0 auto}}
h1{{font-size:1.2rem;font-weight:700;margin-bottom:.25rem}}
.sub{{font-size:.8rem;color:#888;margin-bottom:1.5rem}}
table.summary{{border-collapse:collapse;margin-bottom:1.5rem}}
.summary td{{padding:.35rem .75rem;border:1px solid #333;font-size:.85rem}}
.summary .label{{color:#888;width:180px}}
.summary .val{{color:#fff;font-weight:600}}
.chart{{width:100%;height:180px;margin-bottom:1.5rem;background:#111;border:1px solid #333}}
table.detail{{border-collapse:collapse;width:100%;font-size:.82rem}}
.detail th{{background:#1a1a1a;padding:.45rem .55rem;text-align:left;
            font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
            color:#888;border-bottom:2px solid #333;position:sticky;top:0}}
.detail td{{padding:.4rem .55rem;border-bottom:1px solid #222;vertical-align:top}}
.detail .idx{{color:#666;width:30px}}
.detail .metric{{width:55px;text-align:right;font-variant-numeric:tabular-nums}}
.detail .text{{max-width:350px;line-height:1.45;word-break:break-word}}
.detail .ref{{color:#e0e0e0}}
.detail .hyp{{color:#e0e0e0}}
.detail tr.ok .metric{{color:#666}}
.detail tr.warn .metric{{color:#c90}}
.detail tr.bad .metric{{color:#f44}}
.detail tr:hover{{background:#141414}}
.chart-wrap{{margin-bottom:1.5rem}}
.chart-wrap h2{{font-size:.9rem;margin-bottom:.5rem;color:#aaa}}
.bar{{display:inline-block;margin:1px 0}}
</style>
</head>
<body>
<h1>Subtitle Comparison Report</h1>
<p class="sub">Reference: <b>{html.escape(ref_name)}</b> &nbsp;|&nbsp;
   Hypothesis: <b>{html.escape(hyp_name)}</b></p>

<h2 style="font-size:.9rem;margin-bottom:.5rem;color:#aaa">Summary</h2>
<table class="summary">{summary_html}</table>

<div class="chart-wrap">
<h2>Timestamp Drift (ms) — Start ● End ●</h2>
<canvas class="chart" id="chart"></canvas>
</div>

<h2 style="font-size:.9rem;margin-bottom:.5rem;color:#aaa">Per-segment detail</h2>
<table class="detail">
<thead><tr>
<th>#</th><th>WER</th><th>CER</th><th>StDrift</th><th>EndDrift</th>
<th>Reference</th><th>Hypothesis</th>
</tr></thead>
<tbody>{rows_html_joined}</tbody>
</table>

<script>
// Simple canvas scatter chart for timestamp drift.
const data = {chart_data};
const cv = document.getElementById('chart');
const ctx = cv.getContext('2d');
function resize() {{
  cv.width = cv.clientWidth * devicePixelRatio;
  cv.height = cv.clientHeight * devicePixelRatio;
  draw();
}}
function draw() {{
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  if (!data.length) return;
  const pad = {{ l: 45, r: 15, t: 15, b: 25 }};
  const maxD = Math.max(...data.map(d => Math.max(d.st, d.ed)), 1);
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  // Axes.
  ctx.strokeStyle = '#333';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, pad.t + plotH);
  ctx.lineTo(pad.l + plotW, pad.t + plotH); ctx.stroke();
  // Y labels.
  ctx.fillStyle = '#666';
  ctx.font = `${{10*devicePixelRatio}}px sans-serif`;
  for (let i = 0; i <= 4; i++) {{
    const y = pad.t + plotH - (plotH * i / 4);
    const v = (maxD * i / 4).toFixed(0);
    ctx.fillText(v + 'ms', 2, y + 3*devicePixelRatio);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + plotW, y);
    ctx.strokeStyle = '#1a1a1a'; ctx.stroke();
  }}
  // Bars.
  const bw = Math.max(1, plotW / data.length - 1);
  data.forEach((d, i) => {{
    const x = pad.l + (plotW / data.length) * i;
    const stY = pad.t + plotH - (d.st / maxD) * plotH;
    const edY = pad.t + plotH - (d.ed / maxD) * plotH;
    ctx.fillStyle = 'rgba(100,200,255,.6)';
    ctx.fillRect(x, stY - 1*devicePixelRatio, bw, 2*devicePixelRatio);
    ctx.fillStyle = 'rgba(255,180,80,.6)';
    ctx.fillRect(x, edY - 1*devicePixelRatio, bw, 2*devicePixelRatio);
  }});
}}
window.addEventListener('resize', resize);
resize();
</script>
</body>
</html>"""


# CLI

def main():
    parser = argparse.ArgumentParser(
        description="Compare two subtitle files (WER / CER / timestamp drift)."
    )
    parser.add_argument("reference", type=Path, help="Ground-truth subtitle file.")
    parser.add_argument("hypothesis", type=Path, help="Hypothesis subtitle file.")
    parser.add_argument("--html", type=Path, default=None,
                        help="Write HTML report to this path.")
    parser.add_argument("--min-iou", type=float, default=0.0,
                        help="Minimum time IoU to align a pair (default 0).")
    args = parser.parse_args()

    if not args.reference.is_file():
        print(f"Error: reference not found: {args.reference}", file=sys.stderr)
        sys.exit(1)
    if not args.hypothesis.is_file():
        print(f"Error: hypothesis not found: {args.hypothesis}", file=sys.stderr)
        sys.exit(1)

    ref_segs = parse_file(args.reference)
    hyp_segs = parse_file(args.hypothesis)
    if not ref_segs:
        print("Error: no segments parsed from reference.", file=sys.stderr)
        sys.exit(1)
    if not hyp_segs:
        print("Error: no segments parsed from hypothesis.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(ref_segs)} ref / {len(hyp_segs)} hyp segments.")

    pairs = align_segments(ref_segs, hyp_segs, min_iou=args.min_iou)
    metrics = compute_metrics(pairs)

    if args.html:
        args.html.write_text(
            html_report(metrics, str(args.reference), str(args.hypothesis)),
            encoding="utf-8",
        )
        print(f"HTML report written to {args.html}")
    else:
        print(tui_report(metrics, str(args.reference), str(args.hypothesis)))


if __name__ == "__main__":
    main()
