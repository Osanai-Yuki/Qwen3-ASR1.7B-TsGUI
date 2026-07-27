import { useCallback, useEffect, useRef } from "react";
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

/** Timestamped transcript display with optional segment highlighting.
 *
 * Wide, centered, readable layout: body sans-serif at a comfortable size with
 * generous leading; mono timestamps kept compact. The active line highlights
 * as audio plays and auto-scrolls into view — but only when the user is not
 * manually scrolling. After the user stops scrolling, a short delay re-enables
 * the auto-follow so the view catches up with playback naturally.
 */
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
  // True while the user is actively dragging/scrolling the list. During this
  // window we suppress the auto-scroll effect so the user has full control.
  const isUserScrolling = useRef(false);
  // Timer that clears isUserScrolling after the user stops scrolling.
  const scrollTimer = useRef<number | null>(null);

  const scrollActiveIntoView = useCallback(() => {
    if (activeRef.current && !isUserScrolling.current) {
      activeRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, []);

  // Re-enable auto-scroll shortly after the user stops scrolling.
  const onScroll = useCallback(() => {
    isUserScrolling.current = true;
    if (scrollTimer.current !== null) window.clearTimeout(scrollTimer.current);
    scrollTimer.current = window.setTimeout(() => {
      isUserScrolling.current = false;
      scrollActiveIntoView();
    }, 1200);
  }, [scrollActiveIntoView]);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      if (scrollTimer.current !== null) window.clearTimeout(scrollTimer.current);
    };
  }, [onScroll]);

  // Auto-scroll on currentTime change — but only when the user is not manually
  // scrolling (the onScroll handler gates via isUserScrolling).
  useEffect(() => {
    scrollActiveIntoView();
  }, [currentTime, scrollActiveIntoView]);

  // When audio is paused (currentTime null), always allow scroll-into-view so a
  // click-to-seek jumps to the right line immediately.
  useEffect(() => {
    if (currentTime === null) {
      isUserScrolling.current = false;
      scrollActiveIntoView();
    }
  }, [currentTime, scrollActiveIntoView]);

  // Segments are sorted by start; the active line is the last one whose start
  // has been reached. Using start<=time (not start<=time<end) keeps a line
  // highlighted through gaps between segments instead of dropping the
  // highlight whenever playback sits in a pause between lines.
  const activeIndex = (() => {
    if (currentTime === null) return -1;
    let idx = -1;
    for (let i = 0; i < segments.length; i++) {
      if (segments[i].start <= currentTime) idx = i;
      else break;
    }
    return idx;
  })();

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between px-6 sm:px-10 lg:px-16 py-4 border-b border-white/8">
        <h2 className="font-mono text-xs uppercase tracking-widest text-white/45">
          Transcript
        </h2>
        {alignerUsed && (
          <span className="font-mono text-[11px] uppercase tracking-widest text-amber-400/90 border border-amber-400/30 px-2 py-0.5 rounded-full bg-amber-400/10">
            Word-aligned
          </span>
        )}
      </div>

      <div ref={listRef} className="flex-1 overflow-y-auto min-h-0 overscroll-contain">
        {empty ? (
          <div className="h-full flex flex-col items-center justify-center px-6 text-center animate-fade-in">
            <p className="font-sans text-xl text-white/50 max-w-md">
              {text || "No transcript yet."}
            </p>
            <p className="font-mono text-sm text-white/30 mt-3">
              Upload audio or video to begin transcription.
            </p>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto px-6 sm:px-10 lg:px-16 py-8 space-y-1.5">
            {segments.map((seg, i) => {
              const isActive = i === activeIndex;
              return (
                <div
                  key={i}
                  ref={isActive ? activeRef : undefined}
                  onClick={() => onSeek?.(seg.start)}
                  className={`group grid grid-cols-[auto_1fr] gap-x-4 sm:gap-x-6 px-4 sm:px-5 py-3.5 -mx-4 sm:-mx-5 rounded-2xl cursor-pointer transition-all duration-200 ${
                    isActive
                      ? "bg-white text-black shadow-lg shadow-white/10 scale-[1.01] border border-white/20"
                      : "hover:bg-white/8 text-white/85 border border-transparent"
                  }`}
                >
                  <span
                    className={`font-mono text-sm tabular-nums whitespace-nowrap pt-1.5 transition-colors ${
                      isActive
                        ? "text-black/45"
                        : "text-white/40 group-hover:text-white/60"
                    }`}
                  >
                    {formatShort(seg.start)}
                  </span>
                  <span
                    className={`font-sans text-lg sm:text-xl leading-loose ${
                      isActive ? "font-medium" : ""
                    }`}
                  >
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
