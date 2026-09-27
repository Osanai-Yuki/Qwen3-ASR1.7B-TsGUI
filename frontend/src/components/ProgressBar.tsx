import type { JobStatusResponse } from "../types";
import { useElapsedSeconds } from "../hooks/useElapsed";
import { formatShort } from "../utils/format";

interface Props {
  status: JobStatusResponse | null;
}

const ACTIVE_STATUSES = new Set(["preparing", "transcribing", "aligning"]);

/** Renders the live job status / progress while a transcription is running. */
export function ProgressBar({ status }: Props) {
  const active = !!status && ACTIVE_STATUSES.has(status.status);
  // Local 1s ticker — purely presentational, resets when the job ends.
  const elapsed = useElapsedSeconds(active);

  if (!status || status.status === "idle") return null;

  const progress = Math.max(0, Math.min(100, status.progress ?? 0));
  const isError = status.status === "error";

  return (
    <div className="mt-4 p-4 rounded-2xl bg-white/5 border border-white/8 animate-fade-in">
      <div className="flex items-baseline justify-between mb-3 gap-3">
        <span
          className={`font-mono text-xs uppercase tracking-widest ${
            isError ? "text-red-400" : "text-muted"
          }`}
        >
          {isError ? "Error" : status.status}
        </span>
        <span className="font-mono text-xs text-muted tabular-nums">
          {active && <span className="text-dim">{formatShort(elapsed)} · </span>}
          {progress}%
        </span>
      </div>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
        aria-label="Transcription progress"
        className="h-1.5 w-full bg-white/8 overflow-hidden rounded-full"
      >
        <div
          className={`h-full transition-all duration-500 ease-out rounded-full ${
            isError ? "bg-red-500" : "bg-white animate-progress-glow"
          }`}
          style={{ width: `${progress}%` }}
        />
      </div>
      {status.message && (
        <p className="mt-3 font-mono text-xs text-muted">
          {status.message}
        </p>
      )}
    </div>
  );
}
