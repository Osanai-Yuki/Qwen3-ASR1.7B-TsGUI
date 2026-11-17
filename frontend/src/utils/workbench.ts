// Pure row derivation for the workbench table: reference-mode queue items
// joined with history metadata (including model-generated summaries).

import type { HistoryItem, QueueItem } from "../types";

export type WorkbenchExportState = "pending" | "done" | "failed" | "skipped";
export type WorkbenchSummaryState = WorkbenchExportState;

export interface WorkbenchRow {
  id: string;
  title: string;
  filename: string;
  sourcePath: string;
  batchName: string;
  batchId: string | null;
  status: QueueItem["status"];
  error: string | null;
  historyId: string | null;
  duration: number | null;
  finishedAt: string | null;
  addedAt: string;
  exportState: WorkbenchExportState;
  exportNames: string[];
  exportError: string | null;
  summaryState: WorkbenchSummaryState;
  summaryError: string | null;
  summary: string | null;
  keywords: string[];
  highlights: string[];
}

export const EXPORT_STATE_LABEL: Record<WorkbenchExportState, string> = {
  pending: "等待导出",
  done: "已导出",
  failed: "导出失败",
  skipped: "未启用",
};

export const SUMMARY_STATE_LABEL: Record<WorkbenchSummaryState, string> = {
  pending: "待总结",
  done: "已总结",
  failed: "总结失败",
  skipped: "未启用",
};

export function buildWorkbenchRows(
  items: QueueItem[],
  records: Map<string, HistoryItem>,
): WorkbenchRow[] {
  return items
    .filter((item) => item.source_mode === "reference")
    .map((item) => {
      const record = item.history_id ? records.get(item.history_id) : undefined;
      const summary = record?.summary ?? null;
      return {
        id: item.id,
        title: summary?.title || record?.title || item.title || item.filename,
        filename: item.filename,
        sourcePath: item.source_path || item.filename,
        batchName: item.batch_name || "工作台",
        batchId: item.batch_id ?? null,
        status: item.status,
        error: item.error ?? null,
        historyId: item.history_id ?? null,
        duration: record?.duration ?? null,
        finishedAt: item.finished_at ?? null,
        addedAt: item.added_at,
        exportState: item.export_state ?? "pending",
        exportNames: (item.export_paths ?? []).map((p) => p.name),
        exportError: item.export_error ?? null,
        summaryState: item.summary_state ?? "pending",
        summaryError: item.summary_error ?? null,
        summary: summary?.summary ?? null,
        keywords: summary?.keywords ?? [],
        highlights: summary?.highlights ?? [],
      };
    })
    .sort((a, b) => (b.finishedAt || b.addedAt).localeCompare(a.finishedAt || a.addedAt));
}