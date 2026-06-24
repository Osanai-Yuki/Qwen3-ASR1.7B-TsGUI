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

  return (
    <div className="border border-neutral-800 p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500">
          History
          {items.length > 0 && (
            <span className="text-neutral-600"> ({items.length})</span>
          )}
        </h2>
        {items.length > 0 && (
          <button
            onClick={onClear}
            className="font-mono text-[10px] uppercase tracking-widest text-neutral-500 hover:text-red-500 transition-colors"
          >
            Clear all
          </button>
        )}
      </div>

      {cacheCount > 0 && (
        <div className="flex items-center justify-between mb-4 px-3 py-2 border border-neutral-800 bg-neutral-950">
          <span className="font-mono text-[10px] text-neutral-500">
            Cached audio: {cacheCount} file{cacheCount > 1 ? "s" : ""} ·{" "}
            {formatMB(cacheBytes)}
          </span>
          <button
            onClick={onClearAudioCache}
            className="font-mono text-[10px] uppercase tracking-widest text-neutral-500 hover:text-red-500 transition-colors"
          >
            Clear cache
          </button>
        </div>
      )}

      {items.length === 0 ? (
        <p className="font-mono text-xs text-neutral-600">No history yet.</p>
      ) : (
        <ul className="space-y-1 max-h-72 overflow-y-auto">
          {items.map((item) => (
            <li
              key={item.id}
              onClick={() => onRestore(item.id)}
              className={`group flex items-center gap-3 px-3 py-2 border cursor-pointer transition-colors ${
                activeId === item.id
                  ? "border-neutral-500 bg-neutral-900"
                  : "border-transparent hover:border-neutral-800 hover:bg-neutral-950"
              }`}
            >
              <div className="flex-1 min-w-0">
                <p className="font-mono text-xs font-bold text-neutral-200 truncate flex items-center gap-1">
                  <span className="truncate">{item.filename ?? "unknown"}</span>
                  {item.audio_cache_name && (
                    <span
                      className="text-amber-600 shrink-0"
                      title="converted MP3 cached"
                    >
                      ♪
                    </span>
                  )}
                </p>
                <p className="font-mono text-[10px] text-neutral-600">
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
                className="font-mono text-xs text-neutral-700 group-hover:text-neutral-400 hover:!text-red-500 transition-colors"
                title="Delete"
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
