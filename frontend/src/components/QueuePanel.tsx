import { useState } from "react";
import type {
  JobStatusResponse,
  QueueItem,
  QueueSortKey,
  QueueStateResponse,
} from "../types";
import { IconChevronDown, IconChevronUp, IconClock, IconGrip, IconRetry, IconX } from "./icons";

interface Props {
  state: QueueStateResponse | null;
  /** Live /api/status — shown inline on the running item. */
  jobStatus: JobStatusResponse | null;
  /** All queue actions return an error message, or null on success. The
   * message is shown inline here — a failure must never look like a no-op. */
  onReorder: (ids: string[]) => Promise<string | null>;
  onSort: (key: QueueSortKey, order: "asc" | "desc") => Promise<string | null>;
  onDelete: (id: string) => Promise<string | null>;
  onRetry: (id: string) => Promise<string | null>;
  onTogglePause: () => Promise<string | null>;
  onClearFinished: () => Promise<string | null>;
  /** Open a finished item's transcript (by history id). */
  onOpenResult: (historyId: string) => void;
}

const BADGE: Record<QueueItem["status"], { label: string; cls: string }> = {
  queued: { label: "QUEUED", cls: "text-faint border-white/20" },
  running: { label: "RUNNING", cls: "text-white border-white/60 animate-pulse-block" },
  done: { label: "DONE", cls: "text-emerald-400 border-emerald-400/40" },
  error: { label: "ERROR", cls: "text-red-400 border-red-400/40" },
  cancelled: { label: "SKIPPED", cls: "text-amber-400 border-amber-400/40" },
};

function fmtMB(bytes: number): string {
  return (bytes / 1048576).toFixed(1) + " MB";
}

/**
 * Batch queue side panel: status per item, native HTML5 drag reordering
 * (queued items only — the backend enforces this too) with keyboard-accessible
 * move up/down buttons, one-shot rule sort, pause/resume and finished-items
 * cleanup.
 */
export function QueuePanel({
  state,
  jobStatus,
  onReorder,
  onSort,
  onDelete,
  onRetry,
  onTogglePause,
  onClearFinished,
  onOpenResult,
}: Props) {
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
  // Two-step confirm for deleting the RUNNING item (it aborts in-flight GPU
  // work — the destructive counterpart of removing a merely-queued item).
  const [confirmRunningId, setConfirmRunningId] = useState<string | null>(null);
  // Last failed queue action, shown inline so the failure is visible.
  const [actionError, setActionError] = useState<string | null>(null);

  const items = state?.items ?? [];
  const queuedIds = items.filter((it) => it.status === "queued").map((it) => it.id);
  const doneCount = items.filter((it) => it.status === "done").length;
  const terminalCount = items.filter(
    (it) => it.status !== "queued" && it.status !== "running",
  ).length;

  // Route every action through here so its failure lands in the inline error.
  const run = (action: Promise<string | null>) => {
    setActionError(null);
    void action.then((err) => {
      if (err) setActionError(err);
    });
  };

  const dropOn = (targetId: string) => {
    if (!dragId || dragId === targetId) return;
    const ids = queuedIds.filter((i) => i !== dragId);
    const at = ids.indexOf(targetId);
    if (at < 0) return;
    ids.splice(at, 0, dragId);
    run(onReorder(ids));
  };

  /** Keyboard-accessible reordering: move a queued item one slot up/down. */
  const moveItem = (id: string, dir: -1 | 1) => {
    const ids = [...queuedIds];
    const at = ids.indexOf(id);
    const to = at + dir;
    if (at < 0 || to < 0 || to >= ids.length) return;
    ids.splice(at, 1);
    ids.splice(to, 0, id);
    run(onReorder(ids));
  };

  if (!state || items.length === 0) {
    return (
      <p className="font-mono text-xs text-muted">
        Queue is empty. Use Upload → Batch to enqueue a folder.
      </p>
    );
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <span className="font-mono text-xs text-muted">
          {doneCount}/{items.length} done
        </span>
        <div className="flex-1" />
        <select
          onChange={(e) => {
            const v = e.target.value;
            if (!v) return;
            const [key, order] = v.split(":") as [QueueSortKey, "asc" | "desc"];
            run(onSort(key, order));
            e.target.value = "";
          }}
          defaultValue=""
          className="bg-white/5 border border-white/15 rounded-lg px-2 py-1 font-mono text-[11px] text-muted"
          aria-label="Sort queued items"
        >
          <option value="" disabled>
            Sort…
          </option>
          <option value="name:asc">Name A→Z</option>
          <option value="name:desc">Name Z→A</option>
          <option value="size:asc">Size ↑</option>
          <option value="size:desc">Size ↓</option>
          <option value="added_at:asc">Oldest first</option>
          <option value="added_at:desc">Newest first</option>
        </select>
        <button
          onClick={() => run(onTogglePause())}
          className="btn-ghost btn-ghost-sm"
          title={state.paused ? "Resume the queue" : "Pause after the current item"}
        >
          {state.paused ? "Resume" : "Pause"}
        </button>
        <button
          onClick={() => run(onClearFinished())}
          disabled={terminalCount === 0}
          className="btn-ghost btn-ghost-sm"
          title="Remove done / failed / skipped items"
        >
          Clear done
        </button>
      </div>

      {state.paused && (
        <p className="font-mono text-[11px] text-amber-400 mb-2">
          Queue paused — the current item finishes, then processing stops.
        </p>
      )}

      {actionError && (
        <p role="alert" className="font-mono text-xs text-red-400 mb-2 break-words">
          {actionError}
        </p>
      )}

      {/* Items */}
      <ul className="space-y-1.5">
        {items.map((item) => {
          const badge = BADGE[item.status];
          const draggable = item.status === "queued";
          const isRunning = item.status === "running";
          const queuedIndex = queuedIds.indexOf(item.id);
          return (
            <li
              key={item.id}
              draggable={draggable}
              onDragStart={() => draggable && setDragId(item.id)}
              onDragEnd={() => {
                setDragId(null);
                setOverId(null);
              }}
              onDragOver={(e) => {
                if (dragId && draggable) {
                  e.preventDefault();
                  setOverId(item.id);
                }
              }}
              onDrop={(e) => {
                e.preventDefault();
                if (draggable) dropOn(item.id);
                setDragId(null);
                setOverId(null);
              }}
              onClick={() => {
                if (item.status === "done" && item.history_id) {
                  onOpenResult(item.history_id);
                }
              }}
              role={item.status === "done" && item.history_id ? "button" : undefined}
              tabIndex={item.status === "done" && item.history_id ? 0 : undefined}
              onKeyDown={(e) => {
                if (
                  (e.key === "Enter" || e.key === " ") &&
                  item.status === "done" &&
                  item.history_id
                ) {
                  e.preventDefault();
                  onOpenResult(item.history_id);
                }
              }}
              className={`group px-3 py-2 rounded-xl border transition-all ${
                overId === item.id && dragId !== item.id
                  ? "border-white/50 bg-white/10"
                  : isRunning
                    ? "border-white/25 bg-white/10"
                    : "border-white/10 bg-white/[0.04]"
              } ${item.status === "done" && item.history_id ? "cursor-pointer hover:border-white/25" : ""}`}
            >
              <div className="flex items-center gap-2">
                {draggable && (
                  <span
                    className="cursor-grab text-dim group-hover:text-muted select-none"
                    title="Drag to reorder, or use the arrow buttons"
                  >
                    <IconGrip className="w-3.5 h-3.5" />
                  </span>
                )}
                <span
                  className={`font-mono text-2xs px-1.5 py-0.5 border rounded ${badge.cls}`}
                >
                  {badge.label}
                </span>
                <span
                  className="font-mono text-xs text-white truncate flex-1"
                  title={item.filename}
                >
                  {item.filename}
                </span>
                <span className="font-mono text-2xs text-faint shrink-0">
                  {fmtMB(item.size)}
                </span>
                {item.align && (
                  <span
                    className="text-dim shrink-0"
                    title="Word-level alignment enabled"
                  >
                    <IconClock className="w-3 h-3" />
                  </span>
                )}
                {draggable && (
                  <span className="flex items-center shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        moveItem(item.id, -1);
                      }}
                      disabled={queuedIndex <= 0}
                      aria-label={`Move ${item.filename} up in the queue`}
                      className="btn-icon"
                      title="Move up"
                    >
                      <IconChevronUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        moveItem(item.id, 1);
                      }}
                      disabled={queuedIndex < 0 || queuedIndex >= queuedIds.length - 1}
                      aria-label={`Move ${item.filename} down in the queue`}
                      className="btn-icon"
                      title="Move down"
                    >
                      <IconChevronDown className="w-3.5 h-3.5" />
                    </button>
                  </span>
                )}
                {(item.status === "error" || item.status === "cancelled") && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      run(onRetry(item.id));
                    }}
                    aria-label={`Retry ${item.filename}`}
                    className="btn-icon shrink-0"
                    title="Retry"
                  >
                    <IconRetry className="w-3.5 h-3.5" />
                  </button>
                )}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    // Deleting the running item aborts its in-flight
                    // transcription — arm first, fire on the second click.
                    if (isRunning && confirmRunningId !== item.id) {
                      setConfirmRunningId(item.id);
                      return;
                    }
                    setConfirmRunningId(null);
                    run(onDelete(item.id));
                  }}
                  onBlur={() => setConfirmRunningId(null)}
                  className={`btn-icon shrink-0 ${
                    isRunning && confirmRunningId === item.id
                      ? "text-2xs uppercase tracking-widest text-red-400 bg-red-500/10"
                      : "hover:text-red-400"
                  }`}
                  title={
                    isRunning
                      ? confirmRunningId === item.id
                        ? "Click again to confirm — progress on this item is discarded"
                        : "Skip current item"
                      : "Remove"
                  }
                  aria-label={`${isRunning ? "Skip" : "Remove"} ${item.filename}`}
                >
                  {isRunning && confirmRunningId === item.id ? (
                    "Skip?"
                  ) : (
                    <IconX className="w-3.5 h-3.5" />
                  )}
                </button>
              </div>
              {isRunning && jobStatus && (
                <p
                  className="font-mono text-2xs text-faint mt-1 truncate"
                  title={jobStatus.message}
                >
                  {jobStatus.message || "Working…"} ({jobStatus.progress}%)
                </p>
              )}
              {item.status === "error" && item.error && (
                <p
                  className="font-mono text-2xs text-red-400/80 mt-1 truncate"
                  title={item.error}
                >
                  {item.error}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
