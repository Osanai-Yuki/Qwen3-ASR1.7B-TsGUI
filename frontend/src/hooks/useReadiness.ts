import { useCallback, useEffect, useRef, useState } from "react";
import { fetchReadiness, retryBoot } from "../api";
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
    cancelledRef.current = true;
    if (timer.current !== null) window.clearTimeout(timer.current);
    cancelledRef.current = false;
    // A terminal phase="error" can never be left by polling alone — ask the
    // backend to re-run the boot sequence first (no-op when healthy), then
    // resume polling either way (covers backend-unreachable too).
    retryBoot()
      .catch(() => {})
      .finally(() => poll());
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
  if (error) return `启动出错：${error}`;
  switch (phase) {
    case "starting":
      return "正在初始化本地服务…";
    case "asr_loading":
      return "正在加载识别模型（GPU）…";
    case "asr_warmup":
      return "正在预热识别模型…";
    case "asr_skipped":
      return "开发模式：跳过识别模型…";
    case "aligner_check":
      return "正在检查强制对齐器…";
    case "aligner_loading":
      return "正在加载强制对齐器…";
    case "asr_switching":
      return "正在切换模型…";
    case "ready":
      return "已就绪";
    default:
      return phase ? `${phase}…` : "正在启动…";
  }
}
