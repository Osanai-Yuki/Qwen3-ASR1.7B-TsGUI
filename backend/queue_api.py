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
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .queue_store import QueueStore, SORT_KEYS

logger = logging.getLogger("asr")

router = APIRouter()

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
    }


# endpoints


@router.post("/api/queue/items")
async def queue_add(
    files: list[UploadFile] = File(...),
    align: bool = Form(False),
):
    """Enqueue a batch of uploads. All-or-nothing on validation errors."""
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
        )
        added.append(item)
    logger.info("Queue: enqueued %d item(s), align=%s", len(added), align)
    return {"items": added}


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
    store.remove(qid)
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
    if not store.staged_path(item).is_file():
        return JSONResponse(
            {"error": "gone", "detail": "staged file no longer exists; re-enqueue it"},
            status_code=410,
        )
    store.set_status(qid, "queued")
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


# worker


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

    try:
        staged = store.staged_path(item)
    except ValueError:
        store.set_status(item["id"], "error", error="invalid_staged_name")
        return True
    if not staged.is_file():
        store.set_status(item["id"], "error", error="staged_file_missing")
        return True

    # Mark running before the call so GET /api/queue exposes active_id;
    # a lost race against a manual upload is undone right below (409).
    store.set_status(item["id"], "running")
    # Tag the job about to be claimed so only a delete on this item can cancel
    # it. Cleared in finally: a stale tag would let an old queue item cancel a
    # later manual transcription.
    _deps["set_job_owner"](item["id"])
    try:
        with staged.open("rb") as fh:
            upload = UploadFile(
                file=fh,
                filename=item["filename"],
                size=staged.stat().st_size,
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
        if resp.get("cancelled"):
            # Partial transcript already saved to history by the endpoint.
            store.set_status(item["id"], "cancelled",
                             history_id=resp.get("history_id"))
            store.unlink_staged(item)
            return True
        store.set_status(item["id"], "done", history_id=resp.get("history_id"))
        store.unlink_staged(item)
        logger.info("Queue item %s done (history=%s)",
                    item["id"], resp.get("history_id"))
        return True
    store.set_status(item["id"], "error", error="unexpected_response")
    return True


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
