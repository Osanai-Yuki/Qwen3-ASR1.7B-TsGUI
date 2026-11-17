// Pure row derivation for the workbench table: reference-mode queue items
// joined with history metadata. Summary columns stay null until P2.

import type { HistoryItem, QueueItem } from "../types";

export type WorkbenchExportState = "pending" | "done" | "failed" | "skipped";

export interface WorkbenchRow {
  id: string;
  title: string;
  filename: string;
  sourcePath: string;
  batchName: string;
  status: QueueItem["status"];
  error: string | null;
  historyId: string | null;
  duration: number | null;
  finishedAt: string | null;
  addedAt: string;
  exportState: WorkbenchExportState;
  exportNames: string[];
  exportError: string | null;
}

export const EXPORT_STATE_LABEL: Record<WorkbenchExportState, string> = {
  pending: "等待导出",
  done: "已导出",
  failed: "导出失败",
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
      return {
        id: item.id,
        title: record?.title || item.title || item.filename,
        filename: item.filename,
        sourcePath: item.source_path || item.filename,
        batchName: item.batch_name || "工作台",
        status: item.status,
        error: item.error ?? null,
        historyId: item.history_id ?? null,
        duration: record?.duration ?? null,
        finishedAt: item.finished_at ?? null,
        addedAt: item.added_at,
        exportState: item.export_state ?? "pending",
        exportNames: (item.export_paths ?? []).map((p) => p.name),
        exportError: item.export_error ?? null,
      };
    })
    .sort((a, b) => (b.finishedAt || b.addedAt).localeCompare(a.finishedAt || a.addedAt));
}