"""Tests for VAD-based chunking (backend/audio_chunk.py).

The VAD pipeline is: _detect_silence (ffmpeg) -> _vad_plan (group speech
segments) -> iter_vad_chunks (extract slices on demand).  _detect_silence is
mocked throughout so no ffmpeg is needed; extract_slice is mocked for
iter_vad_chunks.
"""

import backend.audio_chunk as ac


def _stub_src(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(b"")
    return src


def test_vad_plan_groups_speech_across_short_silence(tmp_path, monkeypatch):
    # 20s audio: 0.5s silences (below long_silence=1.0) at [5,5.5] and [15,15.5].
    # Speech: [0,5], [5.5,15], [15.5,20]. chunk_seconds=10, max=15.
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [(5.0, 5.5), (15.0, 15.5)])
    plan = ac._vad_plan(_stub_src(tmp_path), 20.0, 10.0)
    # [0,5]+[5.5,15] span=15<=15, gap=0.5<=1.0 -> absorb to [0,15];
    # adding [15.5,20] would span 20>15 -> close, then [15.5,20].
    assert plan == [(0.0, 15.0), (15.5, 20.0)]


def test_vad_plan_closes_on_long_silence(tmp_path, monkeypatch):
    # Silence [5,10] (5s gap > long_silence=1.0): natural break.
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [(5.0, 10.0)])
    plan = ac._vad_plan(_stub_src(tmp_path), 20.0, 10.0)
    assert plan == [(0.0, 5.0), (10.0, 20.0)]


def test_vad_plan_closes_when_span_exceeds_cap(tmp_path, monkeypatch):
    # Three short speech segments with tiny gaps; together they'd exceed the
    # max_chunk_seconds cap, so the plan closes early.
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [(4.0, 4.2), (8.0, 8.2)])
    # Speech: [0,4], [4.2,8], [8.2,20]. chunk_seconds=10, max=15.
    # [0,4]+[4.2,8] span=8<=15, gap=0.2<=1.0 -> absorb to [0,8];
    # adding [8.2,20] would span 20>15 -> close, then [8.2,20].
    plan = ac._vad_plan(_stub_src(tmp_path), 20.0, 10.0)
    assert plan == [(0.0, 8.0), (8.2, 20.0)]


def test_vad_plan_falls_back_to_fixed_split_for_long_speech(tmp_path, monkeypatch):
    # No silence: one speech segment [0,25]. chunk_seconds=10, max=15.
    # 25 > 15 -> fallback splits into ceil(25/10)=3 sub-chunks.
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [])
    plan = ac._vad_plan(_stub_src(tmp_path), 25.0, 10.0)
    assert len(plan) == 3
    assert plan[0][0] == 0.0
    assert abs(plan[-1][1] - 25.0) < 1e-6
    # Sub-chunks are contiguous.
    for a, b in zip(plan, plan[1:]):
        assert abs(a[1] - b[0]) < 1e-6


def test_vad_plan_empty_when_no_speech(tmp_path, monkeypatch):
    # All silence: no speech segments.
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [(0.0, 20.0)])
    plan = ac._vad_plan(_stub_src(tmp_path), 20.0, 10.0)
    assert plan == []


def test_vad_plan_caps_silence_running_to_eof(tmp_path, monkeypatch):
    # Silence runs to EOF (inf sentinel): capped at duration, so speech is
    # only [0,5].
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [(5.0, float("inf"))])
    plan = ac._vad_plan(_stub_src(tmp_path), 20.0, 10.0)
    assert plan == [(0.0, 5.0)]


def test_vad_plan_rejects_degenerate_chunk_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [])
    try:
        ac._vad_plan(_stub_src(tmp_path), 20.0, 0.5)
    except ValueError:
        return
    raise AssertionError("expected ValueError for chunk_seconds < 1.0")


def _fake_extract_factory():
    """Mock extract_slice that writes a non-empty WAV (>44 bytes)."""

    def fake_extract(input_path, out_path, start, seconds):
        out_path.write_bytes(b"RIFF" + b"\x00" * 40 + b"WAVE")
        return out_path

    return fake_extract


def test_iter_vad_chunks_yields_in_order_with_prev_end(tmp_path, monkeypatch):
    src = _stub_src(tmp_path)
    out_dir = tmp_path / "out"

    monkeypatch.setattr(ac, "extract_slice", _fake_extract_factory())
    plan = [(0.0, 5.0), (7.0, 12.0)]
    chunks = list(ac.iter_vad_chunks(src, out_dir, 10.0, duration=20.0, plan=plan))
    assert len(chunks) == 2
    # (chunk_path, start_offset, prev_end)
    assert chunks[0][1] == 0.0   # start_offset
    assert chunks[0][2] == 0.0   # prev_end (first chunk)
    assert chunks[1][1] == 7.0   # start_offset
    assert chunks[1][2] == 5.0   # prev_end = previous chunk's end


def test_iter_vad_chunks_skips_empty_slices(tmp_path, monkeypatch):
    src = _stub_src(tmp_path)
    out_dir = tmp_path / "out"

    def fake_extract(input_path, out_path, start, seconds):
        # Simulate an empty extraction (e.g. pure silence) -> size <= 44.
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(ac, "extract_slice", fake_extract)
    plan = [(0.0, 5.0), (7.0, 12.0)]
    chunks = list(ac.iter_vad_chunks(src, out_dir, 10.0, duration=20.0, plan=plan))
    assert chunks == []


def test_iter_vad_chunks_computes_plan_when_not_given(tmp_path, monkeypatch):
    src = _stub_src(tmp_path)
    out_dir = tmp_path / "out"

    monkeypatch.setattr(ac, "_detect_silence", lambda *_a, **_kw: [(5.0, 7.0)])
    monkeypatch.setattr(ac, "extract_slice", _fake_extract_factory())
    chunks = list(ac.iter_vad_chunks(src, out_dir, 10.0, duration=20.0))
    # Speech: [0,5], [7,20]. chunk_seconds=10, max=15.
    # [0,5]+[7,20] span=20>15 -> close at [0,5], then [7,20].
    assert len(chunks) == 2
    assert chunks[0][1] == 0.0
    assert chunks[1][1] == 7.0