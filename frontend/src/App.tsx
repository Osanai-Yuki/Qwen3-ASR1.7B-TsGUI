import { useCallback, useEffect, useRef, useState } from "react";
import {
  abortJob,
  clearAudioCache,
  clearHistory,
  deleteHistory,
  fetchAudioCache,
  fetchHealth,
  fetchHistory,
  fetchHistoryRecord,
  transcribe,
} from "./api";
import { useJobStatus } from "./hooks/useJobStatus";
import { useReadiness } from "./hooks/useReadiness";
import {
  isApiError,
  type AudioCacheInfo,
  type HealthResponse,
  type HistoryItem,
  type Segment,
  type Stats,
} from "./types";
import { AudioPlayer } from "./components/AudioPlayer";
import { BootOverlay } from "./components/BootOverlay";
import { ExportBar } from "./components/ExportBar";
import { HistoryPanel } from "./components/HistoryPanel";
import { ModelInfo } from "./components/ModelInfo";
import { ProgressBar } from "./components/ProgressBar";
import { StatsPanel } from "./components/StatsPanel";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { UploadZone } from "./components/UploadZone";

export default function App() {
  const { state: readiness, ready, switching, failed, restart } = useReadiness();
  const [health, setHealth] = useState<HealthResponse | null>(null);

  // Working state
  const [working, setWorking] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const { status: jobStatus } = useJobStatus(working);

  // Result state
  const [text, setText] = useState("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [baseName, setBaseName] = useState("transcript");
  const [error, setError] = useState<string | null>(null);

  // Audio playback state
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const audioUrlRef = useRef(audioUrl);
  const [currentTime, setCurrentTime] = useState<number | null>(null);
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

  // Once ready, fetch health (model + aligner info) and history list.
  useEffect(() => {
    if (!ready || switching) return;
    fetchHealth().then(setHealth).catch(() => {});
    refreshHistory();
    refreshAudioCache();
  }, [ready, switching]);

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
    setCurrentTime(null);
  }, []);

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
    setCurrentTime(null);
  }, [clearAudioUrl]);

  const handleTranscribe = useCallback(
    async (file: File, align: boolean) => {
      setUploadOpen(false);
      setWorking(true);
      setCancelling(false);
      setError(null);
      setText("");
      setSegments([]);
      setStats(null);
      setHistoryId(null);
      setCurrentTime(null);
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
          if (res.cancelled) {
            setError("Transcription cancelled — showing partial result.");
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
    [refreshHistory, refreshAudioCache, clearAudioUrl, setCacheAudioUrl],
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
    async (id: string) => {
      // Don't clobber an in-flight transcription's state.
      if (working) return;
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
        setBaseName((rec.filename || "transcript").replace(/\.[^.]+$/, ""));
        if (rec.audio_cache_name) setCacheAudioUrl(rec.audio_cache_name);
        else clearAudioUrl();
        setHistoryOpen(false);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [working, clearAudioUrl, setCacheAudioUrl],
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

  useEffect(() => {
    audioUrlRef.current = audioUrl;
  }, [audioUrl]);

  useEffect(() => {
    return () => {
      const url = audioUrlRef.current;
      if (url && url.startsWith("blob:")) URL.revokeObjectURL(url);
    };
  }, []);

  if (!ready || switching) {
    return <BootOverlay state={readiness} failed={failed} onRetry={restart} />;
  }

  const hasResult = segments.length > 0 || text.length > 0;
  const progress = jobStatus?.progress ?? 0;

  return (
    <div className="h-screen w-screen p-3 sm:p-4 lg:p-6 box-border">
      <div className="glass flex flex-col h-full text-white overflow-hidden rounded-3xl">
      {/* ── Top toolbar ─────────────────────────────────────────── */}
      <header className="shrink-0 z-20 border-b border-white/10">
        <div className="flex items-center gap-3 px-5 sm:px-8 py-3.5">
          {/* Brand + model info */}
          <div className="flex items-center gap-3 min-w-0">
            <span className="font-mono text-base font-black tracking-tight whitespace-nowrap">
              ASR
            </span>
            <ModelInfo health={health} />
          </div>

          <div className="flex-1" />

          {/* Action buttons */}
          <button
            onClick={() => setUploadOpen(true)}
            disabled={working}
            className="btn-primary"
          >
            <span className="hidden sm:inline">Upload</span>
            <span className="sm:hidden">＋</span>
          </button>
          <button
            onClick={() => setExportOpen(true)}
            disabled={!hasResult}
            className="btn-ghost"
            title="Export"
          >
            <span className="hidden sm:inline">Export</span>
            <span className="sm:hidden">⤓</span>
          </button>
          <button
            onClick={() => setStatsOpen(true)}
            disabled={!stats}
            className="btn-ghost"
            title="Statistics"
          >
            <span className="hidden sm:inline">Stats</span>
            <span className="sm:hidden">▤</span>
          </button>
          <button
            onClick={() => setHistoryOpen(true)}
            className="btn-ghost"
            title="History"
          >
            <span className="hidden sm:inline">History</span>
            <span className="sm:hidden">☰</span>
          </button>
        </div>

        {/* Thin progress / error bar */}
        {(working || error) && (
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

      {/* ── Main: wide transcript ──────────────────────────────── */}
      <main className="flex-1 min-h-0 overflow-hidden">
        {working && (
          <div className="px-5 sm:px-8 py-2 border-b border-white/8 flex items-center justify-between gap-4 animate-fade-in">
            <span className="font-mono text-xs text-white/60 truncate">
              {cancelling
                ? "Cancelling - stopping at next chunk…"
                : (jobStatus?.message ?? "Working…")}
            </span>
            <button
              onClick={handleCancel}
              disabled={cancelling}
              className="btn-ghost shrink-0 py-1 px-3 text-xs"
              title="Cancel transcription"
            >
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          </div>
        )}
        {error && (
          <div className="px-5 sm:px-8 py-3 border-b border-red-500/30 bg-red-500/10 animate-fade-in">
            <p className="font-mono text-sm text-red-300 break-words">{error}</p>
          </div>
        )}
        <div className="h-full overflow-hidden">
          <TranscriptPanel
            segments={segments}
            text={text}
            alignerUsed={stats?.aligner_used ?? false}
            currentTime={currentTime}
            onSeek={handleSeek}
          />
        </div>
      </main>

      {/* ── Bottom player bar ──────────────────────────────────── */}
      {audioUrl && (
        <footer className="shrink-0 border-t border-white/10">
          <AudioPlayer
            audioUrl={audioUrl}
            onTimeUpdate={setCurrentTime}
            seekTo={seekTo}
            onSeeked={() => setSeekTo(null)}
          />
        </footer>
      )}
      </div>

      {/* ── Upload modal ───────────────────────────────────────── */}
      {uploadOpen && (
        <Overlay onClose={() => !working && setUploadOpen(false)} title="Upload audio or video">
          <UploadZone
            onTranscribe={handleTranscribe}
            disabled={working}
            alignerAvailable={health?.aligner_available ?? false}
            compact
          />
          {working && <ProgressBar status={jobStatus} />}
        </Overlay>
      )}

      {/* ── History drawer ─────────────────────────────────────── */}
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

      {/* ── Export dropdown ────────────────────────────────────── */}
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

      {/* ── Statistics overlay ─────────────────────────────────── */}
      {statsOpen && (
        <Overlay onClose={() => setStatsOpen(false)} title="Statistics">
          <StatsPanel stats={stats} />
        </Overlay>
      )}
    </div>
  );
}

/* ── Reusable overlay (centered modal) ─────────────────────────── */
function useDismissOnEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
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
  useDismissOnEscape(onClose);
  useEffect(() => {
    // Move focus into the dialog so keyboard users aren't stranded on the
    // page behind it, and so screen readers announce the dialog title.
    closeRef.current?.focus();
  }, []);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-md animate-fade-in"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="glass-strong w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-3xl animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-white/10 sticky top-0 z-10">
          <h2 className="font-mono text-[13px] uppercase tracking-widest text-white/60">
            {title}
          </h2>
          <button
            ref={closeRef}
            onClick={onClose}
            aria-label={`Close ${title}`}
            className="font-mono text-base text-white/50 hover:text-white transition-colors p-2 -m-2 rounded-lg"
          >
            ✕
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

/* ── Reusable drawer (side panel) ──────────────────────────────── */
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
  useDismissOnEscape(onClose);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  const sideClass =
    side === "left"
      ? "left-0 animate-slide-in-left"
      : "right-0 animate-slide-in-right";
  return (
    <div className="fixed inset-0 z-50 animate-fade-in" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40 backdrop-blur-md" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`glass-strong absolute top-0 ${sideClass} h-full w-full max-w-sm rounded-r-3xl overflow-y-auto`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-white/10 sticky top-0 z-10">
          <h2 className="font-mono text-[13px] uppercase tracking-widest text-white/60">
            {title}
          </h2>
          <button
            ref={closeRef}
            onClick={onClose}
            aria-label={`Close ${title}`}
            className="font-mono text-base text-white/50 hover:text-white transition-colors p-2 -m-2 rounded-lg"
          >
            ✕
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}
