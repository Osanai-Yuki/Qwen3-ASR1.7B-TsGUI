"""Transcription history persistence.

Stores per-job records as JSON files in ``data/history/<id>.json``.
Each record contains the merged text, segment list, performance stats,
and identifying metadata. Raw uploaded audio/video is never persisted,
but MP3s converted from video sources are cached separately under
``data/audio_cache/`` and referenced by ``audio_cache_name``.

Writes are atomic (temp file in the same directory + ``os.replace``) so a
crash mid-write can never leave a half-written record that the list/search
readers would treat as a complete result.

Result identity
---------------
``result_version`` is the durable ordering key for "which attempt of this
task is the current result".  It is *reserved before* an attempt runs (see
``reserve_version`` / the queue worker) so a failed save can be retried
without taking a new number, and so a late save from an older attempt can
never displace a newer one.  File mtime is never used to express result
order — mtime changes when a user opens a result to save their position.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("asr")
_lock = threading.Lock()

# Per-file read ceiling for history records.  A malformed or oversized file placed
# in the history root must not be allowed to exhaust memory while parsing.
MAX_HISTORY_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB

# History IDs are produced by _safe_id() as the first 12 hex chars of a UUID4.
# Enforcing this shape everywhere a record is looked up closes a path-traversal
# hole: the FastAPI route {hid} only blocks literal "/", but on Windows a
# "%5C"-encoded backslash decodes to "\" which pathlib treats as a separator,
# letting an attacker escape the history root. Rejecting anything that isn't a
# 12-char hex string defeats that (and every other traversal vector) at the
# storage layer — the authoritative check, since HistoryStore is also used
# directly in tests.
_VALID_ID = re.compile(r"[0-9a-f]{12}")

# Fallback reasons surfaced by derive_timing_info.  These are *machine* codes;
# the UI maps them to exact wording.
TIMING_FALLBACK_ALIGNMENT_FAILED = "alignment_failed"
TIMING_FALLBACK_ALIGNMENT_UNAVAILABLE = "alignment_unavailable"
TIMING_FALLBACK_NOT_REQUESTED = "alignment_not_requested"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_id() -> str:
    return uuid.uuid4().hex[:12]


def derive_timing_info(
    *,
    segments: list | None,
    align_used: bool = False,
    alignment_requested: bool = False,
    stats: dict | None = None,
) -> dict:
    """Derive what timing data a result *actually* contains.

    ``level`` describes the granularity available in the stored record — never
    what was requested or what the aligner was capable of.  A forced-alignment
    run that ends in ``resegment_words`` still yields sentence segments, so it
    reports ``segment``; claiming ``word`` there would overstate the data.
    """
    stats = stats or {}
    usable: list[tuple[float, float]] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end < start:
            continue
        usable.append((start, end))
    ordered = all(usable[i][0] <= usable[i + 1][0] for i in range(len(usable) - 1))
    level = "segment" if usable and ordered else "none"
    word_count = stats.get("word_count")
    if alignment_requested and not align_used:
        fallback = (
            TIMING_FALLBACK_ALIGNMENT_UNAVAILABLE
            if not stats.get("aligner_available", True)
            else TIMING_FALLBACK_ALIGNMENT_FAILED
        )
    elif not alignment_requested:
        fallback = TIMING_FALLBACK_NOT_REQUESTED
    else:
        fallback = None
    return {
        "level": level,
        "source": "forced_alignment" if align_used else "asr",
        "alignment_requested": bool(alignment_requested),
        "alignment_succeeded": bool(align_used),
        "segment_count": len(usable),
        "word_count": word_count if isinstance(word_count, int) else None,
        "fallback_reason": fallback,
    }


def _normalize_timing(data: dict) -> dict:
    """Best-effort timing for a record written before the field existed.

    Never guesses precision: a record with no usable segments reports ``none``
    even if the file is old.
    """
    existing = data.get("timing")
    if isinstance(existing, dict) and existing.get("level"):
        return existing
    stats = data.get("stats") or {}
    return derive_timing_info(
        segments=data.get("segments") or [],
        align_used=bool(data.get("align_used")),
        alignment_requested=bool(data.get("alignment_requested")),
        stats=stats,
    )


class HistoryStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Reservations live in their own directory so the record globs
        # ("*.json") never mistake a side-car for a transcript.
        self.reserve_dir = self.root / ".reservations"
        self.reserve_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, hid: str) -> Path:
        """Resolved on-disk path for ``hid``, validating the id and containment.

        Raises ``ValueError`` for any id that isn't a 12-char hex string or
        whose resolved path escapes ``self.root``. Callers translate this into
        a not-found result *before* touching the filesystem.
        """
        if not isinstance(hid, str) or not _VALID_ID.fullmatch(hid):
            raise ValueError("invalid history id")
        path = (self.root / f"{hid}.json").resolve()
        # Defense-in-depth: confirm the resolved path is still inside root
        # (catches any future encoding trick that passes the regex).
        if path.parent != self.root:
            raise ValueError("invalid history id")
        return path

    def _next_version_locked(self, task_id: str) -> int:
        """Highest existing version for ``task_id`` + 1.  Caller holds _lock.

        Both saved records and outstanding reservations count, so a version
        that was reserved but never written (save failed, payload only in the
        pending store) is never handed out twice.
        """
        previous = 0
        for existing_path in self.root.glob("*.json"):
            try:
                if existing_path.stat().st_size > MAX_HISTORY_FILE_BYTES:
                    continue
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
                if existing.get("task_id") == task_id:
                    version = int(existing.get("result_version") or 1)
                    previous = max(previous, version)
            except Exception:
                continue
        reservation = self.reserve_dir / f"{task_id}.json"
        if reservation.is_file():
            try:
                prior = json.loads(reservation.read_text(encoding="utf-8"))
                previous = max(previous, int(prior.get("result_version") or 0))
            except Exception:
                pass
        return previous + 1

    def reserve_version(self, task_id: str) -> int:
        """Reserve and persist the next result version for ``task_id``.

        The reservation is written to a side-car file so it is honoured even if
        the transcription never produces a record (e.g. the save failed and the
        payload lives only in the pending store).  A later save for the same
        task — including a *retry* of this attempt — reuses this number instead
        of allocating a new one, which is what stops an older failed attempt
        from claiming a newer slot when it is saved late.
        """
        if not isinstance(task_id, str) or not _VALID_ID.fullmatch(task_id):
            raise ValueError("invalid task id")
        with _lock:
            version = self._next_version_locked(task_id)
            reservation = self.reserve_dir / f"{task_id}.json"
            if reservation.is_file():
                try:
                    prior = json.loads(reservation.read_text(encoding="utf-8"))
                    previous = int(prior.get("result_version") or 0)
                    if previous >= version:
                        return previous
                except Exception:
                    pass
            # A reservation that could not be persisted is not a reservation:
            # the worker must not start an expensive recognition whose result
            # has no reliable version slot.  Let the caller stop and report it.
            payload = {"task_id": task_id, "result_version": version}
            tmp = reservation.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, reservation)
            return version

    def save(
        self,
        *,
        filename: str,
        text: str,
        segments: list[dict],
        stats: dict,
        align_used: bool = False,
        audio_cache_name: str | None = None,
        batch_id: str | None = None,
        batch_name: str | None = None,
        task_id: str | None = None,
        source_path: str | None = None,
        title: str | None = None,
        alignment_requested: bool = False,
        result_version: int | None = None,
        attempt_id: str | None = None,
        timing: dict | None = None,
        reader_position: dict | None = None,
        playback_position: float | None = None,
    ) -> dict:
        hid = _safe_id()
        partial = bool(stats.get("chunk_failures") or stats.get("cancelled"))
        if timing is None:
            timing = derive_timing_info(
                segments=segments,
                align_used=align_used,
                alignment_requested=alignment_requested,
                stats=stats,
            )
        record = {
            "id": hid,
            "created_at": _now_iso(),
            "filename": filename or "unknown",
            "title": title or Path(filename or "transcript").stem or "transcript",
            "batch_id": batch_id,
            "batch_name": batch_name,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "source_path": source_path,
            "result_version": 1,
            "persistence_state": "saved",
            "config_snapshot": {
                "model": stats.get("model"),
                "aligner_model": stats.get("aligner_model"),
                "alignment_requested": bool(alignment_requested),
            },
            "text": text,
            "segments": segments,
            "stats": stats,
            "align_used": bool(align_used),
            "alignment_requested": bool(alignment_requested),
            "alignment_fallback": bool(alignment_requested and not align_used),
            "timing": timing,
            "integrity": "partial" if partial else "complete",
            "missing_ranges": stats.get("chunk_failure_details", []),
            "first_viewed_at": None,
            "read_position": playback_position if playback_position is not None else 0.0,
            "playback_position": playback_position if playback_position is not None else 0.0,
            "reader_position": reader_position,
            "audio_cache_name": audio_cache_name,
        }
        path = self._path(hid)
        with _lock:
            if result_version is not None:
                version = int(result_version)
                if version < 1:
                    raise ValueError("result_version must be >= 1")
                record["result_version"] = version
            elif task_id:
                # Compat path for callers that do not reserve (tests, legacy
                # scripts).  Queue-driven saves always pass an explicit
                # reserved version, so this never renumbers a real attempt.
                record["result_version"] = self._next_version_locked(task_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        logger.info(
            "History saved: %s (%s, task=%s, v%s, attempt=%s)",
            hid, filename, task_id, record["result_version"], attempt_id,
        )
        return record

    def list(self, limit: int = 100) -> list[dict]:
        items: list[dict] = []
        with _lock:
            files = sorted(
                self.root.glob("*.json"),
                key=lambda p: (p.stat().st_mtime, p.name),
                reverse=True,
            )[:limit]
            for p in files:
                try:
                    if p.stat().st_size > MAX_HISTORY_FILE_BYTES:
                        logger.warning(
                            "Skipping oversized history file %s (%d bytes)",
                            p.name, p.stat().st_size,
                        )
                        continue
                    data = json.loads(p.read_text(encoding="utf-8"))
                    items.append({
                        "id": data.get("id", p.stem),
                        "created_at": data.get("created_at"),
                        "filename": data.get("filename"),
                        "duration": data.get("stats", {}).get("audio_duration", 0.0),
                        "char_count": data.get("stats", {}).get("char_count", 0),
                        "align_used": data.get("align_used", False),
                        "segment_count": data.get("stats", {}).get("segment_count", 0),
                        "audio_cache_name": data.get("audio_cache_name"),
                    })
                except Exception as e:
                    logger.warning("Skipping unreadable history %s: %s", p.name, e)
        # Stable ordering: newest first, id as the tie-break so records created
        # in the same second never swap places between requests.
        items.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=True)
        return items

    def search(
        self,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 40,
        batch_id: str | None = None,
        filter_by: str = "all",
    ) -> dict:
        """Search all saved transcripts and return a small, paged result set."""
        needle = query.strip().casefold()
        offset = max(0, int(offset))
        limit = max(1, min(100, int(limit)))
        matched: list[dict] = []
        with _lock:
            files = sorted(
                self.root.glob("*.json"),
                key=lambda p: (p.stat().st_mtime, p.name),
                reverse=True,
            )
            for p in files:
                try:
                    if p.stat().st_size > MAX_HISTORY_FILE_BYTES:
                        continue
                    data = json.loads(p.read_text(encoding="utf-8"))
                    viewed = data.get("first_viewed_at")
                    stats = data.get("stats") or {}
                    integrity = data.get("integrity") or (
                        "partial" if stats.get("chunk_failures") or stats.get("cancelled") else "complete"
                    )
                    if batch_id and data.get("batch_id") != batch_id:
                        continue
                    if filter_by == "unseen" and viewed:
                        continue
                    if filter_by == "partial" and integrity != "partial":
                        continue
                    text = str(data.get("text") or "")
                    # Field-level match reporting: the UI needs to know *where*
                    # the hit came from so opening the result can show the full
                    # transcript (title/source/batch hits) or locate the first
                    # body hit, instead of filtering the body by a title word.
                    fields = {
                        "title": str(data.get("title") or ""),
                        "filename": str(data.get("filename") or ""),
                        "source_path": str(data.get("source_path") or ""),
                        "batch_name": str(data.get("batch_name") or ""),
                    }
                    match_fields = [
                        name for name, value in fields.items()
                        if needle and needle in value.casefold()
                    ]
                    body_hit = bool(needle) and needle in text.casefold()
                    if needle and not match_fields and not body_hit:
                        continue
                    first_match = None
                    if body_hit:
                        at = text.casefold().find(needle)
                        first_match = {"field": "text", "offset": at}
                    elif match_fields:
                        first_match = {"field": match_fields[0], "offset": None}
                    snippet = ""
                    if text:
                        at = text.casefold().find(needle) if needle else 0
                        start = max(0, at - 72)
                        end = min(len(text), max(at, 0) + 168)
                        snippet = text[start:end].strip()
                        if start:
                            snippet = "…" + snippet
                        if end < len(text):
                            snippet += "…"
                    matched.append({
                        "id": data.get("id", p.stem),
                        "created_at": data.get("created_at"),
                        "filename": data.get("filename"),
                        "title": data.get("title") or Path(data.get("filename") or "transcript").stem,
                        "batch_id": data.get("batch_id"),
                        "batch_name": data.get("batch_name"),
                        "task_id": data.get("task_id"),
                        "attempt_id": data.get("attempt_id"),
                        "source_path": data.get("source_path"),
                        "duration": stats.get("audio_duration", 0.0),
                        "char_count": stats.get("char_count", len(text)),
                        "align_used": data.get("align_used", False),
                        "alignment_requested": data.get("alignment_requested", False),
                        "alignment_fallback": data.get("alignment_fallback", bool(data.get("alignment_requested") and not data.get("align_used"))),
                        "timing": _normalize_timing(data),
                        "segment_count": stats.get("segment_count", 0),
                        "integrity": integrity,
                        "missing_ranges": data.get("missing_ranges", stats.get("chunk_failure_details", [])),
                        "first_viewed_at": viewed,
                        "read_position": data.get("read_position", 0.0),
                        "reader_position": data.get("reader_position"),
                        "playback_position": data.get("playback_position", 0.0),
                        "audio_cache_name": data.get("audio_cache_name"),
                        "result_version": data.get("result_version", 1),
                        "match_fields": match_fields,
                        "first_match": first_match,
                        "snippet": snippet,
                    })
                except Exception as e:
                    logger.warning("Skipping unreadable history %s: %s", p.name, e)
        # Stable ordering: created_at desc, then id desc as tie-break.  Without
        # the tie-break, two records created in the same second can swap
        # between page requests and make offset pagination skip or duplicate.
        matched.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=True)
        return {
            "items": matched[offset:offset + limit],
            "total": len(matched),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(matched),
        }

    def update_view(
        self,
        hid: str,
        position: float | None = None,
        *,
        reader_position: dict | None = None,
        playback_position: float | None = None,
    ) -> dict | None:
        """Persist first-view time plus independent read/play positions.

        ``position`` is the legacy seconds field and remains the playback
        position for old clients.  ``reader_position`` is the transcript anchor
        (segment index / text offset / ratio) and is never overwritten by page
        scrolling, so audio playback and reading progress stay independent.
        """
        try:
            path = self._path(hid)
        except ValueError:
            return None
        with _lock:
            if not path.is_file() or path.stat().st_size > MAX_HISTORY_FILE_BYTES:
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["first_viewed_at"] = data.get("first_viewed_at") or _now_iso()
                if position is not None:
                    seconds = max(0.0, min(float(position), 12 * 60 * 60))
                    data["read_position"] = seconds
                    data["playback_position"] = seconds
                if playback_position is not None:
                    data["playback_position"] = max(0.0, min(float(playback_position), 12 * 60 * 60))
                if reader_position is not None:
                    if not isinstance(reader_position, dict):
                        raise ValueError("reader_position must be an object")
                    cleaned: dict[str, Any] = {}
                    if reader_position.get("segment_index") is not None:
                        cleaned["segment_index"] = max(0, int(reader_position["segment_index"]))
                    if reader_position.get("text_offset") is not None:
                        cleaned["text_offset"] = max(0, int(reader_position["text_offset"]))
                    for key in ("within_block_ratio", "scroll_ratio"):
                        value = reader_position.get(key)
                        if value is not None:
                            cleaned[key] = max(0.0, min(float(value), 1.0))
                    if not cleaned:
                        raise ValueError("reader_position has no usable field")
                    data["reader_position"] = cleaned
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, path)
                return {
                    "first_viewed_at": data["first_viewed_at"],
                    "read_position": data.get("read_position", 0.0),
                    "playback_position": data.get("playback_position", 0.0),
                    "reader_position": data.get("reader_position"),
                }
            except Exception as e:
                logger.warning("Failed to update history view %s: %s", hid, e)
                return None

    def batches(self) -> list[dict]:
        """Return durable batch summaries assembled from saved task records.

        Within a batch, one row is kept per ``task_id``: the one with the
        highest ``result_version``.  Version — not file mtime — decides, so
        opening an older version to save its reading position cannot make it
        the batch's "current" result.  Legacy records without a ``task_id``
        are shown independently (keyed by their history id) rather than being
        guessed into a task by filename.
        """
        groups: dict[str, dict] = {}
        with _lock:
            files = sorted(
                self.root.glob("*.json"),
                key=lambda p: (p.stat().st_mtime, p.name),
                reverse=True,
            )
            for p in files:
                try:
                    if p.stat().st_size > MAX_HISTORY_FILE_BYTES:
                        continue
                    data = json.loads(p.read_text(encoding="utf-8"))
                    batch_id = data.get("batch_id") or "legacy-history"
                    group = groups.setdefault(batch_id, {
                        "id": batch_id,
                        "name": data.get("batch_name") or "未分组的历史记录",
                        "created_at": data.get("created_at"),
                        "items": [],
                    })
                    if (data.get("created_at") or "") > (group.get("created_at") or ""):
                        group["created_at"] = data.get("created_at")
                    stats = data.get("stats") or {}
                    try:
                        version = int(data.get("result_version") or 1)
                    except (TypeError, ValueError):
                        version = 1
                    group["items"].append({
                        "id": data.get("task_id") or data.get("id", p.stem),
                        "task_id": data.get("task_id"),
                        "attempt_id": data.get("attempt_id"),
                        "filename": data.get("filename"),
                        "title": data.get("title"),
                        "source_path": data.get("source_path"),
                        "size": stats.get("file_size", 0),
                        "added_at": data.get("created_at"),
                        "status": "cancelled" if stats.get("cancelled") else "done",
                        "integrity": data.get("integrity") or (
                            "partial" if stats.get("chunk_failures") or stats.get("cancelled") else "complete"
                        ),
                        "missing_ranges": data.get("missing_ranges", stats.get("chunk_failure_details", [])),
                        "alignment_fallback": data.get("alignment_fallback", False),
                        "timing": _normalize_timing(data),
                        "history_id": data.get("id", p.stem),
                        "result_version": version,
                        "persistence_state": data.get("persistence_state", "saved"),
                        "duration": stats.get("audio_duration", 0.0),
                        "segment_count": stats.get("segment_count", 0),
                        "first_viewed_at": data.get("first_viewed_at"),
                        "read_position": data.get("read_position", 0.0),
                        "reader_position": data.get("reader_position"),
                        "playback_position": data.get("playback_position", 0.0),
                    })
                except Exception as e:
                    logger.warning("Skipping unreadable history %s: %s", p.name, e)
        for group in groups.values():
            # Keep the newest version per task.  Sort by (version, created_at,
            # history_id) so the winner is deterministic even when two records
            # share a version (e.g. a legacy record written without one).
            group["items"].sort(
                key=lambda item: (
                    item.get("result_version") or 0,
                    item.get("added_at") or "",
                    item.get("history_id") or "",
                ),
                reverse=True,
            )
            seen_tasks: set[str] = set()
            latest_items = []
            for item in group["items"]:
                task_key = item.get("task_id") or item.get("id")
                if task_key in seen_tasks:
                    continue
                seen_tasks.add(task_key)
                latest_items.append(item)
            # Display order: newest task first, independent of which version won.
            latest_items.sort(key=lambda item: (item.get("added_at") or "", item.get("id") or ""), reverse=True)
            group["items"] = latest_items
        return list(groups.values())

    def find_by_attempt(self, attempt_id: str) -> str | None:
        """History id for an attempt, or None.

        Used to repair the queue association after a crash in the window
        between "history committed" and "queue updated": the result exists and
        must not be transcribed a second time.
        """
        if not isinstance(attempt_id, str) or not _VALID_ID.fullmatch(attempt_id):
            return None
        with _lock:
            for p in self.root.glob("*.json"):
                try:
                    if p.stat().st_size > MAX_HISTORY_FILE_BYTES:
                        continue
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if data.get("attempt_id") == attempt_id:
                        return data.get("id")
                except Exception:
                    continue
        return None

    def get(self, hid: str) -> dict[str, Any] | None:
        try:
            path = self._path(hid)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            if path.stat().st_size > MAX_HISTORY_FILE_BYTES:
                logger.warning("Skipping oversized history %s (%d bytes)", hid, path.stat().st_size)
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            stats = data.get("stats") or {}
            data.setdefault(
                "integrity",
                "partial" if stats.get("chunk_failures") or stats.get("cancelled") else "complete",
            )
            data.setdefault("missing_ranges", stats.get("chunk_failure_details", []))
            data.setdefault(
                "alignment_fallback",
                bool(data.get("alignment_requested") and not data.get("align_used")),
            )
            data.setdefault("first_viewed_at", None)
            data.setdefault("read_position", 0.0)
            data.setdefault("playback_position", data.get("read_position", 0.0))
            data.setdefault("reader_position", None)
            data.setdefault("result_version", 1)
            data.setdefault("attempt_id", None)
            data.setdefault("persistence_state", "saved")
            data["timing"] = _normalize_timing(data)
            return data
        except Exception as e:
            logger.warning("Failed to read history %s: %s", hid, e)
            return None

    def delete(self, hid: str) -> bool:
        try:
            path = self._path(hid)
        except ValueError:
            return False
        if not path.exists():
            return False
        with _lock:
            try:
                path.unlink()
                return True
            except Exception as e:
                logger.warning("Failed to delete history %s: %s", hid, e)
                return False

    def clear(self) -> int:
        n = 0
        with _lock:
            for p in list(self.root.glob("*.json")) + list(self.reserve_dir.glob("*.json")):
                try:
                    p.unlink()
                    n += 1
                except Exception:
                    pass
        return n
