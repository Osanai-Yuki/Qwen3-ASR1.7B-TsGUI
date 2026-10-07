"""Persistent batch-transcription queue storage.

Single-file store at ``data/queue/queue.json``: the whole queue state
(``{"version", "paused", "items"}``) is rewritten atomically (temp file +
``os.replace``) on every mutation.  A single file keeps frequent reordering
trivial and gives a natural monotonic ``version`` counter the frontend uses
to skip no-op re-renders.

Staged uploads live next to it in ``data/queue/uploads/<id><suffix>`` and are
deleted once an item reaches ``done``/``cancelled`` (kept on ``error`` so the
item can be retried).  Follows the same hardening conventions as history.py:
module logger, 12-hex ids, containment-validated paths, corrupted state files
are logged and rebuilt instead of crashing the app.
"""
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("asr")

# Same id shape as history.py: first 12 hex chars of a UUID4. Rejecting any
# other shape at the storage layer defeats path traversal through staged
# file names regardless of how the id reaches us.
_VALID_ID = re.compile(r"[0-9a-f]{12}")

# Ceiling for the state file itself — a corrupted/oversized queue.json must
# not exhaust memory while parsing.
MAX_QUEUE_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB

STATUSES = {"queued", "running", "done", "error", "cancelled"}
TERMINAL_STATUSES = {"done", "error", "cancelled"}
SORT_KEYS = {"name", "size", "added_at"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class QueueStore:
    """Thread-safe persistent queue. All public methods take the lock."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads_dir = self.root / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.root / "queue.json"
        self._lock = threading.Lock()
        self._state: dict = {"version": 0, "paused": False, "items": []}
        self._load()

    # persistence

    def _load(self) -> None:
        """Load state from disk; corruption is logged, never fatal.

        Crash recovery: items left in ``running`` by a previous process are
        reset to ``queued`` so they re-run (the transcription never reached
        history, so nothing is duplicated). Orphan staged files (no matching
        item) are removed.
        """
        if self._file.exists():
            try:
                if self._file.stat().st_size > MAX_QUEUE_FILE_BYTES:
                    raise ValueError("queue.json exceeds size ceiling")
                data = json.loads(self._file.read_text(encoding="utf-8"))
                items = [
                    it for it in data.get("items", [])
                    if isinstance(it, dict)
                    and isinstance(it.get("id"), str)
                    and _VALID_ID.fullmatch(it["id"])
                    and it.get("status") in STATUSES
                ]
                self._state = {
                    "version": int(data.get("version", 0)),
                    "paused": bool(data.get("paused", False)),
                    "items": items,
                }
            except Exception as e:
                logger.warning("Queue state unreadable (%s); starting empty", e)
                self._state = {"version": 0, "paused": False, "items": []}

        recovered = 0
        for it in self._state["items"]:
            if it.get("status") == "running":
                it["status"] = "queued"
                it["error"] = None
                recovered += 1
        if recovered:
            logger.info("Queue recovery: reset %d running item(s) to queued", recovered)

        # Remove staged files that no longer belong to any item.
        known = {it.get("staged_name") for it in self._state["items"]}
        for p in self.uploads_dir.iterdir():
            if p.is_file() and p.name not in known:
                try:
                    p.unlink()
                except OSError:
                    pass

        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """Atomic write: temp file in the same dir + os.replace."""
        self._state["version"] = int(self._state.get("version", 0)) + 1
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self._file)

    # helpers

    def _find_locked(self, qid: str) -> dict | None:
        if not isinstance(qid, str) or not _VALID_ID.fullmatch(qid):
            return None
        return next((it for it in self._state["items"] if it["id"] == qid), None)

    def staged_path(self, item: dict) -> Path:
        """Containment-validated absolute path for an item's staged upload."""
        name = item.get("staged_name") or ""
        path = (self.uploads_dir / Path(name).name).resolve()
        if path.parent != self.uploads_dir.resolve():
            raise ValueError("invalid staged name")
        return path

    def staged_total_bytes(self) -> int:
        total = 0
        for p in self.uploads_dir.iterdir():
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    def _unlink_staged(self, item: dict) -> None:
        try:
            self.staged_path(item).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def unlink_staged(self, item: dict) -> None:
        """Public best-effort removal of an item's staged upload."""
        self._unlink_staged(item)

    # public API

    def new_id(self) -> str:
        return _new_id()

    def is_paused(self) -> bool:
        with self._lock:
            return bool(self._state["paused"])

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._state["paused"] = bool(paused)
            self._save_locked()

    def snapshot(self) -> dict:
        """Deep-enough copy of the whole state for the API response."""
        with self._lock:
            active = next(
                (it["id"] for it in self._state["items"] if it["status"] == "running"),
                None,
            )
            return {
                "version": self._state["version"],
                "paused": self._state["paused"],
                "active_id": active,
                "items": [dict(it) for it in self._state["items"]],
            }

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for it in self._state["items"]
                if it["status"] not in TERMINAL_STATUSES
            )

    def get(self, qid: str) -> dict | None:
        with self._lock:
            it = self._find_locked(qid)
            return dict(it) if it else None

    def add(self, *, qid: str, filename: str, staged_name: str, size: int,
            align: bool) -> dict:
        item = {
            "id": qid,
            "filename": filename or "unknown",
            "staged_name": staged_name,
            "size": int(size),
            "align": bool(align),
            "status": "queued",
            "history_id": None,
            "error": None,
            "added_at": _now_iso(),
            "finished_at": None,
        }
        with self._lock:
            self._state["items"].append(item)
            self._save_locked()
        return dict(item)

    def set_status(self, qid: str, status: str, *, history_id: str | None = None,
                   error: str | None = None) -> bool:
        if status not in STATUSES:
            return False
        with self._lock:
            it = self._find_locked(qid)
            if it is None:
                return False
            it["status"] = status
            if history_id is not None:
                it["history_id"] = history_id
            if status == "queued":
                # retry / slot-busy release: wipe stale failure metadata
                it["error"] = None
                it["finished_at"] = None
            else:
                it["error"] = error if status == "error" else it.get("error")
                if status in TERMINAL_STATUSES:
                    it["finished_at"] = _now_iso()
            self._save_locked()
            return True

    def next_queued(self) -> dict | None:
        with self._lock:
            it = next(
                (it for it in self._state["items"] if it["status"] == "queued"),
                None,
            )
            return dict(it) if it else None

    def reorder(self, ids: list[str]) -> bool:
        """Rearrange queued items into the given id order.

        Only status=queued items participate; running/terminal items keep
        their positions. ``ids`` must be exactly the current queued ids
        (a permutation), otherwise the call is rejected — the store is the
        authority, the frontend restriction is cosmetic.
        """
        with self._lock:
            queued = [it for it in self._state["items"] if it["status"] == "queued"]
            if sorted(ids) != sorted(it["id"] for it in queued):
                return False
            by_id = {it["id"]: it for it in queued}
            replacement = iter(by_id[i] for i in ids)
            self._state["items"] = [
                next(replacement) if it["status"] == "queued" else it
                for it in self._state["items"]
            ]
            self._save_locked()
            return True

    def sort_by(self, key: str, desc: bool = False) -> bool:
        """One-shot rule sort of the queued items (same fixed-slot scheme)."""
        if key not in SORT_KEYS:
            return False
        key_fn = {
            "name": lambda it: (it.get("filename") or "").lower(),
            "size": lambda it: it.get("size", 0),
            "added_at": lambda it: it.get("added_at") or "",
        }[key]
        with self._lock:
            queued = [it for it in self._state["items"] if it["status"] == "queued"]
            queued.sort(key=key_fn, reverse=desc)
            replacement = iter(queued)
            self._state["items"] = [
                next(replacement) if it["status"] == "queued" else it
                for it in self._state["items"]
            ]
            self._save_locked()
            return True

    def remove(self, qid: str) -> dict | None:
        """Remove a non-running item and its staged file."""
        with self._lock:
            it = self._find_locked(qid)
            if it is None or it["status"] == "running":
                return None
            self._state["items"].remove(it)
            self._unlink_staged(it)
            self._save_locked()
            return dict(it)

    def clear_finished(self) -> int:
        """Drop all terminal items (and any staged file kept for retry)."""
        with self._lock:
            finished = [
                it for it in self._state["items"]
                if it["status"] in TERMINAL_STATUSES
            ]
            for it in finished:
                self._unlink_staged(it)
            self._state["items"] = [
                it for it in self._state["items"]
                if it["status"] not in TERMINAL_STATUSES
            ]
            self._save_locked()
            return len(finished)
