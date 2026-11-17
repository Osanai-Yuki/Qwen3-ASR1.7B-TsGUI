// Workbench table row derivation checks.
// Run: node --experimental-strip-types tests/workbench.test.mjs
import assert from "node:assert/strict";
import { buildWorkbenchRows, EXPORT_STATE_LABEL } from "../src/utils/workbench.ts";

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
  source_path: "sub/a.wav", batch_name: "会议", history_id: "h1",
  finished_at: "2026-11-17T02:00:00+00:00",
  export_state: "done", export_paths: [{ format: "srt", path: "/x/a.srt", name: "a.srt" }],
};
const refB = {
  ...base, id: "b1", filename: "b.wav", source_mode: "reference",
  added_at: "2026-11-17T03:00:00+00:00", status: "queued",
};

const records = new Map([
  ["h1", { id: "h1", title: "周会纪要", duration: 65.5 }],
]);

const rows = buildWorkbenchRows([upload, refB, refA], records);
assert.deepEqual(rows.map((r) => r.id), ["b1", "a1"], "reference only, newest first");
assert.equal(rows[1].title, "周会纪要");
assert.equal(rows[1].duration, 65.5);
assert.equal(rows[1].sourcePath, "sub/a.wav");
assert.equal(rows[1].batchName, "会议");
assert.deepEqual(rows[1].exportNames, ["a.srt"]);
assert.equal(rows[0].exportState, "pending");
assert.equal(rows[0].duration, null);
assert.equal(rows[0].title, "b.wav");
assert.equal(EXPORT_STATE_LABEL[rows[1].exportState], "已导出");
assert.equal(Object.keys(EXPORT_STATE_LABEL).length, 4);

console.log("workbench row checks passed");