import { useEffect, useRef, useState } from "react";
import { fetchStatus } from "../api";
import type { JobStatusResponse } from "../types";

const POLL_MS = 800;

/**
 * Polls /api/status while `active` is true. Used in parallel with the
 * long-running transcribe POST to drive the progress bar. Stops automatically
 * once the job reaches a terminal state (done/error) or `active` flips false.
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
        if (data.status === "done" || data.status === "error") return;
        timer.current = window.setTimeout(poll, POLL_MS);
      } catch {
        if (cancelled) return;
        // transient network blip — keep trying
        timer.current = window.setTimeout(poll, POLL_MS);
      }
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
