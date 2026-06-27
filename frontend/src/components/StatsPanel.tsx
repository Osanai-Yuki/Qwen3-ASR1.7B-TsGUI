import type { Stats } from "../types";
import { formatBytes, formatDuration } from "../utils/format";

interface Props {
  stats: Stats | null;
}

interface Stat {
  label: string;
  value: string;
}

/** Performance statistics grid: RTF, timing, counts, model. */
export function StatsPanel({ stats }: Props) {
  if (!stats) {
    return (
      <div>
        <h2 className="font-mono text-xs uppercase tracking-widest text-white/45 mb-4">
          Statistics
        </h2>
        <p className="font-mono text-sm text-white/40">
          Awaiting transcription.
        </p>
      </div>
    );
  }

  const rows: Stat[] = [
    { label: "RTF", value: stats.rtf.toFixed(3) },
    { label: "Audio", value: formatDuration(stats.audio_duration) },
    { label: "Process", value: formatDuration(stats.total_time) },
    { label: "ASR time", value: formatDuration(stats.asr_time) },
    {
      label: "Align time",
      value: stats.aligner_used ? formatDuration(stats.align_time) : "—",
    },
    { label: "Chars", value: String(stats.char_count) },
    { label: "Words", value: String(stats.word_count) },
    { label: "Segments", value: String(stats.segment_count) },
    { label: "Chunks", value: String(stats.chunk_count) },
    { label: "File size", value: formatBytes(stats.file_size) },
    { label: "Model", value: stats.model },
    {
      label: "Aligner",
      value: stats.aligner_model ?? "none",
    },
  ];

  return (
    <div className="animate-fade-in">
      <h2 className="font-mono text-xs uppercase tracking-widest text-white/45 mb-4">
        Statistics
      </h2>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex flex-col font-mono text-xs p-3 rounded-xl bg-white/5 border border-white/8"
          >
            <dt className="text-white/40 uppercase tracking-wider text-[10px]">
              {row.label}
            </dt>
            <dd className="text-white/95 break-all mt-0.5">{row.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
