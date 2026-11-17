// Thin fetch wrappers for every backend endpoint.
// All functions throw on network failure; endpoint-level errors are returned
// as ApiError-shaped objects (matching backend behavior) for the caller to
// inspect via isApiError().

import type {
  AlignResponse,
  AppLimits,
  ApiError,
  AudioCacheClearResult,
  AudioCacheInfo,
  BatchesResponse,
  HealthResponse,
  HistoryItem,
  HistoryRecord,
  HistorySearchResponse,
  JobStatusResponse,
  ModelsResponse,
  QueueItem,
  QueueItemResult,
  QueueSortKey,
  QueueStateResponse,
  ReadinessResponse,
  ReaderPosition,
  SwitchModelRequest,
  SwitchModelResponse,
  TranscribeResponse,
  WorkbenchConfig,
  WorkbenchRoot,
  WorkbenchScanResponse,
} from "./types";

/** PATCH /api/history/{hid}/view response. */
export interface HistoryViewResponse {
  first_viewed_at: string;
  read_position: number;
  playback_position: number;
  reader_position?: ReaderPosition | null;
}

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  return (await res.json()) as T;
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/health");
}

export function fetchLimits(): Promise<AppLimits> {
  return getJson<AppLimits>("/api/limits");
}

export function fetchReadiness(): Promise<ReadinessResponse> {
  return getJson<ReadinessResponse>("/api/readiness");
}

export async function shutdownApp(): Promise<void> {
  const response = await fetch("/api/shutdown", { method: "POST" });
  if (!response.ok) throw new Error("无法退出本地服务，请稍后重试。");
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

export function fetchHistorySearch(options: {
  query?: string;
  offset?: number;
  limit?: number;
  batchId?: string | null;
  filter?: "all" | "unseen" | "partial";
} = {}): Promise<HistorySearchResponse> {
  const params = new URLSearchParams();
  if (options.query) params.set("q", options.query);
  if (options.offset) params.set("offset", String(options.offset));
  if (options.limit) params.set("limit", String(options.limit));
  if (options.batchId) params.set("batch_id", options.batchId);
  if (options.filter) params.set("filter", options.filter);
  return getJson<HistorySearchResponse>(`/api/history/search?${params.toString()}`);
}

export function fetchBatches(): Promise<BatchesResponse> {
  return getJson<BatchesResponse>("/api/batches");
}

/** PATCH /api/history/{hid}/view. `position` is the legacy seconds field
 * (playback); reader/playback anchors are independent and never overwrite
 * each other.  `keepalive` lets the pagehide flush finish after unload;
 * sendBeacon cannot send PATCH, so this is the correct last-chance path. */
export async function updateHistoryView(
  hid: string,
  position?: number,
  extra: { reader_position?: ReaderPosition; playback_position?: number } = {},
  options: { keepalive?: boolean } = {},
): Promise<HistoryViewResponse | ApiError> {
  const res = await fetch(`/api/history/${encodeURIComponent(hid)}/view`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ position, ...extra }),
    keepalive: Boolean(options.keepalive),
  });
  return (await res.json()) as HistoryViewResponse | ApiError;
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

/* Batch queue (backend/queue_api.py) */

/** GET /api/queue — whole queue state (poll; compare `version`). */
export function fetchQueue(): Promise<QueueStateResponse> {
  return getJson<QueueStateResponse>("/api/queue");
}

/** POST /api/queue/items — enqueue a batch of files for serial transcription.
 *  `clientImportIds` are caller-minted ids echoed back per item so a partial
 *  accept can be reconciled exactly, never by guessing from name/size. */
export async function enqueueFiles(
  files: File[],
  align: boolean,
  batch: {
    id: string;
    name: string;
    sourcePaths: string[];
    clientImportIds?: string[];
  } = {
    id: crypto.randomUUID().replace(/-/g, "").slice(0, 12),
    name: "未命名批次",
    sourcePaths: files.map((file) => file.name),
  },
): Promise<{ items: QueueItem[] } | ApiError> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  form.append("align", String(align));
  form.append("batch_id", batch.id);
  form.append("batch_name", batch.name);
  form.append("source_paths", JSON.stringify(batch.sourcePaths));
  if (batch.clientImportIds && batch.clientImportIds.length === files.length) {
    form.append("client_import_ids", JSON.stringify(batch.clientImportIds));
  }
  const res = await fetch("/api/queue/items", { method: "POST", body: form });
  return (await res.json()) as { items: QueueItem[] } | ApiError;
}

/** GET /api/queue/items/{id}/result — read one item's result (saved or the
 *  session-only recovery copy) without polling the whole queue. */
export function fetchQueueItemResult(id: string): Promise<QueueItemResult | ApiError> {
  return getJson<QueueItemResult | ApiError>(`/api/queue/items/${encodeURIComponent(id)}/result`);
}

/** POST /api/queue/items/{id}/save — re-commit an already-produced result.
 *  Never re-runs ASR; repeats return the same history id. */
export async function retryQueueSave(
  id: string,
  attemptId: string,
): Promise<{ ok: boolean; history_id: string; already_saved: boolean } | ApiError> {
  const res = await fetch(`/api/queue/items/${encodeURIComponent(id)}/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ attempt_id: attemptId }),
  });
  return (await res.json()) as { ok: boolean; history_id: string; already_saved: boolean } | ApiError;
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

/* Workbench mode (backend/workbench.py) */

export function fetchWorkbenchConfig(): Promise<WorkbenchConfig> {
  return getJson<WorkbenchConfig>("/api/workbench/config");
}

export async function updateWorkbenchConfig(
  patch: Partial<Pick<WorkbenchConfig, "export_root" | "formats" | "auto_export" | "summarize" | "summary_model">>,
): Promise<WorkbenchConfig | ApiError> {
  const res = await fetch("/api/workbench/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return (await res.json()) as WorkbenchConfig | ApiError;
}

export async function addWorkbenchRoot(path: string): Promise<{ root: WorkbenchRoot } | ApiError> {
  const res = await fetch("/api/workbench/roots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return (await res.json()) as { root: WorkbenchRoot } | ApiError;
}

export async function removeWorkbenchRoot(id: string): Promise<{ ok: boolean } | ApiError> {
  const res = await fetch(`/api/workbench/roots/${encodeURIComponent(id)}`, { method: "DELETE" });
  return (await res.json()) as { ok: boolean } | ApiError;
}

export function scanWorkbenchRoot(rootId: string, limit = 2000): Promise<WorkbenchScanResponse | ApiError> {
  return getJson<WorkbenchScanResponse | ApiError>(
    `/api/workbench/scan?root=${encodeURIComponent(rootId)}&limit=${limit}`,
  );
}

/** POST /api/queue/reference — enqueue files in place from an approved root. */
export async function enqueueReference(
  rootId: string,
  relPaths: string[],
  align: boolean,
  batchName: string,
): Promise<{ items: QueueItem[]; batch_id: string } | ApiError> {
  const res = await fetch("/api/queue/reference", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root_id: rootId, rel_paths: relPaths, align, batch_name: batchName }),
  });
  return (await res.json()) as { items: QueueItem[]; batch_id: string } | ApiError;
}

/** POST /api/queue/items/{id}/export — re-export a durable result on demand. */
export async function reexportQueueItem(
  id: string,
): Promise<{ ok: boolean; state: string; paths: { format: string; path: string; name: string }[] } | ApiError> {
  const res = await fetch(`/api/queue/items/${encodeURIComponent(id)}/export`, { method: "POST" });
  return (await res.json()) as { ok: boolean; state: string; paths: { format: string; path: string; name: string }[] } | ApiError;
}
