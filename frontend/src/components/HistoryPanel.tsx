import { useState } from "react";
import type { AudioCacheInfo, HistoryItem } from "../types";
import { formatDateTime, formatDuration } from "../utils/format";
import { IconMusic, IconX } from "./icons";

interface Props {
  items: HistoryItem[];
  activeId: string | null;
  audioCache: AudioCacheInfo | null;
  /** Request full record by id; App performs the fetch and fills the panels.
   * Returns an error message (e.g. "a transcription is running") or null —
   * shown inline so a refused restore is never a silent no-op. */
  onRestore: (id: string) => Promise<string | null>;
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
  // Two-step per-item delete: first click arms, second click fires.
  // Resets on blur so a stray tab-away doesn't leave the destructive prompt armed.
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  // Two-step "Clear cache": deletes converted MP3s (regenerable only by
  // re-transcribing) and stops playback of the open transcript — same
  // arm-then-fire pattern as the other destructive actions.
  const [confirmCache, setConfirmCache] = useState(false);
  // Inline message when a restore is refused (e.g. while a job is running).
  const [restoreMsg, setRestoreMsg] = useState<string | null>(null);

  const restore = (id: string) => {
    void onRestore(id).then((msg) => setRestoreMsg(msg));
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-mono text-xs uppercase tracking-widest text-faint">
          History
          {items.length > 0 && (
            <span className="text-dim"> ({items.length})</span>
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
            className={`font-mono text-2xs uppercase tracking-widest transition-colors ${
              confirmClear ? "text-red-400" : "text-faint hover:text-red-400"
            }`}
          >
            {confirmClear ? "Confirm clear?" : "Clear all"}
          </button>
        )}
      </div>

      {restoreMsg && (
        <p role="status" className="font-mono text-[11px] text-amber-400 mb-3 break-words">
          {restoreMsg}
        </p>
      )}

      {cacheCount > 0 && (
        <div className="flex items-center justify-between mb-4 px-3 py-2 rounded-xl bg-white/5 border border-white/8">
          <span className="font-mono text-2xs text-muted">
            Cached audio: {cacheCount} file{cacheCount > 1 ? "s" : ""} ·{" "}
            {formatMB(cacheBytes)}
          </span>
          <button
            onClick={() => {
              if (confirmCache) {
                onClearAudioCache();
                setConfirmCache(false);
              } else {
                setConfirmCache(true);
              }
            }}
            onBlur={() => setConfirmCache(false)}
            className={`font-mono text-2xs uppercase tracking-widest transition-colors ${
              confirmCache ? "text-red-400" : "text-faint hover:text-red-400"
            }`}
            title={
              confirmCache
                ? "Click again to confirm — playback of the open transcript stops"
                : "Delete all cached converted audio"
            }
          >
            {confirmCache ? "Confirm clear?" : "Clear cache"}
          </button>
        </div>
      )}

      {items.length === 0 ? (
        <p className="font-mono text-xs text-muted">No history yet.</p>
      ) : (
        <ul className="space-y-1.5 max-h-72 overflow-y-auto">
          {items.map((item) => (
            <li
              key={item.id}
              role="button"
              tabIndex={0}
              aria-label={`Restore ${item.filename ?? "item"}`}
              onClick={() => restore(item.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  restore(item.id);
                }
              }}
              className={`group flex items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer transition-all border ${
                activeId === item.id
                  ? "border-white/25 bg-white/12"
                  : "border-transparent hover:border-white/12 hover:bg-white/6"
              }`}
            >
              <div className="flex-1 min-w-0">
                <p className="font-mono text-xs font-bold text-white truncate flex items-center gap-1">
                  <span className="truncate" title={item.filename ?? undefined}>
                    {item.filename ?? "unknown"}
                  </span>
                  {item.audio_cache_name && (
                    <span
                      className="text-amber-400 shrink-0"
                      title="converted MP3 cached"
                    >
                      <IconMusic className="w-3 h-3" />
                    </span>
                  )}
                </p>
                <p className="font-mono text-2xs text-muted mt-0.5">
                  {formatDateTime(item.created_at)} ·{" "}
                  {formatDuration(item.duration)} · {item.segment_count} seg
                  {item.align_used && " · aligned"}
                </p>
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  if (confirmDeleteId === item.id) {
                    onDelete(item.id);
                    setConfirmDeleteId(null);
                  } else {
                    setConfirmDeleteId(item.id);
                  }
                }}
                onBlur={() => setConfirmDeleteId(null)}
                className={`btn-icon shrink-0 ${
                  confirmDeleteId === item.id
                    ? "text-2xs uppercase tracking-widest text-red-400 bg-red-500/10"
                    : "group-hover:text-muted hover:text-red-400"
                }`}
                title={
                  confirmDeleteId === item.id
                    ? "Confirm delete"
                    : "Delete"
                }
                aria-label={
                  confirmDeleteId === item.id
                    ? `Confirm delete ${item.filename ?? "item"}`
                    : `Delete ${item.filename ?? "item"}`
                }
              >
                {confirmDeleteId === item.id ? (
                  "Delete?"
                ) : (
                  <IconX className="w-3.5 h-3.5" />
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
