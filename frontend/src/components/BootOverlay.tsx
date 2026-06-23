import type { ReadinessResponse } from "../types";
import { phaseLabel } from "../hooks/useReadiness";

interface Props {
  state: ReadinessResponse | null;
  failed: boolean;
}

/** Full-screen mask shown while the backend boot sequence runs. */
export function BootOverlay({ state, failed }: Props) {
  const label = phaseLabel(state?.phase ?? "starting", state?.error ?? null);

  return (
    <div className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-black">
      <div className="w-16 h-16 border-2 border-white border-t-transparent animate-spin" />
      <h1 className="mt-8 font-mono text-2xl font-bold tracking-tight">
        ASR Transcription
      </h1>
      <p className="mt-2 font-mono text-sm text-neutral-400">
        Qwen3-ASR-1.7B · CUDA
      </p>
      <p className="mt-6 font-mono text-sm animate-pulse-block">{label}</p>
      {failed && (
        <p className="mt-2 font-mono text-xs text-neutral-500">
          Connecting to backend…
        </p>
      )}
      {state?.phase === "asr_loading" && (
        <p className="mt-1 font-mono text-xs text-neutral-600">
          First load may take a minute
        </p>
      )}
    </div>
  );
}
