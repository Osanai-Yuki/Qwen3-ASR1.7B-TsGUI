"""Batch transcription queue: API endpoints + serial worker.

Deliberately does NOT import backend.main (which mounts this router) — all
main.py collaborators arrive through :func:`init` dependency injection, so
there is no circular import and tests can swap any piece.

The worker never re-implements the transcription pipeline: it awaits the
existing ``/api/transcribe`` endpoint function with a synthetic UploadFile
built from the staged upload, inheriting its three-tier chunk recovery,
cooperative cancel, video extraction, alignment and history persistence.
Mutual exclusion with manual jobs comes for free from ``_claim_job`` inside
that endpoint: a 409 means a manual job holds the slot, so the item is
released back to ``queued`` and the worker backs off.
"""
import asyncio
import base64
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from .queue_store import QueueStore, SORT_KEYS

logger = logging.getLogger("asr")

router = APIRouter()

# Same 12-hex shape history.py enforces.  A save that returns anything else did
# not produce a record, and must not be reported as durable.
_VALID_ID = re.compile(r"[0-9a-f]{12}")

# Hard ceilings for the staging area. Enqueue requests beyond these are
# rejected outright — queue items must never be silently evicted.
QUEUE_MAX_BYTES = int(os.environ.get("ASR_QUEUE_MAX_BYTES", str(16 * 1024 ** 3)))
QUEUE_MAX_ENTRIES = int(os.environ.get("ASR_QUEUE_MAX_ENTRIES", "500"))

# Populated by init(); None until main.py wires us up.
store: QueueStore | None = None
_deps: dict[str, Any] | None = None


def init(
    *,
    data_dir: Path,
    transcribe_fn: Callable[..., Awaitable[Any]],
    stream_upload_to: Callable[..., Awaitable[int]],
    upload_too_large: type[Exception],
    safe_filename: Callable[[str | None], str],
    safe_upload_suffix: Callable[[str | None], str],
    allowed_suffixes: set[str],
    max_upload_bytes: int,
    request_cancel: Callable[..., bool],
    set_job_owner: Callable[[str | None], None],
    is_ready: Callable[[], bool],
    reserve_version_fn: Callable[[str], int] | None = None,
    save_result_fn: Callable[[dict], dict] | None = None,
    find_history_by_attempt_fn: Callable[[str], str | None] | None = None,
    workbench: Any = None,
    auto_export_fn: Callable[..., dict] | None = None,
    read_history_fn: Callable[[str], dict | None] | None = None,
) -> None:
    """Wire the queue to main.py's transcription machinery (DI, no import)."""
    global store, _deps
    store = QueueStore(Path(data_dir) / "queue")
    _deps = {
        "transcribe_fn": transcribe_fn,
        "stream_upload_to": stream_upload_to,
        "upload_too_large": upload_too_large,
        "safe_filename": safe_filename,
        "safe_upload_suffix": safe_upload_suffix,
        "allowed_suffixes": allowed_suffixes,
        "max_upload_bytes": max_upload_bytes,
        "request_cancel": request_cancel,
        "set_job_owner": set_job_owner,
        "is_ready": is_ready,
        # Optional collaborators.  ``reserve_version_fn`` is the history
        # store's version counter; ``save_result_fn`` re-commits a pending
        # payload.  Both default to "unavailable" so the module stays usable
        # (and testable) without them.
        "reserve_version_fn": reserve_version_fn,
        "save_result_fn": save_result_fn,
        "find_history_by_attempt_fn": find_history_by_attempt_fn,
        "workbench": workbench,
        "auto_export_fn": auto_export_fn,
        "read_history_fn": read_history_fn,
    }


# Recoverable save-failed payloads.  Rooted next to the queue so that tests
# which swap ``store`` for a tmp-rooted one also get an isolated pending store.
_pending_cache: dict[str, Any] = {}


def pending() -> Any:
    assert store is not None
    root = str(store.root.parent / "pending_results")
    cached = _pending_cache.get(root)
    if cached is None:
        from .pending_results import PendingResultStore
        cached = PendingResultStore(store.root.parent / "pending_results")
        _pending_cache[root] = cached
    return cached


# Session-owned copy of every produced-but-unsaved payload.  This is the
# authoritative "readable right now" source: it must exist even when the
# recovery directory cannot be created or the queue file cannot be written.
# Disk/queue persistence is best effort on top of it.
_session_results: dict[tuple[str, str], dict] = {}
_session_lock = threading.Lock()


def _read_pending(qid: str, attempt_id: str | None) -> dict | None:
    """Read a pending payload without assuming the disk store is available."""
    if not attempt_id:
        return None
    try:
        payload = pending().get(qid, attempt_id)
        if payload is not None:
            return payload
    except Exception:
        logger.warning("Pending store unavailable while reading %s; using session copy", qid)
    with _session_lock:
        payload = _session_results.get((qid, attempt_id))
        return dict(payload) if payload is not None else None


def _drop_pending(qid: str, attempt_id: str | None) -> None:
    if attempt_id:
        with _session_lock:
            _session_results.pop((qid, attempt_id), None)
    try:
        if attempt_id and pending().get(qid, attempt_id) is not None:
            pending().remove(qid, attempt_id)
    except Exception:
        logger.warning("Could not remove pending result for %s", qid)


def reconcile_attempts() -> int:
    """Repair attempt ownership after a restart.

    Three cases matter:
    * history committed but the queue update was lost → re-associate;
    * a save died mid-flight (``persistence_state="saving"``) → release the
      stale claim so it can be retried;
    * queue capability flags claim a readable payload that no longer exists →
      clear them instead of offering an action that cannot work.
    """
    if store is None or _deps is None:
        return 0
    finder = _deps.get("find_history_by_attempt_fn")
    fixed = 0
    for item in store.snapshot()["items"]:
        attempt = item.get("attempt_id")
        if not attempt:
            continue
        if item.get("history_id"):
            if item.get("persistence_state") != "saved":
                store.set_persistence(
                    item["id"], state="saved",
                    history_id=item["history_id"], attempt_id=attempt,
                )
            continue
        hid = None
        if finder is not None:
            try:
                hid = finder(attempt)
            except Exception:
                logger.exception("Attempt reconciliation lookup failed for %s", attempt)
                continue
        if hid:
            # Recover the status the task had when the crash happened: a
            # cancelled run stays cancellable/re-processable even though its
            # partial text was committed.
            recovered_status = "cancelled" if item.get("status") == "cancelled" else "done"
            store.set_status(
                item["id"], recovered_status, history_id=hid, error=None,
                result={"integrity": item.get("integrity") or "complete"},
            )
            store.set_persistence(item["id"], state="saved", history_id=hid, attempt_id=attempt)
            # A cancelled item keeps its source so it can be re-processed from
            # the beginning; the result text being saved does not change that.
            if recovered_status != "cancelled":
                store.unlink_staged(item)
            fixed += 1
            logger.info("Re-associated attempt %s with history %s", attempt, hid)
            continue
        # Not committed anywhere: any persisted save claim is stale, and the
        # advertised capabilities must match the payload we can actually read.
        stale_claim = item.get("persistence_state") == "saving"
        if item.get("persistence_state") in ("saving", "failed") or item.get("temporary_result_available"):
            payload = _read_pending(item["id"], attempt)
            if payload is not None:
                store.set_persistence(
                    item["id"], state="failed", attempt_id=attempt,
                    temporary=True, can_retry_save=True,
                    save_error="history_save_failed", integrity=item.get("integrity"),
                )
            else:
                store.set_persistence(
                    item["id"], state="unavailable", attempt_id=attempt,
                    temporary=False, can_retry_save=False, save_error=None,
                )
            if stale_claim:
                fixed += 1
                logger.info("Released stale save claim for %s", item["id"])
    return fixed


# endpoints


@router.post("/api/queue/items")
async def queue_add(
    files: list[UploadFile] = File(...),
    align: bool = Form(False),
    batch_id: str = Form(""),
    batch_name: str = Form(""),
    source_paths: str = Form("[]"),
    client_import_ids: str = Form(""),
):
    """Enqueue a batch of uploads. Preflight validation is all-or-nothing;
    streaming size-limit errors return the successfully accepted prefix.

    ``client_import_ids`` is an optional JSON array of client-minted ids, one
    per file, that the caller uses to match receipts back to its own list
    entries.  It is echoed back per item so a partial accept can be reconciled
    exactly — never by guessing from a filename or size (two files with the
    same name and length are routinely different content).
    """
    assert store is not None and _deps is not None
    # Whitelist check up front: in a batch context a bad extension is a user
    # mistake worth naming explicitly, not silently degrading to .bin.
    rejected = [
        _deps["safe_filename"](f.filename)
        for f in files
        if Path(f.filename or "").suffix.lower() not in _deps["allowed_suffixes"]
    ]
    if rejected:
        return JSONResponse(
            {"error": "unsupported_type",
             "detail": "unsupported file type: " + ", ".join(rejected[:5])},
            status_code=422,
        )
    if store.pending_count() + len(files) > QUEUE_MAX_ENTRIES:
        return JSONResponse(
            {"error": "queue_full",
             "detail": f"queue entry limit is {QUEUE_MAX_ENTRIES}"},
            status_code=422,
        )

    try:
        paths = json.loads(source_paths or "[]")
    except (TypeError, ValueError):
        paths = []
    if not isinstance(paths, list) or len(paths) != len(files) or not all(isinstance(p, str) for p in paths):
        paths = [f.filename or "unknown" for f in files]
    # Optional per-file client identity.  Malformed input is a client bug worth
    # naming, not something to silently fall back from — a wrong-length array
    # would otherwise let the caller mis-match receipts.
    import_ids: list[str] = []
    if client_import_ids.strip():
        try:
            parsed_ids = json.loads(client_import_ids)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "bad_request", "detail": "client_import_ids must be a JSON array"},
                status_code=422,
            )
        if not isinstance(parsed_ids, list) or not all(isinstance(i, str) for i in parsed_ids):
            return JSONResponse(
                {"error": "bad_request", "detail": "client_import_ids must be a string array"},
                status_code=422,
            )
        if len(parsed_ids) != len(files):
            return JSONResponse(
                {"error": "bad_request",
                 "detail": "client_import_ids length must match the number of files"},
                status_code=422,
            )
        if len(set(parsed_ids)) != len(parsed_ids):
            return JSONResponse(
                {"error": "bad_request", "detail": "client_import_ids must be unique within the request"},
                status_code=422,
            )
        import_ids = [i[:64] for i in parsed_ids]
    batch_id = batch_id.strip() or store.new_id()
    if not re.fullmatch(r"[0-9a-f]{12}", batch_id):
        return JSONResponse(
            {"error": "bad_request", "detail": "batch_id must be a 12-character hex id"},
            status_code=422,
        )
    batch_name = " ".join(batch_name.split())[:120] or "未命名批次"

    def safe_source_path(value: str, fallback: str) -> str:
        parts = [part for part in value.replace("\\", "/").split("/") if part not in ("", ".", "..")]
        clean = "/".join(parts).strip()[:512]
        return clean or fallback

    added: list[dict] = []
    for f in files:
        qid = store.new_id()
        staged_name = f"{qid}{_deps['safe_upload_suffix'](f.filename)}"
        staged = store.uploads_dir / staged_name
        try:
            size = await _deps["stream_upload_to"](
                f, staged, _deps["max_upload_bytes"],
            )
        except _deps["upload_too_large"]:
            staged.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "payload_too_large",
                 "detail": f"{_deps['safe_filename'](f.filename)} exceeds "
                           f"{_deps['max_upload_bytes']} bytes",
                 "items": added},
                status_code=413,
            )
        if store.staged_total_bytes() > QUEUE_MAX_BYTES:
            staged.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "queue_full",
                 "detail": f"staging area limit is {QUEUE_MAX_BYTES} bytes",
                 "items": added},
                status_code=413,
            )
        item = store.add(
            qid=qid,
            filename=_deps["safe_filename"](f.filename),
            staged_name=staged_name,
            size=size,
            align=align,
            batch_id=batch_id,
            batch_name=batch_name,
            source_path=safe_source_path(paths[len(added)], _deps["safe_filename"](f.filename)),
            client_import_id=(import_ids[len(added)] if len(import_ids) == len(files) else None),
        )
        added.append(item)
    logger.info("Queue: enqueued %d item(s), align=%s", len(added), align)
    return {"items": added}


@router.post("/api/queue/reference")
async def queue_add_reference(req: dict = Body(default={})):
    """Enqueue files in place from an approved workbench root (no staging copy).

    Every relative path is containment-validated against the root at enqueue
    time AND again at execution time, so a root removed or a file moved between
    the two can never read outside the approved tree.
    """
    assert store is not None and _deps is not None
    workbench = _deps.get("workbench")
    if workbench is None:
        return JSONResponse({"error": "unavailable", "detail": "workbench is not configured"}, status_code=503)
    root_id = str(req.get("root_id") or "")
    root = workbench.get_root(root_id)
    if root is None:
        return JSONResponse({"error": "unknown_root"}, status_code=404)
    rel_paths = req.get("rel_paths")
    if not isinstance(rel_paths, list) or not rel_paths or not all(isinstance(p, str) for p in rel_paths):
        return JSONResponse({"error": "bad_request", "detail": "rel_paths must be a non-empty string array"}, status_code=422)
    if store.pending_count() + len(rel_paths) > QUEUE_MAX_ENTRIES:
        return JSONResponse({"error": "queue_full", "detail": f"queue entry limit is {QUEUE_MAX_ENTRIES}"}, status_code=422)
    resolved: list[tuple[str, Path]] = []
    for rel in rel_paths:
        try:
            target = workbench.resolve_source(root_id, rel)
        except ValueError:
            return JSONResponse({"error": "invalid_source_path", "detail": f"path escapes the approved root: {rel[:120]}"}, status_code=422)
        if target.suffix.lower() not in _deps["allowed_suffixes"]:
            return JSONResponse({"error": "unsupported_type", "detail": f"unsupported file type: {target.name}"}, status_code=422)
        if not target.is_file():
            return JSONResponse({"error": "source_gone", "detail": f"no such file: {rel[:120]}"}, status_code=410)
        size = target.stat().st_size
        if size > _deps["max_upload_bytes"]:
            return JSONResponse({"error": "payload_too_large", "detail": f"{target.name} exceeds {_deps['max_upload_bytes']} bytes"}, status_code=413)
        resolved.append((rel, target))
    batch_id = store.new_id()
    batch_name = " ".join(str(req.get("batch_name") or "").split())[:120] or root["label"]
    align = bool(req.get("align", False))
    added: list[dict] = []
    for rel, target in resolved:
        qid = store.new_id()
        added.append(store.add(
            qid=qid,
            filename=_deps["safe_filename"](target.name),
            staged_name=None,
            size=target.stat().st_size,
            align=align,
            batch_id=batch_id,
            batch_name=batch_name,
            source_path=rel,
            source_mode="reference",
            root_id=root_id,
        ))
    logger.info("Queue: enqueued %d reference item(s) from root %s", len(added), root_id)
    return {"items": added, "batch_id": batch_id}


@router.get("/api/queue")
async def queue_state():
    assert store is not None
    return store.snapshot()


@router.post("/api/queue/reorder")
async def queue_reorder(req: dict = Body(default={})):
    assert store is not None
    ids = req.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return JSONResponse(
            {"error": "bad_request", "detail": "ids must be a string array"},
            status_code=422,
        )
    if not store.reorder(ids):
        return JSONResponse(
            {"error": "bad_request",
             "detail": "ids must be a permutation of the queued items"},
            status_code=409,
        )
    return {"ok": True}


@router.post("/api/queue/sort")
async def queue_sort(req: dict = Body(default={})):
    assert store is not None
    key = str(req.get("key", ""))
    order = str(req.get("order", "asc")).lower()
    if key not in SORT_KEYS or order not in ("asc", "desc"):
        return JSONResponse(
            {"error": "bad_request",
             "detail": f"key must be one of {sorted(SORT_KEYS)}"},
            status_code=422,
        )
    store.sort_by(key, desc=(order == "desc"))
    return {"ok": True}


@router.delete("/api/queue/items/{qid}")
async def queue_delete(qid: str):
    assert store is not None and _deps is not None
    item = store.get(qid)
    if item is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if item["status"] == "running":
        # Skip-current: cooperative cancel; the worker marks it cancelled
        # once /api/transcribe winds down at the next chunk boundary. The item
        # id scopes the request, so this cannot cancel a manual transcription
        # that grabbed the single slot while this item was still flagged running.
        _deps["request_cancel"](item["id"])
        return {"ok": True, "skipping": True}
    if item.get("persistence_state") == "saving":
        # Deleting the row (and its payload/source) while a save owns the
        # attempt would let the late save resurrect a half-removed task.
        return JSONResponse(
            {"error": "conflict",
             "detail": "a save for this attempt is in progress; delete after it finishes"},
            status_code=409,
        )
    # Drop the recoverable payload with the item: once the queue row is gone
    # nothing can reach it, so it would leak in memory and on disk and be
    # restored as an orphan after a restart.
    attempt_id = item.get("attempt_id")
    if store.remove(qid) is None:
        return JSONResponse({"error": "conflict"}, status_code=409)
    _drop_pending(qid, attempt_id)
    return {"ok": True, "skipping": False}


@router.post("/api/queue/items/{qid}/retry")
async def queue_retry(qid: str):
    assert store is not None
    item = store.get(qid)
    if item is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if item["status"] not in ("error", "cancelled"):
        return JSONResponse(
            {"error": "bad_request", "detail": "only failed/cancelled items can be retried"},
            status_code=409,
        )
    if item.get("source_mode") == "reference":
        workbench = _deps.get("workbench")
        source = None
        if workbench is not None:
            try:
                source = workbench.resolve_source(item.get("root_id") or "", item.get("source_path") or "")
            except ValueError:
                source = None
        if source is None or not source.is_file():
            return JSONResponse(
                {"error": "source_gone", "detail": "source file no longer exists under the approved root"},
                status_code=410,
            )
    elif not store.staged_path(item).is_file():
        return JSONResponse(
            {"error": "gone", "detail": "staged file no longer exists; re-enqueue it"},
            status_code=410,
        )
    outcome = store.requeue_for_reprocess(qid)
    if outcome == "saving":
        return JSONResponse(
            {"error": "conflict",
             "detail": "a save for this attempt is in progress; retry after it finishes"},
            status_code=409,
        )
    if outcome != "queued":
        return JSONResponse(
            {"error": "conflict",
             "detail": "the task changed while retrying; refresh before trying again"},
            status_code=409,
        )
    return {"ok": True}


@router.post("/api/queue/pause")
async def queue_pause():
    assert store is not None
    store.set_paused(True)
    return {"ok": True, "paused": True}


@router.post("/api/queue/resume")
async def queue_resume():
    assert store is not None
    store.set_paused(False)
    return {"ok": True, "paused": False}


@router.delete("/api/queue/finished")
async def queue_clear_finished():
    assert store is not None
    return {"cleared": store.clear_finished()}


@router.get("/api/queue/items/{qid}/result")
async def queue_item_result(qid: str):
    """Read the result of one queue item without polling the whole queue.

    Returns the durable record location when the result was saved, otherwise
    the recoverable temporary payload (save failed).  The reader uses this to
    resolve a ``ResultRef`` instead of inventing a fake history id.
    """
    assert store is not None
    item = store.get(qid)
    if item is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if item.get("history_id"):
        return {"kind": "saved", "history_id": item["history_id"], "item": item}
    attempt_id = item.get("attempt_id")
    payload = _read_pending(qid, attempt_id) if attempt_id else None
    if payload is None:
        return JSONResponse(
            {"error": "unavailable",
             "detail": "no readable result for this attempt; the temporary text was not retained"},
            status_code=409,
        )
    return {
        "kind": "temporary",
        "item": item,
        "result": {
            "text": payload.get("text", ""),
            "segments": payload.get("segments", []),
            "stats": payload.get("stats") or {},
            "timing": payload.get("timing"),
            "integrity": payload.get("integrity", "complete"),
            "result_version": payload.get("result_version"),
            "filename": payload.get("filename") or item.get("filename"),
            "title": payload.get("title") or item.get("title"),
            "source_path": payload.get("source_path") or item.get("source_path"),
            "batch_id": payload.get("batch_id") or item.get("batch_id"),
            "batch_name": payload.get("batch_name") or item.get("batch_name"),
            "persistence_state": "failed",
            "persisted": bool(payload.get("persisted")),
        },
    }


@router.post("/api/queue/items/{qid}/save")
async def queue_item_save(qid: str, req: dict = Body(default={})):
    """Re-commit an already-produced result. Never re-runs ASR.

    The request must name the attempt it is saving; a mismatched or stale
    attempt is rejected so a late save from an older run cannot overwrite the
    task's current result.  Repeating the call returns the same history id.
    """
    assert store is not None and _deps is not None
    item = store.get(qid)
    if item is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if item.get("history_id"):
        # Idempotent: already committed (e.g. the success response was lost).
        return {"ok": True, "history_id": item["history_id"], "already_saved": True,
                "persistence_state": "saved"}
    attempt_id = req.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return JSONResponse(
            {"error": "bad_request", "detail": "attempt_id is required"}, status_code=422,
        )
    if attempt_id != item.get("attempt_id"):
        return JSONResponse(
            {"error": "conflict",
             "detail": "attempt_id does not match this task's current attempt; "
                       "the temporary result belongs to a different run"},
            status_code=409,
        )
    payload = _read_pending(qid, attempt_id)
    if payload is None:
        return JSONResponse(
            {"error": "unavailable",
             "detail": "temporary result is no longer readable; re-process the original file"},
            status_code=410,
        )
    # Claim before doing any work: two overlapping saves must not both commit.
    claim = store.try_claim_save(qid, attempt_id)
    if claim == "conflict":
        current = store.get(qid) or {}
        if current.get("history_id"):
            return {"ok": True, "history_id": current["history_id"], "already_saved": True,
                    "persistence_state": "saved"}
        return JSONResponse(
            {"error": "conflict",
             "detail": "a save for this attempt is already in progress; retry once it finishes"},
            status_code=409,
        )
    if claim == "absent":
        return JSONResponse({"error": "not_found"}, status_code=404)
    save_fn = _deps.get("save_result_fn")
    if save_fn is None:
        store.release_save_claim(qid, failed=True, attempt_id=attempt_id)
        return JSONResponse(
            {"error": "unavailable", "detail": "history persistence is not configured"},
            status_code=503,
        )
    try:
        record = await asyncio.to_thread(save_fn, payload)
    except Exception as e:
        # Keep the temporary payload and release the claim so the user can retry:
        # a failed retry must not destroy the only readable copy, nor wedge the
        # item in "saving" forever.
        store.release_save_claim(qid, failed=True, attempt_id=attempt_id)
        logger.warning("Save retry failed for %s: %s", qid, e)
        return JSONResponse(
            {"error": "save_failed", "detail": "history write failed again; the temporary result is kept"},
            status_code=500,
        )
    history_id = record.get("id")
    # A save that did not produce a real record must never be reported as
    # durable: claiming persistence with no history id is exactly the
    # "temporary result presented as saved" failure this endpoint exists to
    # prevent.  Keep the payload and let the user retry.
    if not isinstance(history_id, str) or not _VALID_ID.fullmatch(history_id):
        store.release_save_claim(qid, failed=True, attempt_id=attempt_id)
        logger.warning("Save retry for %s returned no usable history id; keeping the payload", qid)
        return JSONResponse(
            {"error": "save_failed",
             "detail": "history write did not produce a record; the temporary result is kept"},
            status_code=500,
        )
    # Restore the status the task would have had if the first save had worked.
    intended = "cancelled" if item.get("status") == "cancelled" else "done"
    committed = store.commit_save(
        qid, attempt_id, history_id,
        intended_status=intended,
        result={
            "integrity": payload.get("integrity", "complete"),
            "missing_ranges": (payload.get("stats") or {}).get("chunk_failure_details", []),
            "alignment_fallback": bool((payload.get("stats") or {}).get("aligner_used") is False),
        },
    )
    if not committed:
        # The save ran while the task changed owner (re-process/delete).  The
        # record exists, but attaching it now would overwrite newer ownership.
        logger.warning(
            "Save for %s finished after the attempt changed; record %s left unattached",
            qid, history_id,
        )
        return JSONResponse(
            {"error": "conflict",
             "detail": "the task changed while saving; refresh and check its current state"},
            status_code=409,
        )
    _drop_pending(qid, attempt_id)
    # Text persistence and source cleanup are separate decisions: a cancelled
    # task keeps its original file so it can still be re-processed from scratch.
    if intended != "cancelled":
        store.unlink_staged(item)
    _run_auto_export(item, payload, history_id)
    logger.info("Queue item %s re-saved as history %s", qid, history_id)
    return {"ok": True, "history_id": history_id, "already_saved": False,
            "persistence_state": "saved"}


@router.post("/api/queue/items/{qid}/export")
async def queue_item_export(qid: str):
    """Re-export a durable result on demand (auto-export failed or was off)."""
    assert store is not None and _deps is not None
    item = store.get(qid)
    if item is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    history_id = item.get("history_id")
    if not history_id:
        return JSONResponse(
            {"error": "no_durable_result", "detail": "save the result before exporting it"},
            status_code=409,
        )
    read_fn = _deps.get("read_history_fn")
    if read_fn is None:
        return JSONResponse({"error": "unavailable", "detail": "history reading is not configured"}, status_code=503)
    record = await asyncio.to_thread(read_fn, history_id)
    if record is None:
        return JSONResponse({"error": "history_gone", "detail": "the history record no longer exists"}, status_code=410)
    outcome = _run_auto_export(item, record, history_id, force=True)
    if outcome.get("error") == "export_root_unset":
        return JSONResponse({"error": "export_root_unset", "detail": "set an export folder in workbench settings first"}, status_code=422)
    if outcome.get("state") == "failed":
        return JSONResponse({"error": outcome.get("error") or "export_failed"}, status_code=500)
    return {"ok": True, **outcome}


# worker


def _run_auto_export(item: dict, payload: dict, history_id: str | None, *, force: bool = False) -> dict:
    """Export a durable result and record the outcome on the queue item.

    Never raises: an export problem must not undo a committed transcription.
    """
    fn = _deps.get("auto_export_fn") if _deps else None
    if fn is None:
        return {"state": "skipped", "paths": [], "error": None}
    try:
        outcome = fn(item, payload, history_id, force=force)
    except Exception:
        logger.exception("Auto-export hook failed for %s", item.get("id"))
        outcome = {"state": "failed", "paths": [], "error": "export_failed"}
    assert store is not None
    store.set_export(item["id"], outcome.get("state", "failed"),
                     paths=outcome.get("paths"), error=outcome.get("error"))
    return outcome


async def process_next_once() -> bool:
    """Consume the head queued item. Returns True when an item reached a
    terminal state (caller may immediately look for the next one); False
    when there is nothing to do or the job slot is busy (caller backs off).

    Kept as a standalone single-step coroutine so tests can drive the queue
    deterministically without the background loop.
    """
    assert store is not None and _deps is not None
    item = store.next_queued()
    if item is None:
        return False

    if item.get("source_mode") == "reference":
        workbench = _deps.get("workbench")
        if workbench is None:
            store.set_status(item["id"], "error", error="workbench_unavailable")
            return True
        try:
            media = workbench.resolve_source(item.get("root_id") or "", item.get("source_path") or "")
        except ValueError as e:
            code = "root_unavailable" if str(e) == "unknown_root" else "invalid_source_path"
            store.set_status(item["id"], "error", error=code)
            return True
        if not media.is_file():
            store.set_status(item["id"], "error", error="source_gone")
            return True
    else:
        try:
            media = store.staged_path(item)
        except ValueError:
            store.set_status(item["id"], "error", error="invalid_staged_name")
            return True
        if not media.is_file():
            store.set_status(item["id"], "error", error="staged_file_missing")
            return True

    # Mark running before the call so GET /api/queue exposes active_id;
    # a lost race against a manual upload is undone right below (409).
    store.set_status(item["id"], "running")
    # Mint the attempt identity and reserve the result version *before* the
    # expensive recognition.  Reserving first is what lets a failed save be
    # retried on the same attempt/version instead of consuming a new number,
    # and what stops an older failed attempt from claiming a newer slot when
    # it is finally saved.
    attempt = store.begin_attempt(item["id"]) or {}
    attempt_id = attempt.get("attempt_id")
    result_version = None
    reserve = _deps.get("reserve_version_fn")
    if reserve is not None and item.get("id"):
        try:
            result_version = int(reserve(item["id"]))
        except Exception:
            # No reliable version reservation means no safe place to attach the
            # result: a failed attempt could later claim a newer slot.  Stop
            # before the expensive recognition and pause new starts until the
            # user resolves the storage problem.
            logger.exception("Version reservation failed for %s", item["id"])
            store.set_status(item["id"], "error", error="version_reservation_failed")
            store.set_persistence(item["id"], state="unavailable", attempt_id=attempt_id)
            store.set_paused(True)
            return True
        store.set_attempt_version(item["id"], result_version)
    # Tag the job about to be claimed so only a delete on this item can cancel
    # it. Cleared in finally: a stale tag would let an old queue item cancel a
    # later manual transcription.
    _deps["set_job_owner"](item["id"])
    try:
        with media.open("rb") as fh:
            metadata = json.dumps({
                "batch_id": item.get("batch_id"),
                "batch_name": item.get("batch_name"),
                "task_id": item.get("id"),
                "source_path": item.get("source_path"),
                "title": item.get("title"),
                "alignment_requested": bool(item.get("align")),
                "attempt_id": attempt_id,
                "result_version": result_version,
            }, ensure_ascii=False).encode("utf-8")
            encoded_metadata = base64.urlsafe_b64encode(metadata).decode("ascii")
            upload = UploadFile(
                file=fh,
                filename=item["filename"],
                size=media.stat().st_size,
                headers=Headers({"x-asr-task-meta": encoded_metadata}),
            )
            resp = await _deps["transcribe_fn"](file=upload, align=bool(item["align"]))
    except Exception:
        logger.exception("Queue item %s failed unexpectedly", item["id"])
        store.set_status(item["id"], "error", error="internal_error")
        return True
    finally:
        _deps["set_job_owner"](None)

    # Normalize the endpoint's dual-shaped result (JSONResponse | dict).
    if isinstance(resp, JSONResponse):
        if resp.status_code == 409:
            # Manual job holds the single slot — release and back off.
            store.set_status(item["id"], "queued")
            return False
        store.set_status(item["id"], "error", error=f"http_{resp.status_code}")
        return True
    if isinstance(resp, dict):
        if resp.get("error"):
            # Persist only the sanitized error code, never the detail text.
            store.set_status(item["id"], "error", error=str(resp["error"]))
            return True
        stats = resp.get("stats") or {}
        text = resp.get("text") or ""
        segments = resp.get("segments") or []
        has_payload = bool(text or segments)
        if resp.get("cancelled"):
            # Keep the staged source so the partial task can be retried.
            # Retain the text *before* any queue write: a queue write failure
            # must not skip the only readable copy.
            retention = {"readable": False, "persisted": False, "over_budget": False}
            if has_payload and not resp.get("history_id"):
                retention = _retain_pending(
                    item, resp, attempt_id=attempt_id,
                    result_version=result_version, integrity="partial",
                )
                store.set_persistence(
                    item["id"], state="failed" if retention["readable"] else "unavailable",
                    attempt_id=attempt_id,
                    temporary=retention["readable"], can_retry_save=retention["readable"],
                    save_error="history_save_failed" if retention["readable"] else None,
                    integrity="partial",
                )
                if retention["over_budget"]:
                    store.set_paused(True)
                    logger.warning(
                        "Pending result budget exhausted; queue paused after %s",
                        item["id"],
                    )
            elif resp.get("history_id"):
                store.set_persistence(item["id"], state="saved",
                                      history_id=resp["history_id"], attempt_id=attempt_id)
                _run_auto_export(item, resp, resp["history_id"])
            store.set_status(item["id"], "cancelled",
                             history_id=resp.get("history_id"),
                             clear_history=not resp.get("history_id"),
                             result={
                                 "integrity": "partial",
                                 "missing_ranges": stats.get("chunk_failure_details", []),
                                 "alignment_fallback": bool(item.get("align")),
                             })
            return True
        if resp.get("history_id"):
            store.set_status(
                item["id"], "done", history_id=resp["history_id"], error=None,
                result={
                    "integrity": "partial" if stats.get("chunk_failures") else "complete",
                    "missing_ranges": stats.get("chunk_failure_details", []),
                    "alignment_fallback": bool(item.get("align") and not stats.get("aligner_used")),
                },
            )
            store.set_persistence(item["id"], state="saved",
                                  history_id=resp["history_id"], attempt_id=attempt_id)
            store.unlink_staged(item)
            _run_auto_export(item, resp, resp["history_id"])
            logger.info("Queue item %s done (history=%s)", item["id"], resp.get("history_id"))
            return True
        if has_payload:
            # The recognition produced text but the durable write failed.  Keep
            # both the staged source and a readable payload so the user can
            # read the text now and retry the save later — the exact opposite of
            # the old behaviour, which deleted the only copy.
            integrity = "partial" if stats.get("chunk_failures") else "complete"
            retention = _retain_pending(item, resp, attempt_id=attempt_id,
                                        result_version=result_version, integrity=integrity)
            # status=error (not done): the transcription finished but the task
            # did not reach a durable state, and only error/cancelled items can
            # be re-processed from the retained source.  Using "done" here made
            # the kept source unreachable dead weight.
            store.set_persistence(
                item["id"], state="failed" if retention["readable"] else "unavailable",
                attempt_id=attempt_id,
                temporary=retention["readable"], can_retry_save=retention["readable"],
                save_error="history_save_failed" if retention["readable"] else None,
                integrity=integrity,
            )
            if retention["over_budget"]:
                store.set_paused(True)
                logger.warning(
                    "Pending result budget exhausted; queue paused after %s",
                    item["id"],
                )
            store.set_status(
                item["id"], "error", history_id=None, clear_history=True,
                error="history_save_failed",
                result={
                    "integrity": integrity,
                    "missing_ranges": stats.get("chunk_failure_details", []),
                    "alignment_fallback": bool(item.get("align") and not stats.get("aligner_used")),
                },
            )
            logger.warning(
                "Queue item %s: history save failed; temporary result retained",
                item["id"],
            )
            return True
        store.set_status(item["id"], "error", error="empty_result")
        return True
    store.set_status(item["id"], "error", error="unexpected_response")
    return True


def _retain_pending(
    item: dict,
    resp: dict,
    *,
    attempt_id: str | None,
    result_version: int | None,
    integrity: str,
) -> dict:
    """Keep a produced-but-unsaved payload readable for this session.

    Returns ``{"readable", "persisted", "over_budget"}``.  The session copy is
    created first and unconditionally; disk recovery is attempted afterwards,
    so an unavailable recovery directory can never make the text disappear.
    Callers must derive the advertised capability flags from ``readable``.
    """
    info = {"readable": False, "persisted": False, "over_budget": False}
    if not attempt_id:
        return info
    stats = resp.get("stats") or {}
    payload = {
        "task_id": item.get("id"),
        "attempt_id": attempt_id,
        "result_version": result_version,
        "title": item.get("title"),
        "filename": item.get("filename"),
        "source_path": item.get("source_path"),
        "batch_id": item.get("batch_id"),
        "batch_name": item.get("batch_name"),
        "integrity": integrity,
        "text": resp.get("text") or "",
        "segments": resp.get("segments") or [],
        "stats": stats,
        "timing": resp.get("timing"),
        "alignment_requested": resp.get("alignment_requested", False),
        "saved": False,
        "persisted": False,
    }
    info["readable"] = bool(payload["text"] or payload["segments"])
    if not info["readable"]:
        return info
    with _session_lock:
        _session_results[(item["id"], attempt_id)] = payload
    try:
        stored = pending().put(task_id=item["id"], attempt_id=attempt_id, payload=payload)
        info["persisted"] = bool(stored.get("persisted"))
        info["over_budget"] = bool(stored.get("over_budget"))
        payload["persisted"] = info["persisted"]
        if not info["persisted"]:
            logger.warning(
                "Pending result for %s is readable this session only "
                "(disk copy unavailable)", item["id"],
            )
    except Exception:
        # The session copy above is the last resort; bookkeeping must never
        # mask or discard the transcription result.
        logger.exception("Could not persist pending result for %s", item["id"])
    return info

async def worker_loop() -> None:
    """Long-lived serial consumer. The whole iteration is exception-guarded
    so the loop can never die silently and strand the queue."""
    logger.info("Queue worker started")
    while True:
        try:
            if (
                store is None
                or _deps is None
                or store.is_paused()
                or not _deps["is_ready"]()
            ):
                await asyncio.sleep(1.0)
                continue
            consumed = await process_next_once()
            if not consumed:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("Queue worker stopped")
            raise
        except Exception:
            logger.exception("Queue worker iteration failed")
            await asyncio.sleep(2.0)
