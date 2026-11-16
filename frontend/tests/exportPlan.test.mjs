// Export-plan regression checks.
//
// The allocator is synchronous and runs on the browser main thread, so a
// collision loop cannot be interrupted from inside the test.  Run the checks
// in a child process with a hard timeout: a loop is a failure, not a hung CI.
//
// Run: node --experimental-strip-types tests/exportPlan.test.mjs
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const WORKER_FLAG = "EXPORT_PLAN_TEST_WORKER";

if (process.env[WORKER_FLAG] !== "1") {
  const child = spawnSync(
    process.execPath,
    ["--experimental-strip-types", fileURLToPath(import.meta.url)],
    {
      env: { ...process.env, [WORKER_FLAG]: "1" },
      timeout: 3000,
      encoding: "utf8",
    },
  );
  process.stdout.write(child.stdout ?? "");
  process.stderr.write(child.stderr ?? "");
  if (child.error?.code === "ETIMEDOUT" || child.signal === "SIGTERM") {
    console.error("FAIL: export planner did not finish in 3000ms (collision loop)");
    process.exit(1);
  }
  if (child.status !== 0) {
    console.error("export-plan checks failed");
    process.exit(child.status ?? 1);
  }
  console.log("export-plan checks passed");
  process.exit(0);
}

const assert = (await import("node:assert/strict")).default;
const { allocateExportPaths, buildExportPlan, exportArchiveName, MANIFEST_NAME, zipBlob } = await import(
  "../src/utils/exportPlan.ts"
);

function item(id, desiredPath, title = id) {
  return { id, title, filename: desiredPath.split("/").pop(), desiredPath, content: "text" };
}

assert.equal(exportArchiveName([{ id: "1", filename: "会议.mp4", title: "会议" }], ["srt", "vtt"]), "会议_SRT-VTT.zip");
assert.equal(exportArchiveName([{ id: "1", filename: "会议.mp4", title: "会议" }, { id: "2", filename: "访谈.mp3", title: "访谈" }], ["srt"]), "会议_等2份_SRT.zip");

function assertUniquePaths(items, plan) {
  const names = plan.files.map((file) => file.finalPath.toLowerCase());
  assert.equal(new Set(names).size, names.length, "duplicate final paths");
  assert.equal(new Set(names).size, items.length, "every item needs its own path");
  for (const name of names) assert.ok(!name.startsWith("/") && !name.includes(".."));
}

// 1. Ordinary two-way collision.
{
  const items = [item("aaaaaaaaaaaa", "folder/same.txt"), item("bbbbbbbbbbbb", "folder/same.txt")];
  const plan = buildExportPlan(items);
  assertUniquePaths(items, plan);
  assert.equal(plan.files[0].finalPath, "folder/same.txt");
  assert.equal(plan.files[1].finalPath, "folder/same__bbbbbbbb.txt");
}

// 2. Three IDs share their first eight characters (the reviewed hang).
{
  const items = [
    item("12345678aaaa", "folder/same.txt"),
    item("12345678bbbb", "folder/same.txt"),
    item("12345678cccc", "folder/same.txt"),
  ];
  const plan = buildExportPlan(items);
  assertUniquePaths(items, plan);
  assert.equal(plan.files.length, 3);
}

// 3. Allocation must not depend on the selection order.
{
  const items = [
    item("12345678aaaa", "folder/same.txt"),
    item("12345678bbbb", "folder/same.txt"),
    item("12345678cccc", "folder/same.txt"),
  ];
  const forward = buildExportPlan(items);
  const reverse = buildExportPlan([...items].reverse());
  const mapForward = new Map(forward.files.map((file) => [file.id, file.finalPath]));
  for (const file of reverse.files) {
    assert.equal(file.finalPath, mapForward.get(file.id), "path changed with selection order");
  }
}

// 4. Case-insensitive collisions (extracted on Windows/macOS).
{
  const items = [
    item("aaaaaaaaaaaa", "A/Same.txt"),
    item("bbbbbbbbbbbb", "a/same.TXT"),
    item("cccccccccccc", "A/SAME.txt"),
  ];
  assertUniquePaths(items, buildExportPlan(items));
}

// 5. A real result named like the manifest, plus two colliding results.
{
  const items = [
    item("12345678aaaa", MANIFEST_NAME),
    item("12345678bbbb", MANIFEST_NAME),
    item("12345678cccc", MANIFEST_NAME),
  ];
  const plan = buildExportPlan(items);
  assertUniquePaths(items, plan);
  assert.ok(
    plan.files.every((file) => file.finalPath.toLowerCase() !== plan.manifestPath.toLowerCase()),
    "manifest path clobbered by a result",
  );
}

// 6. The encoder refuses duplicate names instead of silently renaming.
{
  assert.throws(
    () => zipBlob([{ name: "a.txt", content: "1" }, { name: "A.TXT", content: "2" }]),
    /duplicate archive path/,
  );
}

// 7. Single-file plan still allocates through the same path.
{
  const plan = buildExportPlan([item("aaaaaaaaaaaa", "dir/report.txt")]);
  assert.equal(plan.files[0].finalPath, "dir/report.txt");
  assert.equal(plan.manifestPath, MANIFEST_NAME);
}

// 8. Path normalization strips dangerous segments and control characters.
{
  const { normalizeExportPath } = await import("../src/utils/exportPlan.ts");
  assert.equal(normalizeExportPath("C:\\..\\evil\\..\\meeting.wav", "x.wav", "txt"), "evil/meeting.txt");
  assert.equal(normalizeExportPath(null, "a\u0001b.wav", "SRT"), "ab.srt");
}

console.log("worker: all export-plan assertions passed");
