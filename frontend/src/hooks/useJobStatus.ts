import { useEffect, useRef, useState } from "react";
import { fetchStatus } from "../api";
import type { JobStatusResponse } from "../types";

const POLL_MS = 800;

/**
 * Polls /api/status while `active` is true. Used in parallel with the
 * long-running transcribe POST to drive the progress bar.
 *
 * The timer keeps running through a terminal state on purpose: in a batch,
 * `active` stays true across items because the queue poll never observes the
 * gap between them, so stopping at the first done/error froze the bar at the
 * previous item's 100% for the rest of the run.
 */
export function useJobStatus(active: boolean) {
  const [status, setStatus] = useState<JobStatusResponse | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const data = await fetchStatus();
        if (cancelled) return;
        setStatus(data);
      } catch {
        if (cancelled) return;
        // transient network blip - keep trying
      }
      if (!cancelled) timer.current = window.setTimeout(poll, POLL_MS);
    };

    poll();
    return () => {
      cancelled = true;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [active]);

  const stop = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  };

  return { status, stop };
}
