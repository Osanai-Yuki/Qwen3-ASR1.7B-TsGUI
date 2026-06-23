import type { Segment } from "../types";
import { formatShort } from "../utils/format";

interface Props {
  segments: Segment[];
  text: string;
  alignerUsed: boolean;
}

/** Timestamped transcript display. Each segment is a row with its time range. */
export function TranscriptPanel({ segments, text, alignerUsed }: Props) {
  const empty = segments.length === 0;

  return (
    <div className="border border-neutral-800 flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between px-6 py-4 border-b border-neutral-800">
        <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500">
          Transcript
        </h2>
        {alignerUsed && (
          <span className="font-mono text-[10px] uppercase tracking-widest text-neutral-600 border border-neutral-800 px-2 py-0.5">
            Word-aligned
          </span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-4 min-h-0">
        {empty ? (
          <p className="font-mono text-sm text-neutral-600">
            {text || "No transcript yet. Upload audio to begin."}
          </p>
        ) : (
          <div className="space-y-3">
            {segments.map((seg, i) => (
              <div
                key={i}
                className="grid grid-cols-[auto_1fr] gap-4 font-mono text-sm leading-relaxed"
              >
                <span className="text-neutral-600 tabular-nums whitespace-nowrap pt-0.5">
                  {formatShort(seg.start)}
                </span>
                <span className="text-neutral-100">{seg.text}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
