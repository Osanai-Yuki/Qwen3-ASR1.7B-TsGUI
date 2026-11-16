"""Regression tests for the gizmodo-v1 repair (A01-A11).

Covers the behaviours the acceptance round found broken and the ones the
handoff requires to stay true: save-failure recovery, result-version
selection, import identity, independent read/play positions, and the crash
window between "history committed" and "queue updated".

Run with the project interpreter:
    E:\\anaconda3\\envs\\qwen3asr\\python.exe -m pytest backend/test_repair.py -q
"""
import asyncio
import json
import os
import threading

import pytest

import backend.queue_api as qapi
from backend.history import HistoryStore
from backend.pending_results import PendingResultStore
from backend.queue_store import QueueStore
from backend.test_main import make_client


def _client(tmp_path):
    make_client(tmp_path=tmp_path)
    qapi.store = QueueStore(tmp_path / "queue")
    return qapi.store


def _save(history, **extra):
    return history.save(
        filename=extra.pop("filename", "same.wav"),
        text=extra.pop("text", "梧桐计划"),
        segments=extra.pop("segments", [{"start": 2, "end": 3, "text": "梧桐计划"}]),
        stats=extra.pop("stats", {"audio_duration": 4, "chunk_failures": 0}),
        **extra,
    )


async def _failing_transcribe(**_kwargs):
    return {"history_id": None, "text": "available text", "segments": [], "stats": {}}


# --- A01: import identity -------------------------------------------------

def test_client_import_ids_are_echoed_per_file(tmp_path):
    """Two same-named same-sized files must become two distinct queue items,
    each carrying the caller's own identity."""
    _client(tmp_path)
    client = make_client(tmp_path=tmp_path)
    r = client.post(
        "/api/queue/items",
        files=[("files", ("same.wav", b"1234", "audio/wav")),
               ("files", ("same.wav", b"5678", "audio/wav"))],
        data={
            "batch_id": "aabbccddeeff", "batch_name": "test",
            "source_paths": '["A/same.wav","B/same.wav"]',
            "client_import_ids": '["id-one","id-two"]',
        },
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    assert [i["client_import_id"] for i in items] == ["id-one", "id-two"]
    assert len({i["id"] for i in items}) == 2


def test_client_import_ids_validation(tmp_path):
    _client(tmp_path)
    client = make_client(tmp_path=tmp_path)
    files = [("files", ("a.wav", b"1", "audio/wav")), ("files", ("b.wav", b"2", "audio/wav"))]
    base = {"batch_id": "aabbccddeeff", "batch_name": "t"}
    # Length mismatch is a client bug worth naming, not silently ignored.
    r = client.post("/api/queue/items", files=files, data={**base, "client_import_ids": '["only-one"]'})
    assert r.status_code == 422
    # Duplicates would make receipt matching ambiguous.
    r = client.post("/api/queue/items", files=files, data={**base, "client_import_ids": '["x","x"]'})
    assert r.status_code == 422
    # Malformed JSON.
    r = client.post("/api/queue/items", files=files, data={**base, "client_import_ids": "not-json"})
    assert r.status_code == 422
    # Omitted entirely: old clients must keep working.
    r = client.post("/api/queue/items", files=files, data=base)
    assert r.status_code == 200


def test_partial_accept_returns_only_the_accepted_prefix(tmp_path, monkeypatch):
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "max_upload_bytes", 5)
    client = make_client(tmp_path=tmp_path)
    r = client.post(
        "/api/queue/items",
        files=[("files", ("same.wav", b"1234", "audio/wav")),
               ("files", ("same.wav", b"123456", "audio/wav"))],
        data={
            "batch_id": "aabbccddeeff", "batch_name": "t",
            "source_paths": '["A/same.wav","B/same.wav"]',
            "client_import_ids": '["id-one","id-two"]',
        },
    )
    assert r.status_code == 413
    accepted = r.json()["items"]
    assert len(accepted) == 1
    assert accepted[0]["client_import_id"] == "id-one"
    assert len(store.snapshot()["items"]) == 1


# --- A04: save-failure recovery ------------------------------------------

def test_save_failure_keeps_source_and_exposes_payload(tmp_path, monkeypatch):
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    staged = store.uploads_dir / "x.wav"
    staged.write_bytes(b"x")

    assert asyncio.run(qapi.process_next_once()) is True
    row = store.get(item["id"])
    assert row["history_id"] is None
    assert row["error"] == "history_save_failed"
    # The source must survive: it is the only way to re-process.
    assert staged.is_file()
    assert row["temporary_result_available"] is True
    assert row["can_retry_save"] is True
    assert row["persistence_state"] == "failed"

    client = make_client(tmp_path=tmp_path)
    r = client.get(f"/api/queue/items/{item['id']}/result")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "temporary"
    assert body["result"]["text"] == "available text"
    assert body["result"]["persistence_state"] == "failed"


def test_save_failure_does_not_count_as_durable(tmp_path, monkeypatch):
    """A save-failed task must not be counted as a result that can be revisited
    later, and must stay re-processable from its retained source."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    row = store.snapshot()["items"][0]
    # status=error so /retry can re-process the retained source (the handoff's
    # state table specifies error + history_save_failed for this case).
    assert row["status"] == "error"
    assert row["persistence_state"] == "failed"
    assert row["error"] == "history_save_failed"


def test_save_failed_item_can_be_reprocessed(tmp_path, monkeypatch):
    """The retained source must be reachable: a save failure is not terminal."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    client = make_client(tmp_path=tmp_path)
    r = client.post(f"/api/queue/items/{item['id']}/retry")
    assert r.status_code == 200, r.text
    assert store.get(item["id"])["status"] == "queued"


def test_concurrent_save_claims_produce_one_record(tmp_path, monkeypatch):
    """Two overlapping saves must not commit the same attempt twice."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    attempt = store.get(item["id"])["attempt_id"]

    calls = {"n": 0}
    release = threading.Event()

    def slow_save(payload):
        calls["n"] += 1
        release.wait(timeout=5)
        return {"id": "a" * 12}

    monkeypatch.setitem(qapi._deps, "save_result_fn", slow_save)

    async def run():
        first = asyncio.ensure_future(qapi.queue_item_save(item["id"], {"attempt_id": attempt}))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(qapi.queue_item_save(item["id"], {"attempt_id": attempt}))
        await asyncio.sleep(0.05)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(run())
    # Exactly one caller performed the save; the other is turned away.
    assert calls["n"] == 1
    statuses = sorted(getattr(r, "status_code", 200) for r in results)
    assert 409 in statuses, statuses


def test_save_without_usable_history_id_is_not_reported_saved(tmp_path, monkeypatch):
    """A save that returns no record must not mark the task durable."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    attempt = store.get(item["id"])["attempt_id"]
    monkeypatch.setitem(qapi._deps, "save_result_fn", lambda payload: {"id": None})
    client = make_client(tmp_path=tmp_path)
    r = client.post(f"/api/queue/items/{item['id']}/save", json={"attempt_id": attempt})
    assert r.status_code == 500
    row = store.get(item["id"])
    assert row["persistence_state"] == "failed", "must not claim durable success without a record"
    assert row["history_id"] is None
    # The claim is released, so a later retry is still possible.
    assert store.try_claim_save(item["id"], attempt) == "claimed"


def test_save_failure_survives_recovery_bookkeeping_errors(tmp_path, monkeypatch):
    """A failing pending-store write must not mask the transcription result."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)

    class ExplodingStore:
        def put(self, **_kwargs):
            raise OSError("disk gone")

        def get(self, *_a, **_k):
            return None

    monkeypatch.setattr(qapi, "pending", lambda: ExplodingStore())
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    assert asyncio.run(qapi.process_next_once()) is True
    row = store.get(item["id"])
    assert row["error"] == "history_save_failed"
    # The session copy is the authority: the flags follow readability, and the
    # text is actually fetchable through the public endpoint.
    assert row["temporary_result_available"] is True
    assert row["can_retry_save"] is True
    client = make_client(tmp_path=tmp_path)
    response = client.get(f"/api/queue/items/{item['id']}/result")
    assert response.status_code == 200
    assert response.json()["result"]["text"] == "available text"


def test_retry_save_is_idempotent_and_never_reruns_asr(tmp_path, monkeypatch):
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    attempt = store.get(item["id"])["attempt_id"]
    assert attempt

    calls = {"n": 0}

    def fake_save(payload):
        calls["n"] += 1
        assert payload["text"] == "available text"
        return {"id": "a" * 12}

    monkeypatch.setitem(qapi._deps, "save_result_fn", fake_save)
    client = make_client(tmp_path=tmp_path)
    url = f"/api/queue/items/{item['id']}/save"
    first = client.post(url, json={"attempt_id": attempt})
    assert first.status_code == 200, first.text
    hid = first.json()["history_id"]
    assert first.json()["already_saved"] is False

    second = client.post(url, json={"attempt_id": attempt})
    assert second.status_code == 200
    assert second.json()["history_id"] == hid
    assert second.json()["already_saved"] is True
    # The retry is a re-commit, not a re-recognition.
    assert calls["n"] == 1

    row = store.get(item["id"])
    assert row["history_id"] == hid
    assert row["persistence_state"] == "saved"
    assert row["temporary_result_available"] is False
    assert not (store.uploads_dir / "x.wav").exists()


def test_retry_save_rejects_a_stale_attempt(tmp_path, monkeypatch):
    """A late save from an older run must not overwrite the current result."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    current_attempt = store.get(item["id"])["attempt_id"]

    monkeypatch.setitem(qapi._deps, "save_result_fn", lambda payload: {"id": "b" * 12})
    client = make_client(tmp_path=tmp_path)
    stale = client.post(f"/api/queue/items/{item['id']}/save", json={"attempt_id": "f" * 12})
    assert stale.status_code == 409
    # The real attempt still saves fine.
    ok = client.post(f"/api/queue/items/{item['id']}/save", json={"attempt_id": current_attempt})
    assert ok.status_code == 200
    assert ok.json()["history_id"] == "b" * 12


def test_retry_save_requires_attempt_id(tmp_path, monkeypatch):
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    client = make_client(tmp_path=tmp_path)
    r = client.post(f"/api/queue/items/{item['id']}/save", json={})
    assert r.status_code == 422


def test_retry_save_without_payload_reports_unavailable(tmp_path):
    store = _client(tmp_path)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    store.set_persistence(item["id"], state="failed", attempt_id="c" * 12, temporary=True, can_retry_save=True)
    client = make_client(tmp_path=tmp_path)
    r = client.post(f"/api/queue/items/{item['id']}/save", json={"attempt_id": "c" * 12})
    assert r.status_code == 410


def test_cancelled_save_failure_keeps_partial_readable(tmp_path, monkeypatch):
    store = _client(tmp_path)

    async def cancelled(**_kwargs):
        return {"history_id": None, "text": "partial text", "segments": [], "stats": {}, "cancelled": True}

    monkeypatch.setitem(qapi._deps, "transcribe_fn", cancelled)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    row = store.get(item["id"])
    assert row["status"] == "cancelled"
    assert row["persistence_state"] == "failed"
    assert row["temporary_result_available"] is True
    client = make_client(tmp_path=tmp_path)
    r = client.get(f"/api/queue/items/{item['id']}/result")
    assert r.status_code == 200
    assert r.json()["result"]["text"] == "partial text"


# --- A04.4: crash window --------------------------------------------------

def test_restart_reassociates_a_committed_attempt(tmp_path, monkeypatch):
    """History committed but the queue update was lost: the attempt must not
    be transcribed a second time."""
    store = _client(tmp_path)
    history = HistoryStore(tmp_path / "history")
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    attempt = store.get(item["id"])["attempt_id"]

    # Simulate the crash window: the record exists, the queue never heard about it.
    record = history.save(
        filename="x.wav", text="available text", segments=[], stats={},
        task_id=item["id"], attempt_id=attempt, result_version=1,
    )
    monkeypatch.setitem(qapi._deps, "find_history_by_attempt_fn", history.find_by_attempt)
    assert qapi.reconcile_attempts() == 1
    row = store.get(item["id"])
    assert row["history_id"] == record["id"]
    assert row["persistence_state"] == "saved"
    assert not (store.uploads_dir / "x.wav").exists()
    # Idempotent: a second pass finds nothing to fix.
    assert qapi.reconcile_attempts() == 0


# --- A05: result versions -------------------------------------------------

def test_reserved_version_is_reused_by_the_save_not_renumbered(tmp_path):
    """The worker reserves a number once per attempt; the save (and any save
    retry) carries it, and only a genuinely new attempt takes the next one."""
    history = HistoryStore(tmp_path / "history")
    task = "a" * 12
    first = history.reserve_version(task)
    assert first == 1
    record = history.save(
        filename="a.wav", text="a", segments=[], stats={},
        task_id=task, result_version=first, attempt_id="b" * 12,
    )
    assert record["result_version"] == 1
    # A save retry reuses the reserved number rather than taking a new one.
    retry = history.save(
        filename="a.wav", text="a", segments=[], stats={},
        task_id=task, result_version=first, attempt_id="b" * 12,
    )
    assert retry["result_version"] == 1
    # A new attempt advances.
    assert history.reserve_version(task) == 2


def test_explicit_version_is_honoured_and_not_renumbered(tmp_path):
    history = HistoryStore(tmp_path / "history")
    task = "a" * 12
    history.reserve_version(task)
    record = history.save(
        filename="a.wav", text="a", segments=[], stats={},
        task_id=task, result_version=7, attempt_id="e" * 12,
    )
    assert record["result_version"] == 7
    assert record["attempt_id"] == "e" * 12
    assert history.get(record["id"])["result_version"] == 7


def test_same_second_versions_keep_latest_after_view_and_mtime(tmp_path, monkeypatch):
    """Viewing v1 (which rewrites its mtime) must not make it the batch's
    current result, and the ordering must not depend on file traversal."""
    import backend.history as mod
    import time
    monkeypatch.setattr(mod, "_now_iso", lambda: "2026-10-30T00:00:00+00:00")
    history = HistoryStore(tmp_path / "history")
    task = "1" * 12
    v1 = _save(history, task_id=task, batch_id="aabbccddeeff")
    v2 = _save(history, task_id=task, batch_id="aabbccddeeff")
    assert v2["result_version"] == 2
    time.sleep(0.05)
    history.update_view(v1["id"], 1)
    # Touch the older record so mtime ordering would prefer it.
    path = history._path(v1["id"])
    future = time.time() + 500
    os.utime(path, (future, future))
    batch = history.batches()
    assert len(batch[0]["items"]) == 1
    assert batch[0]["items"][0]["history_id"] == v2["id"]
    assert batch[0]["items"][0]["result_version"] == 2
    # A fresh store over the same directory must reach the same conclusion.
    assert HistoryStore(tmp_path / "history").batches()[0]["items"][0]["history_id"] == v2["id"]


def test_legacy_records_without_task_id_stay_independent(tmp_path):
    """No filename-based guessing: two legacy records are two rows."""
    history = HistoryStore(tmp_path / "history")
    a = history.save(filename="same.wav", text="a", segments=[], stats={})
    b = history.save(filename="same.wav", text="b", segments=[], stats={})
    batch = next(g for g in history.batches() if g["id"] == "legacy-history")
    assert len(batch["items"]) == 2
    assert {i["history_id"] for i in batch["items"]} == {a["id"], b["id"]}


def test_viewing_v1_does_not_mark_v2_viewed(tmp_path):
    history = HistoryStore(tmp_path / "history")
    task = "1" * 12
    v1 = _save(history, task_id=task, batch_id="aabbccddeeff")
    v2 = _save(history, task_id=task, batch_id="aabbccddeeff")
    history.update_view(v1["id"], 3)
    assert history.get(v1["id"])["first_viewed_at"]
    assert history.get(v2["id"])["first_viewed_at"] is None


# --- A11: timing reporting ------------------------------------------------

def test_timing_reports_no_segments_honestly():
    from backend.history import derive_timing_info
    info = derive_timing_info(segments=[], align_used=False, alignment_requested=False, stats={})
    assert info["level"] == "none"
    assert info["alignment_requested"] is False
    assert info["fallback_reason"] == "alignment_not_requested"


def test_timing_requested_but_failed_is_not_called_segment():
    from backend.history import derive_timing_info
    info = derive_timing_info(segments=[], align_used=False, alignment_requested=True, stats={})
    assert info["level"] == "none"
    assert info["fallback_reason"] in ("alignment_failed", "alignment_unavailable")


def test_timing_after_resegment_is_still_segment_level():
    """Forced alignment that ends in resegment_words yields sentence segments;
    claiming word level would overstate the stored data."""
    from backend.history import derive_timing_info
    segments = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 1.0, "end": 2.5, "text": "b"}]
    info = derive_timing_info(segments=segments, align_used=True, alignment_requested=True, stats={"word_count": 4})
    assert info["level"] == "segment"
    assert info["source"] == "forced_alignment"
    assert info["alignment_succeeded"] is True
    assert info["fallback_reason"] is None


def test_unordered_segments_are_not_reported_as_usable():
    from backend.history import derive_timing_info
    segments = [{"start": 5.0, "end": 6.0, "text": "a"}, {"start": 1.0, "end": 2.0, "text": "b"}]
    assert derive_timing_info(segments=segments, align_used=True, alignment_requested=True, stats={})["level"] == "none"


def test_search_reports_match_fields(tmp_path):
    """The reader needs to know *where* the hit came from: a title hit must not
    filter the body by a word the body does not contain."""
    history = HistoryStore(tmp_path / "history")
    _save(history, filename="历史验收记录-000.wav", text="完全不同的正文内容", segments=[], stats={})
    by_title = history.search(query="历史验收记录-000")
    assert by_title["total"] == 1
    assert by_title["items"][0]["match_fields"][0] == "title"
    assert "title" in by_title["items"][0]["match_fields"]
    assert by_title["items"][0]["first_match"]["field"] == "title"
    by_body = history.search(query="完全不同")
    assert by_body["items"][0]["match_fields"] == []
    assert by_body["items"][0]["first_match"]["field"] == "text"
    # A query matching neither field nor body returns nothing.
    assert history.search(query="不存在的词")["total"] == 0


# --- A07: independent reading and playback positions ----------------------

def test_reader_position_does_not_overwrite_playback(tmp_path):
    history = HistoryStore(tmp_path / "history")
    record = _save(history, segments=[{"start": 0, "end": 1, "text": "a"}, {"start": 1, "end": 2, "text": "b"}])
    history.update_view(record["id"], 5.0)
    history.update_view(record["id"], None, reader_position={"segment_index": 3, "scroll_ratio": 0.4})
    got = history.get(record["id"])
    assert got["playback_position"] == 5.0
    assert got["reader_position"]["segment_index"] == 3
    # Scrolling must not clobber the audio position.
    history.update_view(record["id"], None, reader_position={"scroll_ratio": 0.9})
    got = history.get(record["id"])
    assert got["playback_position"] == 5.0
    assert got["reader_position"]["scroll_ratio"] == 0.9


def test_reader_position_validation(tmp_path):
    history = HistoryStore(tmp_path / "history")
    record = _save(history)
    assert history.update_view(record["id"], None, reader_position={"segment_index": -4})["reader_position"]["segment_index"] == 0
    assert history.update_view(record["id"], None, reader_position={"scroll_ratio": 2})["reader_position"]["scroll_ratio"] == 1
    # An unusable object is rejected without corrupting the record.
    assert history.update_view(record["id"], None, reader_position={}) is None
    assert history.update_view(record["id"], None, reader_position="nope") is None


def test_legacy_position_field_is_not_mistaken_for_a_reader_anchor(tmp_path):
    history = HistoryStore(tmp_path / "history")
    record = _save(history)
    raw = json.loads(history._path(record["id"]).read_text(encoding="utf-8"))
    raw["position"] = 7.5
    raw.pop("read_position", None)
    history._path(record["id"]).write_text(json.dumps(raw), encoding="utf-8")
    got = history.get(record["id"])
    assert got["reader_position"] is None
    assert got["playback_position"] == 0.0


# --- pending store ---------------------------------------------------------

def test_pending_store_reports_when_disk_copy_is_unavailable(tmp_path):
    store = PendingResultStore(tmp_path / "pending", max_total_bytes=1)
    info = store.put(task_id="a" * 12, attempt_id="b" * 12, payload={"text": "x"})
    assert info["persisted"] is False
    # The session copy is still readable — losing it would be worse.
    assert store.get("a" * 12, "b" * 12)["text"] == "x"


def test_pending_store_round_trips_and_removes(tmp_path):
    store = PendingResultStore(tmp_path / "pending")
    store.put(task_id="a" * 12, attempt_id="b" * 12, payload={"text": "hello", "result_version": 2})
    assert store.get("a" * 12, "b" * 12)["text"] == "hello"
    assert store.latest_for_task("a" * 12)["result_version"] == 2
    assert store.remove("a" * 12, "b" * 12) is True
    assert store.get("a" * 12, "b" * 12) is None


def test_pending_store_rejects_invalid_ids(tmp_path):
    store = PendingResultStore(tmp_path / "pending")
    assert store.put(task_id="../escape", attempt_id="b" * 12, payload={})["persisted"] is False
    assert store.get("../escape", "b" * 12) is None
    assert store.latest_for_task("not-an-id") is None


def test_pending_store_restores_across_instances(tmp_path):
    root = tmp_path / "pending"
    PendingResultStore(root).put(task_id="a" * 12, attempt_id="b" * 12, payload={"text": "survived"})
    assert PendingResultStore(root).get("a" * 12, "b" * 12)["text"] == "survived"


def test_pending_store_accounts_bytes_on_replace_and_remove(tmp_path):
    """Replacing a payload must not double-count it; removal must free it."""
    store = PendingResultStore(tmp_path / "pending")
    store.put(task_id="a" * 12, attempt_id="b" * 12, payload={"text": "x" * 200})
    first = store.total_bytes()
    assert first > 0
    store.put(task_id="a" * 12, attempt_id="b" * 12, payload={"text": "y"})
    assert store.total_bytes() < first, "replacement leaked the old byte count"
    store.remove("a" * 12, "b" * 12)
    assert store.total_bytes() == 0, "removal did not release the byte count"


def test_restart_releases_a_stale_saving_claim(tmp_path):
    """A save claim belongs to the process that took it.  After a restart the
    persisted "saving" marker must not block every later save forever."""
    store = QueueStore(tmp_path / "queue")
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    store.begin_attempt(item["id"])
    attempt = store.get(item["id"])["attempt_id"]
    store.set_persistence(item["id"], state="failed", attempt_id=attempt)
    assert store.try_claim_save(item["id"], attempt) == "claimed"
    restarted = QueueStore(tmp_path / "queue")
    assert restarted.get(item["id"])["persistence_state"] != "saving"
    assert restarted.try_claim_save(item["id"], attempt) == "claimed"


# --- history durability ----------------------------------------------------

def test_history_write_leaves_no_temp_files(tmp_path):
    history = HistoryStore(tmp_path / "history")
    record = _save(history)
    leftovers = [p.name for p in (tmp_path / "history").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert history.get(record["id"])["text"] == "梧桐计划"


def test_unreadable_history_is_skipped_not_fatal(tmp_path):
    history = HistoryStore(tmp_path / "history")
    good = _save(history)
    (tmp_path / "history" / "deadbeef1234.json").write_text("{not json", encoding="utf-8")
    assert history.get(good["id"])["text"] == "梧桐计划"
    assert history.search()["total"] == 1


def test_attempt_id_survives_a_queue_rewrite(tmp_path):
    """The attempt identity must be persisted with the queue item so a save
    retry after a restart still targets the same run."""
    store = QueueStore(tmp_path / "queue")
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    begun = store.begin_attempt(item["id"])
    assert begun["attempt_id"]
    assert begun["persistence_state"] == "pending"
    reloaded = QueueStore(tmp_path / "queue").get(item["id"])
    assert reloaded["attempt_id"] == begun["attempt_id"]


def test_clear_finished_preserves_save_failed_items(tmp_path):
    """A save-failed task carries status=done, but its text exists only as a
    recoverable payload.  "Collapse finished tasks" must not destroy it."""
    store = QueueStore(tmp_path / "queue")
    for state in ("done", "done", "error", "cancelled"):
        store.add(qid=store.new_id(), filename=f"{state}.wav", staged_name=store.new_id() + ".wav",
                  size=1, align=False)
    items = store.snapshot()["items"]
    # items[0]: genuinely saved.  items[1]: save failed, still status=done.
    store.set_status(items[0]["id"], "done", history_id="a" * 12)
    store.set_persistence(items[0]["id"], state="saved", history_id="a" * 12)
    store.set_status(items[1]["id"], "done", error="history_save_failed", clear_history=True)
    store.set_persistence(items[1]["id"], state="failed", temporary=True, can_retry_save=True,
                          save_error="history_save_failed")
    cleared = store.clear_finished()
    assert cleared == 1, "only the genuinely saved done item is collapsed"
    remaining = {i["id"] for i in store.snapshot()["items"]}
    assert items[1]["id"] in remaining, "save-failed item was dropped"
    assert items[0]["id"] not in remaining


def test_detach_history_resets_persistence_flags(tmp_path):
    """Deleting a history record must not leave the row claiming it is saved."""
    store = QueueStore(tmp_path / "queue")
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    store.set_status(item["id"], "done", history_id="d" * 12)
    store.set_persistence(item["id"], state="saved", history_id="d" * 12, attempt_id="e" * 12)
    assert store.detach_history("d" * 12) == 1
    row = QueueStore(tmp_path / "queue").get(item["id"])
    assert row["history_id"] is None
    assert row["persistence_state"] == "unavailable"
    assert row["temporary_result_available"] is False
    assert row["can_retry_save"] is False
    assert row["save_error"] is None


def test_delete_reaps_the_pending_payload(tmp_path, monkeypatch):
    """Removing a queue item must also drop its recoverable payload, or it
    leaks in memory and on disk and is restored as an orphan after restart."""
    store = _client(tmp_path)
    monkeypatch.setitem(qapi._deps, "transcribe_fn", _failing_transcribe)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    (store.uploads_dir / "x.wav").write_bytes(b"x")
    asyncio.run(qapi.process_next_once())
    attempt = store.get(item["id"])["attempt_id"]
    assert qapi.pending().get(item["id"], attempt) is not None

    client = make_client(tmp_path=tmp_path)
    r = client.delete(f"/api/queue/items/{item['id']}")
    assert r.status_code == 200, r.text
    assert store.get(item["id"]) is None
    assert qapi.pending().get(item["id"], attempt) is None
    remaining = [row["task_id"] for row in qapi.pending().list_recoverable()]
    assert item["id"] not in remaining
    # A fresh store must not resurrect the orphan.
    assert not [p for p in (tmp_path / "pending_results").glob("*.json")]


def test_requeue_clears_the_previous_attempt(tmp_path):
    """A retry is a new execution and must not inherit the old attempt id."""
    store = QueueStore(tmp_path / "queue")
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    first = store.begin_attempt(item["id"])["attempt_id"]
    store.set_status(item["id"], "queued")
    assert store.get(item["id"])["attempt_id"] is None
    assert store.get(item["id"])["persistence_state"] == "unavailable"
    assert store.begin_attempt(item["id"])["attempt_id"] != first


def test_reserve_version_reports_a_write_failure(tmp_path, monkeypatch):
    """A reservation that was not written is not a reservation: the worker
    must stop rather than run ASR without a reliable version slot."""
    import backend.history as history_mod
    history = HistoryStore(tmp_path / "history")

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(history_mod.os, "replace", fail)
    with pytest.raises(OSError):
        history.reserve_version("a" * 12)


def test_delete_is_rejected_while_a_save_owns_the_attempt(tmp_path):
    store = _client(tmp_path)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    staged = store.uploads_dir / "x.wav"
    staged.write_bytes(b"x")
    store.begin_attempt(item["id"])
    attempt = store.get(item["id"])["attempt_id"]
    store.set_persistence(item["id"], state="failed", attempt_id=attempt)
    assert store.try_claim_save(item["id"], attempt) == "claimed"
    client = make_client(tmp_path=tmp_path)
    response = client.delete(f"/api/queue/items/{item['id']}")
    assert response.status_code == 409
    assert store.get(item["id"]) is not None
    assert staged.is_file()


def test_reconcile_clears_capabilities_without_a_readable_payload(tmp_path):
    """After losing the session copy, the row must stop advertising a readable
    temporary result and a retry-save action that cannot work."""
    store = _client(tmp_path)
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    store.begin_attempt(item["id"])
    attempt = store.get(item["id"])["attempt_id"]
    store.set_persistence(
        item["id"], state="failed", attempt_id=attempt,
        temporary=True, can_retry_save=True, save_error="history_save_failed",
    )
    qapi.reconcile_attempts()
    row = store.get(item["id"])
    assert row["persistence_state"] == "unavailable"
    assert row["temporary_result_available"] is False
    assert row["can_retry_save"] is False
    assert row["save_error"] is None


def test_reconcile_keeps_cancelled_status_and_source(tmp_path, monkeypatch):
    """A committed cancelled attempt is still a cancelled task: keep both its
    status and its source so it can be re-processed from scratch."""
    store = _client(tmp_path)
    history = HistoryStore(tmp_path / "history")
    item = store.add(qid=store.new_id(), filename="x.wav", staged_name="x.wav", size=1, align=False)
    staged = store.uploads_dir / "x.wav"
    staged.write_bytes(b"x")
    store.begin_attempt(item["id"])
    attempt = store.get(item["id"])["attempt_id"]
    store.set_status(item["id"], "cancelled")
    store.set_persistence(item["id"], state="failed", attempt_id=attempt, integrity="partial")
    record = history.save(
        filename="x.wav", text="partial", segments=[], stats={"cancelled": True},
        task_id=item["id"], attempt_id=attempt, result_version=1,
    )
    monkeypatch.setitem(qapi._deps, "find_history_by_attempt_fn", history.find_by_attempt)
    assert qapi.reconcile_attempts() == 1
    row = store.get(item["id"])
    assert row["status"] == "cancelled"
    assert row["history_id"] == record["id"]
    assert staged.is_file(), "the source must stay available for a re-run"
