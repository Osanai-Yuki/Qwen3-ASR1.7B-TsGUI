"""Transcription history persistence.

Stores per-job records as JSON files in ``data/history/<id>.json``.
Each record contains the merged text, segment list, performance stats,
and identifying metadata. Raw uploaded audio/video is never persisted,
but MP3s converted from video sources are cached separately under
``data/audio_cache/`` and referenced by ``audio_cache_name``.
"""
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("asr")
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_id() -> str:
    return uuid.uuid4().hex[:12]


class HistoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, hid: str) -> Path:
        return self.root / f"{hid}.json"

    def save(
        self,
        *,
        filename: str,
        text: str,
        segments: list[dict],
        stats: dict,
        align_used: bool = False,
        audio_cache_name: str | None = None,
    ) -> dict:
        hid = _safe_id()
        record = {
            "id": hid,
            "created_at": _now_iso(),
            "filename": filename or "unknown",
            "text": text,
            "segments": segments,
            "stats": stats,
            "align_used": bool(align_used),
            "audio_cache_name": audio_cache_name,
        }
        path = self._path(hid)
        with _lock:
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("History saved: %s (%s)", hid, filename)
        return record

    def list(self, limit: int = 100) -> list[dict]:
        items: list[dict] = []
        with _lock:
            files = sorted(
                self.root.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
            for p in files:
                try:
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
        return items

    def get(self, hid: str) -> dict[str, Any] | None:
        path = self._path(hid)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read history %s: %s", hid, e)
            return None

    def delete(self, hid: str) -> bool:
        path = self._path(hid)
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
            for p in self.root.glob("*.json"):
                try:
                    p.unlink()
                    n += 1
                except Exception:
                    pass
        return n
