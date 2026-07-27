import { useState } from "react";
import type { AudioCacheInfo, HistoryItem } from "../types";
import { formatDateTime, formatDuration } from "../utils/format";

interface Props {
  items: HistoryItem[];
  activeId: string | null;
  audioCache: AudioCacheInfo | null;
  /** Request full record by id; App performs the fetch and fills the panels. */
  onRestore: (id: string) => void;
  onDelete: (id: string) => void;
  onClear: () => void;
  onClearAudioCache: () => void;
}

function formatMB(bytes: number): string {
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

/** History list with restore-on-click, per-item delete, clear-all, and
 * converted-audio cache management. */
export function HistoryPanel({
  items,
  activeId,
  audioCache,
  onRestore,
  onDelete,
  onClear,
  onClearAudioCache,
}: Props) {
  const cacheCount = audioCache?.count ?? 0;
  const cacheBytes = audioCache?.size_bytes ?? 0;
  // Two-step "Clear all": first click arms confirmation, second click fires.
  // Resets on blur so a stray tab-away doesn't leave the destructive prompt armed.
  const [confirmClear, setConfirmClear] = useState(false);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-mono text-xs uppercase tracking-widest text-white/45">
          History
          {items.length > 0 && (
            <span className="text-white/35"> ({items.length})</span>
          )}
        </h2>
        {items.length > 0 && (
          <button
            onClick={() => {
              if (confirmClear) {
                onClear();
                setConfirmClear(false);
              } else {
                setConfirmClear(true);
              }
            }}
            onBlur={() => setConfirmClear(false)}
            className={`font-mono text-[10px] uppercase tracking-widest transition-colors ${
              confirmClear ? "text-red-400" : "text-white/45 hover:text-red-400"
            }`}
          >
            {confirmClear ? "Confirm clear?" : "Clear all"}
          </button>
        )}
      </div>

      {cacheCount > 0 && (
        <div className="flex items-center justify-between mb-4 px-3 py-2 rounded-xl bg-white/5 border border-white/8">
          <span className="font-mono text-[10px] text-white/55">
            Cached audio: {cacheCount} file{cacheCount > 1 ? "s" : ""} ·{" "}
            {formatMB(cacheBytes)}
          </span>
          <button
            onClick={onClearAudioCache}
            className="font-mono text-[10px] uppercase tracking-widest text-white/45 hover:text-red-400 transition-colors"
          >
            Clear cache
          </button>
        </div>
      )}

      {items.length === 0 ? (
        <p className="font-mono text-xs text-white/40">No history yet.</p>
      ) : (
        <ul className="space-y-1.5 max-h-72 overflow-y-auto">
          {items.map((item) => (
            <li
              key={item.id}
              onClick={() => onRestore(item.id)}
              className={`group flex items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer transition-all border ${
                activeId === item.id
                  ? "border-white/25 bg-white/12"
                  : "border-transparent hover:border-white/12 hover:bg-white/6"
              }`}
            >
              <div className="flex-1 min-w-0">
                <p className="font-mono text-xs font-bold text-white/90 truncate flex items-center gap-1">
                  <span className="truncate">{item.filename ?? "unknown"}</span>
                  {item.audio_cache_name && (
                    <span
                      className="text-amber-400 shrink-0"
                      title="converted MP3 cached"
                    >
                      ♪
                    </span>
                  )}
                </p>
                <p className="font-mono text-[10px] text-white/40 mt-0.5">
                  {formatDateTime(item.created_at)} ·{" "}
                  {formatDuration(item.duration)} · {item.segment_count} seg
                  {item.align_used && " · aligned"}
                </p>
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(item.id);
                }}
                className="font-mono text-xs text-white/30 group-hover:text-white/60 hover:!text-red-400 transition-colors p-2 -m-2 rounded-lg"
                title="Delete"
                aria-label={`Delete ${item.filename ?? "item"}`}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
