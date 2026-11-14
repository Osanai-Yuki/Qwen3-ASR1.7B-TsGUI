"""Controlled backend for UI verification of the repair round.

Runs the real FastAPI app from the isolated runtime copy with SKIP_LLAMA=1
(no GPU model), seeds fictional history/queue data, and prints the ids the
browser driver needs.  Never touches the real project data directory.
"""
import json
import os
import shutil
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "runtime"
DATA = RUNTIME / "data"
if DATA.exists():
    shutil.rmtree(DATA)

os.environ["SKIP_LLAMA"] = "1"
os.environ["ALIGNER_BACKEND"] = "cpu"
os.environ["HOST"] = "127.0.0.1"
os.environ["PORT"] = "8765"
sys.path.insert(0, str(RUNTIME))
os.chdir(RUNTIME)

import backend.main as main_mod  # noqa: E402
import backend.queue_api as qapi  # noqa: E402

history = main_mod.history_store

# --- A02: plain-text record with 80 matching lines -------------------------
plain_lines = [
    f"第 {i + 1} 行 独立复验 记录：本段用于检查纯文本查找计数与高亮。"
    for i in range(80)
]
plain = history.save(
    filename="复验-纯文本.wav",
    title="复验纯文本记录",
    text="\n".join(plain_lines),
    segments=[],
    stats={"audio_duration": 0, "char_count": sum(map(len, plain_lines)), "segment_count": 0},
    batch_id="a1a1a1a1a1a1",
    batch_name="复验资料",
    task_id="b1b1b1b1b1b1",
    source_path="复验资料/纯文本.wav",
)

# --- A02: segment record whose last paragraph holds the query -------------
segments = [
    {"start": float(i * 3), "end": float(i * 3 + 2), "text": f"第 {i + 1} 段 普通内容，用于滚动与检索。"}
    for i in range(71)
]
segments.append({"start": 213.0, "end": 215.0, "text": "最后一段 定位词 用于检查段落命中导航。"})
segmented = history.save(
    filename="复验-分段.wav",
    title="复验分段记录",
    text="\n".join(seg["text"] for seg in segments),
    segments=segments,
    stats={"audio_duration": 216, "segment_count": len(segments), "char_count": sum(len(s["text"]) for s in segments)},
    batch_id="a1a1a1a1a1a1",
    batch_name="复验资料",
    task_id="b2b2b2b2b2b2",
    source_path="复验资料/分段.wav",
)

# --- A10: batch selection scope (two readable, one retryable) -------------
sel_batch = "c1c1c1c1c1c1"
sel_name = "筛选验证批次"
done_a = history.save(
    filename="可导出-甲.wav",
    title="可导出甲",
    text="甲记录正文。",
    segments=[{"start": 0, "end": 1, "text": "甲记录正文。"}],
    stats={"audio_duration": 1, "segment_count": 1},
    batch_id=sel_batch, batch_name=sel_name,
    task_id="d1d1d1d1d1d1", source_path="筛选验证/可导出-甲.wav",
)
done_b = history.save(
    filename="隐藏-乙.wav",
    title="隐藏乙",
    text="乙记录正文。",
    segments=[{"start": 0, "end": 1, "text": "乙记录正文。"}],
    stats={"audio_duration": 1, "segment_count": 1},
    batch_id=sel_batch, batch_name=sel_name,
    task_id="d2d2d2d2d2d2", source_path="筛选验证/隐藏-乙.wav",
)

store = qapi.store
assert store is not None
for qid, filename, path, hid in (
    ("d1d1d1d1d1d1", "可导出-甲.wav", "筛选验证/可导出-甲.wav", done_a["id"]),
    ("d2d2d2d2d2d2", "隐藏-乙.wav", "筛选验证/隐藏-乙.wav", done_b["id"]),
):
    store.add(
        qid=qid, filename=filename, staged_name=f"{qid}.wav", size=1024,
        align=False, batch_id=sel_batch, batch_name=sel_name, source_path=path,
    )
    store.set_status(qid, "done", history_id=hid)
    store.set_persistence(qid, state="saved", history_id=hid)
    store.unlink_staged(store.get(qid))

store.add(
    qid="d3d3d3d3d3d3", filename="重试-丙.wav", staged_name="d3d3d3d3d3d3.wav",
    size=1024, align=False, batch_id=sel_batch, batch_name=sel_name,
    source_path="筛选验证/重试-丙.wav",
)
(store.uploads_dir / "d3d3d3d3d3d3.wav").write_bytes(b"x")
store.set_status("d3d3d3d3d3d3", "error", error="test_failure")

seeds = {
    "plain_id": plain["id"],
    "segmented_id": segmented["id"],
    "batch_id": sel_batch,
    "readable_ids": [done_a["id"], done_b["id"]],
}
(RUNTIME / "seeds.json").write_text(json.dumps(seeds, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(seeds, ensure_ascii=False), flush=True)

import uvicorn  # noqa: E402

uvicorn.run(main_mod.app, host="127.0.0.1", port=8765, log_level="warning", access_log=False)
