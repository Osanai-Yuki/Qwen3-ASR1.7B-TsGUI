"""Batch summarization phase: GPU sequential model swap after transcription.

When a batch finishes transcribing and summarization is enabled, the ASR
model is swapped out for an instruct model, every durable result of the
batch is summarized serially, then the original ASR model is swapped back.
While the phase runs the transcription slot is gated (boot ready=False), so
manual jobs get 409 and the queue worker backs off — one model resident in
VRAM at a time.

All main.py collaborators arrive through init() dependency injection; this
module never imports main. Phase progress persists to data/summary_phase.json
so a restart can reconcile an interrupted phase instead of pretending the
instruct model is still loaded.
"""
import asyncio
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

logger = logging.getLogger("asr")

router = APIRouter()

ACTIVE_PHASES = ("swapping_in", "summarizing", "swapping_back")
MAX_TRANSCRIPT_CHARS = 12000

_deps: dict | None = None
_lock = threading.Lock()
_phase: dict = {"phase": "idle", "batch_id": None, "done": 0, "total": 0, "model": None, "error": None}
_runs: list[dict] = []
_task: asyncio.Task | None = None
_phase_file: Path | None = None


def init(
    *,
    data_dir: Path,
    swap_in_fn,
    swap_out_fn,
    read_history_fn,
    save_summary_fn,
    list_batch_items_fn,
    find_item_by_history_fn,
    set_item_summary_fn,
    set_gate_fn,
    config_fn,
    list_summary_models_fn,
    base_url_fn,
    llm_call_fn=None,
) -> None:
    global _deps, _phase_file, _phase, _task
    _deps = {
        "swap_in_fn": swap_in_fn,
        "swap_out_fn": swap_out_fn,
        "read_history_fn": read_history_fn,
        "save_summary_fn": save_summary_fn,
        "list_batch_items_fn": list_batch_items_fn,
        "find_item_by_history_fn": find_item_by_history_fn,
        "set_item_summary_fn": set_item_summary_fn,
        "set_gate_fn": set_gate_fn,
        "config_fn": config_fn,
        "list_summary_models_fn": list_summary_models_fn,
        "base_url_fn": base_url_fn,
        "llm_call_fn": llm_call_fn or _llm_call,
    }
    _phase_file = Path(data_dir) / "summary_phase.json"
    _phase_file.parent.mkdir(parents=True, exist_ok=True)
    _phase = {"phase": "idle", "batch_id": None, "done": 0, "total": 0, "model": None, "error": None}
    _runs.clear()
    _task = None
    _reconcile()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _persist() -> None:
    if _phase_file is None:
        return
    try:
        tmp = _phase_file.with_name(f".summary-phase-{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(_phase, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _phase_file)
    except OSError:
        logger.exception("Could not persist summary phase")


def _reconcile() -> None:
    """A restart means the instruct model is gone (boot loads ASR fresh)."""
    global _phase
    if _phase_file is not None and _phase_file.exists():
        try:
            saved = json.loads(_phase_file.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("phase") in ACTIVE_PHASES:
                _phase = {**_phase, **saved, "phase": "interrupted", "error": "restart_during_summary"}
                logger.warning("Summary phase interrupted by restart; ASR restored by boot")
                _persist()
        except Exception:
            logger.warning("Summary phase file unreadable; starting idle")


def busy() -> bool:
    with _lock:
        return _phase["phase"] in ACTIVE_PHASES


def state() -> dict:
    with _lock:
        return dict(_phase)


def _set_phase(**patch) -> None:
    with _lock:
        _phase.update(patch)
    _persist()


def parse_summary(raw: str) -> dict:
    """Strict-ish JSON contract: {title, summary, keywords[], highlights[]}.

    Tolerates code fences and leading prose, but a missing/empty summary or
    unparseable JSON is a failure, never a fabricated result.
    """
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (raw or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("summary_parse")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("summary_parse")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary_parse")

    def str_list(value, cap, item_cap):
        if not isinstance(value, list):
            return []
        return [v.strip()[:item_cap] for v in value if isinstance(v, str) and v.strip()][:cap]

    return {
        "title": str(data.get("title") or "").strip()[:120],
        "summary": summary.strip()[:2000],
        "keywords": str_list(data.get("keywords"), 12, 40),
        "highlights": str_list(data.get("highlights"), 10, 200),
    }


def build_prompt(text: str, title: str) -> list[dict]:
    transcript = (text or "").strip()
    truncated = len(transcript) > MAX_TRANSCRIPT_CHARS
    body = transcript[:MAX_TRANSCRIPT_CHARS]
    if truncated:
        body += "\n（转写过长，以上为前段节选）"
    return [
        {"role": "system", "content": (
            "你是本地会议纪要整理助手。只输出一个 JSON 对象，不要输出任何其他文字。"
            '字段：title（不超过30字的标题）、summary（150字以内的中文摘要）、'
            'keywords（3-8个关键词字符串数组）、highlights（特别内容：决议、待办、'
            '人名、关键数字等，字符串数组，最多6条）。无法确定的内容不要编造。'
        )},
        {"role": "user", "content": f"原标题：{title or '无'}\n转写全文：\n{body}"},
    ]


async def _llm_call(text: str, title: str) -> str:
    import httpx
    assert _deps is not None
    async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
        resp = await client.post(
            f"{_deps['base_url_fn']()}/v1/chat/completions",
            json={"messages": build_prompt(text, title), "temperature": 0.2,
                  "max_tokens": 1024, "stream": False},
        )
    if resp.status_code != 200:
        raise ValueError("summary_call")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _targets(run: dict) -> list[dict]:
    assert _deps is not None
    if run.get("only"):
        item = _deps["find_item_by_history_fn"](run["only"])
        return [item] if item and item.get("history_id") == run["only"] else []
    items = _deps["list_batch_items_fn"](run["batch_id"]) or []
    durable = [it for it in items if it.get("history_id")]
    return [it for it in durable if (it.get("summary_state") or "pending") in ("pending", "failed")]


def maybe_start_batch(batch_id: str | None) -> bool:
    """Completion hook: queue a summary run when the batch is fully terminal."""
    if not batch_id or _deps is None:
        return False
    cfg = _deps["config_fn"]()
    if not cfg.get("summarize") or not cfg.get("summary_model"):
        return False
    items = _deps["list_batch_items_fn"](batch_id) or []
    if not items or any(it.get("status") not in ("done", "error", "cancelled") for it in items):
        return False
    if not any(it.get("history_id") for it in items):
        return False
    return _enqueue_run({"batch_id": batch_id, "only": None})


def start_batch(batch_id: str) -> tuple[bool, str | None]:
    assert _deps is not None
    cfg = _deps["config_fn"]()
    if not cfg.get("summary_model"):
        return False, "summary_model_unset"
    items = _deps["list_batch_items_fn"](batch_id) or []
    if not any(it.get("history_id") for it in items):
        return False, "no_durable_results"
    return _enqueue_run({"batch_id": batch_id, "only": None}), None


def retry_item(history_id: str) -> tuple[bool, str | None]:
    assert _deps is not None
    cfg = _deps["config_fn"]()
    if not cfg.get("summary_model"):
        return False, "summary_model_unset"
    item = _deps["find_item_by_history_fn"](history_id)
    if item is None:
        return False, "not_found"
    return _enqueue_run({"batch_id": item.get("batch_id") or "legacy", "only": history_id}), None


def _enqueue_run(run: dict) -> bool:
    global _task
    with _lock:
        if any(r["batch_id"] == run["batch_id"] and r.get("only") == run.get("only") for r in _runs):
            return False
        if _phase["phase"] in ACTIVE_PHASES and _phase.get("batch_id") == run["batch_id"] and not run.get("only"):
            return False
        _runs.append(run)
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_run_loop())
    return True


async def _run_loop() -> None:
    assert _deps is not None
    while True:
        with _lock:
            run = _runs.pop(0) if _runs else None
        if run is None:
            # Keep the terminal phase (finished/failed) visible until the next
            # run starts; busy() only reports active phases.
            return
        try:
            await _run_one(run)
        except Exception:
            logger.exception("Summary run failed unexpectedly for %s", run.get("batch_id"))
            restored = await _restore_asr()
            _deps["set_gate_fn"](not restored)
            _set_phase(phase="failed", error="internal_error")


async def _run_one(run: dict) -> None:
    assert _deps is not None
    targets = _targets(run)
    if not targets:
        return
    cfg = _deps["config_fn"]()
    model = cfg.get("summary_model") or ""
    model_path = next((m["path"] for m in _deps["list_summary_models_fn"]() if m["name"] == model), None)
    if model_path is None:
        _set_phase(phase="failed", batch_id=run["batch_id"], error="summary_model_missing", done=0, total=len(targets))
        return
    _set_phase(phase="swapping_in", batch_id=run["batch_id"], model=model, done=0, total=len(targets), error=None)
    try:
        await asyncio.to_thread(_deps["swap_in_fn"], model_path)
    except Exception:
        logger.exception("Summary swap-in failed for batch %s", run["batch_id"])
        await _restore_asr()
        _set_phase(phase="failed", error="swap_in_failed")
        return
    _deps["set_gate_fn"](True)
    _set_phase(phase="summarizing")
    for item in targets:
        await _summarize_one(item, model)
        with _lock:
            _phase["done"] += 1
        _persist()
    _set_phase(phase="swapping_back")
    restored = await _restore_asr()
    _deps["set_gate_fn"](not restored)
    _set_phase(phase="finished" if restored else "failed",
               error=None if restored else "swap_back_failed")


async def _restore_asr() -> bool:
    assert _deps is not None
    try:
        await asyncio.to_thread(_deps["swap_out_fn"])
        return True
    except Exception:
        logger.exception("Swap back to ASR failed; transcription stays gated until boot retry")
        return False


async def _summarize_one(item: dict, model: str) -> None:
    assert _deps is not None
    hid = item["history_id"]
    record = _deps["read_history_fn"](hid)
    if record is None:
        _deps["set_item_summary_fn"](item["id"], "failed", "history_gone")
        return
    try:
        raw = await _deps["llm_call_fn"](record.get("text") or "", record.get("title") or "")
        parsed = parse_summary(raw)
    except ValueError as e:
        _deps["set_item_summary_fn"](item["id"], "failed", str(e) or "summary_parse")
        return
    except Exception:
        logger.exception("Summary call failed for history %s", hid)
        _deps["set_item_summary_fn"](item["id"], "failed", "summary_call")
        return
    summary = {**parsed, "model": model, "generated_at": _now_iso(), "state": "done"}
    try:
        _deps["save_summary_fn"](hid, summary)
    except Exception:
        logger.exception("Could not persist summary for history %s", hid)
        _deps["set_item_summary_fn"](item["id"], "failed", "summary_save")
        return
    _deps["set_item_summary_fn"](item["id"], "done", None)


# endpoints


@router.get("/api/workbench/summary-state")
async def summary_state():
    return state()


@router.get("/api/workbench/summary-models")
async def summary_models():
    assert _deps is not None
    # Paths stay internal (swap-in resolves them); the client only picks names.
    return {"models": [{"name": m["name"], "size": m["size"]} for m in _deps["list_summary_models_fn"]()]}


@router.post("/api/workbench/summarize")
async def summarize_batch(req: dict = Body(default={})):
    assert _deps is not None
    batch_id = str(req.get("batch_id") or "")
    if not batch_id:
        return JSONResponse({"error": "bad_request", "detail": "batch_id is required"}, status_code=422)
    ok, error = start_batch(batch_id)
    if not ok:
        status = 422 if error == "summary_model_unset" else 409
        return JSONResponse({"error": error}, status_code=status)
    return {"ok": True, "queued": True, "state": state()}


@router.post("/api/workbench/summarize-item")
async def summarize_item(req: dict = Body(default={})):
    assert _deps is not None
    hid = str(req.get("history_id") or "")
    if not hid:
        return JSONResponse({"error": "bad_request", "detail": "history_id is required"}, status_code=422)
    ok, error = retry_item(hid)
    if not ok:
        status = 404 if error == "not_found" else 422
        return JSONResponse({"error": error}, status_code=status)
    return {"ok": True, "queued": True, "state": state()}