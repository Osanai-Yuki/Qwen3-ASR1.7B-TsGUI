"""Workbench mode tests: export format parity, root containment, auto-export,
and reference-mode queue execution (no staging copy)."""
import asyncio

import pytest

import backend.main as main_mod
import backend.queue_api as qapi
import backend.workbench as wb
from backend import export_formats as ef
from backend.queue_store import QueueStore
from backend.test_main import make_client, _make_wav_bytes
from backend.workbench import WorkbenchStore


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    """Tmp-rooted workbench + queue stores; deps stay wired to the module."""
    monkeypatch.setattr(wb, "store", WorkbenchStore(tmp_path / "workbench"))
    qapi.store = QueueStore(tmp_path / "queue")
    return wb.store


def _root_with_wav(tmp_path, bench, name="a.wav"):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    (root / name).write_bytes(_make_wav_bytes(0.2))
    item, error = bench.add_root(str(root))
    assert error is None
    return item, root


# export format parity with frontend/src/utils/subtitle.ts


def test_timestamp_rounding_matches_js():
    assert ef.format_timestamp(3661.5) == "01:01:01.500"
    assert ef.format_timestamp(0.0005) == "00:00:00.001"
    assert ef.format_ass_time(3661.5) == "1:01:01.50"
    assert ef.format_ass_time(0.005) == "0:00:00.01"


def test_srt_vtt_txt_shapes_and_control_chars():
    segs = [
        {"start": 0.0, "end": 1.5, "text": "hi\r\nthere\x00"},
        {"start": 1.5, "end": 2.0, "text": "<v x>&amp"},
    ]
    srt = ef.to_srt(segs)
    assert srt == "1\n00:00:00,000 --> 00:00:01,500\nhi\nthere\n\n2\n00:00:01,500 --> 00:00:02,000\n<v x>&amp"
    vtt = ef.to_vtt(segs)
    assert vtt.startswith("WEBVTT\n\n")
    assert "&lt;v x>&amp;amp" in vtt
    assert ef.to_txt(segs) == "hi\r\nthere\x00\n<v x>&amp"


def test_ass_escapes_and_header():
    segs = [{"start": 0.0, "end": 1.0, "text": "a{b}c\\d\ne"}]
    ass = ef.to_ass(segs)
    assert ass.startswith("[Script Info]\nScriptType: v4.00+")
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,a\\{b\\}c\\\\d\\Ne" in ass
    assert ass.endswith("\n")


def test_json_keeps_unicode_and_stats():
    out = ef.to_json("你好", [{"start": 0, "end": 1, "text": "你好"}], {"audio_duration": 1})
    assert "你好" in out
    assert '"audio_duration": 1' in out


def test_coarse_cue_and_no_timing_notice():
    cues, coarse, known = ef.subtitle_cues("hello", [], {"audio_duration": 5})
    assert coarse and known and cues == [{"start": 0, "end": 5, "text": "hello"}]
    assert ef.build_subtitle("srt", "hello", [], {"audio_duration": 5}).startswith("1\n00:00:00,000 --> 00:00:05,000")
    notice = ef.build_subtitle("srt", "hello", [], None)
    assert notice.startswith("没有生成有效时间范围")
    assert notice.endswith("hello")
    # Parity with frontend: TXT joins cues, and an unknown duration yields none.
    assert ef.build_subtitle("txt", "hello", [], None) == ""
    assert ef.build_subtitle("txt", "hello", [], {"audio_duration": 5}) == "hello"


# roots, scan, containment


def test_roots_add_validate_remove(bench, tmp_path):
    assert bench.add_root(str(tmp_path / "missing"))[1] == "not_a_directory"
    file_path = tmp_path / "f.txt"
    file_path.write_text("x", encoding="utf-8")
    assert bench.add_root(str(file_path))[1] == "not_a_directory"
    item, error = bench.add_root(str(tmp_path))
    assert error is None
    assert bench.add_root(str(tmp_path))[1] == "duplicate_root"
    assert bench.remove_root(item["id"]) is True
    assert bench.remove_root(item["id"]) is False


def test_scan_filters_hidden_and_limits(bench, tmp_path):
    item, root = _root_with_wav(tmp_path, bench)
    (root / "b.mp4").write_bytes(b"x")
    (root / "c.txt").write_text("x", encoding="utf-8")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "d.wav").write_bytes(b"x")
    sub = root / "sub"
    sub.mkdir()
    (sub / "e.flac").write_bytes(b"x")
    items, truncated = bench.scan(item["id"])
    assert [it["rel_path"] for it in items] == ["a.wav", "b.mp4", "sub/e.flac"]
    assert truncated is False
    items, truncated = bench.scan(item["id"], limit=2)
    assert len(items) == 2 and truncated is True


def test_resolve_source_containment(bench, tmp_path):
    item, root = _root_with_wav(tmp_path, bench)
    (root / "sub").mkdir()
    (root / "sub" / "e.wav").write_bytes(b"x")
    assert bench.resolve_source(item["id"], "sub/e.wav").is_file()
    for bad in ("../evil.wav", "sub/../../evil.wav", "C:/evil.wav", "/etc/passwd", ""):
        with pytest.raises(ValueError):
            bench.resolve_source(item["id"], bad)
    with pytest.raises(ValueError):
        bench.resolve_source("000000000000", "a.wav")


def test_config_validation_and_persistence(bench, tmp_path):
    assert bench.update_config({"formats": ["exe"]}) == "invalid_formats"
    assert bench.update_config({"formats": []}) == "invalid_formats"
    file_path = tmp_path / "f.txt"
    file_path.write_text("x", encoding="utf-8")
    assert bench.update_config({"export_root": str(file_path)}) == "invalid_export_root"
    assert bench.update_config({"export_root": str(tmp_path / "out"), "formats": ["srt", "txt"]}) is None
    reloaded = WorkbenchStore(tmp_path / "workbench")
    snap = reloaded.snapshot()
    assert snap["formats"] == ["srt", "txt"]
    assert snap["export_root"] == str((tmp_path / "out").resolve())


# export writer


def test_write_exports_names_conflicts_and_atomicity(bench, tmp_path):
    out = tmp_path / "exports"
    args = dict(text="hi", segments=[{"start": 0, "end": 1, "text": "hi"}],
                stats={}, filename="a.wav", export_root=str(out),
                batch_name="batch", formats=["srt", "txt"])
    first = wb.write_exports(identity="aaaabbbb1111", **args)
    assert [w["name"] for w in first] == ["a.srt", "a.txt"]
    second = wb.write_exports(identity="ccccdddd2222", **args)
    assert [w["name"] for w in second] == ["a__ccccdddd.srt", "a__ccccdddd.txt"]
    third = wb.write_exports(identity="ccccdddd3333", **args)
    assert third[0]["name"] == "a__ccccdddd-2.srt"
    assert (out / "batch" / "a.srt").read_text(encoding="utf-8") == ef.to_srt(args["segments"])
    assert not list((out / "batch").glob(".*tmp*"))


def test_batch_dir_and_stem_sanitized(bench, tmp_path):
    out = tmp_path / "exports"
    written = wb.write_exports(
        text="hi", segments=[], stats={"audio_duration": 2},
        filename='weird<>:"/\\|?*name.wav', identity="abcd1234abcd",
        export_root=str(out), batch_name='a/b:c*?', formats=["txt"],
    )
    assert written[0]["path"].startswith(str(out / "a_b_c__"))
    assert written[0]["name"].startswith("___name")


def test_auto_export_states(bench, tmp_path):
    item = {"id": "abcd1234abcd", "filename": "a.wav", "batch_name": "b"}
    resp = {"text": "hi", "segments": [], "stats": {}}
    assert wb.auto_export(item, resp, "hist12345678")["state"] == "skipped"
    bench.update_config({"export_root": str(tmp_path / "out"), "auto_export": False})
    assert wb.auto_export(item, resp, "hist12345678")["state"] == "skipped"
    assert wb.auto_export(item, resp, "hist12345678", force=True)["state"] == "done"
    bench.update_config({"auto_export": True})
    assert wb.auto_export(item, resp, "hist12345678")["state"] == "done"
    blocked = tmp_path / "out2"
    blocked.mkdir()
    bench.update_config({"export_root": str(blocked)})
    blocked.rmdir()
    blocked.write_text("x", encoding="utf-8")
    assert wb.auto_export(item, resp, "hist12345678")["state"] == "failed"
    bench.update_config({"export_root": None})
    forced = wb.auto_export(item, resp, "hist12345678", force=True)
    assert forced["state"] == "failed" and forced["error"] == "export_root_unset"


# reference-mode queue integration


def _mock_pipeline(monkeypatch, chunk):
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 10.0)
    monkeypatch.setattr(main_mod, "split_audio",
                        lambda _src, _out, _sec, _ov, **_kw: [(chunk, 0.0, 0.0)])

    async def fake_chunk(client_arg, chunk_path, initial_prompt=None):
        return {"text": "你好", "segments": [{"start": 0.0, "end": 1.0, "text": "你好"}]}

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)


def test_reference_enqueue_runs_and_auto_exports(bench, tmp_path, monkeypatch):
    item, root = _root_with_wav(tmp_path, bench)
    bench.update_config({"export_root": str(tmp_path / "exports")})
    client = make_client(tmp_path=tmp_path)
    r = client.post("/api/queue/reference", json={
        "root_id": item["id"], "rel_paths": ["a.wav"], "align": False, "batch_name": "会议",
    })
    assert r.status_code == 200
    qitem = r.json()["items"][0]
    assert qitem["source_mode"] == "reference"
    assert qitem["staged_name"] is None
    assert qitem["batch_name"] == "会议"
    assert not list((qapi.store.root / "uploads").iterdir())

    _mock_pipeline(monkeypatch, root / "a.wav")
    assert asyncio.run(qapi.process_next_once()) is True
    done = qapi.store.get(qitem["id"])
    assert done["status"] == "done"
    assert done["history_id"]
    assert done["export_state"] == "done"
    names = sorted(p["name"] for p in done["export_paths"])
    assert names == ["a.json", "a.srt", "a.txt"]
    assert (tmp_path / "exports" / "会议" / "a.srt").is_file()
    # The source file must survive: reference mode never deletes it.
    assert (root / "a.wav").is_file()


def test_reference_source_gone_and_retry(bench, tmp_path, monkeypatch):
    item, root = _root_with_wav(tmp_path, bench)
    client = make_client(tmp_path=tmp_path)
    r = client.post("/api/queue/reference", json={"root_id": item["id"], "rel_paths": ["a.wav"]})
    qid = r.json()["items"][0]["id"]
    (root / "a.wav").unlink()
    assert asyncio.run(qapi.process_next_once()) is True
    assert qapi.store.get(qid)["error"] == "source_gone"
    assert client.post(f"/api/queue/items/{qid}/retry").status_code == 410


def test_reference_escape_and_type_rejected(bench, tmp_path):
    item, root = _root_with_wav(tmp_path, bench)
    (root / "note.txt").write_text("x", encoding="utf-8")
    client = make_client(tmp_path=tmp_path)
    r = client.post("/api/queue/reference", json={"root_id": item["id"], "rel_paths": ["../evil.wav"]})
    assert r.status_code == 422 and r.json()["error"] == "invalid_source_path"
    r = client.post("/api/queue/reference", json={"root_id": item["id"], "rel_paths": ["note.txt"]})
    assert r.status_code == 422 and r.json()["error"] == "unsupported_type"
    r = client.post("/api/queue/reference", json={"root_id": "000000000000", "rel_paths": ["a.wav"]})
    assert r.status_code == 404


def test_reexport_endpoint(bench, tmp_path, monkeypatch):
    item, root = _root_with_wav(tmp_path, bench)
    bench.update_config({"export_root": str(tmp_path / "exports")})
    client = make_client(tmp_path=tmp_path)
    r = client.post("/api/queue/reference", json={"root_id": item["id"], "rel_paths": ["a.wav"]})
    qid = r.json()["items"][0]["id"]
    _mock_pipeline(monkeypatch, root / "a.wav")
    asyncio.run(qapi.process_next_once())
    for p in (tmp_path / "exports").rglob("a.*"):
        p.unlink()
    r = client.post(f"/api/queue/items/{qid}/export")
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert (tmp_path / "exports" / "media" / "a.srt").is_file()
    bench.update_config({"export_root": None})
    assert client.post(f"/api/queue/items/{qid}/export").status_code == 422