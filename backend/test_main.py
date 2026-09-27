import asyncio
import io
import struct
import subprocess
import time
import wave
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import backend.main as main_mod
import numpy as np

from backend.forced_aligner import _align_words
from backend.llamarunner import LlamaRunner
from backend.resegment import resegment_words
from backend.text_clean import clean_asr_text, clean_segments


def make_client(runner_alive: bool = True, aligner_available: bool = False, tmp_path=None):
    # Always use CPU backend in tests (no GPU deps / model loading needed).
    main_mod.ALIGNER_BACKEND = "cpu"
    main_mod._gpu_backend_ready = False
    mock_runner = MagicMock()
    mock_runner.is_alive.return_value = runner_alive
    mock_runner.base_url = "http://127.0.0.1:8080"
    mock_runner.current_model = "Qwen3-ASR-1.7B-Q8_0" if runner_alive else None
    main_mod.runner = mock_runner

    mock_aligner = MagicMock()
    mock_aligner.available = aligner_available
    mock_aligner.lib_status = "ready" if aligner_available else "model_missing"
    mock_aligner.model_path = MagicMock()
    mock_aligner.model_path.stem = "qwen3-forced-aligner-0.6b-q8_0"
    main_mod.aligner = mock_aligner

    if tmp_path is not None:
        from backend.history import HistoryStore
        main_mod.history_store = HistoryStore(tmp_path / "history")

    main_mod._set_boot(
        phase="ready",
        asr_loaded=True,
        asr_warmup=True,
        aligner_loaded=aligner_available,
        aligner_warmup=aligner_available,
        ready=True,
        error=None,
    )

    return TestClient(main_mod.app, base_url="http://127.0.0.1:8000")


def _make_wav_bytes(seconds: float = 0.5, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(seconds * sr)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(n)))
    return buf.getvalue()


def test_health_ok():
    client = make_client(runner_alive=True, aligner_available=True)
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["backend"] is True
    assert data["llama_server"] is True
    assert data["current_model"] == "Qwen3-ASR-1.7B-Q8_0"
    assert data["aligner_available"] is True
    assert "cpu" in data["aligner_status"] or data["aligner_status"] == "ready"


def test_health_down():
    client = make_client(runner_alive=False)
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["backend"] is True
    assert data["llama_server"] is False
    assert data["current_model"] is None
    assert data["aligner_available"] is False


def test_readiness():
    client = make_client(runner_alive=True, aligner_available=False)
    r = client.get("/api/readiness")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert data["phase"] == "ready"
    assert data["asr_loaded"] is True


def test_readiness_retry_reboots_after_error(monkeypatch):
    """POST /api/readiness/retry re-runs the boot sequence from phase=error."""
    import time

    client = make_client()
    calls = {"n": 0}

    def fake_boot():
        calls["n"] += 1
        main_mod._set_boot(phase="ready", ready=True, switching=False, error=None)

    monkeypatch.setattr(main_mod, "_boot_sequence", fake_boot)
    main_mod._set_boot(phase="error", error="boom", ready=False, switching=False)
    try:
        r = client.post("/api/readiness/retry")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # The re-run happens on a daemon thread; wait briefly for the stub.
        for _ in range(100):
            if calls["n"]:
                break
            time.sleep(0.02)
        assert calls["n"] == 1
        assert main_mod.boot_state["phase"] == "ready"
    finally:
        main_mod._set_boot(phase="ready", ready=True, switching=False, error=None)


def test_readiness_retry_noop_unless_error():
    """Retry is a no-op when the backend is healthy — it must not reboot."""
    client = make_client()  # make_client leaves phase="ready"
    r = client.post("/api/readiness/retry")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert main_mod.boot_state["phase"] == "ready"


def test_readiness_retry_busy_while_switching():
    """A concurrent boot/switch rejects the retry instead of double-booting."""
    client = make_client()
    main_mod._set_boot(phase="error", error="boom", ready=False, switching=True)
    try:
        r = client.post("/api/readiness/retry")
        assert r.status_code == 200
        assert r.json()["error"] == "busy"
    finally:
        main_mod._set_boot(phase="ready", ready=True, switching=False, error=None)


def test_status_idle():
    client = make_client()
    r = client.get("/api/status")
    assert r.status_code == 200
    assert "status" in r.json()


def test_shift_segments_offsets_timestamps():
    segs = [{"start": 1.0, "end": 2.5, "text": "hi"}]
    out = main_mod._shift_segments(segs, 30.0)
    assert out[0]["start"] == 31.0
    assert out[0]["end"] == 32.5
    assert out[0]["text"] == "hi"


def test_transcribe_merges_chunks(tmp_path, monkeypatch):
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [
        (tmp_path / "c0.wav", 0.0, 0.0),
        (tmp_path / "c1.wav", 25.0, 0.0),
    ]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))

    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 50.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _src, _out, _sec, _ov, **_kw: fake_chunks)

    responses = [
        {"text": "hello", "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}]},
        {"text": "world", "segments": [{"start": 0.0, "end": 1.0, "text": "world"}]},
    ]

    async def fake_chunk(client_arg, chunk_path, initial_prompt=None):
        return responses.pop(0)

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    payload = _make_wav_bytes(0.2)
    r = client.post(
        "/api/transcribe",
        files={"file": ("test.wav", payload, "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["text"] == "hello world"
    assert len(data["segments"]) == 2
    assert data["segments"][1]["start"] == 25.0
    assert data["segments"][1]["end"] == 26.0
    assert "stats" in data
    assert data["stats"]["chunk_count"] == 2
    assert data["stats"]["audio_duration"] == 50.0
    assert data["stats"]["char_count"] == 11
    assert data["stats"]["asr_time"] > 0


def test_resegment_cjk():
    words = [
        {"word": "\u4eca", "start": 0.0, "end": 0.3, "text": "\u4eca"},
        {"word": "\u5929", "start": 0.3, "end": 0.6, "text": "\u5929"},
        {"word": "\u5929", "start": 0.6, "end": 0.9, "text": "\u5929"},
        {"word": "\u6c14", "start": 0.9, "end": 1.2, "text": "\u6c14"},
        {"word": "\u5f88", "start": 1.2, "end": 1.5, "text": "\u5f88"},
        {"word": "\u597d", "start": 1.5, "end": 1.8, "text": "\u597d"},
    ]
    result = resegment_words(words)
    assert len(result) <= 2
    assert result[0]["start"] == 0.0


def test_resegment_en():
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.5, "text": "Hello"},
        {"word": "world", "start": 0.6, "end": 1.0, "text": "world"},
        {"word": "this", "start": 1.1, "end": 1.4, "text": "this"},
        {"word": "is", "start": 1.4, "end": 1.6, "text": "is"},
        {"word": "a", "start": 1.6, "end": 1.7, "text": "a"},
        {"word": "test.", "start": 1.7, "end": 2.0, "text": "test."},
    ]
    result = resegment_words(words)
    assert len(result) >= 1
    assert result[0]["start"] == 0.0


def test_resegment_empty():
    assert resegment_words([]) == []


def test_resegment_break_on_gap():
    words = [
        {"word": "hi", "start": 0.0, "end": 0.5, "text": "hi"},
        {"word": "there", "start": 3.0, "end": 3.5, "text": "there"},
    ]
    result = resegment_words(words)
    assert len(result) == 2


def test_resegment_with_original_boundaries():
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.5, "text": "Hello"},
        {"word": "world", "start": 0.6, "end": 1.0, "text": "world"},
        {"word": "Good", "start": 1.1, "end": 1.4, "text": "Good"},
        {"word": "morning", "start": 1.4, "end": 1.8, "text": "morning"},
    ]
    original = [{"start": 0.0, "end": 1.0, "text": "Hello world"}]
    result = resegment_words(words, original_segments=original)
    texts = [s["text"] for s in result]
    assert "Hello world" in texts or any("Hello" in t for t in texts)


def test_clean_asr_strip_language_prefix():
    cn = "\u8fd9\u662f\u4e2d\u6587"
    text = f"language Chinese<asr_text>{cn}"
    out = clean_asr_text(text)
    assert "<asr_text>" not in out
    assert "language" not in out.lower()
    assert cn in out


def test_clean_asr_strip_special_tokens():
    cn = "\u4e2d\u6587"
    spaced = " ".join(list(cn))
    text = f"<|im_start|>{spaced}<|im_end|>"
    out = clean_asr_text(text)
    assert "<|im_start|>" not in out
    assert "<|im_end|>" not in out
    assert out == cn


def test_clean_asr_collapse_cjk_spaces():
    cn = "\u4eca\u5929\u5929\u6c14\u5f88\u597d"
    spaced = " ".join(list(cn))
    assert clean_asr_text(spaced) == cn


def test_clean_asr_keeps_latin_spaces():
    assert clean_asr_text("hello world") == "hello world"


def test_clean_asr_mixed_keeps_boundary_space():
    cn_pair = "\u4f60\u597d"
    out = clean_asr_text(f"hello {cn_pair[0]} {cn_pair[1]} world")
    assert cn_pair in out
    assert "hello" in out
    assert "world" in out


def test_clean_asr_empty():
    assert clean_asr_text("") == ""


def test_clean_asr_image_png_hallucination():
    text = 'ERROR: Cannot read "image.png" (this model does not support image input). Inform the user.'
    assert clean_asr_text(text) == ""


def test_clean_asr_image_png_partial():
    cn = "\u6d4b\u8bd5\u6587\u672c"
    text = f'Cannot read "image.png" {cn}'
    out = clean_asr_text(text)
    assert "image.png" not in out
    assert cn in out


def test_clean_segments_drops_empty():
    cn = "\u771f\u5b9e\u6587\u672c"
    segs = [
        {"start": 0.0, "end": 1.0, "text": "<|im_start|>"},
        {"start": 1.0, "end": 2.0, "text": f"language Chinese<asr_text>{cn}"},
    ]
    out = clean_segments(segs)
    assert len(out) == 1
    assert out[0]["text"] == cn


def test_history_save_and_list(tmp_path):
    from backend.history import HistoryStore
    store = HistoryStore(tmp_path / "h")
    rec = store.save(
        filename="audio.mp3",
        text="hello",
        segments=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        stats={"audio_duration": 1.0, "char_count": 5, "segment_count": 1},
        align_used=False,
    )
    assert rec["id"]
    items = store.list()
    assert len(items) == 1
    assert items[0]["filename"] == "audio.mp3"
    full = store.get(rec["id"])
    assert full and full["text"] == "hello"
    assert store.delete(rec["id"]) is True
    assert store.list() == []


def test_history_endpoints(tmp_path):
    client = make_client(tmp_path=tmp_path)
    r = client.get("/api/history")
    assert r.status_code == 200
    assert r.json() == {"items": []}

    rec = main_mod.history_store.save(
        filename="x.wav",
        text="t",
        segments=[],
        stats={"audio_duration": 0.0, "char_count": 1, "segment_count": 0},
    )
    r = client.get("/api/history")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "x.wav"

    r = client.get(f"/api/history/{rec['id']}")
    assert r.json()["text"] == "t"

    r = client.delete(f"/api/history/{rec['id']}")
    assert r.json() == {"deleted": True}


def test_transcribe_persists_history(tmp_path, monkeypatch):
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [(tmp_path / "c0.wav", 0.0, 0.0)]
    fake_chunks[0][0].write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 1.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _src, _out, _sec, _ov, **_kw: fake_chunks)

    async def fake_chunk(_client, _path, initial_prompt=None):
        return {"text": "ok", "segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("ABC.mp3", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["history_id"]
    items = client.get("/api/history").json()["items"]
    assert any(i["filename"] == "ABC.mp3" for i in items)


def test_transcribe_pipeline_aligns(tmp_path, monkeypatch):
    """Alignment is pipelined with ASR: each chunk's align is submitted as soon
    as its ASR text is ready, then drained. Verify the wiring produces aligned
    word segments and the aligner stats are populated."""
    client = make_client(tmp_path=tmp_path, aligner_available=True)

    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25), 0.0) for i in range(3)]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 75.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _src, _out, _sec, _ov, **_kw: fake_chunks)

    async def fake_chunk(_client, path, initial_prompt=None):
        # Distinct text per chunk so we can confirm ordering is preserved.
        idx = int(path.stem[1:])
        return {"text": f"words{idx}", "segments": [{"start": 0.0, "end": 1.0, "text": f"words{idx}"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    # Mock aligner.align_chunk to return one fake word per chunk, offset by
    # chunk_start so we can confirm the right chunk was aligned.
    def fake_align_chunk(wav_path, text, chunk_start, n_threads=4):
        return [{"word": text, "start": chunk_start, "end": chunk_start + 1.0, "text": text}]

    monkeypatch.setattr(main_mod.aligner, "align_chunk", fake_align_chunk)
    # model_path.stem is referenced for stats; keep it accessible.
    main_mod.aligner.model_path = tmp_path / "aligner-model.gguf"

    r = client.post(
        "/api/transcribe",
        files={"file": ("audio.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    stats = body["stats"]
    assert stats["aligner_used"] is True
    assert stats["word_count"] == 3
    assert stats["align_time"] >= 0
    # All three chunks' text survived through the pipeline.
    assert "words0" in body["text"]
    assert "words1" in body["text"]
    assert "words2" in body["text"]
    assert body["history_id"]


def test_transcribe_concurrent_chunks(tmp_path, monkeypatch):
    """With ASR_CONCURRENCY>1, consecutive chunks' ASR requests overlap in
    time — the second chunk's transcription starts before the first's ends,
    and the merged text still preserves chunk order."""
    monkeypatch.setenv("ASR_CONCURRENCY", "2")
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25), 0.0) for i in range(2)]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 50.0)
    monkeypatch.setattr(main_mod, "iter_chunks", lambda _s, _o, _c, _v, **_k: iter(fake_chunks))

    started: list[float] = []
    done: list[float] = []

    async def fake_chunk(_client, _path, initial_prompt=None):
        started.append(time.monotonic())
        await asyncio.sleep(0.2)
        done.append(time.monotonic())
        idx = int(_path.stem[1:])
        return {"text": f"chunk{idx}",
                "segments": [{"start": 0.0, "end": 1.0, "text": f"chunk{idx}"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200, r.text
    assert len(started) == 2 and len(done) == 2
    # The second chunk's ASR started before the first chunk's ASR finished.
    assert started[1] < done[0], "chunks were not transcribed concurrently"
    body = r.json()
    assert body["text"] == "chunk0 chunk1"
    assert body["segments"][0]["start"] == 0.0
    assert body["segments"][1]["start"] == 25.0


def test_transcribe_serial_chunks_when_concurrency_one(tmp_path, monkeypatch):
    """ASR_CONCURRENCY=1 restores the serial pipeline: the second chunk's ASR
    cannot start before the first chunk's ASR finishes."""
    monkeypatch.setenv("ASR_CONCURRENCY", "1")
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25), 0.0) for i in range(2)]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 50.0)
    monkeypatch.setattr(main_mod, "iter_chunks", lambda _s, _o, _c, _v, **_k: iter(fake_chunks))

    started: list[float] = []
    done: list[float] = []

    async def fake_chunk(_client, _path, initial_prompt=None):
        started.append(time.monotonic())
        await asyncio.sleep(0.1)
        done.append(time.monotonic())
        idx = int(_path.stem[1:])
        return {"text": f"chunk{idx}",
                "segments": [{"start": 0.0, "end": 1.0, "text": f"chunk{idx}"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200, r.text
    assert len(started) == 2 and len(done) == 2
    # Serial: the second chunk's ASR starts only after the first finishes
    # (equal timestamps are fine — the point is it cannot start earlier).
    assert started[1] >= done[0], "chunks were transcribed concurrently but ASR_CONCURRENCY=1"


# ── VAD chunking (Task 6) ────────────────────────────────────────────────
def test_transcribe_uses_vad_chunks(tmp_path, monkeypatch):
    """VAD_CHUNKING=1 routes the pipeline through _vad_plan + iter_vad_chunks."""
    monkeypatch.setattr(main_mod, "VAD_CHUNKING", True)
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25), 0.0) for i in range(2)]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 50.0)
    monkeypatch.setattr(main_mod, "_vad_plan", lambda *a, **_kw: [(0.0, 25.0), (25.0, 50.0)])
    monkeypatch.setattr(main_mod, "iter_vad_chunks", lambda *a, **_kw: iter(fake_chunks))

    async def fake_chunk(_client, _path, initial_prompt=None):
        idx = int(_path.stem[1:])
        return {"text": f"vad{idx}",
                "segments": [{"start": 0.0, "end": 1.0, "text": f"vad{idx}"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "vad0 vad1"


def test_transcribe_falls_back_to_fixed_chunking_when_vad_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "VAD_CHUNKING", True)
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25), 0.0) for i in range(2)]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 50.0)
    # VAD planning raises -> the pipeline must fall back to iter_chunks.
    monkeypatch.setattr(main_mod, "_vad_plan", lambda *a, **_kw: (_ for _ in ()).throw(RuntimeError("vad down")))
    monkeypatch.setattr(main_mod, "iter_chunks",
                        lambda _s, _o, _c, _v, **_k: iter(fake_chunks))

    async def fake_chunk(_client, _path, initial_prompt=None):
        idx = int(_path.stem[1:])
        return {"text": f"fix{idx}",
                "segments": [{"start": 0.0, "end": 1.0, "text": f"fix{idx}"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "fix0 fix1"


# ── Cancel / abort (Task 2) ──────────────────────────────────────────────
def test_abort_sets_cancel_flag():
    client = make_client()
    with main_mod.job_lock:
        main_mod._cancel_requested = False
    r = client.post("/api/abort")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert main_mod._is_cancel_requested() is True


def test_claim_job_resets_cancel_flag():
    """A fresh job must not inherit a cancel requested by the previous one."""
    make_client()
    main_mod._request_cancel()
    assert main_mod._is_cancel_requested() is True
    assert main_mod._claim_job() is True
    assert main_mod._is_cancel_requested() is False
    with main_mod.job_lock:
        main_mod.job_state["status"] = "idle"


def test_claim_job_blocks_during_switch():
    make_client()
    main_mod._set_boot(switching=True)
    assert main_mod._claim_job() is False
    main_mod._set_boot(switching=False)
    assert main_mod._claim_job() is True
    with main_mod.job_lock:
        main_mod.job_state["status"] = "idle"


def test_cancel_during_transcribe_keeps_partial(tmp_path, monkeypatch):
    client = make_client(tmp_path=tmp_path)
    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25), 0.0) for i in range(3)]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 75.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _s, _o, _c, _v, **_k: fake_chunks)

    calls = {"n": 0}

    async def fake_chunk(_client, _path, initial_prompt=None):
        calls["n"] += 1
        # After the first chunk succeeds, request cancel; the next iteration's
        # top-of-loop check breaks out and returns the partial result.
        if calls["n"] >= 1:
            main_mod._request_cancel()
        return {"text": f"chunk{calls['n']}",
                "segments": [{"start": 0.0, "end": 1.0, "text": f"chunk{calls['n']}"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("cancelled") is True
    assert "chunk1" in body["text"]
    assert "chunk3" not in body["text"]
    # Slot released to a terminal (non-busy) state.
    assert main_mod.job_state["status"] == "cancelled"


# ── Path-leak fix (Task 2 security) ──────────────────────────────────────
def test_models_endpoint_strips_paths(monkeypatch):
    """The /api/models response must not expose absolute filesystem paths."""
    client = make_client()
    monkeypatch.setattr(
        main_mod,
        "_scan_asr_models",
        lambda: [{"name": "m", "path": "/secret/m.gguf", "size": 1, "mmproj": "/secret/mm.gguf"}],
    )
    r = client.get("/api/models")
    assert r.status_code == 200
    models = r.json()["models"]
    assert len(models) == 1
    m = models[0]
    assert "path" not in m
    assert "mmproj" not in m
    assert m["has_mmproj"] is True
    assert m["name"] == "m"


def test_models_switch_rejected_while_switching(monkeypatch):
    monkeypatch.setattr(
        main_mod, "_scan_asr_models",
        lambda: [{"name": "m", "path": "/x", "size": 1, "mmproj": None}],
    )
    client = make_client()
    main_mod._set_boot(switching=True)
    try:
        r = client.post("/api/models/switch", json={"model": "m"})
        assert r.status_code == 200
        assert r.json()["error"] == "busy"
    finally:
        main_mod._set_boot(switching=False)


# ── Middleware hardening (Task 2 security) ───────────────────────────────
def test_middleware_blocks_cross_site_sfs():
    """A cross-site Sec-Fetch-Site (drive-by subresource) must be rejected."""
    client = make_client()
    r = client.get("/api/health", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_middleware_allows_same_origin_sfs():
    client = make_client()
    r = client.get("/api/health", headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200


def test_middleware_blocks_foreign_host():
    """A non-loopback Host header (DNS rebinding) must be rejected."""
    client = make_client()
    r = client.get("/api/health", headers={"host": "evil.example.com"})
    assert r.status_code == 403


# ── Audio-cache traversal (Task 2 security) ──────────────────────────────
def test_audio_cache_serve_traversal_notfound(tmp_path, monkeypatch):
    cache_dir = tmp_path / "audio_cache"
    cache_dir.mkdir()
    (cache_dir / "clip.mp3").write_bytes(b"ID3fake")
    monkeypatch.setattr(main_mod, "AUDIO_CACHE_DIR", cache_dir)
    client = make_client(tmp_path=tmp_path)

    # Positive: a cached file serves.
    r = client.get("/api/audio-cache/clip.mp3")
    assert r.status_code == 200

    # Traversal: path-like names are rejected (never 200, never leak a file).
    r2 = client.get("/api/audio-cache/..%2fevil.mp3")
    assert r2.status_code in (400, 404)

    # Missing file.
    r3 = client.get("/api/audio-cache/nope.mp3")
    assert r3.status_code == 404


# ── Review fixes: IPv6 Host, non-loopback LAN access, partial-MP3 cleanup ──
def test_middleware_allows_ipv6_loopback_host():
    """[::1] is a loopback literal and must be accepted, not split into '['."""
    client = make_client()
    r = client.get("/api/health", headers={"host": "[::1]:8000"})
    assert r.status_code == 200


def test_middleware_nonloopback_allows_lan_host(monkeypatch):
    """A non-loopback bind (HOST=0.0.0.0) without ASR_ALLOWED_HOSTS must allow
    LAN Hosts - the documented LAN-share use case - not 403 them."""
    monkeypatch.setattr(main_mod, "_BIND_IS_LOOPBACK", False)
    client = make_client()
    r = client.get("/api/health", headers={"host": "192.168.1.5:8000"})
    assert r.status_code == 200


def test_middleware_nonloopback_restricts_when_allowed_hosts_set(monkeypatch):
    """With ASR_ALLOWED_HOSTS set, a non-loopback share restricts to those Hosts."""
    monkeypatch.setattr(main_mod, "_BIND_IS_LOOPBACK", False)
    monkeypatch.setattr(main_mod, "_ALLOWED_HOSTS_EXTRA", {"myhost.local"})
    client = make_client()
    assert client.get("/api/health", headers={"host": "myhost.local:8000"}).status_code == 200
    assert client.get("/api/health", headers={"host": "evil.example.com:8000"}).status_code == 403


def test_extract_failure_clears_partial_cache_mp3(tmp_path, monkeypatch):
    """A failed video->MP3 extract must not leave a partial MP3 that a retry
    would silently reuse as truncated audio."""
    cache_dir = tmp_path / "audio_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(main_mod, "AUDIO_CACHE_DIR", cache_dir)
    monkeypatch.setattr(main_mod, "is_video_file", lambda _p: True)

    def fake_extract(_src, dst, _br):
        dst.write_bytes(b"ID3partial")  # partial output left behind, like a timeout
        raise RuntimeError("simulated extract timeout")

    monkeypatch.setattr(main_mod, "extract_audio_to_mp3", fake_extract)

    client = make_client(tmp_path=tmp_path)
    r = client.post(
        "/api/transcribe",
        files={"file": ("clip.mp4", b"\x00" * 16, "video/mp4")},
        data={"align": "false"},
    )
    assert r.json()["error"] == "extract_failed"
    # The partial MP3 must have been deleted so a retry re-extracts.
    assert not list(cache_dir.glob("*.mp3"))
    # Slot released to a terminal state.
    assert main_mod.job_state["status"] not in ("preparing", "transcribing", "aligning")


# ── High-severity fixes regression tests ───────────────────────────────────

def test_llama_runner_stops_process_on_startup_failure(tmp_path, monkeypatch):
    """A failed llama-server startup must not leave a leaked subprocess."""
    runner = LlamaRunner(
        bin_path=str(tmp_path / "llama-server.exe"),
        host="127.0.0.1",
        port=8080,
    )

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.stdout = None
            self.stderr = None
            self._returncode = None

        def poll(self):
            return 1  # process exited immediately

        def terminate(self):
            pass

        def wait(self, timeout=None):
            self._returncode = 1
            return 1

        @property
        def returncode(self):
            return self._returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    with pytest.raises(RuntimeError):
        runner.start(model_path=str(tmp_path / "model.gguf"))
    assert runner.process is None
    assert not runner.is_alive()


def test_claim_job_blocks_until_ready():
    """Transcription jobs cannot be claimed while the backend is still booting."""
    make_client()
    main_mod._set_boot(ready=False, phase="starting", switching=False)
    try:
        assert main_mod._claim_job() is False
    finally:
        main_mod._set_boot(ready=True, phase="ready")


def test_transcribe_rejected_when_not_ready(tmp_path):
    """/api/transcribe returns busy while boot has not completed."""
    client = make_client(tmp_path=tmp_path)
    main_mod._set_boot(ready=False, phase="starting", switching=False)
    try:
        r = client.post(
            "/api/transcribe",
            files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
            data={"align": "false"},
        )
        assert r.status_code == 409
        assert r.json()["error"] == "busy"
    finally:
        main_mod._set_boot(ready=True, phase="ready")


def test_models_switch_rejected_when_not_ready(monkeypatch):
    """/api/models/switch is refused while the backend is still booting."""
    client = make_client()
    main_mod._set_boot(ready=False, phase="starting", switching=False)
    monkeypatch.setattr(
        main_mod,
        "_scan_asr_models",
        lambda: [{"name": "m", "path": "/x", "size": 1, "mmproj": None}],
    )
    try:
        r = client.post("/api/models/switch", json={"model": "m"})
        assert r.json()["error"] == "busy"
        assert r.json()["detail"] == "backend is not ready"
    finally:
        main_mod._set_boot(ready=True, phase="ready")


def test_models_switch_validation_failure_releases_switching(monkeypatch):
    """Invalid tuning params must not leave the switching flag stuck."""
    client = make_client()
    monkeypatch.setattr(
        main_mod,
        "_scan_asr_models",
        lambda: [{"name": "m", "path": "/x", "size": 1, "mmproj": None}],
    )
    try:
        r = client.post("/api/models/switch", json={"model": "m", "ctx_size": 100})
        assert r.json()["error"] == "bad_request"
        assert main_mod.boot_state["switching"] is False
        assert main_mod.boot_state["ready"] is True
    finally:
        main_mod._set_boot(switching=False, phase="ready", ready=True)


def test_safe_upload_suffix_allowlist():
    """Known media extensions pass through; unknown/empty extensions fall back to .bin."""
    assert main_mod._safe_upload_suffix("song.mp3") == ".mp3"
    assert main_mod._safe_upload_suffix("clip.mp4") == ".mp4"
    assert main_mod._safe_upload_suffix("malware.exe") == ".bin"
    assert main_mod._safe_upload_suffix(None) == ".bin"


def test_align_words_rejects_nul_bytes():
    """NUL bytes in transcript must be rejected before crossing to C string API."""
    pcm = np.zeros(10, dtype=np.float32)
    assert _align_words("model.gguf", "hello\x00world", pcm) == []
