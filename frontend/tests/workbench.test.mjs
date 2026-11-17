// Workbench table row derivation checks.
// Run: node --experimental-strip-types tests/workbench.test.mjs
import assert from "node:assert/strict";
import { buildWorkbenchRows, EXPORT_STATE_LABEL, SUMMARY_STATE_LABEL, renderNamePreview } from "../src/utils/workbench.ts";

const base = {
  size: 10,
  align: false,
  status: "done",
  history_id: null,
  error: null,
  added_at: "2026-11-17T00:00:00+00:00",
  finished_at: null,
};

const upload = { ...base, id: "u1", filename: "up.wav", source_mode: "upload" };
const refA = {
  ...base, id: "a1", filename: "a.wav", source_mode: "reference",
  source_path: "sub/a.wav", batch_name: "会议", batch_id: "b1", history_id: "h1",
  finished_at: "2026-11-17T02:00:00+00:00",
  export_state: "done", export_paths: [{ format: "srt", path: "/x/a.srt", name: "a.srt" }],
  summary_state: "done",
};
const refB = {
  ...base, id: "b1", filename: "b.wav", source_mode: "reference",
  added_at: "2026-11-17T03:00:00+00:00", status: "queued", summary_state: "failed",
  summary_error: "summary_parse",
};

const records = new Map([
  ["h1", {
    id: "h1", title: "周会纪要", duration: 65.5,
    summary: {
      title: "导出流程决议", summary: "决定上线新导出流程。",
      keywords: ["导出", "流程"], highlights: ["张三负责待办"],
      model: "m", generated_at: "2026-11-17T02:10:00+00:00", state: "done",
    },
  }],
]);

const rows = buildWorkbenchRows([upload, refB, refA], records);
assert.deepEqual(rows.map((r) => r.id), ["b1", "a1"], "reference only, newest first");
assert.equal(rows[1].title, "导出流程决议", "summary title wins over record title");
assert.equal(rows[1].duration, 65.5);
assert.equal(rows[1].sourcePath, "sub/a.wav");
assert.equal(rows[1].batchName, "会议");
assert.equal(rows[1].batchId, "b1");
assert.deepEqual(rows[1].exportNames, ["a.srt"]);
assert.equal(rows[1].summary, "决定上线新导出流程。");
assert.deepEqual(rows[1].keywords, ["导出", "流程"]);
assert.deepEqual(rows[1].highlights, ["张三负责待办"]);
assert.equal(rows[1].summaryState, "done");
assert.equal(rows[0].exportState, "pending");
assert.equal(rows[0].summaryState, "failed");
assert.equal(rows[0].summaryError, "summary_parse");
assert.equal(rows[0].summary, null);
assert.deepEqual(rows[0].keywords, []);
assert.equal(rows[0].duration, null);
assert.equal(rows[0].title, "b.wav");
assert.equal(EXPORT_STATE_LABEL[rows[1].exportState], "已导出");
assert.equal(SUMMARY_STATE_LABEL[rows[0].summaryState], "总结失败");
assert.equal(Object.keys(EXPORT_STATE_LABEL).length, 4);
assert.equal(Object.keys(SUMMARY_STATE_LABEL).length, 4);

const parts = { stem: "a", date: "20261117", time: "1405", model: "Qwen3-ASR-1.7B-Q8_0", batch: "b:c", id: "abcd1234" };
assert.equal(renderNamePreview("{stem}_{date}_{time}_{model}_{batch}_{id}", parts), "a_20261117_1405_Qwen3-ASR-1.7B-Q8_0_b_c_abcd1234");
assert.equal(renderNamePreview("{stem}", parts), "a");
assert.equal(renderNamePreview("{unknown}_{stem}", parts), "{unknown}_a");
assert.equal(renderNamePreview("../{stem}", parts), "_a");
assert.equal(renderNamePreview("", parts), "a");

console.log("workbench row checks passed");