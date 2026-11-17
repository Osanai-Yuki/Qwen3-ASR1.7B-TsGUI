import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import {
  clearAudioCache, clearFinishedQueue, deleteQueueItem, enqueueFiles, fetchAudioCache,
  fetchBatches, fetchHealth, fetchHistory, fetchHistoryRecord, fetchLimits, fetchQueueItemResult,
  deleteHistory, fetchQueue, shutdownApp, pauseQueue, reorderQueue, resumeQueue, retryQueueItem, retryQueueSave,
  updateHistoryView,
  addWorkbenchRoot, enqueueReference, fetchWorkbenchConfig, reexportQueueItem,
  removeWorkbenchRoot, scanWorkbenchRoot, updateWorkbenchConfig,
  fetchSummaryState, fetchSummaryModels, startBatchSummary, retryItemSummary,
  reexportBatch,
} from "./api";
import { useJobStatus } from "./hooks/useJobStatus";
import { useQueue } from "./hooks/useQueue";
import { useReadiness, phaseLabel } from "./hooks/useReadiness";
import { useHistoryResults, type ResultFilter } from "./hooks/useHistoryResults";
import type {
  AppLimits, BatchSummary, BatchTask, HealthResponse, HistoryItem, HistoryRecord,
  QueueItem, ReaderPosition, ResultRef, TimingInfo, WorkbenchConfig, WorkbenchScanItem,
  SummaryPhaseState, SummaryModelInfo,
} from "./types";
import { buildSubtitle, subtitleCues, SUBTITLE_FORMATS, type SubtitleFormat } from "./utils/subtitle";
import { formatShort, formatDateTime } from "./utils/format";
import { buildWorkbenchRows, EXPORT_STATE_LABEL, SUMMARY_STATE_LABEL, renderNamePreview, type WorkbenchRow } from "./utils/workbench";
import { isSupported, MAX_UPLOAD_MB } from "./utils/fileTypes";
import { addImportItems, reconcileReceipts, type ImportItem } from "./utils/importSession";
import {
  buildExportPlan, exportArchiveName, normalizeExportPath, zipBlob,
  type ExportCandidate, type LoadedExport,
} from "./utils/exportPlan";
import { loadPreferences, savePreferences, type WorkspacePreferences } from "./utils/preferences";
import { isApiError } from "./types";

type View = "library" | "tasks" | "reader" | "workbench";
type Filter = ResultFilter;
type Origin = {
  view: "library" | "tasks";
  batchId: string | null;
  query: string;
  filter: Filter;
  resultIds: string[];
  /** Parallel to resultIds: how to resolve each neighbour. Needed because a
   *  save-failed item has no history id, so a bare id list would let the pager
   *  walk into a record that cannot be fetched. */
  resultRefs?: ResultRef[];
  /** Reading anchor, not a raw scroll offset: survives re-layout and breaks. */
  readerPosition: ReaderPosition | null;
  /** Set when the opened result is a save-failed temporary payload. */
  temporaryRef?: ResultRef;
};
type Nav = { view: View; batchId: string | null; recordId: string | null; origin: Origin | null; query: string; filter: Filter };
const PAGE_SIZE = 36;
const INITIAL_NAV: Nav = { view: "library", batchId: null, recordId: null, origin: null, query: "", filter: "all" };

function localId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
  return Array.from(crypto.getRandomValues(new Uint8Array(8)), (n) => n.toString(16).padStart(2, "0")).join("").slice(0, 12);
}
function plainName(name: string): string { return name.replace(/\.[^.]+$/, "") || "transcript"; }
/** Stats with defaults: a save-failed temporary payload may carry only the
 *  fields the pipeline had at that moment, and the reader must still render. */
function statsOf(record: HistoryRecord | null) {
  const s = record?.stats ?? ({} as Partial<HistoryRecord["stats"]>);
  const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : 0);
  return {
    char_count: num(s.char_count),
    audio_duration: num(s.audio_duration),
    total_time: num(s.total_time),
    asr_time: num(s.asr_time),
    align_time: num(s.align_time),
    word_count: num(s.word_count),
    chunk_count: num(s.chunk_count),
    file_size: num(s.file_size),
    rtf: num(s.rtf),
    model: s.model || "未知模型",
    aligner_model: s.aligner_model ?? null,
    aligner_used: Boolean(s.aligner_used ?? record?.align_used),
    audio_cache_name: s.audio_cache_name ?? null,
    cancelled: Boolean(s.cancelled),
  };
}
function titleOf(item: { title?: string; filename?: string | null }): string { return item.title || plainName(item.filename || "transcript"); }
function dateOf(value?: string | null): string {
  if (!value) return "日期未记录";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value.slice(0, 10) : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "short", day: "numeric" }).format(d);
}
function durationOf(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "时长待检测";
  const n = Math.round(seconds);
  return Math.floor(n / 60) + ":" + String(n % 60).padStart(2, "0");
}
function sizeOf(bytes?: number): string { return !bytes || bytes <= 0 ? "" : (bytes / 1048576).toFixed(0) + " MB"; }
function canRead(task: Pick<BatchTask, "status" | "history_id">): boolean {
  return Boolean(task.history_id) && (task.status === "done" || task.status === "cancelled");
}
/** Human wording for the timing capability a result actually has. */
function timingLabel(timing?: TimingInfo | null, fallback?: { align_used?: boolean }): string {
  if (!timing) return fallback?.align_used ? "已使用词级强制对齐。" : "时间戳来自转写结果。";
  if (timing.level === "segment") {
    return timing.alignment_succeeded
      ? "使用词级对齐生成的句段时间，可分段定位。"
      : "时间戳来自转写结果，可分段定位。";
  }
  if (timing.alignment_requested && !timing.alignment_succeeded) {
    return "已请求词级对齐，但没有生成可定位时间戳。";
  }
  if (!timing.alignment_requested) return "本次未请求对齐，没有分段时间戳。";
  return "没有可用的分段定位时间戳。";
}
/** Backend job/error text is English; translate the codes the UI shows and
 *  keep anything unknown visible as-is rather than hiding it. */
const BACKEND_TEXT: Record<string, string> = {
  "preparing audio...": "正在准备音频…",
  "extracting audio (video → mp3)...": "正在从视频提取音频…",
  "switching gpu — unloading asr...": "正在释放识别模型（切换 GPU）…",
  "loading gpu forced aligner...": "正在加载 GPU 强制对齐器…",
  "running gpu forced alignment...": "正在进行 GPU 强制对齐…",
  "reloading asr model...": "正在重新加载识别模型…",
  "asr reload failed — retry boot to recover": "识别模型重新加载失败，需重启服务恢复",
  "finalizing forced alignment (crispasr)...": "正在收尾强制对齐…",
  "forced alignment...": "正在进行强制对齐…",
  "complete": "转写完成",
  "cancelled by user": "已由用户取消",
  "ffmpeg not available": "未安装或未找到 ffmpeg，无法处理视频",
  "ffmpeg not found on path": "未安装或未找到 ffmpeg，无法处理视频",
  "ffmpeg_missing": "未安装或未找到 ffmpeg，无法处理视频",
  "audio extraction failed": "音频提取失败",
  "extract_failed": "音频提取失败",
  "audio probe failed": "无法读取音频时长",
  "could not probe audio duration": "无法读取音频时长",
  "probe_failed": "无法读取音频时长",
  "no audio content": "文件中没有可用的音频内容",
  "empty_audio": "文件中没有可用的音频内容",
  "audio has no detectable duration": "文件中没有可用的音频内容",
  "audio split produced no chunks": "文件中没有可用的音频内容",
  "audio too long": "音频过长，超过处理上限",
  "too_long": "音频过长，超过处理上限",
  "audio split failed": "音频分段失败",
  "split_failed": "音频分段失败",
  "audio splitting failed": "音频分段失败",
  "no transcription text returned": "没有生成转写文字",
  "empty_result": "没有生成转写文字",
  "unsupported_type": "不支持的文件类型",
  "queue_full": "队列已满，请稍后再试",
  "payload_too_large": "文件超过大小上限",
  "bad_request": "请求参数有误",
  "not_found": "记录不存在",
  "conflict": "操作与当前状态冲突，请刷新后重试",
  "unavailable": "当前不可用",
  "gone": "原始文件已不存在，请重新添加",
  "staged file no longer exists; re-enqueue it": "原始文件已不存在，请重新添加",
  "only failed/cancelled items can be retried": "只有失败或已取消的任务可以重新转写",
  "history persistence is not configured": "未配置结果保存目录",
  "history write failed again; the temporary result is kept": "结果仍未能保存，临时结果已保留",
  "save_failed": "结果保存失败",
  "staged_file_missing": "原始文件已不存在，请重新添加",
  "invalid_staged_name": "暂存文件名无效，请重新添加文件",
  "version_reservation_failed": "结果版本分配失败，可重新转写",
  "internal_error": "内部错误，可重新转写",
  "boot already in progress": "服务正在启动，请稍候",
  "model is required": "未指定模型",
  "a transcription is in progress": "已有转写任务在进行",
  "a transcription is already in progress": "已有转写任务在进行",
  "a model switch is already in progress": "正在切换模型",
  "backend is not ready": "本地服务尚未就绪",
  "model failed to load; see server logs": "模型加载失败，请查看服务日志",
  "asr load failed; see server logs": "识别模型加载失败，请查看服务日志",
  "model switch failed; see server logs": "模型切换失败，请查看服务日志",
  "gpu:qwen-asr": "GPU 对齐器可用",
  "cpu:crispasr": "CPU 对齐器可用",
  "ready": "对齐器就绪",
};
function statusMessage(raw?: string | null): string {
  const text = (raw ?? "").trim();
  if (!text) return "";
  if (/[一-龥]/.test(text)) return text;
  const exact = BACKEND_TEXT[text.toLowerCase()];
  if (exact) return exact;
  const chunk = /^transcribing chunk (\d+)\/(\d+) \(([^)]+)\)/i.exec(text);
  if (chunk) return "正在转写第 " + chunk[1] + "/" + chunk[2] + " 段（" + chunk[3] + "）";
  const align = /^(?:gpu align|aligning) chunk (\d+)\/(\d+)/i.exec(text);
  if (align) return "正在对齐第 " + align[1] + "/" + align[2] + " 段";
  const model = /^unknown model (.+)$/i.exec(text);
  if (model) return "未知模型：" + model[1];
  const limit = /^audio duration ([0-9.]+)s exceeds limit ([0-9.]+)s$/i.exec(text);
  if (limit) return "音频时长 " + limit[1] + " 秒，超出上限 " + limit[2] + " 秒";
  return text;
}
function statusOf(task: Pick<BatchTask, "status" | "integrity" | "history_id" | "error" | "persistence_state" | "save_error">) {
  if (task.status === "running") return { label: "正在处理", tone: "running", note: "正在转写当前文件" };
  if (task.status === "queued") return { label: "等待处理", tone: "queued", note: "已加入本地队列" };
  if (task.status === "error") {
    if (task.save_error === "history_save_failed" || task.persistence_state === "failed") {
      return { label: "结果未保存", tone: "warning", note: "转写已完成，长期记录保存失败；本次会话仍可查看" };
    }
    return { label: "处理失败", tone: "error", note: statusMessage(task.error) || "可重新转写此任务" };
  }
  if (task.status === "cancelled") {
    if (task.persistence_state === "failed" || task.save_error === "history_save_failed") {
      return { label: "已取消 · 结果未保存", tone: "warning", note: "部分转写本次会话可读，长期记录保存失败" };
    }
    return task.history_id
      ? { label: "已取消，有部分结果", tone: "warning", note: "部分转写已保存" }
      : { label: "已取消，无结果", tone: "warning", note: "原文件保留，可重新转写" };
  }
  if (task.status === "done" && !task.history_id) {
    if (task.persistence_state === "failed" || task.save_error === "history_save_failed") {
      return { label: "结果未保存", tone: "warning", note: "转写已完成，本次会话可读，长期记录保存失败" };
    }
    return task.error === "history_deleted"
      ? { label: "结果已删除", tone: "error", note: "结果库记录已删除" }
      : { label: "结果未保存", tone: "error", note: "文字结果未能保存为长期记录" };
  }
  if (task.integrity === "partial") return { label: "已完成，有缺失", tone: "warning", note: "部分音频片段未能识别" };
  return { label: "已完成", tone: "done", note: "完整结果已保存" };
}
function marked(text: string, query: string) {
  const q = query.trim();
  if (!q) return text;
  const at = text.toLocaleLowerCase().indexOf(q.toLocaleLowerCase());
  return at < 0 ? text : <>{text.slice(0, at)}<mark>{text.slice(at, at + q.length)}</mark>{text.slice(at + q.length)}</>;
}
function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_");
  document.body.appendChild(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
/** Display-only grouping of adjacent segments into reading paragraphs. */
function groupParagraphs(segments: { start: number; end: number; text: string }[]): { index: number; text: string; start: number; end: number }[] {
  const groups: { index: number; text: string; start: number; end: number }[] = [];
  for (const [index, segment] of segments.entries()) {
    const previous = groups[groups.length - 1];
    const gap = previous ? segment.start - previous.end : Infinity;
    const long = segment.text.trim().length > 60;
    const endsSentence = /[。！？.!?…]$/.test(previous?.text.trim() ?? "");
    if (previous && !long && !endsSentence && gap < 2.5) {
      previous.text = previous.text + " " + segment.text;
      previous.end = segment.end;
      continue;
    }
    groups.push({ index, text: segment.text, start: segment.start, end: segment.end });
  }
  return groups;
}
/** Search affordance drawn as geometry: the old "⌕" glyph fell back to a
 *  system font whose ink sat off-centre inside the black disc. */
function SearchIcon() {
  return <svg className="asr-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" aria-hidden="true"><circle cx="10.6" cy="10.6" r="5.9" /><path d="M15.1 15.1 19.4 19.4" /></svg>;
}

function Boot({ state, failed, retry }: { state: ReturnType<typeof useReadiness>["state"]; failed: boolean; retry: () => void }) {
  return <main className="asr-boot"><b className="asr-boot-brand">Qwen ASR</b><h1>正在准备工作区</h1><p>{state ? phaseLabel(state.phase, state.error ? statusMessage(state.error) : null) : failed ? "正在连接本地服务…" : "正在启动本地服务…"}</p>{state?.error && <button className="asr-button asr-button-primary" onClick={retry}>重试启动</button>}<i /></main>;
}

export default function Workspace() {
  const { state: readiness, ready, switching, failed, restart } = useReadiness();
  const { state: queueState, refresh: refreshQueue } = useQueue();
  const queueItems = queueState?.items ?? [];
  const activeTask = queueItems.find((item) => item.id === queueState?.active_id) ?? queueItems.find((item) => item.status === "running") ?? null;
  const { status: jobStatus } = useJobStatus(Boolean(activeTask));
  const [nav, setNav] = useState<Nav>(() => {
    const state = window.history.state as { workspace?: boolean; nav?: Nav } | null;
    return state?.workspace && state.nav ? state.nav : INITIAL_NAV;
  });
  const navRef = useRef(nav);
  useEffect(() => { navRef.current = nav; }, [nav]);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [record, setRecord] = useState<HistoryRecord | null>(null);
  const [recordError, setRecordError] = useState("");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [limits, setLimits] = useState<AppLimits | null>(null);
  const [cacheInfo, setCacheInfo] = useState<{ count: number; size_bytes: number } | null>(null);
  const [pageError, setPageError] = useState("");
  const [toast, setToast] = useState("");
  const [taskQuery, setTaskQuery] = useState("");
  const [taskFilter, setTaskFilter] = useState<"all" | "attention" | "ready" | "waiting">("all");
  const [taskSort, setTaskSort] = useState<"added" | "name">("added");
  const [selectedTasks, setSelectedTasks] = useState<Set<string>>(new Set());
  const [selectedResults, setSelectedResults] = useState<Set<string>>(new Set());
  const [selectedResultData, setSelectedResultData] = useState<Map<string, HistoryItem>>(new Map());
  const [detailTask, setDetailTask] = useState<BatchTask | null>(null);
  const mainRef = useRef<HTMLElement>(null);
  const toastTimer = useRef<number | null>(null);

  const [preferences, setPreferences] = useState<WorkspacePreferences>(() => loadPreferences());
  const patchPreferences = useCallback((patch: Partial<WorkspacePreferences>) => {
    setPreferences((current) => savePreferences({ ...current, ...patch }));
  }, []);

  const [importOpen, setImportOpen] = useState(false);
  const [picked, setPicked] = useState<ImportItem[]>([]);
  const [batchName, setBatchName] = useState("");
  const [batchId, setBatchId] = useState("");
  const [align, setAlign] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const folderInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [exiting, setExiting] = useState(false);
  const [exited, setExited] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [navPanelOpen, setNavPanelOpen] = useState(false);
  const frameRef = useRef<HTMLDivElement>(null);

  const [exportCandidates, setExportCandidates] = useState<ExportCandidate[]>([]);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportFormats, setExportFormats] = useState<SubtitleFormat[]>(["srt"]);
  const [includePartial, setIncludePartial] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [readerQuery, setReaderQuery] = useState("");
  const [readerFilterMode, setReaderFilterMode] = useState(false);
  const [readerHitIndex, setReaderHitIndex] = useState(0);
  const [focusedSegment, setFocusedSegment] = useState<number | null>(null);
  const [audioError, setAudioError] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null);
  const [saveBusyId, setSaveBusyId] = useState<string | null>(null);
  const [batchRetryBusy, setBatchRetryBusy] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);
  const transcriptRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const paragraphRefs = useRef<Array<HTMLParagraphElement | null>>([]);
  const lastPersist = useRef(0);
  const pendingReaderPosition = useRef<ReaderPosition | null>(null);
  const scrollSaveTimer = useRef<number | null>(null);
  const [manualInvalidate, setManualInvalidate] = useState(0);

  // ---- workbench mode (folder-driven batch pipeline) --------------------
  const [wbConfig, setWbConfig] = useState<WorkbenchConfig | null>(null);
  const [wbRootInput, setWbRootInput] = useState("");
  const [wbExportInput, setWbExportInput] = useState("");
  const [wbScanRoot, setWbScanRoot] = useState("");
  const [wbScan, setWbScan] = useState<WorkbenchScanItem[]>([]);
  const [wbTruncated, setWbTruncated] = useState(false);
  const [wbSelected, setWbSelected] = useState<Set<string>>(new Set());
  const [wbBatchName, setWbBatchName] = useState("");
  const [wbAlign, setWbAlign] = useState(false);
  const [wbBusy, setWbBusy] = useState(false);
  const [wbError, setWbError] = useState("");
  const [wbRecords, setWbRecords] = useState<Map<string, HistoryItem>>(new Map());
  const [wbSummary, setWbSummary] = useState<SummaryPhaseState | null>(null);
  const [wbModels, setWbModels] = useState<SummaryModelInfo[]>([]);
  const [wbSummaryBatch, setWbSummaryBatch] = useState("");
  const [wbTemplate, setWbTemplate] = useState("{stem}");
  const [wbPrompt, setWbPrompt] = useState("");

  const notify = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 4200);
  }, []);
  const refreshBatches = useCallback(() => { fetchBatches().then((value) => setBatches(value.batches)).catch(() => {}); }, []);
  const refreshCache = useCallback(() => { fetchAudioCache().then(setCacheInfo).catch(() => {}); }, []);
  const invalidateResults = useCallback(() => setManualInvalidate((value) => value + 1), []);

  useEffect(() => {
    if (!ready || switching) return;
    fetchHealth().then(setHealth).catch(() => {});
    fetchLimits().then(setLimits).catch(() => {});
    refreshCache();
  }, [ready, switching, refreshCache]);
  useEffect(() => {
    if (ready && !switching) refreshBatches();
  }, [ready, switching, queueState?.version, refreshBatches]);

  // A06: invalidate the result list when the *set* of durably-saved results
  // changes, not on every queue version bump.  A task finishing, a save retry
  // succeeding, or a history delete all change this signature; a reorder or a
  // plain progress update does not, so those never trigger a full re-query.
  const savedSignature = useMemo(() => {
    const ids = queueItems
      .filter((item) => (item.persistence_state === "saved" || item.status === "done") && item.history_id)
      .map((item) => item.history_id as string);
    return [...new Set(ids)].sort().join(",");
  }, [queueItems]);
  const invalidateToken = useMemo(
    () => `${savedSignature}|${manualInvalidate}`,
    [savedSignature, manualInvalidate],
  );

  const results = useHistoryResults(
    { q: nav.query, filter: nav.filter, batchId: null },
    { enabled: ready && !switching && nav.view !== "reader", pageSize: PAGE_SIZE, invalidateToken },
  );

  // ---- reader loading (A02: independent of the global query) -------------
  // Depend on a stable key, not the ref object: a fresh object literal every
  // render would re-run this effect forever, leaving the reader stuck on the
  // previous record.
  const tempRefKey = nav.origin?.temporaryRef
    ? nav.origin.temporaryRef.kind === "temporary"
      ? `temporary:${nav.origin.temporaryRef.taskId}:${nav.origin.temporaryRef.attemptId}`
      : `history:${nav.origin.temporaryRef.historyId}`
    : "";
  useEffect(() => {
    if (!nav.recordId || !ready) { setRecord(null); setRecordError(""); return; }
    const ref: ResultRef | null = nav.origin?.temporaryRef
      ?? (nav.recordId.includes(":") ? null : { kind: "history", historyId: nav.recordId });
    let cancelled = false;
    setRecord(null); setRecordError("");
    setFocusedSegment(null); setAudioError(false); setCurrentTime(0); lastPersist.current = 0;
    pendingReaderPosition.current = null;
    transcriptRefs.current = [];
    paragraphRefs.current = [];
    setReaderHitIndex(0);
    const fail = (value: string) => { if (!cancelled) setRecordError(value); };
    if (ref && ref.kind === "temporary") {
      if (!ref.attemptId) { fail("这份结果的临时文字已经不可用，请从原文件重新转写。"); return () => { cancelled = true; }; }
      fetchQueueItemResult(ref.taskId).then((value) => {
        if (cancelled) return;
        if ("error" in value) { fail(value.detail || value.error); return; }
        if (value.kind !== "temporary" || !value.result) { fail("这份结果的临时文字已经不可用，请从原文件重新转写。"); return; }
        const info = value.result;
        setRecord({
          id: `${ref.taskId}:${ref.attemptId}`,
          created_at: "",
          filename: info.filename,
          text: info.text,
          segments: info.segments,
          stats: info.stats,
          align_used: Boolean(info.stats.aligner_used),
          audio_cache_name: info.stats.audio_cache_name ?? null,
          title: info.title,
          batch_id: info.batch_id,
          batch_name: info.batch_name,
          source_path: info.source_path,
          integrity: info.integrity as "complete" | "partial",
          result_version: info.result_version ?? undefined,
          timing: info.timing,
          temporary: true,
          first_viewed_at: null,
          read_position: 0,
          playback_position: 0,
          reader_position: null,
          persistence_state: "failed",
        });
      }).catch((error: unknown) => fail(error instanceof Error ? error.message : String(error)));
      return () => { cancelled = true; };
    }
    if (!ref) { fail("这份结果已经不可用。"); return () => { cancelled = true; }; }
    fetchHistoryRecord(ref.historyId).then((value) => {
      if (cancelled) return;
      if ("error" in value) { fail(value.detail || value.error); return; }
      setRecord(value);
      updateHistoryView(value.id, value.playback_position ?? value.read_position ?? 0).then((view) => {
        if (!("first_viewed_at" in view) || cancelled) return;
        setBatches((current) => current.map((batch) => ({
          ...batch,
          items: batch.items.map((item) => item.history_id === value.id
            ? { ...item, first_viewed_at: view.first_viewed_at, read_position: view.read_position,
                reader_position: view.reader_position, playback_position: view.playback_position }
            : item),
        })));
      }).catch(() => {});
    }).catch((error: unknown) => fail(error instanceof Error ? error.message : String(error)));
    return () => { cancelled = true; };
  }, [nav.recordId, tempRefKey, ready]);

  // Audio position restore — once, after media metadata is available.
  useEffect(() => {
    if (!record?.audio_cache_name || !audioRef.current) return;
    const audio = audioRef.current;
    const savedPosition = record.playback_position ?? record.read_position ?? 0;
    if (!savedPosition) return;
    const seek = () => { audio.currentTime = savedPosition; setCurrentTime(audio.currentTime); };
    audio.addEventListener("loadedmetadata", seek, { once: true });
    if (audio.readyState >= 1) seek();
    return () => audio.removeEventListener("loadedmetadata", seek);
  }, [record?.id, record?.audio_cache_name, record?.playback_position, record?.read_position]);

  function applyScrollRatio(ratio: number) {
    const adapter = scrollAdapter();
    if (adapter.isWindow) {
      window.scrollTo({ top: ratio * (document.documentElement.scrollHeight - window.innerHeight) });
    } else if (adapter.element) {
      adapter.element.scrollTop = ratio * (adapter.element.scrollHeight - adapter.element.clientHeight);
    }
  }
  /** Which element actually scrolls right now (window on mobile/short, main on desktop). */
  function scrollAdapter(): { element: HTMLElement | null; isWindow: boolean } {
    const main = mainRef.current;
    if (!main) return { element: null, isWindow: true };
    const style = window.getComputedStyle(main);
    const constrained = style.overflowY === "auto" || style.overflowY === "scroll";
    const actuallyScrolls = main.scrollHeight > main.clientHeight + 4;
    if (!constrained || !actuallyScrolls) return { element: null, isWindow: true };
    return { element: main, isWindow: false };
  }
  /** Keep a restored or searched passage below the sticky reader controls. */
  function scrollReaderNodeIntoView(node: HTMLElement, behavior: ScrollBehavior = "auto") {
    const adapter = scrollAdapter();
    const scroller = adapter.isWindow ? document.documentElement : adapter.element;
    const toolbar = mainRef.current?.querySelector<HTMLElement>(".asr-reader-top");
    if (!scroller || !toolbar) { node.scrollIntoView({ block: "center", behavior }); return; }
    const top = adapter.isWindow ? parseFloat(window.getComputedStyle(toolbar).top) || 0 : 0;
    const previous = scroller.style.scrollPaddingTop;
    scroller.style.scrollPaddingTop = `${top + toolbar.getBoundingClientRect().height + 8}px`;
    try { node.scrollIntoView({ block: "center", behavior }); }
    finally { scroller.style.scrollPaddingTop = previous; }
  }
  /** Scroll to a saved anchor using whichever presentation is on screen. */
  function scrollToAnchor(anchor: ReaderPosition | null): boolean {
    if (!anchor) return false;
    if (anchor.segment_index != null) {
      const direct = transcriptRefs.current[anchor.segment_index];
      if (direct?.isConnected) { scrollReaderNodeIntoView(direct); return true; }
      // Paragraph view: pick the group that contains the saved segment.
      let best: HTMLParagraphElement | null = null;
      for (const node of paragraphRefs.current) {
        if (!node || !node.isConnected) continue;
        const index = Number(node.dataset.segmentIndex ?? -1);
        if (index >= 0 && index <= anchor.segment_index) best = node;
        if (index > anchor.segment_index) break;
      }
      if (best) { scrollReaderNodeIntoView(best); return true; }
    }
    if (anchor.text_offset != null) {
      let target: HTMLParagraphElement | null = null;
      for (const node of paragraphRefs.current) {
        if (!node || !node.isConnected) continue;
        const offset = Number(node.dataset.offset ?? -1);
        if (offset < 0) continue;
        target = node;
        if (offset >= anchor.text_offset) break;
      }
      if (target) { scrollReaderNodeIntoView(target); return true; }
    }
    if (anchor.scroll_ratio != null) { applyScrollRatio(anchor.scroll_ratio); return true; }
    return false;
  }
  /** Restore after layout (and again after local fonts settle). */
  function restoreAnchorSoon(anchor: ReaderPosition | null | undefined) {
    if (!anchor) return () => {};
    const timers = [60, 320].map((delay) => window.setTimeout(() => scrollToAnchor(anchor), delay));
    if (document.fonts?.ready) {
      document.fonts.ready.then(() => scrollToAnchor(anchor)).catch(() => {});
    }
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }
  /** Reading-position restore — after the transcript has rendered. */
  useEffect(() => {
    if (!record || record.temporary) return;
    return restoreAnchorSoon(record.reader_position);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [record?.id, record?.temporary]);

  // ---- popstate / history ------------------------------------------------
  useEffect(() => {
    const onPop = (event: PopStateEvent) => {
      const saved = event.state as { workspace?: boolean; nav?: Nav } | null;
      const next = saved?.workspace && saved.nav ? saved.nav : INITIAL_NAV;
      setNav(next);
      if (next.view !== "reader") setReaderQuery("");
      setSelectedTasks(new Set()); setSelectedResults(new Set());
      if (next.view === "reader" && next.recordId) {
        // Returning to a result restores its reading anchor, not a scroll offset.
        const anchor = next.origin?.readerPosition ?? null;
        window.setTimeout(() => scrollToAnchor(anchor), 80);
      }
    };
    const state = window.history.state as { workspace?: boolean } | null;
    if (!state?.workspace) window.history.replaceState({ workspace: true, nav: navRef.current }, "", window.location.pathname + window.location.search + "#library");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // ---- modal focus / inert ----------------------------------------------
  useEffect(() => {
    const isOpen = importOpen || exportOpen || settingsOpen || Boolean(detailTask);
    const frame = frameRef.current;
    if (!isOpen || !frame) return;
    frame.setAttribute("inert", "");
    const dialog = document.querySelector<HTMLElement>(".asr-modal");
    const focusables = () => Array.from(dialog?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ) ?? []);
    const previous = document.activeElement as HTMLElement | null;
    window.setTimeout(() => focusables()[0]?.focus(), 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (importOpen) setImportOpen(false);
        else if (exportOpen) setExportOpen(false);
        else if (detailTask) setDetailTask(null);
        else setSettingsOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const list = focusables();
      if (!list.length) return;
      const first = list[0]; const last = list[list.length - 1];
      if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); }
      else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      frame.removeAttribute("inert");
      window.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [importOpen, exportOpen, settingsOpen, detailTask]);

  // ---- drag & drop ------------------------------------------------------
  useEffect(() => {
    let depth = 0;
    const onEnter = (event: globalThis.DragEvent) => { if (event.dataTransfer?.types.includes("Files")) { depth += 1; setDragActive(true); } };
    const onLeave = (event: globalThis.DragEvent) => { if (event.dataTransfer?.types.includes("Files")) { depth = Math.max(0, depth - 1); if (!depth) setDragActive(false); } };
    const onOver = (event: globalThis.DragEvent) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); };
    const onDrop = (event: globalThis.DragEvent) => {
      depth = 0; setDragActive(false);
      if (event.defaultPrevented) return;
      const files = Array.from(event.dataTransfer?.files ?? []);
      if (!files.length) return;
      event.preventDefault(); openImport(files);
    };
    window.addEventListener("dragenter", onEnter); window.addEventListener("dragleave", onLeave);
    window.addEventListener("dragover", onOver); window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter); window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("dragover", onOver); window.removeEventListener("drop", onDrop);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [limits]);

  const pushNav = useCallback((next: Nav, replace = false) => {
    const hash = next.view === "reader" && next.recordId
      ? "#reader/" + next.recordId
      : next.view === "tasks" ? "#tasks/" + (next.batchId || "")
      : next.view === "workbench" ? "#workbench" : "#library";
    const state = { workspace: true, nav: next };
    const url = window.location.pathname + window.location.search + hash;
    if (replace) window.history.replaceState(state, "", url); else window.history.pushState(state, "", url);
    setNav(next); setPageError("");
  }, []);
  const navigate = useCallback((view: "library" | "tasks", batchId: string | null = nav.batchId) => {
    setSelectedTasks(new Set()); setSelectedResults(new Set());
    pushNav({ ...nav, view, batchId, recordId: null, origin: null });
  }, [nav, pushNav]);
  function setQuery(query: string) {
    setSelectedResults(new Set()); setSelectedResultData(new Map());
    pushNav({ ...nav, query }, true);
  }
  function setFilter(filter: Filter) {
    setSelectedResults(new Set()); setSelectedResultData(new Map());
    pushNav({ ...nav, filter }, true);
  }

  // ---- workbench handlers ------------------------------------------------
  const loadWbConfig = useCallback(async () => {
    const cfg = await fetchWorkbenchConfig();
    setWbConfig(cfg);
    setWbExportInput(cfg.export_root || "");
    setWbTemplate(cfg.export_name_template || "{stem}");
    setWbPrompt(cfg.summary_prompt || "");
    setWbScanRoot((current) => current || cfg.roots[0]?.id || "");
  }, []);
  const refreshWbRecords = useCallback(async () => {
    const { items } = await fetchHistory();
    setWbRecords(new Map(items.map((it) => [it.id, it])));
  }, []);
  useEffect(() => {
    if (nav.view !== "workbench") return;
    void loadWbConfig();
    void refreshWbRecords();
  }, [nav.view, queueState?.version, loadWbConfig, refreshWbRecords]);
  const wbRows = useMemo(() => buildWorkbenchRows(queueItems, wbRecords), [queueItems, wbRecords]);
  const wbBatches = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of wbRows) if (row.batchId && !seen.has(row.batchId)) seen.set(row.batchId, row.batchName);
    return [...seen.entries()].map(([id, name]) => ({ id, name }));
  }, [wbRows]);
  useEffect(() => {
    if (nav.view !== "workbench") return;
    void fetchSummaryModels().then((r) => setWbModels(r.models));
    const tick = () => {
      void fetchSummaryState().then(setWbSummary);
      void refreshWbRecords();
    };
    tick();
    const timer = window.setInterval(tick, 3000);
    return () => window.clearInterval(timer);
  }, [nav.view, refreshWbRecords]);

  async function wbToggleSummarize() {
    if (!wbConfig) return;
    const res = await updateWorkbenchConfig({ summarize: !wbConfig.summarize });
    if (!isApiError(res)) setWbConfig(res);
  }
  async function wbSelectModel(name: string) {
    const res = await updateWorkbenchConfig({ summary_model: name || null });
    if (!isApiError(res)) setWbConfig(res);
  }
  async function wbStartBatch() {
    const batchId = wbSummaryBatch || wbBatches[0]?.id;
    if (!batchId) return;
    setWbError("");
    const res = await startBatchSummary(batchId);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    notify("已加入总结队列；换模期间转写暂停");
    void fetchSummaryState().then(setWbSummary);
  }
  async function wbRetrySummary(historyId: string) {
    setWbError("");
    const res = await retryItemSummary(historyId);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    notify("已加入单条总结队列");
    void fetchSummaryState().then(setWbSummary);
  }
  async function wbSaveTemplate() {
    setWbError("");
    const res = await updateWorkbenchConfig({ export_name_template: wbTemplate.trim() });
    if (isApiError(res)) { setWbError("命名模板无效：需包含 {stem}，占位符仅限 stem/date/time/model/batch/id"); return; }
    setWbConfig(res);
    notify("命名模板已保存");
  }
  async function wbSavePrompt() {
    setWbError("");
    const res = await updateWorkbenchConfig({ summary_prompt: wbPrompt.trim() || null });
    if (isApiError(res)) { setWbError("prompt 模板无效（不超过 4000 字）"); return; }
    setWbConfig(res);
    notify(wbPrompt.trim() ? "总结 prompt 已保存" : "已恢复默认总结 prompt");
  }
  async function wbReexportBatch() {
    const batchId = wbSummaryBatch || wbBatches[0]?.id;
    if (!batchId) return;
    setWbError("");
    const res = await reexportBatch(batchId);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    notify(`批量重导完成：成功 ${res.done}，失败 ${res.failed}`);
    void refreshQueue();
  }
  async function wbForceSummarize() {
    const batchId = wbSummaryBatch || wbBatches[0]?.id;
    if (!batchId) return;
    setWbError("");
    const res = await startBatchSummary(batchId, true);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    notify("已加入整批重总结队列（含已完成条目）");
    void fetchSummaryState().then(setWbSummary);
  }
  const wbNamePreview = renderNamePreview(wbTemplate, {
    stem: "demo-meeting",
    date: new Date().toISOString().slice(0, 10).replace(/-/g, ""),
    time: new Date().toTimeString().slice(0, 5).replace(":", ""),
    model: readiness?.current_model || "asr-model",
    batch: wbBatchName.trim() || "批次",
    id: "abcd1234",
  });

  async function wbAddRoot() {
    const path = wbRootInput.trim();
    if (!path) return;
    setWbBusy(true); setWbError("");
    const res = await addWorkbenchRoot(path);
    setWbBusy(false);
    if (isApiError(res)) {
      setWbError(res.error === "not_a_directory" ? "路径不存在或不是文件夹"
        : res.error === "duplicate_root" ? "该文件夹已批准" : statusMessage(res.error));
      return;
    }
    setWbRootInput("");
    await loadWbConfig();
    notify("已批准文件夹 " + res.root.label);
  }
  async function wbRemoveRoot(id: string) {
    await removeWorkbenchRoot(id);
    if (wbScanRoot === id) { setWbScanRoot(""); setWbScan([]); setWbSelected(new Set()); }
    await loadWbConfig();
  }
  async function wbDoScan() {
    if (!wbScanRoot) return;
    setWbBusy(true); setWbError("");
    const res = await scanWorkbenchRoot(wbScanRoot);
    setWbBusy(false);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    setWbScan(res.items);
    setWbTruncated(res.truncated);
    setWbSelected(new Set(res.items.map((it) => it.rel_path)));
  }
  async function wbSaveExport() {
    const res = await updateWorkbenchConfig({ export_root: wbExportInput.trim() || null });
    if (isApiError(res)) { setWbError("导出目录无效：路径不存在或不是文件夹"); return; }
    setWbConfig(res);
    notify("导出设置已保存");
  }
  async function wbToggleAuto() {
    if (!wbConfig) return;
    const res = await updateWorkbenchConfig({ auto_export: !wbConfig.auto_export });
    if (!isApiError(res)) setWbConfig(res);
  }
  async function wbToggleFormat(fmt: string) {
    if (!wbConfig) return;
    const next = wbConfig.formats.includes(fmt)
      ? wbConfig.formats.filter((f) => f !== fmt)
      : [...wbConfig.formats, fmt];
    if (!next.length) { setWbError("至少保留一种导出格式"); return; }
    const res = await updateWorkbenchConfig({ formats: next });
    if (!isApiError(res)) setWbConfig(res);
  }
  async function wbEnqueue() {
    if (!wbScanRoot || !wbSelected.size) return;
    setWbBusy(true); setWbError("");
    const res = await enqueueReference(wbScanRoot, [...wbSelected], wbAlign, wbBatchName.trim());
    setWbBusy(false);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    notify(`已加入 ${res.items.length} 个文件，按顺序转写`);
    setWbSelected(new Set());
    setWbBatchName("");
    void refreshQueue();
  }
  async function wbReexport(id: string) {
    setWbBusy(true); setWbError("");
    const res = await reexportQueueItem(id);
    setWbBusy(false);
    if (isApiError(res)) { setWbError(statusMessage(res.error)); return; }
    notify("已重新导出");
    void refreshQueue();
  }
  function wbOpenResult(row: WorkbenchRow) {
    if (!row.historyId) return;
    pushNav({
      ...nav, view: "reader", recordId: row.historyId,
      origin: { view: "library", batchId: null, query: "", filter: "all", resultIds: [row.historyId], readerPosition: null },
    });
  }

  // ---- import (A01: stable client identity, no name/size dedupe) --------
  const reasonFor = useCallback((file: File): string | null => {
    const maxBytes = limits?.max_upload_bytes ?? MAX_UPLOAD_MB * 1048576;
    const maxMb = Math.round(maxBytes / 1048576);
    if (!isSupported(file.name)) return "不支持的文件类型";
    if (file.size > maxBytes) return `超过 ${maxMb} MB 上限`;
    return null;
  }, [limits]);
  function addFiles(incoming: File[]) {
    setPicked((previous) => addImportItems(previous, incoming, reasonFor));
  }
  function openImport(files: File[] = []) {
    setBatchId(localId());
    setBatchName(files[0]
      ? ((files[0] as File & { webkitRelativePath?: string }).webkitRelativePath?.split("/")[0] || "新建转写批次")
      : "新建转写批次");
    setImportError("");
    // A10: keep the user's last choice; never silently reset it.
    setAlign(loadPreferences().alignmentRequested);
    setImportOpen(true);
    if (files.length) addFiles(files);
  }
  function onPicker(event: ChangeEvent<HTMLInputElement>) {
    addFiles(Array.from(event.target.files ?? [])); event.target.value = "";
  }
  async function submitImport() {
    const valid = picked.filter((item) => item.selected && !item.reason);
    if (!batchName.trim()) { setImportError("先给这一批文件起个名称。"); return; }
    if (!valid.length || importBusy) return;
    setImportBusy(true); setImportError("");
    try {
      const response = await enqueueFiles(
        valid.map((item) => item.file),
        align && Boolean(health?.aligner_available),
        {
          id: batchId || localId(),
          name: batchName.trim(),
          sourcePaths: valid.map((item) => item.path),
          clientImportIds: valid.map((item) => item.clientId),
        },
      );
      if ("error" in response) {
        if (response.items?.length) {
          // Reconcile by client id: the first item may be accepted while the
          // second is over the limit, and only the accepted one disappears.
          const accepted = response.items
            .map((item) => item.client_import_id)
            .filter((id): id is string => Boolean(id));
          setPicked((rows) => reconcileReceipts(rows, accepted));
          refreshQueue(); refreshBatches();
          setImportError(statusMessage(response.detail || response.error) + "。已提交文件已从清单移除，其余文件仍保留。");
        } else setImportError(statusMessage(response.detail || response.error));
        return;
      }
      patchPreferences({ alignmentRequested: align });
      setImportOpen(false); setPicked([]); setTaskQuery(""); setTaskFilter("all");
      pushNav({ ...nav, view: "tasks", batchId: batchId || response.items[0]?.batch_id || null, recordId: null, origin: null });
      refreshQueue(); refreshBatches();
      notify("“" + batchName.trim() + "”已加入待处理，共 " + response.items.length + " 个文件。");
    } catch (error) { setImportError(error instanceof Error ? error.message : String(error)); }
    finally { setImportBusy(false); }
  }

  const activeBatch = useMemo(() => batches.find((batch) => batch.id === nav.batchId) ?? batches[0] ?? null, [batches, nav.batchId]);
  const taskRows = useMemo(() => {
    if (!activeBatch) return [];
    let rows = [...activeBatch.items];
    const needle = taskQuery.trim().toLocaleLowerCase();
    if (needle) rows = rows.filter((item) => (item.filename + " " + (item.source_path || "") + " " + (item.error || "")).toLocaleLowerCase().includes(needle));
    if (taskFilter === "attention") rows = rows.filter((item) => item.status === "error" || item.status === "cancelled" || item.integrity === "partial" || item.persistence_state === "failed");
    if (taskFilter === "ready") rows = rows.filter((item) => canRead(item) || item.temporary_result_available);
    if (taskFilter === "waiting") rows = rows.filter((item) => item.status === "queued" || item.status === "running");
    if (taskSort === "name") rows.sort((a, b) => a.filename.localeCompare(b.filename, "zh-CN", { numeric: true }));
    else rows.sort((a, b) => (b.added_at || "").localeCompare(a.added_at || ""));
    return rows;
  }, [activeBatch, taskQuery, taskFilter, taskSort]);
  const queued = useMemo(() => queueItems.filter((item) => item.status === "queued"), [queueItems]);
  const doneCount = activeBatch?.items.filter((item) => item.status === "done" && item.integrity !== "partial" && item.persistence_state !== "failed").length ?? 0;
  // Cancelled work is its own bucket: it used to be folded into “有缺失”, so the
  // number did not match what the label said.
  const partialCount = activeBatch?.items.filter((item) => item.integrity === "partial" && item.status !== "cancelled").length ?? 0;
  const cancelledCount = activeBatch?.items.filter((item) => item.status === "cancelled").length ?? 0;
  const failedCount = activeBatch?.items.filter((item) => item.status === "error" && item.persistence_state !== "failed").length ?? 0;
  const unsavedCount = activeBatch?.items.filter((item) => item.persistence_state === "failed" || item.save_error === "history_save_failed").length ?? 0;
  const attentionCount = activeBatch?.items.filter((item) => item.status === "error" || item.status === "cancelled" || item.integrity === "partial" || item.persistence_state === "failed").length ?? 0;
  const queuedInBatch = activeBatch?.items.filter((item) => item.status === "queued").length ?? 0;
  const runningInBatch = activeBatch?.items.filter((item) => item.status === "running").length ?? 0;

  // ---- reader navigation -------------------------------------------------
  function captureReaderPosition(): ReaderPosition | null {
    const adapter = scrollAdapter();
    const ratio = adapter.isWindow
      ? (window.scrollY || 0) / Math.max(1, document.documentElement.scrollHeight - window.innerHeight)
      : adapter.element
        ? adapter.element.scrollTop / Math.max(1, adapter.element.scrollHeight - adapter.element.clientHeight)
        : 0;
    return pendingReaderPosition.current
      ? { ...pendingReaderPosition.current, scroll_ratio: ratio }
      : { scroll_ratio: ratio };
  }
  function openReaderFromLibrary(item: HistoryItem) {
    const origin: Origin = {
      view: "library", batchId: nav.batchId,
      query: nav.query, filter: nav.filter,
      resultIds: results.items.map((entry) => entry.id),
      resultRefs: results.items.map((entry) => ({ kind: "history", historyId: entry.id }) as ResultRef),
      readerPosition: captureReaderPosition(),
    };
    // A02: seed the in-document find ONLY for a genuine body hit.  A title,
    // source or batch hit opens the full transcript instead of filtering it.
    const bodyHit = item.first_match?.field === "text" || (Boolean(nav.query.trim()) && !item.match_fields?.length);
    setReaderQuery(bodyHit ? nav.query.trim() : "");
    setReaderFilterMode(false);
    pushNav({ ...nav, view: "reader", recordId: item.id, origin });
  }
  /** Refs for every row in the current task list, in list order. */
  const taskRefs = useCallback((): ResultRef[] => taskRows.map((row) => {
    const temporary = row.persistence_state === "failed" || row.save_error === "history_save_failed";
    if (temporary && row.attempt_id) {
      return { kind: "temporary", taskId: row.id, attemptId: row.attempt_id } as ResultRef;
    }
    return { kind: "history", historyId: row.history_id! } as ResultRef;
  }).filter((ref) => ref.kind !== "history" || Boolean(ref.historyId)), [taskRows]);
  function openReaderFromTask(item: BatchTask) {
    const refs = taskRefs();
    const at = refs.findIndex((ref) => ref.kind === "history" && ref.historyId === item.history_id);
    const origin: Origin = {
      view: "tasks", batchId: nav.batchId, query: nav.query, filter: nav.filter,
      resultIds: refs.map((ref) => (ref.kind === "history" ? ref.historyId : `${ref.taskId}:${ref.attemptId}`)),
      resultRefs: refs,
      readerPosition: null,
      temporaryRef: at < 0 ? undefined : refs[at],
    };
    setReaderQuery(""); setReaderFilterMode(false);
    pushNav({ ...nav, view: "reader", recordId: item.history_id!, origin });
  }
  function openTemporaryReader(item: BatchTask) {
    const refs = taskRefs();
    const ref: ResultRef = { kind: "temporary", taskId: item.id, attemptId: item.attempt_id ?? "" };
    const origin: Origin = {
      view: "tasks", batchId: nav.batchId, query: nav.query, filter: nav.filter,
      resultIds: refs.map((entry) => (entry.kind === "history" ? entry.historyId : `${entry.taskId}:${entry.attemptId}`)),
      resultRefs: refs,
      readerPosition: null,
      temporaryRef: ref,
    };
    setReaderQuery(""); setReaderFilterMode(false);
    // The recordId must match the pager's composite form, otherwise indexOf
    // misses and "next" resolves back to this same item.
    pushNav({ ...nav, view: "reader", recordId: `${item.id}:${item.attempt_id ?? ""}`, origin });
  }
  function backToList() {
    const origin = nav.origin;
    if (origin && window.history.state) { window.history.back(); return; }
    navigate(origin?.view ?? "library", origin?.batchId ?? null);
  }
  function neighbor(direction: -1 | 1) {
    const origin = nav.origin;
    if (!origin || !nav.recordId) return;
    const at = origin.resultIds.indexOf(nav.recordId);
    const id = origin.resultIds[at + direction];
    if (!id) return;
    const ref = origin.resultRefs?.[at + direction];
    pushNav({ ...nav, recordId: id, origin: ref ? { ...origin, temporaryRef: ref } : origin }, true);
  }
  function toggleTask(id: string) {
    setSelectedTasks((value) => { const next = new Set(value); next.has(id) ? next.delete(id) : next.add(id); return next; });
  }
  function toggleResult(item: HistoryItem) {
    setSelectedResults((value) => { const next = new Set(value); next.has(item.id) ? next.delete(item.id) : next.add(item.id); return next; });
    setSelectedResultData((value) => new Map(value).set(item.id, item));
  }
  function openExport(list: ExportCandidate[]) { setExportCandidates(list); setIncludePartial(false); setExportFormats(["srt"]); setExportOpen(true); }
  function toggleExportFormat(format: SubtitleFormat) {
    // Keep the declared order so identical selections always yield identical paths.
    setExportFormats((current) => current.includes(format)
      ? current.filter((item) => item !== format)
      : SUBTITLE_FORMATS.map((option) => option.id).filter((id) => current.includes(id) || id === format));
  }
  // Selection scope is the loaded batch, independent of the current search or
  // filter.  Deriving exports from taskRows silently shrank a hidden selection
  // to zero; the bulk actions must use the full selected set.
  const selectedTaskRows = useMemo(
    () => activeBatch?.items.filter((item) => selectedTasks.has(item.id)) ?? [],
    [activeBatch, selectedTasks],
  );
  const taskExports = selectedTaskRows.filter((item) => canRead(item)).map((item) => ({ id: item.history_id!, title: titleOf(item), filename: item.filename, integrity: item.integrity || "complete", sourcePath: item.source_path }));
  const retryableSelected = selectedTaskRows.filter((item) => item.status === "error" || item.status === "cancelled");
  const hiddenSelectedCount = selectedTaskRows.filter(
    (item) => !taskRows.some((row) => row.id === item.id),
  ).length;
  const resultExports = [...selectedResults].map((id) => selectedResultData.get(id)).filter((item): item is HistoryItem => Boolean(item)).map((item) => ({ id: item.id, title: titleOf(item), filename: item.filename || "transcript", integrity: item.integrity, sourcePath: item.source_path }));
  const exportIncluded = exportCandidates.filter((item) => includePartial || item.integrity !== "partial");
  const exportFileCount = exportIncluded.length * Math.max(exportFormats.length, 1);

  // ---- export (A08: final paths decided before serialising) -------------
  async function exportNow() {
    const candidates = exportCandidates.filter((item) => includePartial || item.integrity !== "partial");
    if (!candidates.length || !exportFormats.length || exportBusy) return;
    setExportBusy(true);
    try {
      // Each result is fetched once; every selected format is serialised from
      // that same payload so multiple formats cannot disagree with each other.
      const loaded: LoadedExport[] = [];
      const timingByExportId: Record<string, { level: string; coarse: boolean; duration_known: boolean }> = {};
      for (const item of candidates) {
        const value = await fetchHistoryRecord(item.id);
        if ("error" in value) throw new Error(value.detail || value.error);
        const cueInfo = subtitleCues(value.text, value.segments, value.stats);
        timingByExportId[item.id] = {
          level: value.timing?.level ?? (value.segments.length ? "segment" : "none"),
          coarse: cueInfo.coarse,
          duration_known: cueInfo.durationKnown,
        };
        for (const format of exportFormats) {
          loaded.push({
            ...item,
            key: item.id + "@" + format,
            format,
            desiredPath: normalizeExportPath(value.source_path || item.sourcePath, value.filename || item.filename, format),
            content: buildSubtitle(format, value.text, value.segments, value.stats),
          });
        }
      }
      const plan = buildExportPlan(loaded);
      if (plan.files.length === 1 && candidates.length === 1 && !exportCandidates.some((item) => item.integrity === "partial")) {
        const one = plan.files[0];
        saveBlob(new Blob([one.content], { type: "text/plain;charset=utf-8" }), one.finalPath.split("/").pop() || "transcript." + exportFormats[0]);
      } else {
        const manifest = {
          generated_at: new Date().toISOString(),
          formats: exportFormats.map((format) => format.toUpperCase()),
          included: plan.files.map((item) => ({
            id: item.id, title: item.title, path: item.finalPath,
            format: (item.format || exportFormats[0]).toUpperCase(),
            integrity: item.integrity, persistence_state: item.persistenceState,
            timing: timingByExportId[item.id] ?? null,
            warnings: item.warnings,
          })),
          excluded_partial: exportCandidates
            .filter((item) => !includePartial && item.integrity === "partial")
            .map((item) => ({ id: item.id, title: item.title, reason: "partial result excluded by default" })),
          warnings: plan.warnings,
        };
        const files = plan.files.map((item) => ({ name: item.finalPath, content: item.content }));
        files.push({ name: plan.manifestPath, content: JSON.stringify(manifest, null, 2) });
        saveBlob(zipBlob(files), exportArchiveName(candidates, exportFormats));
      }
      setExportOpen(false);
      const formatLabel = exportFormats.map((format) => format.toUpperCase()).join(" / ");
      notify("已生成 " + plan.files.length + " 个 " + formatLabel + " 文件，覆盖 " + candidates.length + " 份结果，并开始下载。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
    finally { setExportBusy(false); }
  }

  // ---- queue actions -----------------------------------------------------
  async function togglePause() {
    try {
      if (queueState?.paused) await resumeQueue(); else await pauseQueue();
      refreshQueue();
      notify(queueState?.paused ? "队列已继续。" : activeTask ? "当前文件完成后会暂停后续任务。" : "队列已暂停。继续后才会启动下一项。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }
  async function retryTask(item: BatchTask) {
    try {
      const response = await retryQueueItem(item.id);
      if ("error" in response) throw new Error(response.detail || response.error);
      refreshQueue(); refreshBatches(); notify(item.filename + " 已重新加入待处理。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }
  /** Batch retry is sequential on purpose: the queue itself is serial. */
  async function retrySelected() {
    if (batchRetryBusy) return;
    const rows = retryableSelected;
    if (!rows.length) return;
    setBatchRetryBusy(true);
    let accepted = 0;
    const failed: string[] = [];
    for (const row of rows) {
      try {
        const response = await retryQueueItem(row.id);
        if ("error" in response) throw new Error(response.detail || response.error);
        accepted += 1;
      } catch {
        failed.push(row.filename);
      }
    }
    setBatchRetryBusy(false);
    refreshQueue(); refreshBatches();
    if (failed.length) {
      notify(`已加入 ${accepted} 项，${failed.length} 项未能重试：${failed.slice(0, 3).join("、")}`);
    } else {
      notify(accepted + " 项已按顺序重新加入待处理。");
    }
  }
  /** Save-retry: re-commit an already-produced result. Never re-runs ASR. */
  async function retrySave(item: BatchTask) {
    if (!item.attempt_id || saveBusyId) return;
    setSaveBusyId(item.id);
    try {
      const response = await retryQueueSave(item.id, item.attempt_id);
      if ("error" in response) throw new Error(response.detail || response.error);
      refreshQueue(); refreshBatches(); invalidateResults();
      notify(response.already_saved ? item.filename + " 已保存，无需重复写入。" : item.filename + " 已保存到长期记录。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
    finally { setSaveBusyId(null); }
  }
  async function removeTask(item: QueueItem | BatchTask) {
    try {
      const response = await deleteQueueItem(item.id);
      if ("error" in response) throw new Error(response.detail || response.error);
      refreshQueue(); refreshBatches(); setConfirmCancelId(null); invalidateResults();
      notify(response.skipping ? "已请求取消当前文件；生成的部分结果会保留。" : "任务已从队列移除。历史结果仍保留。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }
  async function moveQueued(id: string, delta: -1 | 1) {
    const ids = queued.map((item) => item.id);
    const at = ids.indexOf(id); const to = at + delta;
    if (at < 0 || to < 0 || to >= ids.length) return;
    ids.splice(at, 1); ids.splice(to, 0, id);
    try {
      const response = await reorderQueue(ids);
      if ("error" in response) throw new Error(response.detail || response.error);
      refreshQueue();
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }
  async function clearDone() {
    const amount = queueItems.filter((item) => item.status === "done" && item.persistence_state !== "failed").length;
    if (!amount) return;
    try {
      await clearFinishedQueue(); refreshQueue(); refreshBatches(); invalidateResults();
      notify("已移除 " + amount + " 个已完成任务；结果仍保留在结果库。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }
  async function clearCache() {
    if (!cacheInfo?.count || cacheBusy) return;
    if (!window.confirm("清理 " + cacheInfo.count + " 个视频音轨缓存（" + sizeOf(cacheInfo.size_bytes) + "）？清理后这些视频结果无法回听，文字记录会保留。")) return;
    setCacheBusy(true);
    try {
      const response = await clearAudioCache(); refreshCache();
      notify("已清理 " + response.cleared + " 个音轨缓存，释放 " + sizeOf(response.bytes_freed) + "。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
    finally { setCacheBusy(false); }
  }
  async function deletePartialResult(item: HistoryItem) {
    if (!window.confirm(`删除“${titleOf(item)}”的转写结果？对应记录会从结果库移除。`)) return;
    try {
      const response = await deleteHistory(item.id);
      if (!response.deleted) throw new Error("结果记录已不存在或删除失败。");
      setSelectedResults((current) => { const next = new Set(current); next.delete(item.id); return next; });
      setSelectedResultData((current) => { const next = new Map(current); next.delete(item.id); return next; });
      refreshBatches(); invalidateResults(); notify("转写结果已删除。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }
  async function deleteCurrentRecord() {
    if (!record || !window.confirm("删除这份转写结果？对应记录会从结果库移除。")) return;
    if (record.temporary) { setPageError("这份结果尚未保存到长期记录，关闭此页即无法再查看。"); return; }
    try {
      const response = await deleteHistory(record.id);
      if (!response.deleted) throw new Error("结果记录已不存在。");
      const origin = nav.origin;
      pushNav(origin
        ? { ...nav, view: origin.view, batchId: origin.batchId, recordId: null, origin: null, query: origin.query, filter: origin.filter }
        : INITIAL_NAV, true);
      refreshBatches(); invalidateResults(); notify("转写结果已删除。");
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
  }

  // ---- reading / playback position --------------------------------------
  function persistPosition(position: number) {
    if (!record || record.temporary) return;
    if (Date.now() - lastPersist.current < 1500) return;
    lastPersist.current = Date.now();
    updateHistoryView(record.id, position).catch(() => {});
  }
  /** Capture the transcript anchor from the element nearest the viewport top. */
  function captureReaderPositionFromDom(): ReaderPosition | null {
    const adapter = scrollAdapter();
    const scroller = adapter.isWindow ? null : adapter.element;
    const toolbarBottom = mainRef.current?.querySelector(".asr-reader-top")?.getBoundingClientRect().bottom ?? 0;
    const top = Math.max((scroller ? scroller.getBoundingClientRect().top : 0) + 90, toolbarBottom + 8);
    const ratio = adapter.isWindow
      ? (window.scrollY || 0) / Math.max(1, document.documentElement.scrollHeight - window.innerHeight)
      : scroller
        ? scroller.scrollTop / Math.max(1, scroller.scrollHeight - scroller.clientHeight)
        : 0;
    if (record?.segments.length) {
      let index = -1;
      for (const [i, node] of transcriptRefs.current.entries()) {
        if (node && node.getBoundingClientRect().bottom > top) { index = i; break; }
      }
      if (index < 0) {
        for (const [i, node] of paragraphRefs.current.entries()) {
          if (node && node.getBoundingClientRect().bottom > top) { index = i; break; }
        }
      }
      return index >= 0
        ? { segment_index: index, within_block_ratio: 0, scroll_ratio: ratio }
        : { scroll_ratio: ratio };
    }
    // Plain text has no segments: anchor by the character offset of the first
    // visible line so a refresh lands on the same sentence, not just a ratio.
    for (const node of paragraphRefs.current) {
      if (node && node.getBoundingClientRect().bottom > top) {
        return { text_offset: Number(node.dataset.offset ?? 0), scroll_ratio: ratio };
      }
    }
    return { scroll_ratio: ratio };
  }
  function trackReaderScroll() {
    if (nav.view !== "reader" || !record) return;
    const anchor = captureReaderPositionFromDom();
    pendingReaderPosition.current = anchor;
    if (record.temporary) return;
    const now = Date.now();
    if (now - lastPersist.current >= 1500) {
      lastPersist.current = now;
      updateHistoryView(record.id, undefined, { reader_position: anchor ?? undefined }).catch(() => {});
      return;
    }
    // A trailing save: throttling must not drop the position where the user
    // actually stopped, because that is the one they expect to be restored.
    if (scrollSaveTimer.current !== null) return;
    const delay = Math.max(0, 1500 - (now - lastPersist.current));
    scrollSaveTimer.current = window.setTimeout(() => {
      scrollSaveTimer.current = null;
      if (!record || record.temporary) return;
      const latest = pendingReaderPosition.current;
      if (!latest) return;
      lastPersist.current = Date.now();
      updateHistoryView(record.id, undefined, { reader_position: latest }).catch(() => {});
    }, delay);
  }
  function seekTo(seconds: number) {
    if (audioRef.current && !audioError) {
      audioRef.current.currentTime = seconds;
      audioRef.current.play().catch(() => {});
      setCurrentTime(seconds);
    } else {
      const index = record?.segments.findIndex((segment) => segment.start <= seconds && segment.end >= seconds) ?? -1;
      const target = index >= 0 ? transcriptRefs.current[index] : null;
      if (target) scrollReaderNodeIntoView(target, "smooth");
    }
    if (record && !record.temporary) updateHistoryView(record.id, seconds).catch(() => {});
  }
  function onAudioTime() {
    const seconds = audioRef.current?.currentTime ?? 0;
    setCurrentTime(seconds); persistPosition(seconds);
  }
  // Flush the final position when leaving the reader.
  useEffect(() => {
    if (nav.view !== "reader") return;
    return () => {
      const position = pendingReaderPosition.current ?? captureReaderPositionFromDom();
      if (position && record && !record.temporary) {
        void updateHistoryView(record.id, undefined, { reader_position: position });
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.view, nav.recordId, record?.id]);

  // Bind the element that really scrolls.  On phones/short windows that is
  // window, not <main>, so the old single onScroll handler never fired.
  useEffect(() => {
    if (nav.view !== "reader" || !record) return;
    const handler = () => trackReaderScroll();
    window.addEventListener("scroll", handler, { passive: true });
    const main = mainRef.current;
    main?.addEventListener("scroll", handler, { passive: true });
    return () => {
      window.removeEventListener("scroll", handler);
      main?.removeEventListener("scroll", handler);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.view, record?.id, record?.temporary]);

  useEffect(() => () => {
    if (scrollSaveTimer.current !== null) {
      window.clearTimeout(scrollSaveTimer.current);
      scrollSaveTimer.current = null;
    }
  }, [record?.id]);

  // A breakpoint change re-flows the transcript: keep the last anchor and
  // restore it after the new layout exists instead of keeping a dead offset.
  useEffect(() => {
    if (nav.view !== "reader" || !record || record.temporary) return;
    let timer: number | null = null;
    let restoreTimer: number | null = null;
    const onLayoutChange = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const anchor = pendingReaderPosition.current ?? captureReaderPositionFromDom();
        pendingReaderPosition.current = anchor;
        void updateHistoryView(record.id, undefined, { reader_position: anchor ?? undefined }).catch(() => {});
        if (restoreTimer !== null) window.clearTimeout(restoreTimer);
        restoreTimer = window.setTimeout(() => scrollToAnchor(anchor), 120);
      }, 140);
    };
    const narrow = window.matchMedia("(max-width: 679px)");
    const short = window.matchMedia("(max-height: 560px)");
    narrow.addEventListener("change", onLayoutChange);
    short.addEventListener("change", onLayoutChange);
    window.addEventListener("resize", onLayoutChange);
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      if (restoreTimer !== null) window.clearTimeout(restoreTimer);
      narrow.removeEventListener("change", onLayoutChange);
      short.removeEventListener("change", onLayoutChange);
      window.removeEventListener("resize", onLayoutChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.view, record?.id, record?.temporary]);

  // Last-chance save on unload.  PATCH + keepalive (sendBeacon is POST-only).
  useEffect(() => {
    if (nav.view !== "reader" || !record || record.temporary) return;
    const flush = () => {
      const position = pendingReaderPosition.current ?? captureReaderPositionFromDom();
      if (position) {
        void updateHistoryView(record.id, undefined, { reader_position: position }, { keepalive: true }).catch(() => {});
      }
    };
    const onVisibility = () => { if (document.visibilityState === "hidden") flush(); };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.view, record?.id, record?.temporary]);

  const continuation = results.items.find((item) => (item.read_position || 0) > 0) ?? results.items[0] ?? null;
  const audioUrl = record?.audio_cache_name ? "/api/audio-cache/" + encodeURIComponent(record.audio_cache_name) : null;
  const activeSegment = record?.segments.findIndex((segment) => segment.start <= currentTime && segment.end >= currentTime) ?? -1;
  const query = readerQuery.trim();
  const lowerQuery = query.toLocaleLowerCase();
  const paragraphs = useMemo(
    () => (record && preferences.readerView === "paragraph" ? groupParagraphs(record.segments) : []),
    [record, preferences.readerView],
  );
  const plainLines = useMemo(
    () => (record && !record.segments.length ? (record.text || "").split(/\n+/).filter(Boolean) : []),
    [record],
  );
  const segmentMatchIndexes = useMemo(() => {
    if (!record || !query) return [] as number[];
    return record.segments
      .map((segment, index) => ({ segment, index }))
      .filter(({ segment }) => segment.text.toLocaleLowerCase().includes(lowerQuery))
      .map(({ index }) => index);
  }, [record, query, lowerQuery]);
  const paragraphMatchIndexes = useMemo(() => {
    if (!query) return [] as number[];
    return paragraphs
      .filter((group) => group.text.toLocaleLowerCase().includes(lowerQuery))
      .map((group) => group.index);
  }, [query, lowerQuery, paragraphs]);
  const plainMatchIndexes = useMemo(() => {
    if (!query) return [] as number[];
    return plainLines
      .map((line, index) => (line.toLocaleLowerCase().includes(lowerQuery) ? index : -1))
      .filter((index) => index >= 0);
  }, [query, lowerQuery, plainLines]);
  // Count what the current presentation actually highlights: paragraphs in
  // paragraph view, lines in a plain-text record, segments in subtitle view.
  const readerMode: "segment" | "paragraph" | "plain" = !record?.segments.length
    ? "plain"
    : preferences.readerView === "paragraph" ? "paragraph" : "segment";
  const readerMatchIndexes = readerMode === "segment"
    ? segmentMatchIndexes
    : readerMode === "paragraph" ? paragraphMatchIndexes : plainMatchIndexes;
  const readerMatchCount = readerMatchIndexes.length;
  const currentHit = readerMatchCount
    ? ((readerHitIndex % readerMatchCount) + readerMatchCount) % readerMatchCount + 1
    : 0;
  const visibleTranscriptIndexes = useMemo(() => {
    if (!record) return [] as number[];
    const all = record.segments.map((_segment, index) => index);
    if (!query || !readerFilterMode) return all;
    return segmentMatchIndexes;
  }, [record, query, readerFilterMode, segmentMatchIndexes]);
  const transcriptTabStop = focusedSegment !== null && visibleTranscriptIndexes.includes(focusedSegment)
    ? focusedSegment
    : (visibleTranscriptIndexes[0] ?? -1);
  function focusReaderHit(index: number) {
    const target = readerMatchIndexes[index];
    if (target == null) return;
    const node = readerMode === "segment"
      ? transcriptRefs.current[target]
      : paragraphRefs.current[target];
    if (node) scrollReaderNodeIntoView(node);
  }
  function goToReaderHit(delta: number) {
    if (!readerMatchCount) return;
    const next = ((readerHitIndex + delta) % readerMatchCount + readerMatchCount) % readerMatchCount;
    setReaderHitIndex(next);
    focusReaderHit(next);
  }
  useEffect(() => {
    if (!query || !readerMatchCount) return;
    const timer = window.setTimeout(() => focusReaderHit(0), 160);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [record?.id, query, readerMode, readerMatchCount]);
  useEffect(() => { setReaderHitIndex(0); }, [record?.id, readerQuery]);
  const timing = record?.timing ?? null;
  const timingText = timingLabel(timing, record ?? undefined);
  const isTemporary = Boolean(record?.temporary);
  const stats = statsOf(record);

  async function exitApp() {
    if (exiting) return;
    setMoreOpen(false);
    setExiting(true);
    try {
      const snapshot = await fetchQueue();
      const unfinished = snapshot.items.filter((item) => item.status === "queued" || item.status === "running");
      const message = unfinished.length
        ? `还有 ${unfinished.length} 个文件正在处理或等待处理。退出会停止本地服务，未完成文件重开后从头处理，已保存结果保留。\n\n确定退出吗？`
        : "退出将关闭本地服务。已保存的结果会保留。\n\n确定退出吗？";
      if (!window.confirm(message)) return;
      await shutdownApp();
      setExited(true);
    } catch (error) { setPageError(error instanceof Error ? error.message : String(error)); }
    finally { setExiting(false); }
  }

  if (exited) return <main className="asr-boot"><b className="asr-boot-brand">Qwen ASR</b><h1>已退出</h1><p>本地服务正在关闭，可以关闭此页面。需要使用时请重新启动应用。</p></main>;
  if (!ready || switching) return <Boot state={readiness} failed={failed} retry={restart} />;

  return <div className={"asr-app asr-view-" + nav.view}>
    <div className="asr-topline"><span>本地语音转写</span><span className="asr-topline-status"><i className={activeTask ? "is-running" : ""} />{activeTask ? "正在转写 " + activeTask.filename : queueState?.paused ? "队列已暂停" : queued.length ? queued.length + " 项等待处理" : "本地服务已就绪"}</span></div>
    <div className="asr-frame" ref={frameRef}>
      <header className="asr-masthead">
        <div className="asr-machine"><b>{dateOf(new Date().toISOString())}</b><span><i />本地运行</span></div>
        <button className="asr-brand" onClick={() => navigate("library", null)} aria-label="Qwen ASR 结果库"><span>Qwen ASR</span></button>
        <button className="asr-button asr-new" onClick={() => openImport()}><span>新建转写</span><b>＋</b></button>
        <div className="asr-more-wrap"><button className="asr-more" aria-label="更多设置" aria-expanded={moreOpen} onClick={() => setMoreOpen((open) => !open)}>•••</button>
          {moreOpen && <div className="asr-popover" role="menu"><button role="menuitem" onClick={() => { setSettingsOpen(true); setMoreOpen(false); }}>模型与存储</button><button role="menuitem" onClick={() => { refreshBatches(); refreshCache(); results.reload(); setMoreOpen(false); }}>刷新状态</button><button role="menuitem" disabled={exiting} onClick={() => void exitApp()}>退出应用</button><p>{health?.current_model || "模型状态读取中"}<small>{statusMessage(health?.aligner_status) || "对齐器状态未知"}</small></p></div>}
        </div>
      </header>
      <nav className="asr-nav" aria-label="主导航">
        <div className="asr-nav-links">
          <button className={nav.view === "tasks" || (nav.view === "reader" && nav.origin?.view === "tasks") ? "selected" : ""} onClick={() => navigate("tasks", activeBatch?.id ?? null)}>任务中心 <span className="asr-count">{queueItems.filter((item) => item.status === "queued" || item.status === "running").length}</span></button>
          <button className={nav.view === "library" || (nav.view === "reader" && nav.origin?.view === "library") ? "selected" : ""} onClick={() => navigate("library", null)}>结果库</button>
          <button className={nav.view === "workbench" ? "selected" : ""} onClick={() => pushNav({ ...nav, view: "workbench", recordId: null, origin: null })}>工作台</button>
          <button className="asr-nav-panel-toggle" aria-expanded={navPanelOpen} aria-controls="asr-nav-panel" onClick={() => setNavPanelOpen((open) => !open)}>搜索与批次</button>
        </div>
        <div className="asr-nav-runtime"><i className={activeTask ? "is-running" : ""} />{activeTask ? "正在转写 " + activeTask.filename : queueState?.paused ? "当前队列已暂停" : "工作区已就绪"}</div>
      </nav>
      {navPanelOpen && <div className="asr-nav-panel" id="asr-nav-panel">
        <label className="asr-global-search"><span><SearchIcon /></span><input value={nav.query} onChange={(event) => setQuery(event.target.value)} placeholder="查找会议、访谈或原话" aria-label="搜索标题、来源和转写内容" /></label>
        <div className="asr-tags" role="group" aria-label="结果筛选">{([["all", "全部"], ["unseen", "未查看"], ["partial", "有缺失"]] as const).map(([value, label]) => <button key={value} className={nav.filter === value ? "active" : ""} aria-pressed={nav.filter === value} onClick={() => setFilter(value)}># {label}</button>)}</div>
        <div className="asr-nav-batches">{batches.length ? batches.slice(0, 4).map((batch) => <button key={batch.id} className={nav.view === "tasks" && nav.batchId === batch.id ? "active" : ""} onClick={() => { setTaskQuery(""); setTaskFilter("all"); setNavPanelOpen(false); navigate("tasks", batch.id); }}>{batch.name}<small>{batch.items.filter((item) => canRead(item)).length} 份可查看</small></button>) : <span>还没有批次</span>}</div>
      </div>}
      <div className="asr-workspace">
        <main className="asr-main" ref={mainRef} tabIndex={-1}>
          {pageError && <div className="asr-alert asr-alert-error" role="alert"><span>{statusMessage(pageError)}</span><button onClick={() => setPageError("")} aria-label="关闭错误">×</button></div>}
          {nav.view === "library" && <section aria-labelledby="library-title">
            <div className="asr-page-heading asr-library-heading"><div><h1 id="library-title" className="asr-display">Library</h1><p>结果库</p><small>按会议主题、访谈对象或原话查找。</small></div><span className="asr-result-count">{results.total} 份可查看</span></div>
            {continuation && <article className="asr-continue">
              <div className="asr-continue-meta"><span>{dateOf(continuation.created_at)}</span><span>{continuation.read_position ? "继续阅读 · 上次读到 " + formatShort(continuation.read_position) : "最近结果"}</span></div>
              <h2>{titleOf(continuation)}</h2><p className="asr-source">{continuation.filename}{continuation.batch_name ? " / " + continuation.batch_name : ""}</p>
              {continuation.snippet && <p className="asr-quote">{marked(continuation.snippet, nav.query)}</p>}
              <div className="asr-continue-foot"><span>{continuation.integrity === "partial" ? "结果有缺失" : "完整转写已保存"}</span><button className="asr-button asr-button-small" onClick={() => openReaderFromLibrary(continuation)}>接着阅读</button></div>
            </article>}
            <div className="asr-section-heading"><h2 className="asr-display">Latest</h2><span>最近生成的会议与访谈</span></div>
            <div className="asr-library-tools"><span>打开结果会自动标记为已查看；导出时会保留原文件名和来源。</span><div>
              <button className="asr-text-button" disabled={!selectedResults.size} onClick={() => openExport(resultExports)}>导出所选{selectedResults.size ? " (" + selectedResults.size + ")" : ""}</button>
              <button className="asr-text-button" disabled={!results.items.length} onClick={() => openExport(results.items.map((item) => ({ id: item.id, title: titleOf(item), filename: item.filename || "transcript", integrity: item.integrity, sourcePath: item.source_path })))}>导出当前列表 {results.loadedCount} 份</button>
              <button className="asr-text-button" onClick={() => patchPreferences({ listStyle: preferences.listStyle === "grid" ? "compact" : "grid" })}>{preferences.listStyle === "grid" ? "切换紧凑列表" : "切换两列版面"}</button>
            </div></div>
            {results.loading && !results.items.length ? <p className="asr-empty">正在查找记录…</p>
              : results.error ? <div className="asr-empty"><h2>读取结果库失败</h2><p>{statusMessage(results.error)}</p><button className="asr-button" onClick={() => results.reload()}>重试</button></div>
              : results.items.length ? <div className={"asr-result-grid" + (preferences.listStyle === "compact" ? " asr-result-grid-compact" : "")}>
                {results.items.map((item) => <article className="asr-result-card" key={item.id}>
                  <label className="asr-card-check"><input type="checkbox" checked={selectedResults.has(item.id)} onChange={() => toggleResult(item)} aria-label={"选择 " + titleOf(item) + " 导出"} /></label>
                  <button className="asr-result-open" onClick={() => openReaderFromLibrary(item)}>
                    <span className="asr-card-meta">{dateOf(item.created_at)}{item.batch_name ? "　" + item.batch_name : ""}</span><strong>{titleOf(item)}</strong>
                    <span className="asr-source">{item.source_path || item.filename}</span><span className="asr-excerpt">{item.snippet || "没有可显示的转写内容。"}</span>
                    <span className={"asr-integrity " + (item.integrity === "partial" ? "warning" : "")}>{item.integrity === "partial" ? "有缺失" : "已完成"}{item.first_viewed_at ? " · 已查看" : " · 未查看"}{item.timing && item.timing.level === "none" ? " · 无分段定位" : ""}</span>
                  </button>
                  {item.integrity === "partial" && <button className="asr-text-button asr-error-text" aria-label={"删除 " + titleOf(item)} onClick={() => void deletePartialResult(item)}>删除此份</button>}
                </article>)}
              </div>
              : nav.query.trim() || nav.filter !== "all"
                ? <div className="asr-empty"><h2>本次查找没有匹配结果</h2><p>当前条件：{nav.query.trim() ? "关键词“" + nav.query.trim() + "”" : "全部关键词"}{nav.filter !== "all" ? " · 筛选：" + (nav.filter === "unseen" ? "未查看" : "有缺失") : ""}。清除条件即可看到全部记录。</p><button className="asr-button asr-button-primary" onClick={() => { setQuery(""); setFilter("all"); }}>清除关键词与筛选</button></div>
                : <div className="asr-empty"><h2>还没有转写结果</h2><p>导入音频或视频文件，结果会保存在这里，方便以后搜索和回听。</p><button className="asr-button asr-button-primary" onClick={() => openImport()}>新建转写</button></div>}
            {results.hasMore && <div className="asr-load-more"><button className="asr-button" disabled={results.loading} onClick={() => results.loadMore()}>{results.loading ? "正在加载…" : "加载更多结果"}</button><span>已显示 {results.loadedCount} / {results.total}</span></div>}
          </section>}

          {nav.view === "tasks" && <section className="asr-tasks-page" aria-labelledby="tasks-title">
            <div className="asr-page-heading asr-task-heading"><div><h1 id="tasks-title" className="asr-display">Tasks</h1><h2>{activeBatch?.name || "任务中心"}</h2></div><div className="asr-heading-actions"><button className="asr-button" onClick={togglePause} disabled={!queueItems.some((item) => item.status === "queued" || item.status === "running")}>{queueState?.paused ? "继续队列" : activeTask ? "当前文件完成后暂停" : "暂停队列"}</button><button className="asr-button asr-button-primary" disabled={!taskExports.length} onClick={() => openExport(taskExports)}>导出所选</button></div></div>
            {!activeBatch ? <div className="asr-empty"><h2>还没有任务</h2><p>选择一个文件或文件夹，创建第一批转写任务。</p><button className="asr-button asr-button-primary" onClick={() => openImport()}>新建转写</button></div> : <>
              <div className="asr-task-summary"><span><b>{doneCount}</b> 完整</span><span className="asr-warning-text"><b>{partialCount}</b> 有缺失</span>{cancelledCount ? <span className="asr-warning-text"><b>{cancelledCount}</b> 已取消</span> : null}<span className="asr-error-text"><b>{failedCount}</b> 失败</span>{unsavedCount ? <span className="asr-warning-text"><b>{unsavedCount}</b> 未保存</span> : null}<span><b>{runningInBatch}</b> 处理中</span><span><b>{queuedInBatch}</b> 等待</span></div>
              {activeTask && <div className={"asr-active-strip " + (queueState?.paused ? "paused" : "")}><div><strong>{activeTask.filename} · {queueState?.paused ? "当前文件完成后暂停全部任务" : "正在转写"}</strong><span>{statusMessage(jobStatus?.message) || "正在准备音轨"}</span>{jobStatus && <div className="asr-progress"><span style={{ width: jobStatus.progress + "%" }} /></div>}</div><b className="asr-progress-number">{jobStatus ? Math.round(jobStatus.progress) + "%" : "…"}</b><button className="asr-text-button" onClick={() => setConfirmCancelId(confirmCancelId === activeTask.id ? null : activeTask.id)}>{confirmCancelId === activeTask.id ? "暂不取消" : "取消当前文件"}</button>{confirmCancelId === activeTask.id && <button className="asr-text-button asr-error-text" onClick={() => void removeTask(activeTask)}>确认取消并保留部分结果</button>}</div>}
              {queueState?.paused && !activeTask && <div className="asr-active-strip paused"><div><strong>队列已暂停</strong><span>文件会等待，需要点击“继续队列”才会开始下一项。</span></div></div>}
              <div className="asr-task-toolbar"><label className="asr-search-wrap"><span><SearchIcon /></span><input value={taskQuery} onChange={(event) => setTaskQuery(event.target.value)} placeholder="搜索文件名或来源…" aria-label="搜索本批次文件" /></label><div className="asr-filters" role="group" aria-label="筛选任务">{([["all", "全部"], ["attention", "需关注 " + attentionCount], ["ready", "可查看"], ["waiting", "等待"]] as const).map(([value, label]) => <button key={value} className={taskFilter === value ? "active" : ""} aria-pressed={taskFilter === value} onClick={() => setTaskFilter(value)}>{label}</button>)}</div><button className="asr-sort-button" onClick={() => setTaskSort((value) => value === "name" ? "added" : "name")}>按{taskSort === "name" ? "导入时间" : "文件名"}排序</button></div>
              {selectedTasks.size > 0 && <div className="asr-bulk-bar"><b>已选 {selectedTasks.size} 项</b><span>选择范围：当前批次，可跨筛选保留{hiddenSelectedCount ? `（其中 ${hiddenSelectedCount} 项在当前筛选外）` : ""}。可导出 {taskExports.length} 项 · 可重试 {retryableSelected.length} 项</span><button className="asr-button asr-button-small" disabled={!taskExports.length} onClick={() => openExport(taskExports)}>导出所选</button><button className="asr-button asr-button-small" disabled={!retryableSelected.length || batchRetryBusy} onClick={() => void retrySelected()}>{batchRetryBusy ? "正在加入…" : "批量重新转写"}</button><button className="asr-text-button" onClick={() => setSelectedTasks(new Set())}>清除选择</button></div>}
              <div className="asr-task-table" role="table" aria-label={activeBatch.name + " 文件任务"}>
                <div className="asr-task-head" role="row"><span role="columnheader">文件 / 来源 <button className="asr-sort-inline" onClick={() => setTaskSort((value) => value === "name" ? "added" : "name")}>↕</button></span><span role="columnheader">处理状态</span><span role="columnheader">阅读状态</span><span role="columnheader">操作</span></div>
                <div className="asr-task-scroll">{taskRows.map((item) => {
                  const status = statusOf(item); const opened = canRead(item);
                  const retryable = item.status === "error" || item.status === "cancelled";
                  const unsaved = item.persistence_state === "failed" || item.save_error === "history_save_failed";
                  return <article className={"asr-task-row " + (item.status === "running" ? "current" : "")} role="row" key={item.id}>
                    <div className="asr-task-file"><label className="asr-selectbox"><input type="checkbox" checked={selectedTasks.has(item.id)} onChange={() => toggleTask(item.id)} aria-label={"选择 " + item.filename} /></label><div className="asr-task-file-copy"><button className="asr-file-title" onClick={() => (opened ? openReaderFromTask(item) : unsaved ? openTemporaryReader(item) : setDetailTask(item))}>{titleOf(item)}</button><span className="asr-file-meta" title={item.source_path || item.filename}>{item.source_path || item.filename}{item.duration ? " · " + durationOf(item.duration) : item.status === "queued" ? " · 时长待检测" : ""}</span></div></div>
                    <div className="asr-task-state"><span className={"asr-status-label " + status.tone}><i />{status.label}</span><small title={status.note}>{status.note}</small></div>
                    <div className="asr-task-view">{opened ? <span className="asr-view-state">{item.first_viewed_at ? "已查看" : "未查看"}</span> : unsaved ? <span className="asr-view-state asr-warning-text">未保存</span> : <span className="asr-view-state">—</span>}</div>
                    <div className="asr-task-actions">
                      {opened && <button className="asr-text-button" onClick={() => openReaderFromTask(item)}>打开结果</button>}
                      {unsaved && <button className="asr-text-button asr-warning-text" onClick={() => openTemporaryReader(item)}>查看未保存结果</button>}
                      {unsaved && item.can_retry_save && <button className="asr-text-button" disabled={saveBusyId === item.id} onClick={() => void retrySave(item)}>{saveBusyId === item.id ? "正在保存…" : "重新保存"}</button>}
                      {unsaved && <button className="asr-text-button" onClick={() => setDetailTask(item)}>详情</button>}
                      {retryable && <button className="asr-text-button" onClick={() => void retryTask(item)}>重新转写</button>}
                      {item.status === "error" && item.error && <span className="asr-row-error" title={statusMessage(item.error)}>{statusMessage(item.error)}</span>}
                    </div>
                  </article>;
                })}{!taskRows.length && <p className="asr-empty-inline">没有符合当前搜索和筛选条件的任务。</p>}</div>
              </div>
              <div className="asr-task-foot"><span>显示 {taskRows.length} / {activeBatch.items.length} 项</span><span>查看排序不会改变处理顺序。</span><button className="asr-text-button" onClick={() => void clearDone()}>移除已完成任务</button></div>
            </>}
          </section>}

          {nav.view === "workbench" && <section className="asr-wb-page" aria-labelledby="wb-title">
            <div className="asr-page-heading"><div><h1 id="wb-title" className="asr-display">Workbench</h1><h2>工作台 · 文件夹批处理</h2></div></div>
            {wbError && <div className="asr-active-strip paused"><div><strong>{wbError}</strong></div><button className="asr-text-button" onClick={() => setWbError("")}>关闭</button></div>}
            <div className="asr-wb-grid">
              <div className="asr-wb-panel">
                <h3>批准的文件夹</h3>
                <div className="asr-wb-root-add"><input className="asr-wb-input" value={wbRootInput} onChange={(event) => setWbRootInput(event.target.value)} placeholder="文件夹绝对路径，如 E:\meetings" aria-label="文件夹路径" /><button className="asr-button asr-button-small" disabled={wbBusy || !wbRootInput.trim()} onClick={() => void wbAddRoot()}>批准</button></div>
                {wbConfig?.roots.length ? <ul className="asr-wb-roots">{wbConfig.roots.map((root) => <li key={root.id}><button className={"asr-wb-root " + (wbScanRoot === root.id ? "selected" : "")} title={root.path} onClick={() => { setWbScanRoot(root.id); setWbScan([]); setWbSelected(new Set()); }}>{root.label}</button><button className="asr-text-button" onClick={() => void wbRemoveRoot(root.id)}>移除</button></li>)}</ul> : <p className="asr-empty-inline">还没有批准的文件夹。批准后才能扫描与入队；处理只读源文件，不复制。</p>}
              </div>
              <div className="asr-wb-panel">
                <h3>自动命名导出</h3>
                <div className="asr-wb-root-add"><input className="asr-wb-input" value={wbExportInput} onChange={(event) => setWbExportInput(event.target.value)} placeholder="导出根目录（留空则不自动导出）" aria-label="导出根目录" /><button className="asr-button asr-button-small" onClick={() => void wbSaveExport()}>保存</button></div>
                <div className="asr-wb-formats">{SUBTITLE_FORMATS.map((fmt) => <label key={fmt.id} className="asr-check-option"><input type="checkbox" checked={wbConfig?.formats.includes(fmt.id) ?? false} onChange={() => void wbToggleFormat(fmt.id)} />{fmt.label}</label>)}</div>
                <label className="asr-check-option"><input type="checkbox" checked={wbConfig?.auto_export ?? false} onChange={() => void wbToggleAuto()} />转写完成后自动导出</label>
                <p className="asr-import-note">写入「导出根目录 / 批次名 / 原文件名.格式」；重名自动加后缀，不覆盖已有文件。</p>
                <div className="asr-wb-root-add"><input className="asr-wb-input" value={wbTemplate} onChange={(event) => setWbTemplate(event.target.value)} placeholder="{stem}" aria-label="导出命名模板" /><button className="asr-button asr-button-small" onClick={() => void wbSaveTemplate()}>保存模板</button></div>
                <p className="asr-import-note">占位符：{"{stem} {date} {time} {model} {batch} {id}"}（必须含 {"{stem}"}）。预览：<b>{wbNamePreview}.srt</b></p>
                <label className="asr-check-option"><input type="checkbox" checked={wbConfig?.summarize ?? false} onChange={() => void wbToggleSummarize()} />批次完成后自动总结</label>
                <select className="asr-wb-input" value={wbConfig?.summary_model ?? ""} onChange={(event) => void wbSelectModel(event.target.value)} aria-label="总结模型">
                  <option value="">选择总结模型（models/summary）</option>
                  {wbModels.map((model) => <option key={model.name} value={model.name}>{model.name}</option>)}
                </select>
                <p className="asr-import-note">总结采用批后顺序换模：换入 instruct 模型期间转写暂停（409），完成后自动换回原 ASR 模型。摘要/关键词/特别内容由模型生成，可单条重跑。</p>
                <textarea className="asr-wb-input asr-wb-prompt" value={wbPrompt} onChange={(event) => setWbPrompt(event.target.value)} rows={4} placeholder="留空使用内置默认 prompt（要求只输出 JSON：title/summary/keywords/highlights）" aria-label="总结 prompt 模板" />
                <div className="asr-wb-root-add"><button className="asr-button asr-button-small" onClick={() => void wbSavePrompt()}>保存 prompt</button><button className="asr-text-button" onClick={() => { setWbPrompt(""); void updateWorkbenchConfig({ summary_prompt: null }).then((r) => { if (!isApiError(r)) { setWbConfig(r); notify("已恢复默认总结 prompt"); } }); }}>恢复默认</button></div>
              </div>
            </div>
            {wbSummary && (wbSummary.phase === "swapping_in" || wbSummary.phase === "summarizing" || wbSummary.phase === "swapping_back") && <div className="asr-active-strip"><div><strong>总结阶段 · {wbSummary.phase === "summarizing" ? `正在总结 ${wbSummary.done}/${wbSummary.total}` : wbSummary.phase === "swapping_in" ? "正在换入总结模型" : "正在换回 ASR 模型"}</strong><span>模型 {wbSummary.model} · 期间转写暂停，队列自动等待</span></div></div>}
            {wbSummary && wbSummary.phase === "failed" && <div className="asr-active-strip paused"><div><strong>总结阶段失败：{statusMessage(wbSummary.error)}</strong><span>{wbSummary.error === "swap_back_failed" ? "ASR 未恢复：请用启动重试恢复模型后再总结" : "可重试本批次总结"}</span></div>{wbSummary.batch_id && wbSummary.error !== "swap_back_failed" && <button className="asr-text-button" onClick={() => void startBatchSummary(wbSummary.batch_id!).then((r) => { if (isApiError(r)) setWbError(statusMessage(r.error)); else void fetchSummaryState().then(setWbSummary); })}>重试总结</button>}</div>}
            {wbSummary && wbSummary.phase === "interrupted" && <div className="asr-active-strip paused"><div><strong>上次总结被重启中断</strong><span>ASR 已由启动恢复；未完成的条目可单条重总结。</span></div></div>}
            <div className="asr-wb-panel">
              <h3>扫描与顺序转写</h3>
              <div className="asr-wb-scanbar">
                <select className="asr-wb-input" value={wbScanRoot} onChange={(event) => { setWbScanRoot(event.target.value); setWbScan([]); setWbSelected(new Set()); }} aria-label="选择文件夹">{(wbConfig?.roots ?? []).map((root) => <option key={root.id} value={root.id}>{root.label}</option>)}</select>
                <button className="asr-button asr-button-small" disabled={wbBusy || !wbScanRoot} onClick={() => void wbDoScan()}>扫描</button>
                <input className="asr-wb-input" value={wbBatchName} onChange={(event) => setWbBatchName(event.target.value)} placeholder="批次名（默认文件夹名）" aria-label="批次名" />
                <label className="asr-check-option"><input type="checkbox" checked={wbAlign} onChange={(event) => setWbAlign(event.target.checked)} />词级对齐</label>
                <button className="asr-button asr-button-primary asr-button-small" disabled={wbBusy || !wbSelected.size} onClick={() => void wbEnqueue()}>顺序转写所选{wbSelected.size ? ` (${wbSelected.size})` : ""}</button>
              </div>
              {wbTruncated && <p className="asr-import-note">文件过多，仅显示前 {wbScan.length} 项；可分批入队。</p>}
              {wbScan.length > 0 && <div className="asr-wb-scanlist">{wbScan.map((it) => <label key={it.rel_path} className="asr-wb-scanrow"><input type="checkbox" checked={wbSelected.has(it.rel_path)} onChange={() => setWbSelected((current) => { const next = new Set(current); if (next.has(it.rel_path)) next.delete(it.rel_path); else next.add(it.rel_path); return next; })} /><span className="asr-wb-scanpath" title={it.rel_path}>{it.rel_path}</span><span className="asr-file-meta">{(it.size / 1048576).toFixed(1)} MB</span></label>)}</div>}
            </div>
            <div className="asr-wb-panel">
              <h3>批处理结果</h3>
              <div className="asr-wb-scanbar">
                <select className="asr-wb-input" value={wbSummaryBatch || wbBatches[0]?.id || ""} onChange={(event) => setWbSummaryBatch(event.target.value)} aria-label="选择要总结的批次">{wbBatches.map((batch) => <option key={batch.id} value={batch.id}>{batch.name}</option>)}</select>
                <button className="asr-button asr-button-small" disabled={!wbBatches.length || !wbConfig?.summary_model} onClick={() => void wbStartBatch()}>总结本批次</button>
                <button className="asr-button asr-button-small" disabled={!wbBatches.length || !wbConfig?.summary_model} onClick={() => void wbForceSummarize()}>重总结本批次</button>
                <button className="asr-button asr-button-small" disabled={!wbBatches.length || !wbConfig?.export_root} onClick={() => void wbReexportBatch()}>批量重导</button>
              </div>
              <div className="asr-wb-table" role="table" aria-label="工作台结果">
                <div className="asr-wb-head" role="row"><span role="columnheader">标题</span><span role="columnheader">摘要</span><span role="columnheader">时长</span><span role="columnheader">完成时间</span><span role="columnheader">关键词</span><span role="columnheader">特别内容</span><span role="columnheader">导出</span><span role="columnheader">总结</span></div>
                {wbRows.map((row) => <div className="asr-wb-row" role="row" key={row.id}>
                  <span role="cell"><button className="asr-file-title" disabled={!row.historyId} onClick={() => wbOpenResult(row)}>{row.title}</button><small className="asr-file-meta" title={row.sourcePath}>{row.batchName} · {row.sourcePath}</small></span>
                  <span role="cell" className="asr-wb-clamp" title={row.summary ?? undefined}>{row.summary ?? "—"}</span>
                  <span role="cell">{row.duration != null ? durationOf(row.duration) : row.status === "done" ? "—" : "待检测"}</span>
                  <span role="cell">{row.finishedAt ? formatDateTime(row.finishedAt) : row.error ? statusMessage(row.error) : row.status === "running" ? "处理中" : row.status === "queued" ? "等待" : "—"}</span>
                  <span role="cell" className="asr-wb-clamp" title={row.keywords.join("、") || undefined}>{row.keywords.join("、") || "—"}</span>
                  <span role="cell" className="asr-wb-clamp" title={row.highlights.join("；") || undefined}>{row.highlights.join("；") || "—"}</span>
                  <span role="cell"><span className={"asr-status-label " + (row.exportState === "failed" ? "error" : row.exportState === "done" ? "running" : "")}><i />{EXPORT_STATE_LABEL[row.exportState]}</span>{row.exportNames.length > 0 && <small className="asr-file-meta" title={row.exportNames.join(", ")}>{row.exportNames.join(", ")}</small>}{(row.exportState === "failed" || row.exportState === "skipped") && row.historyId && <button className="asr-text-button" disabled={wbBusy} onClick={() => void wbReexport(row.id)}>重新导出</button>}</span>
                  <span role="cell"><span className={"asr-status-label " + (row.summaryState === "failed" ? "error" : row.summaryState === "done" ? "running" : "")}><i />{SUMMARY_STATE_LABEL[row.summaryState]}</span>{row.summaryError && <small className="asr-file-meta">{statusMessage(row.summaryError)}</small>}{row.historyId && <button className="asr-text-button" disabled={!wbConfig?.summary_model} onClick={() => void wbRetrySummary(row.historyId!)}>重总结</button>}</span>
                </div>)}
                {!wbRows.length && <p className="asr-empty-inline">还没有工作台任务。批准文件夹 → 扫描 → 顺序转写 → 自动导出。</p>}
              </div>
            </div>
          </section>}

          {nav.view === "reader" && <section className="asr-reader" aria-labelledby="reader-title">
            <div className="asr-reader-top"><button className="asr-text-button" onClick={backToList}>‹ 返回{nav.origin?.view === "tasks" ? "任务中心" : "结果库"}</button><div className="asr-reader-pager"><button className="asr-button asr-button-small" onClick={() => neighbor(-1)} disabled={!nav.origin || nav.origin.resultIds.indexOf(nav.recordId || "") <= 0}>上一份</button><span>{nav.origin ? Math.max(1, nav.origin.resultIds.indexOf(nav.recordId || "") + 1) + " / " + nav.origin.resultIds.length : "结果"}</span><button className="asr-button asr-button-small" onClick={() => neighbor(1)} disabled={!nav.origin || nav.origin.resultIds.indexOf(nav.recordId || "") >= nav.origin.resultIds.length - 1}>下一份</button>{record && <button className="asr-button asr-button-primary asr-button-small" onClick={() => openExport([{ id: record.id, title: titleOf(record), filename: record.filename, integrity: record.integrity, sourcePath: record.source_path, persistenceState: record.persistence_state }])}>导出此份</button>}{record && !record.temporary && <button className="asr-button asr-button-small asr-button-danger" onClick={() => void deleteCurrentRecord()}>删除此份</button>}</div></div>
            {recordError ? <div className="asr-empty"><h2>无法打开这份结果</h2><p>{statusMessage(recordError)}</p></div> : !record ? <p className="asr-empty">正在打开结果…</p> : <>
              <article className="asr-reader-document"><h1 id="reader-title">{titleOf(record)}</h1><p className="asr-reader-meta">{record.batch_name || "未分组的历史记录"} / {record.source_path || record.filename} · 时长 {durationOf(stats.audio_duration)} · {record.first_viewed_at ? "已查看" : "刚刚打开"}</p>
                <div className="asr-reader-flags">
                  {isTemporary && <div className="asr-notice asr-notice-warning"><b>这份结果尚未保存</b><span>转写文字本次会话可读；长期记录保存失败。关闭此页后可能无法再看，可从原文件重新转写。</span></div>}
                  {record.integrity === "partial" && <div className="asr-notice asr-notice-warning"><b>这份结果有缺失</b><span>{record.missing_ranges?.length ? record.missing_ranges.join("；") : "部分音频片段未能识别。"}</span></div>}
                  {timing && timing.level === "none" && <div className="asr-notice asr-notice-warning"><b>没有分段定位时间戳</b><span>{timingText}</span></div>}
                  {stats.cancelled && <div className="asr-notice asr-notice-warning"><b>任务已取消</b><span>保留了取消前生成的部分结果；可在任务中心重新转写原文件。</span></div>}
                </div>
                <details className="asr-reader-technical">
                  <summary>
                    <span>处理统计与配置</span>
                    <span className="asr-reader-stats"><span>RTF {stats.rtf.toFixed(2)}</span><span>处理耗时 {durationOf(stats.total_time)}</span></span>
                  </summary>
                  <dl>
                    <div><dt>字符数</dt><dd>{stats.char_count.toLocaleString()}</dd></div>
                    <div><dt>段数</dt><dd>{record.segments.length.toLocaleString()}</dd></div>
                    <div><dt>识别耗时</dt><dd>{durationOf(stats.asr_time)}</dd></div>
                    <div><dt>对齐耗时</dt><dd>{stats.aligner_used ? durationOf(stats.align_time) : "未运行"}</dd></div>
                    <div><dt>词数</dt><dd>{stats.word_count.toLocaleString()}</dd></div>
                    <div><dt>音频片段</dt><dd>{stats.chunk_count.toLocaleString()}</dd></div>
                    <div><dt>原文件大小</dt><dd>{sizeOf(stats.file_size) || "未知"}</dd></div>
                    <div><dt>模型</dt><dd>{stats.model}</dd></div>
                    <div><dt>是否使用 Aligner</dt><dd>{stats.aligner_used ? "是" : "否"}</dd></div>
                    <div><dt>对齐器模型</dt><dd>{stats.aligner_model || (stats.aligner_used ? "未知" : "未使用")}</dd></div>
                    <div><dt>结果版本</dt><dd>{record.result_version || 1}</dd></div>
                  </dl>
                </details>
                <div className="asr-reader-tools">
                  <label className="asr-reader-search"><span><SearchIcon /></span><input value={readerQuery} onChange={(event) => setReaderQuery(event.target.value)} placeholder="在这份结果中查找…" aria-label="在转写结果中搜索" />{readerQuery && <button onClick={() => setReaderQuery("")}>清除</button>}</label>
                  <div className="asr-reader-view-toggle"><button className="asr-text-button" onClick={() => patchPreferences({ readerView: preferences.readerView === "paragraph" ? "subtitle" : "paragraph" })}>{preferences.readerView === "paragraph" ? "切换字幕行" : "切换段落阅读"}</button>{query && <label className="asr-check-option asr-inline-option"><input type="checkbox" checked={readerFilterMode} onChange={(event) => setReaderFilterMode(event.target.checked)} /><span>只看命中段</span></label>}</div>
                  {query && <div className="asr-reader-hits" role="group" aria-label="查找命中导航">
                    <span role="status">{readerMatchCount ? currentHit + " / " + readerMatchCount + " 处" : "0 处"}</span>
                    <button className="asr-text-button" disabled={!readerMatchCount} onClick={() => goToReaderHit(-1)}>上一处</button>
                    <button className="asr-text-button" disabled={!readerMatchCount} onClick={() => goToReaderHit(1)}>下一处</button>
                  </div>}
                </div>
                {query && readerMatchCount === 0 && <p className="asr-empty-inline" role="status">{readerFilterMode ? "本份结果未找到匹配内容，正文已被“只看命中段”隐藏。取消勾选即可恢复全文。" : "本份结果未找到匹配内容。全文仍可见，清除查找即可取消高亮。"}</p>}
                {record.segments.length ? (preferences.readerView === "paragraph" ? <div className="asr-transcript asr-paragraph-view" aria-label="转写内容">
                  {paragraphs.map((group) => {
                    const hidden = readerFilterMode && query && !group.text.toLocaleLowerCase().includes(query.toLocaleLowerCase());
                    if (hidden) return null;
                    return <p key={group.index} ref={(element) => { paragraphRefs.current[group.index] = element; }} data-segment-index={group.index} data-offset={record.segments.slice(0, group.index).reduce((sum, segment) => sum + segment.text.length + 1, 0)}>{marked(group.text, readerQuery)}</p>;
                  })}
                </div> : <div className="asr-transcript" aria-label="转写内容">{record.segments.map((segment, index) => {
                  if (readerFilterMode && query && !segment.text.toLocaleLowerCase().includes(query.toLocaleLowerCase())) return null;
                  return <button key={String(segment.start) + "-" + index} ref={(element) => { transcriptRefs.current[index] = element; }} tabIndex={transcriptTabStop === index ? 0 : -1} onFocus={() => setFocusedSegment(index)} onKeyDown={(event) => {
                    const at = visibleTranscriptIndexes.indexOf(index);
                    const next = event.key === "ArrowDown" ? visibleTranscriptIndexes[at + 1] : event.key === "ArrowUp" ? visibleTranscriptIndexes[at - 1] : event.key === "Home" ? visibleTranscriptIndexes[0] : event.key === "End" ? visibleTranscriptIndexes[visibleTranscriptIndexes.length - 1] : undefined;
                    if (next !== undefined) { event.preventDefault(); setFocusedSegment(next); transcriptRefs.current[next]?.focus(); }
                  }} className={"asr-transcript-line " + (index === activeSegment ? "active" : "")} onClick={() => seekTo(segment.start)} aria-label={"跳转到 " + formatShort(segment.start)}><time>{formatShort(segment.start)}</time><span>{marked(segment.text, readerQuery)}</span></button>;
                })}</div>) : <div className="asr-plain-transcript">{plainLines.map((line, index) => {
                  if (readerFilterMode && query && !line.toLocaleLowerCase().includes(lowerQuery)) return null;
                  const offset = plainLines.slice(0, index).reduce((sum, value) => sum + value.length + 1, 0);
                  return <p key={index} ref={(element) => { paragraphRefs.current[index] = element; }} data-offset={offset}>{marked(line, readerQuery)}</p>;
                })}</div>}
                {audioUrl && !audioError ? <div className="asr-audio-wrap"><audio ref={audioRef} controls preload="metadata" src={audioUrl} onTimeUpdate={onAudioTime} onError={() => setAudioError(true)} /><span>{(record.playback_position || record.read_position || 0) > 0 ? "上次读到 " + formatShort(record.playback_position || record.read_position || 0) : "播放进度会自动保存"}</span></div> : <p className="asr-media-note">{audioError ? "缓存音轨不可用。文字记录仍可阅读和导出。" : "原始音频未保留。文字记录仍可阅读和导出。"}</p>}
                <p className="asr-reader-tail">{timingText} 文字与音轨缓存分开保存。</p>
              </article>
            </>}
          </section>}
        </main>

        <aside className="asr-sidebar">
          <section className="asr-side-search" aria-label="搜索结果"><label className="asr-global-search"><span><SearchIcon /></span><input value={nav.query} onChange={(event) => setQuery(event.target.value)} placeholder="查找会议、访谈或原话" aria-label="搜索标题、来源和转写内容" /></label><div className="asr-tags" role="group" aria-label="结果筛选">{([["all", "全部"], ["unseen", "未查看"], ["partial", "有缺失"]] as const).map(([value, label]) => <button key={value} className={nav.filter === value ? "active" : ""} aria-pressed={nav.filter === value} onClick={() => setFilter(value)}># {label}</button>)}</div></section>
          {activeTask ? <section className="asr-side-section"><div className="asr-side-heading"><h2 className="asr-display">Queue</h2><span>正在处理</span></div><strong className="asr-queue-name">{activeTask.filename}</strong><p className="asr-queue-copy">{statusMessage(jobStatus?.message) || "正在准备音轨"}</p><div className="asr-queue-progress"><span style={{ width: (jobStatus?.progress ?? 0) + "%" }} /></div><div className="asr-queue-meta"><span>{jobStatus ? Math.round(jobStatus.progress) + "%" : "处理中"}</span><span>{queued.length} 项等待</span></div><button className="asr-text-button" onClick={togglePause}>{queueState?.paused ? "继续队列" : "当前文件完成后暂停"}</button></section> : queueState?.paused ? <section className="asr-side-section"><div className="asr-side-heading"><h2 className="asr-display">Queue</h2><span>已暂停</span></div><p className="asr-queue-copy">文件会等待，需要点击“继续队列”才会开始下一项。</p><button className="asr-button asr-button-small" onClick={togglePause}>继续队列</button></section> : null}
          {queued.length > 0 && <section className="asr-side-section asr-queue-order"><div className="asr-side-heading"><h2 className="asr-display">Next up</h2><span>{queued.length} 项待处理</span></div><p className="asr-side-copy">调整等待顺序不会影响当前文件。</p><ol>{queued.map((item, index) => <li key={item.id}><span className="asr-order-number">{index + 1}</span><span className="asr-queued-title" title={item.filename}>{item.filename}</span><button disabled={index === 0} aria-label={item.filename + " 向前移动"} onClick={() => void moveQueued(item.id, -1)}>↑</button><button disabled={index === queued.length - 1} aria-label={item.filename + " 向后移动"} onClick={() => void moveQueued(item.id, 1)}>↓</button></li>)}</ol></section>}
          <section className="asr-side-section asr-batches-section"><div className="asr-side-heading"><h2 className="asr-display">Batches</h2><span>最近批次</span></div>{batches.length ? batches.map((batch) => {
            const readyCount = batch.items.filter((item) => canRead(item)).length;
            return <button key={batch.id} className={"asr-batch-link " + (nav.view === "tasks" && nav.batchId === batch.id ? "selected" : "")} onClick={() => { setTaskQuery(""); setTaskFilter("all"); navigate("tasks", batch.id); }}><strong>{batch.name}</strong><small>{readyCount} 份可查看 / {batch.items.length} 个文件</small></button>;
          }) : <p className="asr-side-copy">最近的转写批次会显示在这里。</p>}<button className="asr-storage-link" onClick={() => { refreshCache(); setSettingsOpen(true); }}>存储与音轨说明</button></section>
          <section className="asr-side-section asr-runtime-section"><div className="asr-side-heading"><h2 className="asr-display">Setup</h2><span>本机配置</span></div><strong>{health?.current_model || "Qwen3-ASR"}</strong><p>{health?.aligner_available ? "强制对齐器可用 · " + health.aligner_backend : "未启用强制对齐 · 无分段时间戳"}</p><button className="asr-text-button" onClick={() => setSettingsOpen(true)}>查看模型与存储说明</button></section>
        </aside>
      </div>
      <footer className="asr-footer"><span>Qwen ASR · 本地语音转写 · 中文字体：MiSans</span><span>退出应用会停止本地处理；重开后未完成文件从头处理，已保存结果保留。浏览器中关闭页面不会停止服务。</span></footer>
    </div>

    {importOpen && <div className="asr-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !importBusy) setImportOpen(false); }}><section className="asr-modal" role="dialog" aria-modal="true" aria-labelledby="import-title">
      <header className="asr-modal-heading"><div><h2 id="import-title">新建转写</h2><p>选文件、核对清单，再加入本地任务队列。</p></div><button className="asr-modal-close" aria-label="关闭" onClick={() => !importBusy && setImportOpen(false)}>×</button></header>
      <div className="asr-modal-body" onDragOver={(event: DragEvent<HTMLDivElement>) => { event.preventDefault(); setDragActive(true); }} onDragLeave={() => setDragActive(false)} onDrop={(event: DragEvent<HTMLDivElement>) => { event.preventDefault(); event.stopPropagation(); setDragActive(false); addFiles(Array.from(event.dataTransfer.files)); }}>
        <label className="asr-field"><span>批次名称</span><input value={batchName} maxLength={120} onChange={(event) => setBatchName(event.target.value)} /></label>
        <div className={"asr-drop-zone " + (dragActive ? "active" : "")}><b>把音频或视频拖到这里</b><span>支持音频文件、视频文件和整个文件夹。不同来源的同名文件会分别保留。</span><div><button className="asr-button" onClick={() => folderInput.current?.click()}>选择文件夹</button><button className="asr-button" onClick={() => fileInput.current?.click()}>选择文件</button></div></div>
        <input ref={folderInput} className="asr-hidden" type="file" multiple {...({ webkitdirectory: "" } as Record<string, string>)} onChange={onPicker} /><input ref={fileInput} className="asr-hidden" type="file" multiple onChange={onPicker} />
        <div className="asr-import-summary">发现 {picked.length} 项 · 已选 {picked.filter((item) => item.selected && !item.reason).length} 项 · 排除 {picked.filter((item) => item.reason).length} 项</div>
        {picked.length > 0 && <div className="asr-import-list">{picked.map((item) => <div className={"asr-import-row " + (item.reason ? "disabled" : "")} key={item.key}><label><input type="checkbox" checked={item.selected} disabled={Boolean(item.reason)} onChange={(event) => setPicked((rows) => rows.map((row) => row.key === item.key ? { ...row, selected: event.target.checked } : row))} /></label><div><strong>{item.file.name}</strong><small title={item.path}>{item.sourceUnknown ? item.path + "（来源目录未提供）" : item.path}</small></div><span className={item.reason ? "asr-warning-text" : ""}>{item.reason || sizeOf(item.file.size)}</span><button aria-label={"移除 " + item.file.name} onClick={() => setPicked((rows) => rows.filter((row) => row.key !== item.key))}>×</button></div>)}</div>}
        {/* Remember the choice immediately: requiring a successful submit first
            loses it when the dialog is closed without importing. */}
        <label className={"asr-check-option " + (health?.aligner_available ? "" : "disabled")}><input type="checkbox" checked={align} disabled={!health?.aligner_available} onChange={(event) => { setAlign(event.target.checked); patchPreferences({ alignmentRequested: event.target.checked }); }} /><span>请求词级时间戳{health?.aligner_available ? " · " + health.aligner_backend + " 对齐器可用" : " · 当前没有可用的强制对齐器"}</span></label>
        <p className="asr-import-note">{activeTask || queueState?.paused ? "文件会等待当前文件完成后继续；暂停状态下需要点击“继续队列”才会开始。" : "文件会按清单顺序依次处理。每个文件形成独立任务，可分别查看和重试。"}</p>{importError && <p className="asr-form-error" role="alert">{statusMessage(importError)}</p>}
      </div>
      <footer className="asr-modal-footer"><span>{picked.filter((item) => item.selected && !item.reason).length} 个文件将进入“{batchName || "未命名批次"}”</span><div><button className="asr-button" onClick={() => setImportOpen(false)} disabled={importBusy}>取消</button><button className="asr-button asr-button-primary" disabled={!picked.some((item) => item.selected && !item.reason) || importBusy} onClick={() => void submitImport()}>{importBusy ? "正在加入…" : (activeTask || queueState?.paused ? "加入待处理" : "开始处理") + " · " + picked.filter((item) => item.selected && !item.reason).length + " 个文件"}</button></div></footer>
    </section></div>}

    {exportOpen && <div className="asr-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !exportBusy) setExportOpen(false); }}><section className="asr-modal asr-modal-narrow" role="dialog" aria-modal="true" aria-labelledby="export-title">
      <header className="asr-modal-heading"><div><h2 id="export-title">导出转写结果</h2><p>完整结果默认包含；有缺失的结果需要明确选择。</p></div><button className="asr-modal-close" onClick={() => setExportOpen(false)} aria-label="关闭">×</button></header>
      <div className="asr-modal-body"><p className="asr-export-summary">已选择 {exportCandidates.length} 份结果：{exportCandidates.filter((item) => item.integrity !== "partial").length} 份完整{exportCandidates.some((item) => item.integrity === "partial") ? "，" + exportCandidates.filter((item) => item.integrity === "partial").length + " 份有缺失（默认不导出，可在下方勾选一起导出）" : ""}。</p><div className="asr-format-field"><span>文件格式 · 可多选</span><div className="asr-format-options" role="group" aria-label="导出文件格式">{SUBTITLE_FORMATS.map((format) => <label key={format.id} className={"asr-format-option" + (exportFormats.includes(format.id) ? " checked" : "")}><input type="checkbox" checked={exportFormats.includes(format.id)} onChange={() => toggleExportFormat(format.id)} /><span>{format.label}</span><small>.{format.ext}</small></label>)}</div>{exportFormats.length ? null : <p className="asr-form-error">请至少保留一种格式。</p>}</div>{exportCandidates.some((item) => item.integrity === "partial") && <label className="asr-check-option"><input type="checkbox" checked={includePartial} onChange={(event) => setIncludePartial(event.target.checked)} /><span>同时导出有缺失的结果</span></label>}<p className="asr-import-note">多份结果或多种格式会打包为一个 ZIP，并附导出清单；只有单个文件时直接下载。TXT/JSON 不含时间轴；字幕格式在缺少分段时间戳时只生成整段粗略时间并标注。</p></div>
      <footer className="asr-modal-footer"><span>将生成 {exportFileCount} 个文件（{exportIncluded.length} 份结果 × {exportFormats.length || 1} 种格式）</span><div><button className="asr-button" onClick={() => setExportOpen(false)} disabled={exportBusy}>取消</button><button className="asr-button asr-button-primary" disabled={!exportFormats.length || !exportIncluded.length || exportBusy} onClick={() => void exportNow()}>{exportBusy ? "正在生成…" : "导出 " + exportFileCount + " 个文件"}</button></div></footer>
    </section></div>}

    {detailTask && <div className="asr-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetailTask(null); }}><section className="asr-modal asr-modal-narrow" role="dialog" aria-modal="true" aria-labelledby="detail-title">
      <header className="asr-modal-heading"><div><h2 id="detail-title">任务详情</h2><p>{titleOf(detailTask)}</p></div><button className="asr-modal-close" onClick={() => setDetailTask(null)} aria-label="关闭">×</button></header>
      <div className="asr-modal-body"><div className="asr-setting-row"><span>完整文件名</span><strong>{detailTask.filename}</strong></div><div className="asr-setting-row"><span>来源</span><strong>{detailTask.source_path || "来源目录未提供"}</strong></div><div className="asr-setting-row"><span>导入时间</span><strong>{detailTask.added_at ? dateOf(detailTask.added_at) : "未知"}</strong></div><div className="asr-setting-row"><span>时长</span><strong>{detailTask.duration ? durationOf(detailTask.duration) : "待检测"}</strong></div><div className="asr-setting-row"><span>任务参数</span><strong>{detailTask.align ? "请求词级对齐" : "未请求对齐"}</strong></div><div className="asr-setting-row"><span>当前阶段</span><strong>{statusOf(detailTask).label}</strong></div>{detailTask.error && <div className="asr-setting-row"><span>原因</span><strong>{statusMessage(detailTask.error)}</strong></div>}<p className="asr-import-note">这里显示的是后端记录的真实信息；没有结果时不会编造会议日期或来源。</p></div>
      <footer className="asr-modal-footer"><span>未完成文件重开会从头处理，不是片段续跑。</span><div><button className="asr-button" onClick={() => setDetailTask(null)}>关闭</button>{(detailTask.status === "error" || detailTask.status === "cancelled") && <button className="asr-button asr-button-primary" onClick={() => { setDetailTask(null); void retryTask(detailTask); }}>重新转写</button>}</div></footer>
    </section></div>}

    {settingsOpen && <div className="asr-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSettingsOpen(false); }}><section className="asr-modal asr-modal-narrow" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <header className="asr-modal-heading"><div><h2 id="settings-title">模型与存储</h2><p>转写任务在本机运行。</p></div><button className="asr-modal-close" onClick={() => setSettingsOpen(false)} aria-label="关闭">×</button></header>
      <div className="asr-modal-body"><div className="asr-setting-row"><span>语音识别模型</span><strong>{health?.current_model || "状态未知"}</strong></div><div className="asr-setting-row"><span>强制对齐器</span><strong>{health?.aligner_available ? health.aligner_backend + " · 可用" : "不可用，使用段级时间戳"}</strong></div><div className="asr-setting-row"><span>视频音轨缓存</span><strong>{cacheInfo ? cacheInfo.count + " 个 · " + sizeOf(cacheInfo.size_bytes) : "读取中"}</strong></div><div className="asr-setting-row"><span>结果库版面</span><strong>{preferences.listStyle === "grid" ? "两列版面" : "紧凑列表"}</strong></div><div className="asr-setting-row"><span>阅读视图</span><strong>{preferences.readerView === "paragraph" ? "段落阅读" : "字幕行"}</strong></div><p className="asr-import-note">清理后这些视频结果将无法回听，文字记录会保留；应用不长期保留导入的音频副本。</p><p className="asr-import-note">退出应用会停止本地处理；重开后未完成文件从头处理，已保存结果保留。浏览器中关闭页面不会停止服务。</p><button className="asr-button asr-danger-button" disabled={!cacheInfo?.count || cacheBusy} onClick={() => void clearCache()}>{cacheBusy ? "正在清理…" : "清理视频音轨缓存"}</button></div>
      <footer className="asr-modal-footer"><span>文字记录与音轨缓存分别管理</span><button className="asr-button asr-button-primary" onClick={() => setSettingsOpen(false)}>完成</button></footer>
    </section></div>}
    {dragActive && !importOpen && <div className="asr-drag-overlay" aria-hidden="true"><div><b>松开以导入文件</b><span>同名但来自不同目录的文件会分别保留。</span></div></div>}
    {toast && <div className="asr-toast" role="status">{toast}</div>}
  </div>;
}
