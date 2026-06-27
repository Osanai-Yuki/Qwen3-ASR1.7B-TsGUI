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
    <div>
      <h2 className="font-mono text-xs uppercase tracking-widest text-white/45 mb-4">
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
            className="btn-ghost py-2.5 font-mono text-xs font-bold uppercase"
          >
            {f.label}
          </button>
        ))}
      </div>
    </div>
  );
}
