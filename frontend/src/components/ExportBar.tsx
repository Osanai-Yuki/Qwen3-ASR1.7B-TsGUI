import type { Segment, Stats } from "../types";
import { SUBTITLE_FORMATS, downloadSubtitle } from "../utils/subtitle";

interface Props {
  text: string;
  segments: Segment[];
  stats: Stats | null;
  baseName: string;
  disabled: boolean;
}

/** Multi-format export buttons (SRT / VTT / ASS / TXT / JSON). */
export function ExportBar({
  text,
  segments,
  stats,
  baseName,
  disabled,
}: Props) {
  const name = baseName || "transcript";

  return (
    <div className="border border-neutral-800 p-6">
      <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500 mb-4">
        Export
      </h2>
      <div className="grid grid-cols-5 gap-2">
        {SUBTITLE_FORMATS.map((f) => (
          <button
            key={f.id}
            disabled={disabled}
            onClick={() =>
              downloadSubtitle(f.id, text, segments, stats, name)
            }
            className="py-2 font-mono text-xs font-bold uppercase border border-neutral-700 text-neutral-300 hover:border-white hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            {f.label}
          </button>
        ))}
      </div>
    </div>
  );
}
