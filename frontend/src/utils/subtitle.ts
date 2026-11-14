// Subtitle / transcript export in five formats.
// All generators take the segment list and return a string ready to download.

import type { Segment, Stats } from "../types";
import { formatAssTime, formatTimestamp } from "./format";

/** SRT — index + "HH:MM:SS,mmm --> HH:MM:SS,mmm" + text. */
export function toSRT(segments: Segment[]): string {
  return segments
    .map((seg, i) => {
      const start = formatTimestamp(seg.start).replace(".", ",");
      const end = formatTimestamp(seg.end).replace(".", ",");
      return `${i + 1}\n${start} --> ${end}\n${normalizeSubtitleText(seg.text)}`;
    })
    .join("\n\n");
}

/** WebVTT — "WEBVTT" header + "HH:MM:SS.mmm --> HH:MM:SS.mmm" + text. */
export function toVTT(segments: Segment[]): string {
  const body = segments
    .map((seg) => {
      const start = formatTimestamp(seg.start);
      const end = formatTimestamp(seg.end);
      return `${start} --> ${end}\n${escapeVttText(seg.text)}`;
    })
    .join("\n\n");
  return `WEBVTT\n\n${body}`;
}

function normalizeSubtitleText(text: string): string {
  // A bare \r would end an SRT cue line and split a VTT cue in two, and other
  // C0 controls make the file unparseable. The backend only strips \n and
  // spaces, so transcript text can still carry them.
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "")
    .replace(/\n{2,}/g, "\n");
}

function escapeVttText(text: string): string {
  // VTT cue text parses markup: a transcript that literally says "<v person>"
  // or starts a line with NOTE would otherwise become a cue tag or a comment.
  return normalizeSubtitleText(text).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function escapeAssText(text: string): string {
  // Backslash first, so the \N we emit for line breaks is not re-escaped.
  // A lone \r must go too: it terminates the Dialogue line and lets the rest
  // of the transcript be read as new ASS sections.
  return text
    .replace(/\\/g, "\\\\")
    .replace(/\{/g, "\\{")
    .replace(/\}/g, "\\}")
    .replace(/[\r\n]+/g, "\\N")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "");
}

/** ASS — script info + default style + Dialogue lines (centisecond timing). */
export function toASS(segments: Segment[]): string {
  const header = `[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Inter,72,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
`;
  const events = segments
    .map(
      (seg) =>
        `Dialogue: 0,${formatAssTime(seg.start)},${formatAssTime(seg.end)},Default,,0,0,0,,${escapeAssText(seg.text)}`,
    )
    .join("\n");
  return header + events + "\n";
}

/** Plain text — one segment per line. */
export function toTXT(segments: Segment[]): string {
  return segments.map((seg) => seg.text).join("\n");
}

/** Full JSON dump of text, segments and stats. */
export function toJSON(
  text: string,
  segments: Segment[],
  stats: Stats | null,
): string {
  return JSON.stringify({ text, segments, stats }, null, 2);
}

export type SubtitleFormat = "srt" | "vtt" | "ass" | "txt" | "json";

export const SUBTITLE_FORMATS: {
  id: SubtitleFormat;
  label: string;
  ext: string;
  mime: string;
}[] = [
  { id: "srt", label: "SRT", ext: "srt", mime: "application/x-subrip" },
  { id: "vtt", label: "VTT", ext: "vtt", mime: "text/vtt" },
  { id: "ass", label: "ASS", ext: "ass", mime: "text/plain" },
  { id: "txt", label: "TXT", ext: "txt", mime: "text/plain" },
  { id: "json", label: "JSON", ext: "json", mime: "application/json" },
];

/** What the exporter actually produced, so the manifest can describe it
 *  honestly instead of implying per-sentence timing that does not exist. */
export interface SubtitleCues {
  cues: Segment[];
  /** True when a single whole-pass cue was synthesised from the plain text. */
  coarse: boolean;
  /** False when the audio duration is unknown, so no valid range exists. */
  durationKnown: boolean;
}

/**
 * Resolve the cue list for export.
 *
 * The backend falls back to the bare text field when a chunk yields no usable
 * segment timestamps.  Exporting an empty cue list would hand back a file with
 * nothing in it while the UI still shows the transcript, so a single
 * whole-pass cue is synthesised — but only when the duration is genuinely
 * known.  With an unknown duration there is no honest time range, and emitting
 * `00:00:00 --> 00:00:00` would look like real timing that was never there.
 */
export function subtitleCues(
  text: string,
  segments: Segment[],
  stats: Stats | null,
): SubtitleCues {
  if (segments.length > 0) return { cues: segments, coarse: false, durationKnown: true };
  if (!text.trim()) return { cues: [], coarse: false, durationKnown: false };
  const duration = stats?.audio_duration;
  const known = typeof duration === "number" && isFinite(duration) && duration > 0;
  if (!known) return { cues: [], coarse: true, durationKnown: false };
  return { cues: [{ start: 0, end: duration, text: text.trim() }], coarse: true, durationKnown: true };
}

const TIMED_FORMATS: SubtitleFormat[] = ["srt", "vtt", "ass"];

/**
 * Build the export string for a given format.
 *
 * A timed format with no usable segments and an unknown duration returns an
 * explicit notice plus the full text rather than a fabricated all-zero cue.
 */
export function buildSubtitle(
  format: SubtitleFormat,
  text: string,
  segments: Segment[],
  stats: Stats | null,
): string {
  const { cues, coarse, durationKnown } = subtitleCues(text, segments, stats);
  if (TIMED_FORMATS.includes(format) && coarse && !durationKnown) {
    return [
      "没有生成有效时间范围：本次转写没有分段定位时间戳，音频时长也未知。",
      "以下为完整转写文字；需要时间轴时请使用 TXT 或 JSON 格式。",
      "",
      text.trim(),
    ].join("\n");
  }
  switch (format) {
    case "srt":
      return toSRT(cues);
    case "vtt":
      return toVTT(cues);
    case "ass":
      return toASS(cues);
    case "txt":
      return toTXT(cues);
    case "json":
      return toJSON(text, segments, stats);
  }
}

/** Trigger a browser download of the generated subtitle content. */
export function downloadSubtitle(
  format: SubtitleFormat,
  text: string,
  segments: Segment[],
  stats: Stats | null,
  baseName: string,
): void {
  const spec = SUBTITLE_FORMATS.find((f) => f.id === format)!;
  const content = buildSubtitle(format, text, segments, stats);
  const blob = new Blob([content], { type: `${spec.mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const safeBaseName = (baseName || "transcript").replace(
    /[<>:"\/\\|?*\x00-\x1f]/g,
    "_",
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safeBaseName}.${spec.ext}`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
