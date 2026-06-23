import { useEffect, useRef, useState } from "react";
import { fetchReadiness } from "../api";
import type { ReadinessResponse } from "../types";

const POLL_MS = 800;

/**
 * Polls /api/readiness until the backend boot sequence reports `ready`.
 * Returns the latest state plus a human-friendly phase label for the overlay.
 */
export function useReadiness() {
  const [state, setState] = useState<ReadinessResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const data = await fetchReadiness();
        if (cancelled) return;
        setState(data);
        setFailed(false);
        if (data.ready) return; // stop polling once ready
        timer.current = window.setTimeout(poll, POLL_MS);
      } catch {
        if (cancelled) return;
        setFailed(true);
        // backend may still be starting; retry
        timer.current = window.setTimeout(poll, POLL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);

  return {
    state,
    ready: state?.ready ?? false,
    failed,
  };
}

/** Map a readiness phase to a readable label for the boot overlay. */
export function phaseLabel(phase: string, error: string | null): string {
  if (error) return `Error: ${error}`;
  switch (phase) {
    case "starting":
      return "Initializing…";
    case "asr_loading":
      return "Loading ASR model (GPU)…";
    case "asr_warmup":
      return "Warming up ASR model…";
    case "asr_skipped":
      return "ASR skipped (dev mode)…";
    case "aligner_check":
      return "Checking forced aligner…";
    case "aligner_loading":
      return "Loading forced aligner…";
    case "ready":
      return "Ready";
    default:
      return phase ? `${phase}…` : "Starting…";
  }
}
