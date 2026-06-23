import type { JobStatusResponse } from "../types";

interface Props {
  status: JobStatusResponse | null;
}

/** Renders the live job status / progress while a transcription is running. */
export function ProgressBar({ status }: Props) {
  if (!status || status.status === "idle") return null;

  const progress = Math.max(0, Math.min(100, status.progress ?? 0));
  const isError = status.status === "error";

  return (
    <div className="border border-neutral-800 p-6">
      <div className="flex items-baseline justify-between mb-3">
        <span
          className={`font-mono text-xs uppercase tracking-widest ${
            isError ? "text-red-500" : "text-neutral-500"
          }`}
        >
          {isError ? "Error" : status.status}
        </span>
        <span className="font-mono text-xs text-neutral-500">
          {progress}%
        </span>
      </div>
      <div className="h-1 w-full bg-neutral-900">
        <div
          className={`h-full ${isError ? "bg-red-600" : "bg-white"} transition-all`}
          style={{ width: `${progress}%` }}
        />
      </div>
      {status.message && (
        <p className="mt-3 font-mono text-xs text-neutral-400">
          {status.message}
        </p>
      )}
    </div>
  );
}
