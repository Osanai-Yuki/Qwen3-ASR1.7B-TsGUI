import type { ReadinessResponse } from "../types";
import { phaseLabel } from "../hooks/useReadiness";

interface Props {
  state: ReadinessResponse | null;
  failed: boolean;
}

/** Full-screen mask shown while the backend boot sequence runs. */
export function BootOverlay({ state, failed }: Props) {
  const label = phaseLabel(state?.phase ?? "starting", state?.error ?? null);
  const isError = state?.phase === "error";

  return (
    <div className="fixed inset-0 z-50 flex flex-col items-center justify-center animate-fade-in">
      <div className="relative w-16 h-16">
        <div className="absolute inset-0 border-2 border-white/10 rounded-full" />
        <div className="absolute inset-0 border-2 border-white border-t-transparent rounded-full animate-spin" />
      </div>
      <h1 className="mt-8 font-mono text-2xl font-black tracking-tight text-white">
        ASR Transcription
      </h1>
      <p className="mt-2 font-mono text-sm text-white/45">
        Qwen3-ASR · CUDA
      </p>
      <p
        className={`mt-6 font-mono text-sm ${
          isError ? "text-red-400" : "text-white/70 animate-pulse-block"
        }`}
      >
        {label}
      </p>
      {failed && !isError && (
        <p className="mt-2 font-mono text-xs text-white/35">
          Connecting to backend…
        </p>
      )}
      {(state?.phase === "asr_loading" || state?.phase === "asr_switching") && (
        <p className="mt-1 font-mono text-xs text-white/30">
          First load may take a minute
        </p>
      )}
      {state?.error && (
        <div className="mt-4 max-w-md px-6">
          <p className="font-mono text-xs text-red-400/70 text-center break-words">
            {state.error}
          </p>
        </div>
      )}
    </div>
  );
}
