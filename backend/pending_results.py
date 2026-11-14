"""Recoverable payloads for transcriptions whose history save failed.

When a transcription produces text but the history write fails (disk full,
permissions, corruption), the text is still valuable: the user can read it
this session and retry the save without re-running ASR.  This module keeps
those payloads in memory (always) and on disk (best effort) under
``data/pending_results/``.

The queue summary never embeds a payload — only light capability flags — so
``GET /api/queue`` stays small and cheap to poll.  Capacity is enforced by
actual bytes; when the ceiling is reached the in-memory copy is still kept
(it is the last resort) and :meth:`put` reports ``persisted=False`` so the UI
can say "readable this session, not durable".

Ids follow the same 12-hex shape as history/queue records and are validated
before any filesystem access, so a crafted id can never escape the root.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("asr")

_VALID_ID = re.compile(r"[0-9a-f]{12}")

# A single recovery file larger than this is treated as corrupt/foreign and
# skipped rather than parsed (mirrors the history ceiling).
MAX_PENDING_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB

# Total on-disk budget for recovery files.  Unlike the history ceiling this is
# a *budget we manage*: callers are told when it is exhausted instead of
# silently dropping the only readable copy of a failed result.
MAX_PENDING_TOTAL_BYTES = int(os.environ.get("ASR_PENDING_MAX_BYTES", str(64 * 1024 * 1024)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _VALID_ID.fullmatch(value) else None


class PendingResultStore:
    """Thread-safe in-memory + on-disk store for save-failed payloads."""

    def __init__(self, root: Path, max_total_bytes: int = MAX_PENDING_TOTAL_BYTES):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_total_bytes = int(max_total_bytes)
        self._lock = threading.Lock()
        # (task_id, attempt_id) -> payload dict.  The memory copy is the
        # authoritative "readable right now" source; disk is best-effort.
        self._mem: dict[tuple[str, str], dict] = {}
        self._sizes: dict[tuple[str, str], int] = {}
        self._mem_bytes = 0
        self._load()

    # persistence

    def _path(self, task_id: str, attempt_id: str) -> Path:
        tid = _safe_id(task_id)
        aid = _safe_id(attempt_id)
        if tid is None or aid is None:
            raise ValueError("invalid pending result id")
        path = (self.root / f"{tid}__{aid}.json").resolve()
        if path.parent != self.root:
            raise ValueError("invalid pending result id")
        return path

    def _load(self) -> None:
        """Restore recovery files from a previous process, best effort."""
        restored = 0
        for p in self.root.glob("*.json"):
            try:
                if p.stat().st_size > MAX_PENDING_FILE_BYTES:
                    continue
                data = json.loads(p.read_text(encoding="utf-8"))
                key = (data.get("task_id"), data.get("attempt_id"))
                if _safe_id(key[0]) and _safe_id(key[1]) and key not in self._mem:
                    self._mem[key] = data
                    restored += 1
            except Exception as e:
                logger.warning("Skipping unreadable pending result %s: %s", p.name, e)
        self._mem_bytes = sum(self._size_of(record) for record in self._mem.values())
        self._sizes = {
            key: self._size_of(record) for key, record in self._mem.items()
        }
        if restored:
            logger.info("Restored %d pending result(s) from disk", restored)

    @staticmethod
    def _size_of(record: dict) -> int:
        return len(json.dumps(record, ensure_ascii=False).encode("utf-8"))

    def _remember_locked(self, key: tuple[str, str], record: dict) -> None:
        """Install a record and keep the byte accounting exact."""
        self._forget_locked(key)
        size = self._size_of(record)
        self._mem[key] = record
        self._sizes[key] = size
        self._mem_bytes += size

    def _forget_locked(self, key: tuple[str, str]) -> None:
        self._mem.pop(key, None)
        self._mem_bytes -= self._sizes.pop(key, 0)
        if self._mem_bytes < 0:
            self._mem_bytes = 0

    def _disk_bytes(self) -> int:
        total = 0
        for p in self.root.glob("*.json"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    # public API

    def put(self, *, task_id: str, attempt_id: str, payload: dict) -> dict:
        """Store a payload. Returns ``{"persisted", "over_budget", "bytes"}``.

        The in-memory copy is always stored.  The disk copy is written only if
        it fits the remaining budget; ``over_budget`` tells the worker to stop
        starting new jobs instead of silently growing memory while every disk
        write keeps failing.  A failed disk write never removes the in-memory
        copy or raises — losing the only readable result is worse than telling
        the user it will not survive a restart.
        """
        info = {"persisted": False, "over_budget": False, "bytes": 0}
        try:
            path = self._path(task_id, attempt_id)
        except ValueError:
            logger.warning("Refusing to store pending result with invalid id")
            return info
        key = (task_id, attempt_id)
        record = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "stored_at": _now_iso(),
            "payload": payload,
        }
        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        info["bytes"] = len(blob)
        with self._lock:
            self._remember_locked(key, record)
            try:
                existing_bytes = path.stat().st_size
            except OSError:
                existing_bytes = 0
            if self._disk_bytes() - existing_bytes + len(blob) <= self.max_total_bytes:
                try:
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(blob.decode("utf-8"), encoding="utf-8")
                    os.replace(tmp, path)
                    info["persisted"] = True
                except OSError as e:
                    # Disk full / permissions: keep the session copy, report it.
                    logger.warning("Could not persist pending result %s: %s", path.name, e)
            else:
                info["over_budget"] = True
                logger.warning(
                    "Pending result budget exhausted (%d bytes); keeping %s in memory only",
                    self.max_total_bytes, path.name,
                )
            if self._mem_bytes > self.max_total_bytes:
                # Disk may still accept the file, but the session copy has
                # reached the ceiling: stop starting new jobs instead of
                # letting memory grow without bound.
                info["over_budget"] = True
                logger.warning(
                    "Pending result memory budget exhausted (%d > %d bytes)",
                    self._mem_bytes, self.max_total_bytes,
                )
        # Record the outcome *inside* the stored document too, so a payload
        # restored from disk after a restart reports the same durability.
        record["persisted"] = bool(info["persisted"])
        if info["persisted"]:
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        with self._lock:
            # The final document carries one extra boolean field.
            self._remember_locked(key, record)
        return info

    def get(self, task_id: str, attempt_id: str) -> dict | None:
        """Return the payload for one attempt, memory first then disk."""
        try:
            path = self._path(task_id, attempt_id)
        except ValueError:
            return None
        key = (task_id, attempt_id)
        with self._lock:
            record = self._mem.get(key)
            if record is not None:
                return self._with_persisted(record)
            try:
                if path.stat().st_size > MAX_PENDING_FILE_BYTES:
                    return None
                record = json.loads(path.read_text(encoding="utf-8"))
                payload = record.get("payload")
                if isinstance(payload, dict):
                    self._remember_locked(key, record)
                    return self._with_persisted(record)
            except Exception as e:
                logger.warning("Failed to read pending result %s: %s", path.name, e)
        return None

    @staticmethod
    def _with_persisted(record: dict) -> dict:
        payload = dict(record.get("payload") or {})
        payload["persisted"] = bool(record.get("persisted"))
        return payload

    def latest_for_task(self, task_id: str) -> dict | None:
        """Payload of the highest result_version stored for a task, or None."""
        if _safe_id(task_id) is None:
            return None
        with self._lock:
            candidates = [
                (key, rec) for key, rec in self._mem.items() if key[0] == task_id
            ]
        if not candidates:
            return None

        def version(item: tuple[tuple[str, str], dict]) -> int:
            payload = item[1].get("payload") or {}
            try:
                return int(payload.get("result_version") or 1)
            except (TypeError, ValueError):
                return 1

        best = max(candidates, key=lambda item: (version(item), item[0][1]))
        return best[1].get("payload")

    def remove(self, task_id: str, attempt_id: str) -> bool:
        """Drop a payload (after a successful save). Returns True if gone."""
        try:
            path = self._path(task_id, attempt_id)
        except ValueError:
            return False
        with self._lock:
            self._forget_locked((task_id, attempt_id))
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to remove pending result %s: %s", path.name, e)
                return False
        return True

    def total_bytes(self) -> int:
        with self._lock:
            return self._mem_bytes

    def list_recoverable(self) -> list[dict]:
        """Lightweight capability rows for the queue summary / recovery UI."""
        rows: list[dict] = []
        with self._lock:
            items = list(self._mem.values())
        for record in items:
            payload = record.get("payload") or {}
            stats = payload.get("stats") or {}
            rows.append({
                "task_id": record.get("task_id"),
                "attempt_id": record.get("attempt_id"),
                "stored_at": record.get("stored_at"),
                "filename": stats.get("filename"),
                "title": payload.get("title") or stats.get("filename"),
                "char_count": stats.get("char_count", 0),
                "segment_count": stats.get("segment_count", 0),
                "result_version": payload.get("result_version", 1),
                "integrity": payload.get("integrity", "complete"),
            })
        rows.sort(key=lambda row: row.get("stored_at") or "", reverse=True)
        return rows
