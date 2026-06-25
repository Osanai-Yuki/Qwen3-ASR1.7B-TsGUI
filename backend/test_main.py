import io
import struct
import wave
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import backend.main as main_mod
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

    return TestClient(main_mod.app)


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
        (tmp_path / "c0.wav", 0.0),
        (tmp_path / "c1.wav", 25.0),
    ]
    for p, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))

    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 50.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _src, _out, _sec: fake_chunks)

    responses = [
        {"text": "hello", "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}]},
        {"text": "world", "segments": [{"start": 0.0, "end": 1.0, "text": "world"}]},
    ]

    async def fake_chunk(client_arg, chunk_path):
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

    fake_chunks = [(tmp_path / "c0.wav", 0.0)]
    fake_chunks[0][0].write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 1.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _src, _out, _sec: fake_chunks)

    async def fake_chunk(_client, _path):
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

    fake_chunks = [(tmp_path / f"c{i}.wav", float(i * 25)) for i in range(3)]
    for p, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 75.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _src, _out, _sec: fake_chunks)

    async def fake_chunk(_client, path):
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
