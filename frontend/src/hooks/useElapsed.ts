import { useEffect, useState } from "react";

/** Seconds elapsed while `active` is true, ticking once per second; resets to
 * 0 when `active` goes false. Purely presentational (job elapsed time), so it
 * lives entirely client-side. */
export function useElapsedSeconds(active: boolean): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const t0 = Date.now();
    const id = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - t0) / 1000)),
      1000,
    );
    return () => window.clearInterval(id);
  }, [active]);
  return elapsed;
}
