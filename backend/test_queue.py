"""Tests for the batch transcription queue (queue_store + queue_api).

Follows the same patterns as test_main.py: make_client() for boot/mocks,
monkeypatched probe/split/chunk fakes instead of a real llama-server, and
the single-step ``process_next_once`` coroutine for deterministic worker
behavior (the background loop is never started under TestClient).
"""
import asyncio
import json

import backend.main as main_mod
import backend.queue_api as qapi
from backend.queue_store import QueueStore
from backend.test_main import make_client, _make_wav_bytes


def _fresh_store(tmp_path):
    """Swap the module-global store for a tmp-rooted one (deps stay wired)."""
    qapi.store = QueueStore(tmp_path / "queue")
    return qapi.store


def _enqueue_wavs(client, names, align=False):
    files = [("files", (n, _make_wav_bytes(0.1), "audio/wav")) for n in names]
    return client.post("/api/queue/items", files=files, data={"align": str(align).lower()})


# QueueStore unit tests


def test_store_add_and_persist(tmp_path):
    store = QueueStore(tmp_path / "queue")
    a = store.add(qid=store.new_id(), filename="a.wav", staged_name="x.wav",
                  size=10, align=False)
    store.add(qid=store.new_id(), filename="b.wav", staged_name="y.wav",
              size=20, align=True)
    snap = store.snapshot()
    assert [it["filename"] for it in snap["items"]] == ["a.wav", "b.wav"]
    assert snap["active_id"] is None

    # Reload from disk — items survive, ids validated.
    store2 = QueueStore(tmp_path / "queue")
    snap2 = store2.snapshot()
    assert [it["id"] for it in snap2["items"]] == [a["id"], snap["items"][1]["id"]]


def test_store_corrupted_json_recovers(tmp_path):
    root = tmp_path / "queue"
    root.mkdir(parents=True)
    (root / "queue.json").write_text("{not json!", encoding="utf-8")
    store = QueueStore(root)
    assert store.snapshot()["items"] == []
    # And the store is usable afterwards.
    store.add(qid=store.new_id(), filename="a.wav", staged_name="x.wav",
              size=1, align=False)
    assert len(store.snapshot()["items"]) == 1


def test_store_running_reset_on_load(tmp_path):
    store = QueueStore(tmp_path / "queue")
    it = store.add(qid=store.new_id(), filename="a.wav", staged_name="x.wav",
                   size=1, align=False)
    store.set_status(it["id"], "running")
    assert store.snapshot()["active_id"] == it["id"]

    store2 = QueueStore(tmp_path / "queue")
    item = store2.snapshot()["items"][0]
    assert item["status"] == "queued"
    assert store2.snapshot()["active_id"] is None


def test_store_reorder_only_queued(tmp_path):
    store = QueueStore(tmp_path / "queue")
    ids = []
    for n in ("a", "b", "c"):
        it = store.add(qid=store.new_id(), filename=f"{n}.wav",
                       staged_name=f"{n}.wav", size=1, align=False)
        ids.append(it["id"])
    store.set_status(ids[0], "running")

    # Only b and c are queued; reorder them reversed.
    assert store.reorder([ids[2], ids[1]]) is True
    names = [it["filename"] for it in store.snapshot()["items"]]
    assert names == ["a.wav", "c.wav", "b.wav"]  # running 'a' keeps its slot

    # Including the running id (not a queued permutation) is rejected.
    assert store.reorder([ids[0], ids[1], ids[2]]) is False


def test_store_sort_by(tmp_path):
    store = QueueStore(tmp_path / "queue")
    sizes = {"c.wav": 30, "a.wav": 10, "b.wav": 20}
    for name, size in sizes.items():
        store.add(qid=store.new_id(), filename=name, staged_name=name,
                  size=size, align=False)
    assert store.sort_by("name") is True
    assert [i["filename"] for i in store.snapshot()["items"]] == ["a.wav", "b.wav", "c.wav"]
    assert store.sort_by("size", desc=True) is True
    assert [i["size"] for i in store.snapshot()["items"]] == [30, 20, 10]
    assert store.sort_by("nope") is False


# API tests


def test_enqueue_rejects_unsupported_type(tmp_path):
    client = make_client(tmp_path=tmp_path)
    _fresh_store(tmp_path)
    r = client.post(
        "/api/queue/items",
        files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))],
        data={"align": "false"},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "unsupported_type"
    assert qapi.store.snapshot()["items"] == []


def test_enqueue_and_state(tmp_path):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    r = _enqueue_wavs(client, ["one.wav", "two.wav"], align=True)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all(it["status"] == "queued" and it["align"] for it in items)
    # Staged files were written.
    for it in items:
        assert (store.uploads_dir / it["staged_name"]).is_file()

    state = client.get("/api/queue").json()
    assert state["paused"] is False
    assert state["active_id"] is None
    assert [it["filename"] for it in state["items"]] == ["one.wav", "two.wav"]


def test_worker_success_consumes_and_persists_history(tmp_path, monkeypatch):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    _enqueue_wavs(client, ["clip.wav"])

    fake_chunks = [(tmp_path / "c0.wav", 0.0, 0.0)]
    fake_chunks[0][0].write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 1.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _s, _o, _c, _v, **_k: fake_chunks)

    async def fake_chunk(_client, _path, initial_prompt=None):
        return {"text": "ok", "segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    assert asyncio.run(qapi.process_next_once()) is True
    item = store.snapshot()["items"][0]
    assert item["status"] == "done"
    assert item["history_id"]
    # Staged upload cleaned up; history record exists.
    assert not (store.uploads_dir / item["staged_name"]).exists()
    hist = client.get("/api/history").json()["items"]
    assert any(h["id"] == item["history_id"] for h in hist)


def test_worker_backs_off_when_slot_busy(tmp_path):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    _enqueue_wavs(client, ["clip.wav"])

    assert main_mod._claim_job() is True  # simulate a manual job holding the slot
    try:
        assert asyncio.run(qapi.process_next_once()) is False
        assert store.snapshot()["items"][0]["status"] == "queued"
    finally:
        main_mod.set_job("idle")


def test_worker_marks_error_and_keeps_staged(tmp_path, monkeypatch):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    _enqueue_wavs(client, ["clip.wav"])

    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 1.0)

    def broken_split(*_a, **_k):
        raise RuntimeError("boom with C:\\secret\\path")

    monkeypatch.setattr(main_mod, "split_audio", broken_split)

    assert asyncio.run(qapi.process_next_once()) is True
    item = store.snapshot()["items"][0]
    assert item["status"] == "error"
    # Only the sanitized error code is persisted, never the detail/paths.
    assert item["error"] == "split_failed"
    # Staged file kept for retry.
    assert (store.uploads_dir / item["staged_name"]).is_file()


def test_delete_running_item_cancels_only_its_own_job(tmp_path):
    """A stale "running" flag must not cancel somebody else's job, but deleting
    an item that really does own the slot cancels it."""
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    qid = _enqueue_wavs(client, ["clip.wav"]).json()["items"][0]["id"]

    # No worker claimed the slot. This is the case that used to abort an
    # unrelated manual transcription.
    store.set_status(qid, "running")
    resp = client.delete(f"/api/queue/items/{qid}")
    assert resp.json() == {"ok": True, "skipping": True}
    assert main_mod._is_cancel_requested() is False

    # Same item, now genuinely holding the slot.
    main_mod._set_job_owner(qid)
    assert main_mod._claim_job() is True
    resp2 = client.delete(f"/api/queue/items/{qid}")
    assert resp2.json() == {"ok": True, "skipping": True}
    assert main_mod._is_cancel_requested() is True

    with main_mod.job_lock:
        main_mod._cancel_requested = False
        main_mod.job_state["status"] = "idle"
    main_mod._set_job_owner(None)


def test_request_cancel_scoped_to_another_owner_is_ignored():
    main_mod._set_job_owner("aaa111")
    assert main_mod._claim_job() is True
    try:
        assert main_mod._request_cancel("bbb222") is False
        assert main_mod._is_cancel_requested() is False
        # An unscoped cancel (POST /api/abort) always applies.
        assert main_mod._request_cancel() is True
        assert main_mod._is_cancel_requested() is True
    finally:
        with main_mod.job_lock:
            main_mod._cancel_requested = False
            main_mod.job_state["status"] = "idle"
        main_mod._set_job_owner(None)


def test_delete_queued_removes_item_and_staged(tmp_path):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    r = _enqueue_wavs(client, ["clip.wav"])
    item = r.json()["items"][0]

    resp = client.delete(f"/api/queue/items/{item['id']}")
    assert resp.json() == {"ok": True, "skipping": False}
    assert store.snapshot()["items"] == []
    assert not (store.uploads_dir / item["staged_name"]).exists()


def test_retry_only_from_terminal_failure(tmp_path):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    r = _enqueue_wavs(client, ["clip.wav"])
    qid = r.json()["items"][0]["id"]

    # queued item cannot be retried
    assert client.post(f"/api/queue/items/{qid}/retry").status_code == 409

    store.set_status(qid, "error", error="asr_error")
    assert client.post(f"/api/queue/items/{qid}/retry").json() == {"ok": True}
    item = store.snapshot()["items"][0]
    assert item["status"] == "queued"
    assert item["error"] is None


def test_pause_resume_and_clear_finished(tmp_path):
    client = make_client(tmp_path=tmp_path)
    store = _fresh_store(tmp_path)
    r = _enqueue_wavs(client, ["a.wav", "b.wav"])
    ids = [it["id"] for it in r.json()["items"]]

    assert client.post("/api/queue/pause").json()["paused"] is True
    assert store.is_paused() is True
    assert client.post("/api/queue/resume").json()["paused"] is False

    store.set_status(ids[0], "done", history_id="abcdefabcdef")
    assert client.delete("/api/queue/finished").json() == {"cleared": 1}
    remaining = store.snapshot()["items"]
    assert [it["id"] for it in remaining] == [ids[1]]


def test_reorder_endpoint_validates(tmp_path):
    client = make_client(tmp_path=tmp_path)
    _fresh_store(tmp_path)
    r = _enqueue_wavs(client, ["a.wav", "b.wav"])
    ids = [it["id"] for it in r.json()["items"]]

    ok = client.post("/api/queue/reorder", json={"ids": [ids[1], ids[0]]})
    assert ok.json() == {"ok": True}
    state = client.get("/api/queue").json()
    assert [it["id"] for it in state["items"]] == [ids[1], ids[0]]

    bad = client.post("/api/queue/reorder", json={"ids": [ids[0]]})
    assert bad.status_code == 409
    assert client.post("/api/queue/reorder", json={"ids": "nope"}).status_code == 422


def test_queue_state_version_increments(tmp_path):
    client = make_client(tmp_path=tmp_path)
    _fresh_store(tmp_path)
    v0 = client.get("/api/queue").json()["version"]
    _enqueue_wavs(client, ["a.wav"])
    v1 = client.get("/api/queue").json()["version"]
    assert v1 > v0
