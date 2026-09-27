import type { HealthResponse } from "../types";

interface Props {
  health: HealthResponse | null;
}

/** Compact model + aligner-backend indicator for the top toolbar.
 *
 * Per design: no model switching UI — only show the loaded model name and
 * whether alignment runs on CPU (CrispASR) or GPU (qwen-asr).
 */
export function ModelInfo({ health }: Props) {
  const model = health?.current_model ?? "—";
  const backend = health?.aligner_backend ?? "cpu";
  const isGpu = backend === "gpu";

  return (
    <div className="hidden md:flex items-center gap-2 min-w-0">
      <span className="font-mono text-sm text-faint truncate max-w-[16rem]">
        {model}
      </span>
      <span
        className={`font-mono text-[11px] uppercase tracking-widest px-2 py-0.5 rounded-full border ${
          isGpu
            ? "border-amber-400/40 text-amber-400 bg-amber-400/10"
            : "border-white/20 text-muted bg-white/5"
        }`}
        title={`Aligner backend: ${backend}`}
      >
        {isGpu ? "GPU" : "CPU"}
      </span>
    </div>
  );
}
