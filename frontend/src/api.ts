// Thin fetch wrappers for every backend endpoint.
// All functions throw on network failure; endpoint-level errors are returned
// as ApiError-shaped objects (matching backend behavior) for the caller to
// inspect via isApiError().

import type {
  AlignResponse,
  ApiError,
  HealthResponse,
  HistoryItem,
  HistoryRecord,
  JobStatusResponse,
  ModelsResponse,
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
