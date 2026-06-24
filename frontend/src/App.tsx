import { useCallback, useEffect, useState } from "react";
import {
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
  type HistoryItem,
  type Segment,
  type Stats,
} from "./types";
import { AudioPlayer } from "./components/AudioPlayer";
import { BootOverlay } from "./components/BootOverlay";
import { ExportBar } from "./components/ExportBar";
import { HistoryPanel } from "./components/HistoryPanel";
import { ModelPanel } from "./components/ModelPanel";
import { ProgressBar } from "./components/ProgressBar";
import { StatsPanel } from "./components/StatsPanel";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { UploadZone } from "./components/UploadZone";

export default function App() {
  const { state: readiness, ready, switching, failed, restart: restartReadiness } =
    useReadiness();
  const [alignerAvailable, setAlignerAvailable] = useState(false);

  // Working state
  const [working, setWorking] = useState(false);
  const { status: jobStatus } = useJobStatus(working);

  // Result state
  const [text, setText] = useState("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [baseName, setBaseName] = useState("transcript");
  const [error, setError] = useState<string | null>(null);

  // Audio playback state (Object URL of the uploaded file)
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState<number | null>(null);
  const [seekTo, setSeekTo] = useState<number | null>(null);

  // History state
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([]);

  // Converted-audio cache state (MP3s extracted from video sources)
  const [audioCache, setAudioCache] = useState<AudioCacheInfo | null>(null);

  // Once ready, fetch health (for aligner availability) and history list.
  useEffect(() => {
    if (!ready || switching) return;
    fetchHealth()
      .then((h) => setAlignerAvailable(h.aligner_available))
      .catch(() => {});
    refreshHistory();
    refreshAudioCache();
  }, [ready, switching]);

  const refreshHistory = useCallback(() => {
    fetchHistory()
      .then((r) => setHistoryItems(r.items))
      .catch(() => {});
  }, []);

  const refreshAudioCache = useCallback(() => {
    fetchAudioCache()
      .then((c) => setAudioCache(c))
      .catch(() => {});
  }, []);

  // Cleanup the current audio URL on unmount or replacement. Only Object URLs
  // (blob:) created from uploaded files need revoking; backend cache URLs do not.
  const clearAudioUrl = useCallback(() => {
    setAudioUrl((prev) => {
      if (prev && prev.startsWith("blob:")) URL.revokeObjectURL(prev);
      return null;
    });
    setCurrentTime(null);
  }, []);

  /** Set the audio source to a backend cached-MP3 URL (no revocation needed). */
  const setCacheAudioUrl = useCallback((cacheName: string) => {
    setAudioUrl((prev) => {
      if (prev && prev.startsWith("blob:")) URL.revokeObjectURL(prev);
      return `/api/audio-cache/${encodeURIComponent(cacheName)}`;
    });
    setCurrentTime(null);
  }, []);

  const handleTranscribe = useCallback(
    async (file: File, align: boolean) => {
      setWorking(true);
      setError(null);
      setText("");
      setSegments([]);
      setStats(null);
      setHistoryId(null);
      setCurrentTime(null);
      setBaseName((file.name || "transcript").replace(/\.[^.]+$/, ""));

      // Create a local Object URL for immediate audio playback during work.
      // For video sources the backend caches a converted MP3; once the
      // response arrives we switch playback to that cached file (so it
      // reflects the actual converted audio and survives history restore).
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
          refreshHistory();
          refreshAudioCache();
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setWorking(false);
      }
    },
    [refreshHistory, refreshAudioCache, clearAudioUrl, setCacheAudioUrl],
  );

  const handleRestore = useCallback(
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
        setBaseName((rec.filename || "transcript").replace(/\.[^.]+$/, ""));
        // History records can replay audio if a converted MP3 was cached.
        if (rec.audio_cache_name) setCacheAudioUrl(rec.audio_cache_name);
        else clearAudioUrl();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [clearAudioUrl, setCacheAudioUrl],
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
    // If the currently playing track was a cached MP3, drop it.
    clearAudioUrl();
    refreshAudioCache();
  }, [clearAudioUrl, refreshAudioCache]);

  const handleSeek = useCallback((time: number) => {
    setSeekTo(time);
  }, []);

  // Model switch handlers
  const handleSwitchStart = useCallback(() => {
    restartReadiness();
  }, [restartReadiness]);

  const handleSwitchDone = useCallback(() => {
    // readiness polling will naturally pick up the new state
  }, []);

  // Show boot overlay during initial boot or model switching
  if (!ready || switching) {
    return <BootOverlay state={readiness} failed={failed} />;
  }

  const hasResult = segments.length > 0 || text.length > 0;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-neutral-800 px-8 py-5">
        <div className="flex items-baseline justify-between">
          <h1 className="font-mono text-lg font-black tracking-tight">
            ASR TRANSCRIPTION
          </h1>
          <span className="font-mono text-xs text-neutral-600">
            Qwen3-ASR · CUDA
          </span>
        </div>
      </header>

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-[400px_1fr] gap-px bg-neutral-800">
        {/* Left column: controls */}
        <div className="bg-black flex flex-col gap-px overflow-y-auto">
          <UploadZone
            onTranscribe={handleTranscribe}
            disabled={working}
            alignerAvailable={alignerAvailable}
          />
          {audioUrl && (
            <AudioPlayer
              audioUrl={audioUrl}
              onTimeUpdate={setCurrentTime}
              seekTo={seekTo}
            />
          )}
          {working && <ProgressBar status={jobStatus} />}
          {error && (
            <div className="border border-red-900 bg-black p-6 animate-fade-in">
              <p className="font-mono text-xs uppercase tracking-widest text-red-500 mb-2">
                Error
              </p>
              <p className="font-mono text-xs text-neutral-300 break-words">
                {error}
              </p>
            </div>
          )}
          <ModelPanel
            switching={switching}
            onSwitchStart={handleSwitchStart}
            onSwitchDone={handleSwitchDone}
          />
          <StatsPanel stats={stats} />
          <ExportBar
            text={text}
            segments={segments}
            stats={stats}
            baseName={baseName}
            disabled={!hasResult}
          />
          <HistoryPanel
            items={historyItems}
            activeId={historyId}
            audioCache={audioCache}
            onRestore={handleRestore}
            onDelete={handleDelete}
            onClear={handleClear}
            onClearAudioCache={handleClearAudioCache}
          />
        </div>

        {/* Right column: transcript */}
        <div className="bg-black p-px">
          <div className="h-[calc(100vh-73px)]">
            <TranscriptPanel
              segments={segments}
              text={text}
              alignerUsed={stats?.aligner_used ?? false}
              currentTime={currentTime}
              onSeek={handleSeek}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
