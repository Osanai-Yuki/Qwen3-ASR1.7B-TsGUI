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
}

/** GET /api/health */
export interface HealthResponse {
  backend: boolean;
  llama_server: boolean;
  current_model: string | null;
  aligner_available: boolean;
  aligner_status: string;
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
}

/** GET /api/status — current job status. */
export interface JobStatusResponse {
  status: "idle" | "preparing" | "transcribing" | "aligning" | "done" | "error";
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
