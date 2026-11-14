"""The independent reviewer's 7 boundary checks, promoted into the suite.

These reproduce the P1 gaps found on 2026-10-31: session payload retention
without a recovery directory, save/re-process ownership, restart after an
interrupted save, cancelled-source retention, pending-budget backpressure,
and aborting recognition when the version reservation fails.

Originally docs/acceptance/gizmodo-v1-repair-review-20261031/checks_independent.py.
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import backend.main as main_mod
import backend.queue_api as qapi
from backend.pending_results import PendingResultStore
from backend.queue_store import QueueStore
from backend.test_main import make_client


def setup_failed(tmp_path, monkeypatch, *, cancelled=False, run=True):
    client = make_client(tmp_path=tmp_path)
    store = QueueStore(tmp_path / "queue")
    qapi.store = store
    item = store.add(qid=store.new_id(), filename="sample.wav",
                     staged_name="sample.wav", size=1, align=False)
    staged = store.uploads_dir / "sample.wav"
    staged.write_bytes(b"x")

    async def fake(**_kwargs):
        return {"text": "unique recoverable transcript", "segments": [],
                "history_id": None, "stats": {"audio_duration": 10, "cancelled": cancelled},
                "cancelled": cancelled}

    monkeypatch.setitem(qapi._deps, "transcribe_fn", fake)
    if run:
        assert asyncio.run(qapi.process_next_once()) is True
    return client, store, item, staged


def test_cancelled_result_still_reprocessable_after_save_retry(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch, cancelled=True)
    attempt = store.get(item["id"])["attempt_id"]
    r = client.post(f"/api/queue/items/{item['id']}/save", json={"attempt_id": attempt})
    assert r.status_code == 200, r.text
    rerun = client.post(f"/api/queue/items/{item['id']}/retry")
    assert staged.exists() and rerun.status_code == 200, {
        "source_exists": staged.exists(), "retry_status": rerun.status_code, "body": rerun.json(),
    }


def test_pending_directory_failure_keeps_readable_session_copy(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch, run=False)

    def unavailable():
        raise OSError("pending directory cannot be created")

    with monkeypatch.context() as m:
        m.setattr(qapi, "pending", unavailable)
        assert asyncio.run(qapi.process_next_once()) is True
    r = client.get(f"/api/queue/items/{item['id']}/result")
    assert r.status_code == 200 and r.json()["result"]["text"], {
        "flags": store.get(item["id"]), "read_status": r.status_code, "read_body": r.json(),
    }


def test_save_retry_blocks_reprocessing_until_claim_is_resolved(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch)
    attempt = store.get(item["id"])["attempt_id"]
    entered, release = threading.Event(), threading.Event()

    def slow_save(payload):
        entered.set()
        assert release.wait(5)
        return main_mod._save_result_payload(payload)

    monkeypatch.setitem(qapi._deps, "save_result_fn", slow_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post, f"/api/queue/items/{item['id']}/save",
            json={"attempt_id": attempt},
        )
        assert entered.wait(5)
        rerun = client.post(f"/api/queue/items/{item['id']}/retry")
        release.set()
        saved = future.result(timeout=5)
    assert rerun.status_code == 409, {
        "rerun_status": rerun.status_code, "save_status": saved.status_code,
        "final_queue": store.get(item["id"]), "source_exists": staged.exists(),
    }


def test_cancelled_queue_write_failure_keeps_partial_payload(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch, cancelled=True, run=False)
    original = store._save_locked

    def fail_cancel_status():
        if store._state["items"][0]["status"] == "cancelled":
            raise OSError("queue disk write failed")
        return original()

    with monkeypatch.context() as m:
        m.setattr(store, "_save_locked", fail_cancel_status)
        try:
            asyncio.run(qapi.process_next_once())
        except OSError:
            pass
    r = client.get(f"/api/queue/items/{item['id']}/result")
    assert r.status_code == 200 and r.json()["result"]["text"], {
        "read_status": r.status_code, "flags": store.get(item["id"]),
    }


def test_pending_budget_exhaustion_pauses_following_work(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch, run=False)
    p = PendingResultStore(tmp_path / "pending_results", max_total_bytes=1)
    monkeypatch.setattr(qapi, "pending", lambda: p)
    assert asyncio.run(qapi.process_next_once()) is True
    assert store.is_paused(), {"paused": store.is_paused(), "memory_bytes": p.total_bytes()}


def test_failed_version_reservation_does_not_start_recognition(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch, run=False)
    calls = []

    async def recognize(**_kwargs):
        calls.append("ASR")
        return {"history_id": None, "text": "text", "segments": [], "stats": {}}

    def reserve(task):
        raise OSError("version reservation disk full")

    monkeypatch.setitem(qapi._deps, "transcribe_fn", recognize)
    monkeypatch.setitem(qapi._deps, "reserve_version_fn", reserve)
    asyncio.run(qapi.process_next_once())
    assert calls == [], {"calls": calls, "queue": store.get(item["id"])}


def test_interrupted_save_can_be_retried_after_restart(tmp_path, monkeypatch):
    client, store, item, staged = setup_failed(tmp_path, monkeypatch)
    attempt = store.get(item["id"])["attempt_id"]
    assert store.try_claim_save(item["id"], attempt) == "claimed"
    # Crash before history commit: no process still owns the persisted claim.
    restarted = QueueStore(tmp_path / "queue")
    qapi.store = restarted
    qapi.reconcile_attempts()
    r = client.post(f"/api/queue/items/{item['id']}/save", json={"attempt_id": attempt})
    assert r.status_code == 200, {
        "save_status": r.status_code, "body": r.json(), "state": restarted.get(item["id"]),
    }
