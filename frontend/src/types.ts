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

/** What timing data a result *actually* contains (backend derive_timing_info).
 *  `level` is the granularity stored in the record, never what was requested. */
export interface TimingInfo {
  level: "word" | "segment" | "none" | "unknown";
  source: "forced_alignment" | "asr" | "unknown";
  alignment_requested: boolean;
  alignment_succeeded: boolean;
  segment_count: number;
  word_count: number | null;
  fallback_reason?: string | null;
}

/** Transcript anchor for reading position, independent of playback seconds. */
export interface ReaderPosition {
  segment_index?: number;
  text_offset?: number;
  within_block_ratio?: number;
  scroll_ratio?: number;
}

/** Orthogonal to QueueItemStatus: did the text reach durable storage? */
export type PersistenceState = "pending" | "saved" | "failed" | "unavailable";

/** Where a readable result lives. A temporary result has no history id and
 *  must never be shown as "saved". */
export type ResultRef =
  | { kind: "history"; historyId: string }
  | { kind: "temporary"; taskId: string; attemptId: string };

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
  cancelled?: boolean;
  chunk_failures?: number;
  chunk_failure_details?: string[];
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

export interface AppLimits {
  max_upload_bytes: number;
  max_audio_duration_seconds: number;
  queue_max_entries: number;
  queue_pending_entries: number;
  queue_max_bytes: number;
  queue_staged_bytes: number;
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
  items?: QueueItem[];
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
  title?: string;
  batch_id?: string | null;
  batch_name?: string | null;
  task_id?: string | null;
  source_path?: string | null;
  integrity?: "complete" | "partial";
  missing_ranges?: string[];
  alignment_requested?: boolean;
  alignment_fallback?: boolean;
  result_version?: number;
  attempt_id?: string | null;
  persistence_state?: PersistenceState;
  first_viewed_at?: string | null;
  read_position?: number;
  reader_position?: ReaderPosition | null;
  playback_position?: number;
  timing?: TimingInfo | null;
  match_fields?: string[];
  first_match?: { field: string; offset: number | null } | null;
  snippet?: string;
}

export interface HistorySearchResponse {
  items: HistoryItem[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
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
  title?: string;
  batch_id?: string | null;
  batch_name?: string | null;
  task_id?: string | null;
  source_path?: string | null;
  integrity?: "complete" | "partial";
  missing_ranges?: string[];
  alignment_requested?: boolean;
  alignment_fallback?: boolean;
  result_version?: number;
  attempt_id?: string | null;
  persistence_state?: PersistenceState;
  config_snapshot?: { model?: string | null; aligner_model?: string | null; alignment_requested?: boolean };
  first_viewed_at?: string | null;
  read_position?: number;
  reader_position?: ReaderPosition | null;
  playback_position?: number;
  timing?: TimingInfo | null;
  /** True while the transcript text came from the session-only recovery copy. */
  temporary?: boolean;
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
  title?: string;
  batch_id?: string | null;
  batch_name?: string | null;
  source_path?: string | null;
  integrity?: "complete" | "partial" | null;
  missing_ranges?: string[];
  alignment_fallback?: boolean;
  attempt_id?: string | null;
  result_version?: number | null;
  persistence_state?: PersistenceState;
  temporary_result_available?: boolean;
  can_retry_save?: boolean;
  save_error?: string | null;
  client_import_id?: string | null;
}

/** GET /api/queue/items/{qid}/result — a saved result points at history; a
 *  save-failed one carries the session-only payload. */
export interface QueueItemResult {
  kind: "saved" | "temporary";
  history_id?: string;
  item?: QueueItem;
  result?: {
    text: string;
    segments: Segment[];
    stats: Stats;
    timing?: TimingInfo | null;
    integrity: string;
    result_version?: number | null;
    filename: string;
    title?: string;
    source_path?: string | null;
    batch_id?: string | null;
    batch_name?: string | null;
    persistence_state: PersistenceState;
    persisted: boolean;
  };
}

export interface BatchTask {
  id: string;
  task_id?: string | null;
  filename: string;
  title?: string;
  source_path?: string | null;
  size?: number;
  align?: boolean;
  added_at?: string | null;
  finished_at?: string | null;
  status: QueueItemStatus;
  history_id: string | null;
  error?: string | null;
  integrity?: "complete" | "partial" | null;
  missing_ranges?: string[];
  alignment_fallback?: boolean;
  timing?: TimingInfo | null;
  attempt_id?: string | null;
  persistence_state?: PersistenceState;
  temporary_result_available?: boolean;
  can_retry_save?: boolean;
  save_error?: string | null;
  result_version?: number | null;
  duration: number;
  segment_count: number;
  first_viewed_at: string | null;
  read_position: number;
  reader_position?: ReaderPosition | null;
  playback_position?: number;
}

export interface BatchSummary {
  id: string;
  name: string;
  created_at: string | null;
  items: BatchTask[];
  counts: Record<string, number>;
}

export interface BatchesResponse {
  batches: BatchSummary[];
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
