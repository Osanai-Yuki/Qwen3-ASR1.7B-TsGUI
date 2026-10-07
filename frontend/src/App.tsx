import { useCallback, useEffect, useRef, useState } from "react";
import {
  abortJob,
  clearAudioCache,
  clearFinishedQueue,
  clearHistory,
  deleteHistory,
  deleteQueueItem,
  enqueueFiles,
  fetchAudioCache,
  fetchHealth,
  fetchHistory,
  fetchHistoryRecord,
  pauseQueue,
  reorderQueue,
  resumeQueue,
  retryQueueItem,
  sortQueue,
  transcribe,
} from "./api";
import { useJobStatus } from "./hooks/useJobStatus";
import { useQueue } from "./hooks/useQueue";
import { useReadiness } from "./hooks/useReadiness";
import {
  isApiError,
  type AudioCacheInfo,
  type HealthResponse,
  type HistoryItem,
  type QueueSortKey,
  type Segment,
  type Stats,
} from "./types";
import { AudioPlayer } from "./components/AudioPlayer";
import { BatchUploadZone } from "./components/BatchUploadZone";
import { BootOverlay } from "./components/BootOverlay";
import { ExportBar } from "./components/ExportBar";
import { HistoryPanel } from "./components/HistoryPanel";
import {
  IconChart,
  IconDownload,
  IconHistory,
  IconList,
  IconPlay,
  IconPlus,
  IconX,
} from "./components/icons";
import { ModelInfo } from "./components/ModelInfo";
import { ProgressBar } from "./components/ProgressBar";
import { QueuePanel } from "./components/QueuePanel";
import { StatsPanel } from "./components/StatsPanel";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { UploadZone } from "./components/UploadZone";
import { validateFile } from "./utils/fileTypes";
import { createAudioClock } from "./utils/audioClock";

const UPLOAD_TABS = ["single", "batch"] as const;
type UploadTab = (typeof UPLOAD_TABS)[number];

export default function App() {
  const { state: readiness, ready, switching, failed, restart } = useReadiness();
  const [health, setHealth] = useState<HealthResponse | null>(null);

  // Working state
  const [working, setWorking] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  // Batch queue state (backend-persisted; adaptive polling via useQueue)
  const { state: queueState, refresh: refreshQueue } = useQueue();
  const queueItems = queueState?.items ?? [];
  const activeQueueItem =
    queueItems.find((it) => it.id === queueState?.active_id) ?? null;
  const queueActive = activeQueueItem !== null;
  const queuePendingCount = queueItems.filter((it) => it.status === "queued").length;
  const queueDoneCount = queueItems.filter((it) => it.status === "done").length;
  // A job slot is occupied either by a manual upload or the queue worker.
  const busy = working || queueActive;

  const { status: jobStatus } = useJobStatus(busy);

  // Result state
  const [text, setText] = useState("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [baseName, setBaseName] = useState("transcript");
  const [error, setError] = useState<string | null>(null);
  // Non-error notice (e.g. a user-initiated cancel) — amber, not red.
  const [notice, setNotice] = useState<string | null>(null);
  // Two-step "Skip" confirm for the running queue item (first click arms,
  // second click fires) — mirrors the destructive-action pattern in
  // HistoryPanel so a stray click can't discard in-progress GPU work.
  const [confirmSkip, setConfirmSkip] = useState(false);

  // Audio playback state. Playback time lives in a shared clock (utils/
  // audioClock) rather than React state: timeupdate fires ~4×/second and
  // state here would re-render the entire tree on every tick. TranscriptPanel
  // subscribes to the clock directly; seekTo stays as state (user-initiated,
  // rare).
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const audioUrlRef = useRef(audioUrl);
  const [audioClock] = useState(createAudioClock);
  const [seekTo, setSeekTo] = useState<number | null>(null);

  // History + cache state
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([]);
  const [audioCache, setAudioCache] = useState<AudioCacheInfo | null>(null);

  // Overlay panels. Upload modal opens by default on entry (after boot) so
  // the user can immediately drop a file; it can be dismissed freely.
  const [uploadOpen, setUploadOpen] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [statsOpen, setStatsOpen] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  const [uploadTab, setUploadTab] = useState<"single" | "batch">("single");
  // Accumulated batch selection. Owned here (not inside BatchUploadZone) so a
  // second drop appends to the first instead of remounting the picker and
  // discarding it. Cleared when the upload modal opens and on successful enqueue.
  const [picked, setPicked] = useState<File[]>([]);
  // True while files are dragged over the window — drives the drop hint overlay.
  const [dragActive, setDragActive] = useState(false);

  // Once ready, fetch health (model + aligner info) and history list.
  useEffect(() => {
    if (!ready || switching) return;
    fetchHealth().then(setHealth).catch(() => {});
    refreshHistory();
    refreshAudioCache();
  }, [ready, switching]);

  // Disarm the Skip confirm whenever the running queue item changes (or the
  // queue goes idle), so a stale armed state never carries to the next item.
  const activeQueueItemId = activeQueueItem?.id ?? null;
  useEffect(() => {
    setConfirmSkip(false);
  }, [activeQueueItemId]);

  const refreshHistory = useCallback(() => {
    fetchHistory().then((r) => setHistoryItems(r.items)).catch(() => {});
  }, []);

  const refreshAudioCache = useCallback(() => {
    fetchAudioCache().then((c) => setAudioCache(c)).catch(() => {});
  }, []);

  const clearAudioUrl = useCallback(() => {
    setAudioUrl((prev) => {
      if (prev && prev.startsWith("blob:")) URL.revokeObjectURL(prev);
      return null;
    });
    audioClock.set(null);
  }, [audioClock]);

  const setCacheAudioUrl = useCallback((cacheName: string) => {
    if (!/^[A-Za-z0-9._-]+$/.test(cacheName)) {
      clearAudioUrl();
      setError("Invalid audio cache name");
      return;
    }
    setAudioUrl((prev) => {
      if (prev && prev.startsWith("blob:")) URL.revokeObjectURL(prev);
      return `/api/audio-cache/${encodeURIComponent(cacheName)}`;
    });
    audioClock.set(null);
  }, [clearAudioUrl, audioClock]);

  /** Push playback ticks into the shared clock (see audioClock.ts). */
  const handleTimeUpdate = useCallback(
    (t: number) => audioClock.set(t),
    [audioClock],
  );

  /** Load a history record into the main view (shared by history restore,
   * queue auto-show and queue item clicks). */
  const loadRecord = useCallback(
    async (id: string) => {
      try {
        const rec = await fetchHistoryRecord(id);
        if (isApiError(rec)) {
          setError(rec.detail || rec.error);
          return;
        }
        setText(rec.text);
        setSegments(rec.segments);
        setStats(rec.stats);
        setHistoryId(rec.id);
        setError(null);
        setNotice(null);
        setBaseName((rec.filename || "transcript").replace(/\.[^.]+$/, ""));
        if (rec.audio_cache_name) setCacheAudioUrl(rec.audio_cache_name);
        else clearAudioUrl();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [clearAudioUrl, setCacheAudioUrl],
  );

  const handleTranscribe = useCallback(
    async (file: File, align: boolean) => {
      setUploadOpen(false);
      setWorking(true);
      setCancelling(false);
      setError(null);
      setNotice(null);
      setText("");
      setSegments([]);
      setStats(null);
      setHistoryId(null);
      audioClock.set(null);
      setBaseName((file.name || "transcript").replace(/\.[^.]+$/, ""));

      clearAudioUrl();
      const url = URL.createObjectURL(file);
      setAudioUrl(url);

      try {
        const res = await transcribe(file, align);
        if (isApiError(res)) {
          setError(res.detail || res.error);
        } else {
          setText(res.text);
          setSegments(res.segments);
          setStats(res.stats);
          setHistoryId(res.history_id);
          const cacheName = res.stats?.audio_cache_name ?? null;
          if (cacheName) setCacheAudioUrl(cacheName);
          // Backend stopped at a chunk boundary and kept the partial transcript.
          // A user-initiated cancel is not an error — route it to the amber
          // notice channel instead of the red error bar.
          if (res.cancelled) {
            setNotice("Cancelled — showing partial result.");
          }
          refreshHistory();
          refreshAudioCache();
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setWorking(false);
        setCancelling(false);
      }
    },
    [refreshHistory, refreshAudioCache, clearAudioUrl, setCacheAudioUrl, audioClock],
  );

  const handleCancel = useCallback(async () => {
    if (!working || cancelling) return;
    setCancelling(true);
    // Graceful cancel: the backend breaks at the next chunk boundary and
    // resolves /api/transcribe with the partial result, which we await so
    // nothing is lost. fire-and-forget the signal itself.
    abortJob().catch(() => {});
  }, [working, cancelling]);

  const handleRestore = useCallback(
    async (id: string): Promise<string | null> => {
      // Don't clobber an in-flight transcription's state — and say so instead
      // of silently ignoring the click (HistoryPanel shows the message inline).
      if (working) {
        return "A transcription is still running — wait for it to finish before restoring from history.";
      }
      await loadRecord(id);
      setHistoryOpen(false);
      return null;
    },
    [working, loadRecord],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      await deleteHistory(id);
      if (historyId === id) {
        setText("");
        setSegments([]);
        setStats(null);
        setHistoryId(null);
        clearAudioUrl();
      }
      refreshHistory();
    },
    [historyId, refreshHistory, clearAudioUrl],
  );

  const handleClear = useCallback(async () => {
    await clearHistory();
    setText("");
    setSegments([]);
    setStats(null);
    setHistoryId(null);
    clearAudioUrl();
    refreshHistory();
  }, [refreshHistory, clearAudioUrl]);

  const handleClearAudioCache = useCallback(async () => {
    await clearAudioCache();
    clearAudioUrl();
    refreshAudioCache();
  }, [clearAudioUrl, refreshAudioCache]);

  const handleSeek = useCallback((time: number) => {
    setSeekTo(time);
  }, []);

  // Batch queue actions
  // Returns null on success or an error message on failure. The message is
  // shown inline by BatchUploadZone (the global error bar sits behind the
  // upload modal), and mirrored to the global error channel so the
  // drop-on-window enqueue path (no modal open) still surfaces failures.
  const handleEnqueue = useCallback(
    async (files: File[], align: boolean): Promise<string | null> => {
      try {
        const res = await enqueueFiles(files, align);
        if (isApiError(res)) {
          const msg = res.detail || res.error;
          setError(msg);
          return msg;
        }
        setError(null);
        refreshQueue();
        return null;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        return msg;
      }
    },
    [refreshQueue],
  );

  /** Run a queue mutation, mapping endpoint errors and network failures to a
   * message (null on success). The message is mirrored to the global error
   * channel and returned so QueuePanel can show it inline — a failed action
   * must never look like a silent no-op. */
  const runQueueAction = useCallback(
    async (action: () => Promise<unknown>): Promise<string | null> => {
      try {
        const res = await action();
        if (isApiError(res)) {
          const msg = res.detail || res.error;
          setError(msg);
          return msg;
        }
        return null;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        return msg;
      }
    },
    [],
  );

  const handleQueueReorder = useCallback(
    async (ids: string[]) => {
      const err = await runQueueAction(() => reorderQueue(ids));
      refreshQueue();
      return err;
    },
    [runQueueAction, refreshQueue],
  );

  const handleQueueSort = useCallback(
    async (key: QueueSortKey, order: "asc" | "desc") => {
      const err = await runQueueAction(() => sortQueue(key, order));
      refreshQueue();
      return err;
    },
    [runQueueAction, refreshQueue],
  );

  const handleQueueDelete = useCallback(
    async (id: string) => {
      const err = await runQueueAction(() => deleteQueueItem(id));
      refreshQueue();
      return err;
    },
    [runQueueAction, refreshQueue],
  );

  const handleQueueRetry = useCallback(
    async (id: string) => {
      const err = await runQueueAction(() => retryQueueItem(id));
      refreshQueue();
      return err;
    },
    [runQueueAction, refreshQueue],
  );

  const handleQueueTogglePause = useCallback(async () => {
    const err = await runQueueAction(() =>
      queueState?.paused ? resumeQueue() : pauseQueue(),
    );
    refreshQueue();
    return err;
  }, [queueState?.paused, runQueueAction, refreshQueue]);

  const handleQueueClearFinished = useCallback(async () => {
    const err = await runQueueAction(() => clearFinishedQueue());
    refreshQueue();
    return err;
  }, [runQueueAction, refreshQueue]);

  const handleOpenQueueResult = useCallback(
    (hid: string) => {
      void loadRecord(hid);
      setQueueOpen(false);
    },
    [loadRecord],
  );

  // Auto-show queue results: when an item we previously saw in a non-done
  // state turns done, load its transcript — unless a manual job is running
  // or the user is inspecting something else (historyId changed by hand).
  const prevQueueStatusRef = useRef<Map<string, string>>(new Map());
  const lastAutoShownRef = useRef<string | null>(null);
  const historyIdRef = useRef<string | null>(null);
  useEffect(() => {
    historyIdRef.current = historyId;
  }, [historyId]);

  useEffect(() => {
    if (!queueState) return;
    const prev = prevQueueStatusRef.current;
    const finished = queueState.items.filter(
      (it) =>
        it.status === "done" &&
        it.history_id &&
        prev.has(it.id) &&
        prev.get(it.id) !== "done",
    );
    prevQueueStatusRef.current = new Map(
      queueState.items.map((it) => [it.id, it.status]),
    );
    if (finished.length === 0) return;
    refreshHistory();
    refreshAudioCache();
    const target = finished[finished.length - 1];
    const current = historyIdRef.current;
    if (!working && (current === null || current === lastAutoShownRef.current)) {
      lastAutoShownRef.current = target.history_id;
      void loadRecord(target.history_id!);
    }
  }, [queueState, working, refreshHistory, refreshAudioCache, loadRecord]);

  // Global drag & drop: one file while idle transcribes immediately; one
  // file while busy is enqueued; multiple files prefill the Batch tab.
  // A depth counter tracks dragenter/dragleave pairs so the drop-hint
  // overlay stays up while moving across child elements.
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => !!e.dataTransfer?.types.includes("Files");
    const onDragEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth += 1;
      setDragActive(true);
    };
    const onDragLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragActive(false);
    };
    const onDragOver = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault();
    };
    const onDrop = (e: DragEvent) => {
      depth = 0;
      setDragActive(false);
      if (e.defaultPrevented) return; // UploadZone already handled it
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length === 0) return;
      e.preventDefault();
      if (files.length === 1) {
        // Same client-side validation as the picker paths — the window drop
        // used to bypass the type/size checks entirely. (align stays off to
        // match the picker default; the Batch tab offers the toggle.)
        const err = validateFile(files[0]);
        if (err) {
          setError(err);
          return;
        }
        if (!busy) handleTranscribe(files[0], false);
        else void handleEnqueue(files, false);
        return;
      }
      // Append to the existing selection instead of replacing it — the old
      // code reset the list and remounted the picker on every drop, so only the
      // last drop's files ever reached the queue. `picked` is cleared on modal
      // open and after a successful enqueue, so this never accumulates stale
      // files. De-duplicate by name+size (re-picking the same folder is common).
      setPicked((prev) => {
        const seen = new Set(prev.map((f) => `${f.name}__${f.size}`));
        const next = [...prev];
        for (const f of files) {
          if (!seen.has(`${f.name}__${f.size}`)) next.push(f);
        }
        return next;
      });
      setUploadTab("batch");
      setUploadOpen(true);
    };
    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [busy, handleTranscribe, handleEnqueue]);

  useEffect(() => {
    audioUrlRef.current = audioUrl;
  }, [audioUrl]);

  useEffect(() => {
    return () => {
      const url = audioUrlRef.current;
      if (url && url.startsWith("blob:")) URL.revokeObjectURL(url);
    };
  }, []);

  // Screen-reader announcement when a busy stretch (manual job or queue)
  // ends with a transcript on screen. The live region is always mounted;
  // the text changes only on the busy→done transition.
  const wasBusyRef = useRef(false);
  useEffect(() => {
    wasBusyRef.current = busy;
  }, [busy]);

  if (!ready || switching) {
    return <BootOverlay state={readiness} failed={failed} onRetry={restart} />;
  }

  const hasResult = segments.length > 0 || text.length > 0;
  const completionAnnouncement =
    !busy && wasBusyRef.current && hasResult ? "Transcription complete." : "";
  const progress = jobStatus?.progress ?? 0;
  // While a modal/drawer is open the page behind it is inert: focus and
  // screen-reader navigation stay inside the dialog.
  const anyPanelOpen =
    uploadOpen || historyOpen || exportOpen || statsOpen || queueOpen;

  return (
    <div className="h-screen w-screen p-3 sm:p-4 lg:p-6 box-border">
      <div
        className="glass flex flex-col h-full text-white overflow-hidden rounded-3xl"
        inert={anyPanelOpen}
      >
      {/* Top toolbar */}
      <header className="shrink-0 z-20 border-b border-white/10">
        <div className="flex items-center gap-3 px-5 sm:px-8 py-3.5">
          {/* Brand + model info */}
          <div className="flex items-center gap-3 min-w-0">
            <h1 className="font-mono text-base font-black tracking-tight whitespace-nowrap">
              ASR
            </h1>
            <ModelInfo health={health} />
          </div>

          <div className="flex-1" />

          {/* Live batch progress chip */}
          {activeQueueItem && (
            <button
              onClick={() => setQueueOpen(true)}
              className="hidden md:flex items-center gap-2 font-mono text-[11px] text-muted border border-white/15 rounded-lg px-2.5 py-1.5 hover:border-white/40 transition-colors max-w-[240px]"
              title="Batch queue progress"
            >
              <IconPlay className="w-3 h-3 shrink-0 animate-pulse-block" />
              <span className="shrink-0">
                {queueDoneCount + 1}/{queueItems.length}
              </span>
              <span className="truncate" title={activeQueueItem.filename}>
                {activeQueueItem.filename}
              </span>
            </button>
          )}

          {/* Action buttons */}
          <button
            onClick={() => {
              setPicked([]); // fresh selection for a new upload session
              // While the queue is running the Single tab is inert — land on
              // Batch, the only tab that still accepts files.
              setUploadTab(busy ? "batch" : "single");
              setUploadOpen(true);
            }}
            disabled={working}
            className="btn-primary"
            aria-label="Upload"
          >
            <span className="hidden sm:inline">Upload</span>
            <IconPlus className="sm:hidden w-4 h-4" />
          </button>
          <button
            onClick={() => setQueueOpen(true)}
            className="btn-ghost relative"
            title="Batch queue"
            aria-label="Batch queue"
          >
            <span className="hidden sm:inline">Queue</span>
            <IconList className="sm:hidden w-4 h-4" />
            {queuePendingCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 min-w-[18px] h-[18px] px-1 rounded-full bg-white text-black font-mono text-2xs font-bold flex items-center justify-center">
                {queuePendingCount}
              </span>
            )}
          </button>
          <button
            onClick={() => setExportOpen(true)}
            disabled={!hasResult}
            className="btn-ghost"
            title="Export"
            aria-label="Export"
          >
            <span className="hidden sm:inline">Export</span>
            <IconDownload className="sm:hidden w-4 h-4" />
          </button>
          <button
            onClick={() => setStatsOpen(true)}
            disabled={!stats}
            className="btn-ghost"
            title="Statistics"
            aria-label="Statistics"
          >
            <span className="hidden sm:inline">Stats</span>
            <IconChart className="sm:hidden w-4 h-4" />
          </button>
          <button
            onClick={() => setHistoryOpen(true)}
            className="btn-ghost"
            title="History"
            aria-label="History"
          >
            <span className="hidden sm:inline">History</span>
            <IconHistory className="sm:hidden w-4 h-4" />
          </button>
        </div>

        {/* Thin progress / error bar */}
        {(busy || error) && (
          <div className="h-0.5 w-full bg-white/5 overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ease-out ${
                error ? "bg-red-500" : "bg-white animate-progress-glow"
              }`}
              style={{ width: `${error ? 100 : Math.max(2, progress)}%` }}
            />
          </div>
        )}
      </header>

      {/* Main: wide transcript */}
      <main className="flex-1 min-h-0 overflow-hidden">
        {/* Screen-reader completion announcement — always mounted so the
            live region exists before its text changes. */}
        <p className="sr-only" role="status">
          {completionAnnouncement}
        </p>
        {busy && (
          <div className="px-5 sm:px-8 py-2 border-b border-white/8 flex items-center justify-between gap-4 animate-fade-in">
            <span role="status" className="font-mono text-xs text-muted truncate">
              {queueActive
                ? `[${queueDoneCount + 1}/${queueItems.length}] ${activeQueueItem?.filename} — ${jobStatus?.message ?? "Working…"}`
                : cancelling
                  ? "Cancelling - stopping at next chunk…"
                  : (jobStatus?.message ?? "Working…")}
            </span>
            <span className="font-mono text-xs text-muted tabular-nums shrink-0">
              {jobStatus ? `${Math.round(jobStatus.progress)}%` : ""}
            </span>
            <div className="flex items-center gap-2 shrink-0">
              {queueActive ? (
                <>
                  <button
                    onClick={() => {
                      if (!activeQueueItem) return;
                      // Two-step: skipping aborts the in-flight transcription
                      // and discards its progress, so the first click only arms.
                      if (confirmSkip) {
                        handleQueueDelete(activeQueueItem.id);
                        setConfirmSkip(false);
                      } else {
                        setConfirmSkip(true);
                      }
                    }}
                    onBlur={() => setConfirmSkip(false)}
                    className={`btn-ghost btn-ghost-sm ${
                      confirmSkip ? "text-red-400 border-red-400/50" : ""
                    }`}
                    title={
                      confirmSkip
                        ? "Click again to confirm — progress on this item is discarded"
                        : "Skip the current item; the queue continues"
                    }
                  >
                    {confirmSkip ? "Confirm skip?" : "Skip"}
                  </button>
                  <button
                    onClick={handleQueueTogglePause}
                    className="btn-ghost btn-ghost-sm"
                    title={
                      queueState?.paused
                        ? "Resume the queue"
                        : "Pause the queue after this item"
                    }
                  >
                    {queueState?.paused ? "Resume" : "Pause"}
                  </button>
                </>
              ) : (
                <button
                  onClick={handleCancel}
                  disabled={cancelling}
                  className="btn-ghost btn-ghost-sm"
                  title="Cancel transcription"
                >
                  {cancelling ? "Cancelling…" : "Cancel"}
                </button>
              )}
            </div>
          </div>
        )}
        {error && (
          <div
            role="alert"
            className="px-5 sm:px-8 py-3 border-b border-red-500/30 bg-red-500/10 animate-fade-in"
          >
            <p className="font-mono text-sm text-red-300 break-words">{error}</p>
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="px-5 sm:px-8 py-3 border-b border-amber-500/30 bg-amber-500/10 animate-fade-in"
          >
            <p className="font-mono text-sm text-amber-300 break-words">{notice}</p>
          </div>
        )}
        {/* Post-completion call to action: jump straight to the next file */}
        {hasResult && !busy && (
          <div className="px-5 sm:px-8 py-2.5 border-b border-white/8 flex items-center gap-3 animate-fade-in">
            {queuePendingCount > 0 ? (
              <button
                onClick={() => setQueueOpen(true)}
                className="btn-primary py-1.5"
              >
                Queue pending · view →
              </button>
            ) : (
              <button
                onClick={() => {
                  setPicked([]); // fresh selection for the next upload
                  setUploadTab("single");
                  setUploadOpen(true);
                }}
                className="btn-primary py-1.5"
              >
                Next file →
              </button>
            )}
            <span className="font-mono text-xs text-muted truncate">
              {baseName}
            </span>
          </div>
        )}
        <div className="h-full overflow-hidden">
          {busy && !hasResult ? (
            <div className="h-full flex items-center justify-center px-6">
              <div className="w-full max-w-md">
                <ProgressBar status={jobStatus} />
              </div>
            </div>
          ) : (
            <TranscriptPanel
              segments={segments}
              text={text}
              alignerUsed={stats?.aligner_used ?? false}
              hasAudio={audioUrl !== null}
              audioClock={audioClock}
              onSeek={handleSeek}
            />
          )}
        </div>
      </main>

      {/* Bottom player bar */}
      {audioUrl && (
        <footer className="shrink-0 border-t border-white/10">
          <AudioPlayer
            audioUrl={audioUrl}
            onTimeUpdate={handleTimeUpdate}
            seekTo={seekTo}
            onSeeked={() => setSeekTo(null)}
          />
        </footer>
      )}
      </div>

      {/* Upload modal */}
      {uploadOpen && (
        <Overlay onClose={() => !working && setUploadOpen(false)} title="Upload audio or video">
          {/* Single / Batch tabs — APG tabs pattern: roving tabindex, arrow
              keys switch selection, panels are labelled and linked. */}
          <div className="flex gap-1 mb-4" role="tablist" aria-label="Upload mode">
            {UPLOAD_TABS.map((tab) => (
              <button
                key={tab}
                role="tab"
                id={`upload-tab-${tab}`}
                aria-selected={uploadTab === tab}
                aria-controls="upload-panel"
                tabIndex={uploadTab === tab ? 0 : -1}
                onClick={() => setUploadTab(tab)}
                onKeyDown={(e) => {
                  const at = UPLOAD_TABS.indexOf(uploadTab);
                  let next: UploadTab | null = null;
                  if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                    next = UPLOAD_TABS[(at + 1) % UPLOAD_TABS.length];
                  } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                    next = UPLOAD_TABS[(at - 1 + UPLOAD_TABS.length) % UPLOAD_TABS.length];
                  } else if (e.key === "Home") {
                    next = UPLOAD_TABS[0];
                  } else if (e.key === "End") {
                    next = UPLOAD_TABS[UPLOAD_TABS.length - 1];
                  }
                  if (next) {
                    e.preventDefault();
                    setUploadTab(next);
                    document.getElementById(`upload-tab-${next}`)?.focus();
                  }
                }}
                className={`font-mono text-xs uppercase tracking-widest px-3 py-1.5 rounded-lg border transition-colors ${
                  uploadTab === tab
                    ? "border-white/40 bg-white/15 text-white"
                    : "border-white/10 text-faint hover:text-white"
                }`}
              >
                {tab === "single" ? "Single" : "Batch / Folder"}
              </button>
            ))}
          </div>
          <div
            id="upload-panel"
            role="tabpanel"
            aria-labelledby={`upload-tab-${uploadTab}`}
            tabIndex={0}
          >
            {uploadTab === "single" ? (
              <>
                <UploadZone
                  onTranscribe={handleTranscribe}
                  disabled={busy}
                  alignerAvailable={health?.aligner_available ?? false}
                  compact
                />
                {busy && (
                  <p className="font-mono text-xs text-faint mt-2">
                    {queueActive
                      ? "A queue item is processing — use the Batch tab to add more files."
                      : "Drag a file onto the window to enqueue it."}
                  </p>
                )}
              </>
            ) : (
              <BatchUploadZone
                picked={picked}
                onPickedChange={setPicked}
                onEnqueue={handleEnqueue}
                disabled={false}
                alignerAvailable={health?.aligner_available ?? false}
              />
            )}
          </div>
        </Overlay>
      )}

      {/* History drawer */}
      {historyOpen && (
        <Drawer side="left" onClose={() => setHistoryOpen(false)} title="History">
          <HistoryPanel
            items={historyItems}
            activeId={historyId}
            audioCache={audioCache}
            onRestore={handleRestore}
            onDelete={handleDelete}
            onClear={handleClear}
            onClearAudioCache={handleClearAudioCache}
          />
        </Drawer>
      )}

      {/* Queue drawer */}
      {queueOpen && (
        <Drawer side="right" onClose={() => setQueueOpen(false)} title="Batch queue">
          <QueuePanel
            state={queueState}
            jobStatus={queueActive ? jobStatus : null}
            onReorder={handleQueueReorder}
            onSort={handleQueueSort}
            onDelete={handleQueueDelete}
            onRetry={handleQueueRetry}
            onTogglePause={handleQueueTogglePause}
            onClearFinished={handleQueueClearFinished}
            onOpenResult={handleOpenQueueResult}
          />
        </Drawer>
      )}

      {/* Export dropdown */}
      {exportOpen && (
        <Overlay onClose={() => setExportOpen(false)} title="Export transcript">
          <ExportBar
            text={text}
            segments={segments}
            stats={stats}
            baseName={baseName}
            disabled={!hasResult}
          />
        </Overlay>
      )}

      {/* Statistics overlay */}
      {statsOpen && (
        <Overlay onClose={() => setStatsOpen(false)} title="Statistics">
          <StatsPanel stats={stats} />
        </Overlay>
      )}

      {/* Global drop hint (pointer-events-none: drops fall through to the
          window handlers above). Suppressed while the upload modal is open —
          UploadZone has its own drag styling there. */}
      {dragActive && !uploadOpen && (
        <div className="fixed inset-0 z-40 pointer-events-none flex items-center justify-center p-6 animate-fade-in">
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
          <div className="relative glass-strong border-2 border-dashed border-white/50 rounded-3xl px-10 py-8">
            <p className="font-mono text-sm text-white text-center">
              {busy ? "Drop to add to the queue" : "Drop audio or video to transcribe"}
            </p>
            <p className="font-mono text-xs text-muted text-center mt-2">
              Multiple files open the batch tab
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

/* Reusable overlay (centered modal) */
function useDismissOnEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

/** Keep Tab / Shift+Tab cycling inside the referenced dialog while mounted. */
function useFocusTrap(ref: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const focusables = el.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        last.focus();
        e.preventDefault();
      } else if (!e.shiftKey && document.activeElement === last) {
        first.focus();
        e.preventDefault();
      }
    };
    el.addEventListener("keydown", onKeyDown);
    return () => el.removeEventListener("keydown", onKeyDown);
  }, [ref]);
}

/** Restore focus to whatever was focused before the dialog mounted. */
function useFocusReturn() {
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    return () => prev?.focus?.();
  }, []);
}

function Overlay({
  children,
  onClose,
  title,
}: {
  children: React.ReactNode;
  onClose: () => void;
  title: string;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // Play the exit animation before unmounting so the dialog doesn't hard-cut.
  const [closing, setClosing] = useState(false);
  const requestClose = useCallback(() => {
    if (closing) return;
    setClosing(true);
    window.setTimeout(onClose, 180);
  }, [closing, onClose]);
  useDismissOnEscape(requestClose);
  useFocusTrap(dialogRef);
  useFocusReturn();
  useEffect(() => {
    // Move focus into the dialog so keyboard users aren't stranded on the
    // page behind it, and so screen readers announce the dialog title.
    closeRef.current?.focus();
  }, []);
  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-md ${closing ? "animate-fade-out" : "animate-fade-in"}`}
      onClick={requestClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`glass-strong w-full max-w-lg max-h-[85vh] flex flex-col overflow-hidden rounded-3xl ${closing ? "animate-slide-down" : "animate-slide-up"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Fixed header — the content area below scrolls independently, so
            scrolled content can never bleed through the title bar (the old
            sticky header had no background of its own). */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-white/10 shrink-0">
          <h2 className="font-mono text-[13px] uppercase tracking-widest text-muted">
            {title}
          </h2>
          <button
            ref={closeRef}
            onClick={requestClose}
            aria-label={`Close ${title}`}
            className="btn-icon"
          >
            <IconX className="w-4 h-4" />
          </button>
        </div>
        <div className="p-5 flex-1 min-h-0 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}

/* Reusable drawer (side panel) */
function Drawer({
  children,
  onClose,
  title,
  side = "left",
}: {
  children: React.ReactNode;
  onClose: () => void;
  title: string;
  side?: "left" | "right";
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // Play the exit animation before unmounting so the drawer doesn't hard-cut.
  const [closing, setClosing] = useState(false);
  const requestClose = useCallback(() => {
    if (closing) return;
    setClosing(true);
    window.setTimeout(onClose, 220);
  }, [closing, onClose]);
  useDismissOnEscape(requestClose);
  useFocusTrap(dialogRef);
  useFocusReturn();
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  const sideClass =
    side === "left"
      ? `left-0 rounded-r-3xl ${closing ? "animate-slide-out-left" : "animate-slide-in-left"}`
      : `right-0 rounded-l-3xl ${closing ? "animate-slide-out-right" : "animate-slide-in-right"}`;
  return (
    <div
      className={`fixed inset-0 z-50 ${closing ? "animate-fade-out" : "animate-fade-in"}`}
      onClick={requestClose}
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-md" />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`glass-strong absolute top-0 ${sideClass} h-full w-full max-w-sm flex flex-col overflow-hidden`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Fixed header — see Overlay for why this is not sticky. */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-white/10 shrink-0">
          <h2 className="font-mono text-[13px] uppercase tracking-widest text-muted">
            {title}
          </h2>
          <button
            ref={closeRef}
            onClick={requestClose}
            aria-label={`Close ${title}`}
            className="btn-icon"
          >
            <IconX className="w-4 h-4" />
          </button>
        </div>
        <div className="p-5 flex-1 min-h-0 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}
