import { useCallback, useEffect, useState } from "react";
import {
  clearHistory,
  deleteHistory,
  fetchHealth,
  fetchHistory,
  fetchHistoryRecord,
  transcribe,
} from "./api";
import { useJobStatus } from "./hooks/useJobStatus";
import { useReadiness } from "./hooks/useReadiness";
import { isApiError, type HistoryItem, type Stats } from "./types";
import { BootOverlay } from "./components/BootOverlay";
import { ExportBar } from "./components/ExportBar";
import { HistoryPanel } from "./components/HistoryPanel";
import { ProgressBar } from "./components/ProgressBar";
import { StatsPanel } from "./components/StatsPanel";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { UploadZone } from "./components/UploadZone";

export default function App() {
  const { state: readiness, ready, failed } = useReadiness();
  const [alignerAvailable, setAlignerAvailable] = useState(false);

  // Working state
  const [working, setWorking] = useState(false);
  const { status: jobStatus } = useJobStatus(working);

  // Result state
  const [text, setText] = useState("");
  const [segments, setSegments] = useState<
    { start: number; end: number; text: string }[]
  >([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [baseName, setBaseName] = useState("transcript");
  const [error, setError] = useState<string | null>(null);

  // History state
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([]);

  // Once ready, fetch health (for aligner availability) and history list.
  useEffect(() => {
    if (!ready) return;
    fetchHealth()
      .then((h) => setAlignerAvailable(h.aligner_available))
      .catch(() => {});
    refreshHistory();
  }, [ready]);

  const refreshHistory = useCallback(() => {
    fetchHistory()
      .then((r) => setHistoryItems(r.items))
      .catch(() => {});
  }, []);

  const handleTranscribe = useCallback(
    async (file: File, align: boolean) => {
      setWorking(true);
      setError(null);
      setText("");
      setSegments([]);
      setStats(null);
      setHistoryId(null);
      setBaseName((file.name || "transcript").replace(/\.[^.]+$/, ""));

      try {
        const res = await transcribe(file, align);
        if (isApiError(res)) {
          setError(res.detail || res.error);
        } else {
          setText(res.text);
          setSegments(res.segments);
          setStats(res.stats);
          setHistoryId(res.history_id);
          refreshHistory();
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setWorking(false);
      }
    },
    [refreshHistory],
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
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      await deleteHistory(id);
      if (historyId === id) {
        setText("");
        setSegments([]);
        setStats(null);
        setHistoryId(null);
      }
      refreshHistory();
    },
    [historyId, refreshHistory],
  );

  const handleClear = useCallback(async () => {
    await clearHistory();
    setText("");
    setSegments([]);
    setStats(null);
    setHistoryId(null);
    refreshHistory();
  }, [refreshHistory]);

  if (!ready) {
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
            Qwen3-ASR-1.7B · CUDA
          </span>
        </div>
      </header>

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-px bg-neutral-800">
        {/* Left column: controls */}
        <div className="bg-black flex flex-col gap-px">
          <UploadZone
            onTranscribe={handleTranscribe}
            disabled={working}
            alignerAvailable={alignerAvailable}
          />
          {working && <ProgressBar status={jobStatus} />}
          {error && (
            <div className="border border-red-900 bg-black p-6">
              <p className="font-mono text-xs uppercase tracking-widest text-red-500 mb-2">
                Error
              </p>
              <p className="font-mono text-xs text-neutral-300 break-words">
                {error}
              </p>
            </div>
          )}
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
            onRestore={handleRestore}
            onDelete={handleDelete}
            onClear={handleClear}
          />
        </div>

        {/* Right column: transcript */}
        <div className="bg-black p-px">
          <div className="h-[calc(100vh-73px)]">
            <TranscriptPanel
              segments={segments}
              text={text}
              alignerUsed={stats?.aligner_used ?? false}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
