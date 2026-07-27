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
      return `${start} --> ${end}\n${normalizeSubtitleText(seg.text)}`;
    })
    .join("\n\n");
  return `WEBVTT\n\n${body}`;
}

function normalizeSubtitleText(text: string): string {
  return text.replace(/\n{2,}/g, "\n");
}

function escapeAssText(text: string): string {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/\{/g, "\\{")
    .replace(/\}/g, "\\}")
    .replace(/\n/g, "\\N");
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

/** Build the export string for a given format. */
export function buildSubtitle(
  format: SubtitleFormat,
  text: string,
  segments: Segment[],
  stats: Stats | null,
): string {
  switch (format) {
    case "srt":
      return toSRT(segments);
    case "vtt":
      return toVTT(segments);
    case "ass":
      return toASS(segments);
    case "txt":
      return toTXT(segments);
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
