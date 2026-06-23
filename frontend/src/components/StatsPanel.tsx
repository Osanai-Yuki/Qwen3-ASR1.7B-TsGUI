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
      <div className="border border-neutral-800 p-6">
        <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500 mb-4">
          Statistics
        </h2>
        <p className="font-mono text-sm text-neutral-600">
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
    <div className="border border-neutral-800 p-6">
      <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500 mb-4">
        Statistics
      </h2>
      <dl className="space-y-2">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex items-baseline justify-between gap-4 font-mono text-xs"
          >
            <dt className="text-neutral-500 uppercase tracking-wider">
              {row.label}
            </dt>
            <dd className="text-neutral-200 text-right break-all">{row.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
