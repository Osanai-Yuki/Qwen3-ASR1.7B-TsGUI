import { useCallback, useEffect, useRef, useState } from "react";
import { fetchQueue } from "../api";
import type { QueueStateResponse } from "../types";

const POLL_LIVE_MS = 1000; // something queued/running — keep it fresh
const POLL_IDLE_MS = 3000; // only terminal items left — relax

/**
 * Polls GET /api/queue with an adaptive interval (same setTimeout-chain
 * pattern as useJobStatus). Skips setState when the backend `version`
 * counter hasn't moved so a static queue causes zero re-renders.
 * `refresh()` forces an immediate poll after a mutation (enqueue/reorder/…).
 */
export function useQueue() {
  const [state, setState] = useState<QueueStateResponse | null>(null);
  const timer = useRef<number | null>(null);
  const versionRef = useRef<number>(-1);
  const cancelledRef = useRef(false);

  const poll = useCallback(async () => {
    let delay = POLL_IDLE_MS;
    try {
      const data = await fetchQueue();
      if (cancelledRef.current) return;
      if (data.version !== versionRef.current) {
        versionRef.current = data.version;
        setState(data);
      }
      const live = data.items.some(
        (it) => it.status === "queued" || it.status === "running",
      );
      delay = live && !data.paused ? POLL_LIVE_MS : POLL_IDLE_MS;
    } catch {
      // transient network blip — keep trying at the idle rate
    }
    if (cancelledRef.current) return;
    timer.current = window.setTimeout(poll, delay);
  }, []);

  const refresh = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    versionRef.current = -1; // force the next response through
    void poll();
  }, [poll]);

  useEffect(() => {
    cancelledRef.current = false;
    void poll();
    return () => {
      cancelledRef.current = true;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [poll]);

  return { state, refresh };
}
