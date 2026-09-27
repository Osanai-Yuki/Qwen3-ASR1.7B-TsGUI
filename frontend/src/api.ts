// Thin fetch wrappers for every backend endpoint.
// All functions throw on network failure; endpoint-level errors are returned
// as ApiError-shaped objects (matching backend behavior) for the caller to
// inspect via isApiError().

import type {
  AlignResponse,
  ApiError,
  AudioCacheClearResult,
  AudioCacheInfo,
  HealthResponse,
  HistoryItem,
  HistoryRecord,
  JobStatusResponse,
  ModelsResponse,
  QueueItem,
  QueueSortKey,
  QueueStateResponse,
  ReadinessResponse,
  SwitchModelRequest,
  SwitchModelResponse,
  TranscribeResponse,
} from "./types";

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  return (await res.json()) as T;
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/health");
}

export function fetchReadiness(): Promise<ReadinessResponse> {
  return getJson<ReadinessResponse>("/api/readiness");
}

/** POST /api/readiness/retry — re-run the boot sequence after phase="error".
 * No-op when the backend is healthy or already booting. */
export async function retryBoot(): Promise<{ ok: boolean } | ApiError> {
  const res = await fetch("/api/readiness/retry", { method: "POST" });
  return (await res.json()) as { ok: boolean } | ApiError;
}

export function fetchStatus(): Promise<JobStatusResponse> {
  return getJson<JobStatusResponse>("/api/status");
}

/** POST /api/transcribe. Long-running (up to 600s); poll fetchStatus() in
 * parallel to show progress while this promise is pending. */
export async function transcribe(
  file: File,
  align: boolean,
): Promise<TranscribeResponse | ApiError> {
  const form = new FormData();
  form.append("file", file);
  form.append("align", String(align));
  const res = await fetch("/api/transcribe", { method: "POST", body: form });
  return (await res.json()) as TranscribeResponse | ApiError;
}

/** POST /api/abort - request cancellation of the in-flight transcription.
 * The backend stops at the next chunk boundary and resolves /api/transcribe
 * with the partial result (cancelled: true); the client awaits that response
 * rather than aborting the fetch, so partial work is preserved. */
export async function abortJob(): Promise<{ ok: boolean }> {
  const res = await fetch("/api/abort", { method: "POST" });
  return (await res.json()) as { ok: boolean };
}

/** POST /api/align — standalone forced alignment (audio + text). */
export async function alignStandalone(
  file: File,
  text: string,
): Promise<AlignResponse | ApiError> {
  const form = new FormData();
  form.append("file", file);
  form.append("text", text);
  const res = await fetch("/api/align", { method: "POST", body: form });
  return (await res.json()) as AlignResponse | ApiError;
}

export function fetchHistory(): Promise<{ items: HistoryItem[] }> {
  return getJson<{ items: HistoryItem[] }>("/api/history");
}

export function fetchHistoryRecord(
  hid: string,
): Promise<HistoryRecord | ApiError> {
  return getJson<HistoryRecord | ApiError>(`/api/history/${hid}`);
}

export async function deleteHistory(
  hid: string,
): Promise<{ deleted: boolean }> {
  const res = await fetch(`/api/history/${hid}`, { method: "DELETE" });
  return (await res.json()) as { deleted: boolean };
}

export async function clearHistory(): Promise<{ cleared: number }> {
  const res = await fetch("/api/history", { method: "DELETE" });
  return (await res.json()) as { cleared: number };
}

/** GET /api/audio-cache — list cached converted-audio MP3s. */
export function fetchAudioCache(): Promise<AudioCacheInfo> {
  return getJson<AudioCacheInfo>("/api/audio-cache");
}

/** DELETE /api/audio-cache — remove all cached converted-audio MP3s. */
export async function clearAudioCache(): Promise<AudioCacheClearResult> {
  const res = await fetch("/api/audio-cache", { method: "DELETE" });
  return (await res.json()) as AudioCacheClearResult;
}

export function fetchModels(): Promise<ModelsResponse> {
  return getJson<ModelsResponse>("/api/models");
}

export async function switchModel(
  req: SwitchModelRequest,
): Promise<SwitchModelResponse | ApiError> {
  const res = await fetch("/api/models/switch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  return (await res.json()) as SwitchModelResponse | ApiError;
}

/* ── Batch queue (backend/queue_api.py) ────────────────────────── */

/** GET /api/queue — whole queue state (poll; compare `version`). */
export function fetchQueue(): Promise<QueueStateResponse> {
  return getJson<QueueStateResponse>("/api/queue");
}

/** POST /api/queue/items — enqueue a batch of files for serial transcription. */
export async function enqueueFiles(
  files: File[],
  align: boolean,
): Promise<{ items: QueueItem[] } | ApiError> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  form.append("align", String(align));
  const res = await fetch("/api/queue/items", { method: "POST", body: form });
  return (await res.json()) as { items: QueueItem[] } | ApiError;
}

/** POST /api/queue/reorder — ids must be a permutation of the queued items. */
export async function reorderQueue(
  ids: string[],
): Promise<{ ok: boolean } | ApiError> {
  const res = await fetch("/api/queue/reorder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  return (await res.json()) as { ok: boolean } | ApiError;
}

/** POST /api/queue/sort — one-shot rule sort of the queued items. */
export async function sortQueue(
  key: QueueSortKey,
  order: "asc" | "desc",
): Promise<{ ok: boolean } | ApiError> {
  const res = await fetch("/api/queue/sort", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, order }),
  });
  return (await res.json()) as { ok: boolean } | ApiError;
}

/** DELETE /api/queue/items/{id} — queued: remove; running: skip current. */
export async function deleteQueueItem(
  id: string,
): Promise<{ ok: boolean; skipping: boolean } | ApiError> {
  const res = await fetch(`/api/queue/items/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  return (await res.json()) as { ok: boolean; skipping: boolean } | ApiError;
}

/** POST /api/queue/items/{id}/retry — error/cancelled back to queued. */
export async function retryQueueItem(
  id: string,
): Promise<{ ok: boolean } | ApiError> {
  const res = await fetch(`/api/queue/items/${encodeURIComponent(id)}/retry`, {
    method: "POST",
  });
  return (await res.json()) as { ok: boolean } | ApiError;
}

export async function pauseQueue(): Promise<{ ok: boolean; paused: boolean }> {
  const res = await fetch("/api/queue/pause", { method: "POST" });
  return (await res.json()) as { ok: boolean; paused: boolean };
}

export async function resumeQueue(): Promise<{ ok: boolean; paused: boolean }> {
  const res = await fetch("/api/queue/resume", { method: "POST" });
  return (await res.json()) as { ok: boolean; paused: boolean };
}

/** DELETE /api/queue/finished — drop all done/error/cancelled items. */
export async function clearFinishedQueue(): Promise<{ cleared: number }> {
  const res = await fetch("/api/queue/finished", { method: "DELETE" });
  return (await res.json()) as { cleared: number };
}
