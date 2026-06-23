import { useCallback, useEffect, useRef, useState } from "react";
import { fetchReadiness } from "../api";
import type { ReadinessResponse } from "../types";

const POLL_MS = 800;

/**
 * Polls /api/readiness until the backend boot sequence reports `ready`.
 * Also re-polls when the backend enters a switching state (ready=false).
 * Returns the latest state, a human-friendly phase label, and a restart()
 * function to manually re-trigger polling (e.g. after initiating a model switch).
 */
export function useReadiness() {
  const [state, setState] = useState<ReadinessResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const timer = useRef<number | null>(null);
  const cancelledRef = useRef(false);

  const poll = useCallback(async () => {
    try {
      const data = await fetchReadiness();
      if (cancelledRef.current) return;
      setState(data);
      setFailed(false);
      // Keep polling if not ready OR if currently switching models
      if (!data.ready || data.switching) {
        timer.current = window.setTimeout(poll, POLL_MS);
      }
    } catch {
      if (cancelledRef.current) return;
      setFailed(true);
      timer.current = window.setTimeout(poll, POLL_MS);
    }
  }, []);

  const restart = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    poll();
  }, [poll]);

  useEffect(() => {
    cancelledRef.current = false;
    poll();
    return () => {
      cancelledRef.current = true;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [poll]);

  return {
    state,
    ready: state?.ready ?? false,
    switching: state?.switching ?? false,
    failed,
    restart,
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
    case "asr_switching":
      return "Switching model…";
    case "ready":
      return "Ready";
    default:
      return phase ? `${phase}…` : "Starting…";
  }
}
