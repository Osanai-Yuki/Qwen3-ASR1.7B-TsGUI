import { useEffect, useRef } from "react";
import type { Segment } from "../types";
import { formatShort } from "../utils/format";

interface Props {
  segments: Segment[];
  text: string;
  alignerUsed: boolean;
  /** Current audio playback time (seconds). Null if no audio loaded. */
  currentTime: number | null;
  /** Called when a transcript line is clicked; passes the segment start time. */
  onSeek?: (time: number) => void;
}

/** Timestamped transcript display with optional segment highlighting. */
export function TranscriptPanel({
  segments,
  text,
  alignerUsed,
  currentTime,
  onSeek,
}: Props) {
  const empty = segments.length === 0;
  const listRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the active segment
  useEffect(() => {
    if (activeRef.current) {
      activeRef.current.scrollIntoView({
        block: "center",
        behavior: "smooth",
      });
    }
  }, [currentTime]);

  // Find the index of the segment that contains currentTime
  const activeIndex =
    currentTime !== null
      ? segments.findIndex(
          (s) => currentTime >= s.start && currentTime < s.end,
        )
      : -1;

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

      <div ref={listRef} className="flex-1 overflow-y-auto px-6 py-4 min-h-0">
        {empty ? (
          <p className="font-mono text-sm text-neutral-600">
            {text || "No transcript yet. Upload audio to begin."}
          </p>
        ) : (
          <div className="space-y-1">
            {segments.map((seg, i) => {
              const isActive = i === activeIndex;
              return (
                <div
                  key={i}
                  ref={isActive ? activeRef : undefined}
                  onClick={() => onSeek?.(seg.start)}
                  className={`grid grid-cols-[auto_1fr] gap-4 px-3 py-2 -mx-3 cursor-pointer transition-colors duration-150 ${
                    isActive
                      ? "bg-white text-black"
                      : "hover:bg-neutral-900 text-neutral-100"
                  }`}
                >
                  <span
                    className={`font-mono text-xs tabular-nums whitespace-nowrap pt-0.5 ${
                      isActive ? "text-neutral-500" : "text-neutral-600"
                    }`}
                  >
                    {formatShort(seg.start)}
                  </span>
                  <span className="font-mono text-sm leading-relaxed">
                    {seg.text}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
