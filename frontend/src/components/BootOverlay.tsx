import type { ReadinessResponse } from "../types";
import { phaseLabel } from "../hooks/useReadiness";

interface Props {
  state: ReadinessResponse | null;
  failed: boolean;
  /** Re-poll readiness (from useReadiness.restart). Shown on error/connect-failure. */
  onRetry?: () => void;
}

/** Boot phases rendered as a checklist so a long first load feels like
 * progress rather than a hung spinner. Order mirrors backend/main.py's
 * boot sequence: ASR load → warmup → aligner check. */
const BOOT_STEPS = [
  { key: "asr", label: "Load ASR model (GPU)" },
  { key: "warmup", label: "Warm up ASR" },
  { key: "aligner", label: "Check forced aligner" },
];

/** Map a readiness phase to the index of the in-progress step
 * (steps before it are done; BOOT_STEPS.length = all done). */
function stepIndex(phase: string): number {
  switch (phase) {
    case "asr_warmup":
    case "asr_skipped":
      return 1;
    case "aligner_check":
    case "aligner_loading":
      return 2;
    case "ready":
      return BOOT_STEPS.length;
    default:
      // "starting" / "asr_loading" / "asr_switching" — first step in progress.
      return 0;
  }
}

/** Full-screen mask shown while the backend boot sequence runs. */
export function BootOverlay({ state, failed, onRetry }: Props) {
  const label = phaseLabel(state?.phase ?? "starting", state?.error ?? null);
  const isError = state?.phase === "error";
  const currentStep = stepIndex(state?.phase ?? "starting");

  return (
    <div className="fixed inset-0 z-50 flex flex-col items-center justify-center animate-fade-in">
      <div className="relative w-16 h-16">
        <div className="absolute inset-0 border-2 border-white/10 rounded-full" />
        <div className="absolute inset-0 border-2 border-white border-t-transparent rounded-full animate-spin" />
      </div>
      <h1 className="mt-8 font-mono text-2xl font-black tracking-tight text-white">
        ASR Transcription
      </h1>
      <p className="mt-2 font-mono text-sm text-faint">
        Qwen3-ASR · CUDA
      </p>
      {!isError && (
        <ol className="mt-6 space-y-1.5 font-mono text-xs" aria-label="Boot progress">
          {BOOT_STEPS.map((s, i) => {
            const done = currentStep > i;
            const current = currentStep === i;
            return (
              <li
                key={s.key}
                className={`flex items-center gap-2 transition-colors ${
                  done ? "text-muted" : current ? "text-white" : "text-dim"
                }`}
              >
                <span aria-hidden className={current ? "animate-pulse-block" : ""}>
                  {done ? "✓" : current ? "●" : "○"}
                </span>
                {s.label}
              </li>
            );
          })}
        </ol>
      )}
      {/* role="status" announces phase transitions (loading → warmup →
          ready) to screen readers during the otherwise-silent boot. */}
      <p
        role="status"
        className={`mt-6 font-mono text-sm ${
          isError ? "text-red-400" : "text-muted animate-pulse-block"
        }`}
      >
        {label}
      </p>
      {failed && !isError && (
        <p className="mt-2 font-mono text-xs text-dim">
          Connecting to backend…
        </p>
      )}
      {(state?.phase === "asr_loading" || state?.phase === "asr_switching") && (
        <p className="mt-1 font-mono text-xs text-dim">
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
      {(isError || (failed && !isError)) && (
        <div className="mt-4 max-w-md px-6 flex flex-col items-center gap-4">
          {isError ? (
            <p className="font-mono text-xs text-faint text-center">
              The ASR model may be missing or failed to load (insufficient VRAM,
              corrupt weights). Verify the files under{" "}
              <span className="text-muted">models/asr/</span> and restart the
              app if the error persists.
            </p>
          ) : (
            <p className="font-mono text-xs text-faint text-center">
              Can't reach the backend. Make sure the server is running.
            </p>
          )}
          {onRetry && (
            <button onClick={onRetry} className="btn-primary">
              {isError ? "Retry" : "Reconnect"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
