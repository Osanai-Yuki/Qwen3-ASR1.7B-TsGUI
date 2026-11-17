"""Workbench mode: approved folder roots, scanning, and auto-export.

Folder-driven batch processing references source files in place (no staging
copy), so every path that reaches the queue or the exporter must be validated
against a user-approved root here. Config lives in ``data/workbench.json``
and follows queue_store.py's hardening conventions: atomic writes, size
ceiling, corrupted state rebuilt instead of crashing.
"""
import json
import logging
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from . import export_formats

logger = logging.getLogger("asr")

router = APIRouter()

_VALID_ID = re.compile(r"[0-9a-f]{12}")
MAX_CONFIG_BYTES = 1024 * 1024
SCAN_LIMIT_CAP = 5000
_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
_C0 = re.compile(r"[\x00-\x1f\x7f]")

store: "WorkbenchStore | None" = None
_suffixes: set[str] = set()


def init(*, data_dir: Path, allowed_suffixes: set[str]) -> None:
    global store, _suffixes
    store = WorkbenchStore(Path(data_dir))
    _suffixes = set(allowed_suffixes)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def safe_component(value: str, fallback: str = "transcript") -> str:
    """One filesystem-safe path component, mirroring exportPlan.ts rules."""
    text = _C0.sub("", unicodedata.normalize("NFC", str(value or "")))
    text = _ILLEGAL.sub("_", text).lstrip(".").strip()
    return text[:120] or fallback


def export_stem(filename: str) -> str:
    # Split on both separators explicitly: Path().name differs across platforms.
    leaf = safe_component(str(filename or "").replace("\\", "/").split("/")[-1])
    stem = re.sub(r"\.[^.]+$", "", leaf)[:80].rstrip(". ")
    return stem or "transcript"


class WorkbenchStore:
    """Thread-safe persisted workbench config (roots + export preferences)."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = self.root / "workbench.json"
        self._lock = threading.Lock()
        self._state: dict = {
            "version": 0,
            "roots": [],
            "export_root": None,
            "formats": ["srt", "txt", "json"],
            "auto_export": True,
            "summarize": False,
            "summary_model": None,
        }
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            if self._file.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("workbench.json exceeds size ceiling")
            data = json.loads(self._file.read_text(encoding="utf-8"))
            roots = [
                r for r in data.get("roots", [])
                if isinstance(r, dict) and isinstance(r.get("id"), str)
                and _VALID_ID.fullmatch(r["id"]) and isinstance(r.get("path"), str)
            ]
            formats = [f for f in data.get("formats", []) if f in export_formats.FORMATS]
            self._state = {
                "version": int(data.get("version", 0)),
                "roots": roots,
                "export_root": data.get("export_root") if isinstance(data.get("export_root"), str) else None,
                "formats": formats or ["srt", "txt", "json"],
                "auto_export": bool(data.get("auto_export", True)),
                "summarize": bool(data.get("summarize", False)),
                "summary_model": data.get("summary_model") if isinstance(data.get("summary_model"), str) else None,
            }
        except Exception as e:
            logger.warning("Workbench config unreadable (%s); starting empty", e)

    def _save_locked(self) -> None:
        self._state["version"] += 1
        tmp = self._file.with_name(f".workbench-{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            logger.exception("Could not persist workbench config")
            tmp.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def add_root(self, path: str) -> tuple[dict | None, str | None]:
        candidate = Path(str(path or "").strip().strip('"'))
        if not candidate.is_dir():
            return None, "not_a_directory"
        resolved = str(candidate.resolve())
        with self._lock:
            if any(r["path"] == resolved for r in self._state["roots"]):
                return None, "duplicate_root"
            item = {
                "id": _new_id(),
                "path": resolved,
                "label": candidate.resolve().name or resolved,
                "added_at": _now_iso(),
            }
            self._state["roots"].append(item)
            self._save_locked()
            return dict(item), None

    def remove_root(self, rid: str) -> bool:
        with self._lock:
            before = len(self._state["roots"])
            self._state["roots"] = [r for r in self._state["roots"] if r["id"] != rid]
            if len(self._state["roots"]) == before:
                return False
            self._save_locked()
            return True

    def get_root(self, rid: str) -> dict | None:
        with self._lock:
            return next((dict(r) for r in self._state["roots"] if r["id"] == rid), None)

    def resolve_source(self, rid: str, rel_path: str) -> Path:
        """Containment-validated absolute path inside an approved root."""
        root = self.get_root(rid)
        if root is None:
            raise ValueError("unknown_root")
        base = Path(root["path"]).resolve()
        rel = str(rel_path or "").replace("\\", "/")
        if not rel or rel.startswith("/") or rel.startswith("~") or re.match(r"^[a-zA-Z]:", rel):
            raise ValueError("invalid_rel_path")
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            raise ValueError("path_escapes_root")
        return target

    def scan(self, rid: str, limit: int = 2000) -> tuple[list[dict], bool]:
        root = self.get_root(rid)
        if root is None:
            raise ValueError("unknown_root")
        base = Path(root["path"])
        if not base.is_dir():
            raise ValueError("root_gone")
        limit = max(1, min(int(limit), SCAN_LIMIT_CAP))
        found: list[dict] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and not (Path(dirpath) / d).is_symlink()
            )
            for name in sorted(filenames):
                if Path(name).suffix.lower() not in _suffixes:
                    continue
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                if len(found) >= limit:
                    truncated = True
                    return found, truncated
                found.append({
                    "rel_path": full.relative_to(base).as_posix(),
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                })
        found.sort(key=lambda it: it["rel_path"])
        return found, truncated

    def update_config(self, patch: dict) -> str | None:
        with self._lock:
            if "export_root" in patch:
                value = patch["export_root"]
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        return "invalid_export_root"
                    target = Path(value.strip().strip('"'))
                    if target.exists() and not target.is_dir():
                        return "invalid_export_root"
                    value = str(target.resolve())
                self._state["export_root"] = value
            if "formats" in patch:
                formats = patch["formats"]
                if (not isinstance(formats, list) or not formats
                        or not all(f in export_formats.FORMATS for f in formats)):
                    return "invalid_formats"
                self._state["formats"] = list(dict.fromkeys(formats))
            if "auto_export" in patch:
                if not isinstance(patch["auto_export"], bool):
                    return "invalid_auto_export"
                self._state["auto_export"] = patch["auto_export"]
            if "summarize" in patch:
                if not isinstance(patch["summarize"], bool):
                    return "invalid_summarize"
                self._state["summarize"] = patch["summarize"]
            if "summary_model" in patch:
                value = patch["summary_model"]
                if value is not None and not isinstance(value, str):
                    return "invalid_summary_model"
                self._state["summary_model"] = value
            self._save_locked()
        return None


def write_exports(
    *,
    text: str,
    segments: list[dict],
    stats: dict | None,
    filename: str,
    identity: str,
    export_root: str,
    batch_name: str | None,
    formats: list[str],
) -> list[dict]:
    """Atomically write one file per format under <export_root>/<batch>/.

    Collisions get the exportPlan.ts suffix rule (__<id8>, then __<id8>-n)
    compared case-insensitively, so re-runs never overwrite an earlier export.
    """
    batch_dir = safe_component(batch_name or "workbench", "workbench")
    directory = Path(export_root) / batch_dir
    directory.mkdir(parents=True, exist_ok=True)
    stem = export_stem(filename)
    tag_base = re.sub(r"[^0-9a-zA-Z]", "", identity)[:8] or "00000000"
    written: list[dict] = []
    for fmt in formats:
        desired = directory / f"{stem}.{fmt}"
        target = desired
        counter = 1
        while target.exists() or _case_insensitive_exists(directory, target.name):
            tag = f"__{tag_base}" if counter == 1 else f"__{tag_base}-{counter}"
            target = directory / f"{stem}{tag}.{fmt}"
            counter += 1
        content = export_formats.build_subtitle(fmt, text, segments, stats)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        written.append({"format": fmt, "path": str(target), "name": target.name})
    return written


def _case_insensitive_exists(directory: Path, name: str) -> bool:
    lowered = name.lower()
    try:
        return any(p.name.lower() == lowered for p in directory.iterdir())
    except OSError:
        return False


# Module facade for queue_api's dependency injection (store is swapped in tests).


def get_root(rid: str) -> dict | None:
    assert store is not None
    return store.get_root(rid)


def resolve_source(rid: str, rel_path: str) -> Path:
    assert store is not None
    return store.resolve_source(rid, rel_path)


def auto_export(item: dict, resp: dict, history_id: str | None, *, force: bool = False) -> dict:
    """Completion hook: export a durable result per workbench preferences.

    Returns {"state", "paths", "error"} for the queue item; never raises, so
    an export failure can never undo a committed transcription. ``force``
    (manual re-export) ignores the auto_export switch but still needs a root.
    """
    assert store is not None
    cfg = store.snapshot()
    if not cfg["export_root"]:
        if force:
            return {"state": "failed", "paths": [], "error": "export_root_unset"}
        return {"state": "skipped", "paths": [], "error": None}
    if not force and not cfg["auto_export"]:
        return {"state": "skipped", "paths": [], "error": None}
    try:
        written = write_exports(
            text=resp.get("text") or "",
            segments=resp.get("segments") or [],
            stats=resp.get("stats") or {},
            filename=item.get("filename") or "transcript",
            identity=history_id or item.get("id") or "00000000",
            export_root=cfg["export_root"],
            batch_name=item.get("batch_name"),
            formats=cfg["formats"],
        )
    except Exception:
        logger.exception("Auto-export failed for queue item %s", item.get("id"))
        return {"state": "failed", "paths": [], "error": "export_failed"}
    return {"state": "done", "paths": written, "error": None}


# endpoints


@router.get("/api/workbench/config")
async def workbench_config():
    assert store is not None
    return store.snapshot()


@router.put("/api/workbench/config")
async def workbench_config_update(req: dict = Body(default={})):
    assert store is not None
    error = store.update_config(req if isinstance(req, dict) else {})
    if error:
        return JSONResponse({"error": error}, status_code=422)
    return store.snapshot()


@router.post("/api/workbench/roots")
async def workbench_add_root(req: dict = Body(default={})):
    assert store is not None
    item, error = store.add_root(str(req.get("path") or ""))
    if error:
        status = 409 if error == "duplicate_root" else 422
        return JSONResponse({"error": error}, status_code=status)
    return {"root": item}


@router.delete("/api/workbench/roots/{rid}")
async def workbench_remove_root(rid: str):
    assert store is not None
    if not store.remove_root(rid):
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"ok": True}


@router.get("/api/workbench/scan")
async def workbench_scan(root: str = Query(...), limit: int = Query(2000)):
    assert store is not None
    try:
        items, truncated = store.scan(root, limit)
    except ValueError as e:
        code = str(e)
        status = 404 if code == "unknown_root" else 410
        return JSONResponse({"error": code}, status_code=status)
    return {"items": items, "truncated": truncated}