// TypeScript types for the backend API contract.
// Shapes are derived from backend/main.py (endpoints), resegment.py (segment
// output), forced_aligner.py (word output), history.py (records) and the
// stats dict assembled in main.py:transcribe.

/** A timed transcript segment. Shape from resegment.py / llama-server. */
export interface Segment {
  start: number;
  end: number;
  text: string;
}

/** A word-level aligned unit. Shape from forced_aligner.py:align_chunk. */
export interface AlignedWord {
  word: string;
  start: number;
  end: number;
  text: string;
}

/** Performance + metadata stats. Assembled in main.py:transcribe. */
export interface Stats {
  filename: string | null;
  file_size: number;
  audio_duration: number;
  chunk_count: number;
  chunk_seconds: number;
  asr_time: number;
  align_time: number;
  total_time: number;
  rtf: number;
  char_count: number;
  segment_count: number;
  word_count: number;
  aligner_used: boolean;
  model: string;
  aligner_model: string | null;
  source_was_video?: boolean;
  audio_cache_name: string | null;
  mp3_bitrate?: number | null;
}

/** GET /api/health */
export interface HealthResponse {
  backend: boolean;
  llama_server: boolean;
  current_model: string | null;
  aligner_available: boolean;
  aligner_status: string;
  aligner_backend: string;
}

/** GET /api/readiness — boot sequence progress. */
export interface ReadinessResponse {
  phase: string;
  asr_loaded: boolean;
  asr_warmup: boolean;
  aligner_loaded: boolean;
  aligner_warmup: boolean;
  ready: boolean;
  error: string | null;
  current_model: string | null;
  switching: boolean;
}

/** GET /api/status — current job status. */
export interface JobStatusResponse {
  status:
    | "idle"
    | "preparing"
    | "transcribing"
    | "aligning"
    | "done"
    | "error"
    | "cancelled";
  progress: number;
  message: string;
  result: unknown;
}

/** POST /api/transcribe success body. */
export interface TranscribeResponse {
  text: string;
  segments: Segment[];
  stats: Stats;
  history_id: string | null;
  /** True when the user cancelled mid-transcription; text/segments are partial. */
  cancelled?: boolean;
}

/** Generic error body returned by failing endpoints. */
export interface ApiError {
  error: string;
  detail: string;
}

/** POST /api/align success body. */
export interface AlignResponse {
  words: AlignedWord[];
  count: number;
}

/** History list item (GET /api/history). Shape from history.py:list. */
export interface HistoryItem {
  id: string;
  created_at: string;
  filename: string | null;
  duration: number;
  char_count: number;
  align_used: boolean;
  segment_count: number;
  audio_cache_name: string | null;
}

/** Full history record (GET /api/history/{hid}). Shape from history.py:save. */
export interface HistoryRecord {
  id: string;
  created_at: string;
  filename: string;
  text: string;
  segments: Segment[];
  stats: Stats;
  align_used: boolean;
  audio_cache_name: string | null;
}

/** A discovered ASR model in models/asr/. Shape from _scan_asr_models.
 *  Absolute paths are intentionally NOT exposed (server re-resolves by name). */
export interface ModelInfo {
  name: string;
  size: number;
  has_mmproj: boolean;
}

/** GET /api/models */
export interface ModelsResponse {
  models: ModelInfo[];
  current_model: string | null;
  tuning: {
    ctx_size: number;
    kv_quant: string;
    n_gpu_layers: number;
    threads: number | null;
  };
}

/** POST /api/models/switch request body. */
export interface SwitchModelRequest {
  model: string;
  ctx_size?: number;
  kv_quant?: string;
  n_gpu_layers?: number;
  threads?: number | null;
  batch_size?: number;
  ubatch_size?: number;
  flash_attn?: boolean;
  mmproj_offload?: boolean;
}

/** POST /api/models/switch success body. */
export interface SwitchModelResponse {
  ok: boolean;
  current_model: string;
}

/** A cached converted-audio MP3 entry (GET /api/audio-cache). */
export interface AudioCacheItem {
  name: string;
  size: number;
  mtime: number;
}

/** GET /api/audio-cache */
export interface AudioCacheInfo {
  count: number;
  size_bytes: number;
  items: AudioCacheItem[];
}

/** DELETE /api/audio-cache */
export interface AudioCacheClearResult {
  cleared: number;
  bytes_freed: number;
}

/** Type guard: does a transcribe response represent an error? */
export function isApiError(obj: unknown): obj is ApiError {
  return (
    typeof obj === "object" &&
    obj !== null &&
    "error" in obj &&
    typeof (obj as ApiError).error === "string"
  );
}

/** Batch queue item lifecycle (backend/queue_store.py). */
export type QueueItemStatus =
  | "queued"
  | "running"
  | "done"
  | "error"
  | "cancelled";

/** A batch queue entry (GET /api/queue). */
export interface QueueItem {
  id: string;
  filename: string;
  size: number;
  align: boolean;
  status: QueueItemStatus;
  history_id: string | null;
  error: string | null;
  added_at: string;
  finished_at: string | null;
}

/** GET /api/queue — whole queue state. `version` is a monotonic counter;
 * skip re-rendering when it hasn't changed. `active_id` is the item the
 * worker is currently transcribing (null when idle or a manual job runs). */
export interface QueueStateResponse {
  version: number;
  paused: boolean;
  active_id: string | null;
  items: QueueItem[];
}

/** POST /api/queue/sort keys. */
export type QueueSortKey = "name" | "size" | "added_at";
