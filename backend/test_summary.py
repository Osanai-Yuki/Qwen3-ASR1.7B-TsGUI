"""Summary phase tests: JSON contract, swap state machine, gating, search."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import backend.main as main_mod
import backend.queue_api as qapi
import backend.summary as sm
import backend.workbench as wb
from backend.history import HistoryStore
from backend.queue_store import QueueStore
from backend.test_main import make_client, _make_wav_bytes


def _setup(tmp_path, monkeypatch, *, llm=None, swap_in_error=False, swap_out_error=False):
    monkeypatch.setattr(wb, "store", wb.WorkbenchStore(tmp_path / "workbench"))
    wb.store.update_config({"summarize": True, "summary_model": "m"})
    qapi.store = QueueStore(tmp_path / "queue")
    history = HistoryStore(tmp_path / "history")
    calls = {"swap_in": [], "swap_out": 0, "gate": []}
    records = {}
    for hid, text in (("h1", "今天决定上线新导出流程。张三负责待办。"), ("h2", "第二段转写内容。")):
        rec = history.save(filename=f"{hid}.wav", text=text,
                           segments=[{"start": 0, "end": 1, "text": text}], stats={})
        records[hid] = rec["id"]
    items = []
    for hid in ("h1", "h2"):
        it = qapi.store.add(qid=qapi.store.new_id(), filename=f"{hid}.wav", staged_name=None,
                            size=1, align=False, batch_id="b1", batch_name="批次",
                            source_mode="reference", root_id="r1")
        qapi.store.set_status(it["id"], "done", history_id=records[hid])
        items.append(it)

    def swap_in(path):
        calls["swap_in"].append(path)
        if swap_in_error:
            raise RuntimeError("load failed")

    def swap_out():
        calls["swap_out"] += 1
        if swap_out_error:
            raise RuntimeError("restore failed")

    async def fake_llm(text, title):
        if llm is not None:
            return llm(text, title)
        if "张三" in text:
            return json.dumps({"title": "导出流程会议", "summary": "决定上线新导出流程。",
                               "keywords": ["导出", "流程"], "highlights": ["张三负责待办"]},
                              ensure_ascii=False)
        return "我不会输出 JSON"

    sm.init(
        data_dir=tmp_path / "summary",
        swap_in_fn=swap_in, swap_out_fn=swap_out,
        read_history_fn=history.get, save_summary_fn=history.save_summary,
        list_batch_items_fn=lambda bid: [i for i in qapi.store.snapshot()["items"] if i.get("batch_id") == bid],
        find_item_by_history_fn=lambda hid: next((i for i in qapi.store.snapshot()["items"] if i.get("history_id") == hid), None),
        set_item_summary_fn=lambda qid, state, error: qapi.store.set_summary(qid, state, error),
        set_gate_fn=lambda active: calls["gate"].append(active),
        config_fn=lambda: wb.store.snapshot(),
        list_summary_models_fn=lambda: [{"name": "m", "path": str(tmp_path / "m.gguf"), "size": 1}],
        base_url_fn=lambda: "http://127.0.0.1:1",
        llm_call_fn=fake_llm,
    )
    return calls, records, items, history


def _drive():
    async def main():
        sm.start_batch("b1")
        if sm._task is not None:
            await sm._task
    asyncio.run(main())


# JSON contract


def test_parse_summary_valid_fenced_and_prose():
    payload = {"title": "t", "summary": "s", "keywords": ["a", " b ", 3], "highlights": ["h"]}
    raw = json.dumps(payload, ensure_ascii=False)
    assert sm.parse_summary(raw)["keywords"] == ["a", "b"]
    assert sm.parse_summary("```json\n" + raw + "\n```")["summary"] == "s"
    assert sm.parse_summary("好的，结果如下：\n" + raw + "\n以上。")["title"] == "t"


def test_parse_summary_rejects_bad_contract():
    for raw in ("", "no json here", "{not json}", json.dumps({"title": "t"}),
                json.dumps({"summary": "   "}), "[1,2]"):
        with pytest.raises(ValueError):
            sm.parse_summary(raw)


def test_parse_summary_caps():
    raw = json.dumps({"summary": "x" * 5000, "keywords": ["k"] * 30, "highlights": ["h"] * 30})
    out = sm.parse_summary(raw)
    assert len(out["summary"]) == 2000
    assert len(out["keywords"]) == 12
    assert len(out["highlights"]) == 10


def test_build_prompt_truncates():
    messages = sm.build_prompt("字" * (sm.MAX_TRANSCRIPT_CHARS + 500), "t")
    assert "节选" in messages[1]["content"]
    assert len(messages[1]["content"]) < sm.MAX_TRANSCRIPT_CHARS + 200


# phase machine


def test_phase_happy_path_and_per_item_degradation(tmp_path, monkeypatch):
    calls, records, items, history = _setup(tmp_path, monkeypatch)
    _drive()
    assert calls["swap_in"] == [str(tmp_path / "m.gguf")]
    assert calls["swap_out"] == 1
    assert calls["gate"] == [True, False]
    state = sm.state()
    assert state["phase"] == "finished" and state["done"] == 2 and state["total"] == 2
    by_hid = {i["history_id"]: i for i in qapi.store.snapshot()["items"]}
    assert by_hid[records["h1"]]["summary_state"] == "done"
    assert by_hid[records["h2"]]["summary_state"] == "failed"
    assert by_hid[records["h2"]]["summary_error"] == "summary_parse"
    saved = history.get(records["h1"])["summary"]
    assert saved["summary"] == "决定上线新导出流程。"
    assert saved["keywords"] == ["导出", "流程"]
    assert saved["state"] == "done" and saved["model"] == "m"
    assert history.get(records["h2"]).get("summary") is None


def test_phase_swap_in_failure_restores_asr(tmp_path, monkeypatch):
    calls, _, _, _ = _setup(tmp_path, monkeypatch, swap_in_error=True)
    _drive()
    assert calls["swap_out"] == 1
    assert calls["gate"] == []
    assert sm.state()["phase"] == "failed"
    assert sm.state()["error"] == "swap_in_failed"


def test_phase_swap_out_failure_keeps_gate(tmp_path, monkeypatch):
    calls, _, _, _ = _setup(tmp_path, monkeypatch, swap_out_error=True)
    _drive()
    assert sm.state()["phase"] == "failed"
    assert sm.state()["error"] == "swap_back_failed"
    assert calls["gate"] == [True, True]


def test_retry_item_reruns_done_summary(tmp_path, monkeypatch):
    calls, records, items, history = _setup(tmp_path, monkeypatch)
    _drive()

    async def again():
        ok, error = sm.retry_item(records["h1"])
        assert ok and error is None
        if sm._task is not None:
            await sm._task
    asyncio.run(again())
    assert sm.state()["total"] == 1
    assert calls["swap_out"] == 2


def test_maybe_start_batch_requires_terminal_and_config(tmp_path, monkeypatch):
    calls, records, items, history = _setup(tmp_path, monkeypatch)

    async def checks():
        qapi.store.set_status(items[0]["id"], "queued")
        assert sm.maybe_start_batch("b1") is False
        qapi.store.set_status(items[0]["id"], "done", history_id=records["h1"])
        assert sm.maybe_start_batch("b1") is True
        wb.store.update_config({"summarize": False})
        assert sm.maybe_start_batch("b1") is False
    asyncio.run(checks())


def test_restart_reconciles_interrupted_phase(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sm._set_phase(phase="summarizing", batch_id="b1")
    sm.init(
        data_dir=tmp_path / "summary",
        swap_in_fn=lambda p: None, swap_out_fn=lambda: None,
        read_history_fn=lambda h: None, save_summary_fn=lambda h, s: None,
        list_batch_items_fn=lambda b: [], find_item_by_history_fn=lambda h: None,
        set_item_summary_fn=lambda q, s, e: False, set_gate_fn=lambda a: None,
        config_fn=lambda: {}, list_summary_models_fn=lambda: [],
        base_url_fn=lambda: "",
    )
    assert sm.state()["phase"] == "interrupted"
    assert sm.busy() is False


# gating of the transcription slot


def test_transcribe_and_queue_gated_during_summary(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    client = make_client(tmp_path=tmp_path)
    sm._set_phase(phase="summarizing", batch_id="b1")
    main_mod._set_boot(ready=False, phase="summarizing")
    r = client.post("/api/transcribe", files={"file": ("a.wav", _make_wav_bytes(0.1), "audio/wav")},
                    data={"align": "false"})
    assert r.status_code == 409 and r.json()["error"] == "batch_summarizing"
    it = qapi.store.add(qid=qapi.store.new_id(), filename="q.wav", staged_name="q.wav",
                        size=1, align=False, batch_id="b2")
    (qapi.store.uploads_dir / "q.wav").write_bytes(_make_wav_bytes(0.1))
    assert asyncio.run(qapi.process_next_once()) is False
    assert qapi.store.get(it["id"])["status"] == "queued"
    sm._set_phase(phase="idle")
    main_mod._set_boot(ready=True, phase="ready")


# history summary persistence and search


def test_history_summary_search_and_list(tmp_path):
    history = HistoryStore(tmp_path / "history")
    rec = history.save(filename="a.wav", text="正文内容", segments=[], stats={})
    assert history.save_summary(rec["id"], {"summary": "导出流程决议", "keywords": ["黄金", "出口"],
                                           "highlights": [], "state": "done"})
    hit = history.search(query="黄金")
    assert hit["items"] and "keywords" in hit["items"][0]["match_fields"]
    hit2 = history.search(query="决议")
    assert "summary" in hit2["items"][0]["match_fields"]
    listed = history.list(10)[0]
    assert listed["summary"]["keywords"] == ["黄金", "出口"]
    assert listed["title"]


# real httpx client against a stub llama-server


def test_llm_call_over_http_with_hostile_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1],::1")
    _setup(tmp_path, monkeypatch)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.dumps({"choices": [{"message": {"content": "{\"summary\":\"ok\"}"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sm._deps["base_url_fn"] = lambda: f"http://127.0.0.1:{server.server_address[1]}"
        sm._deps["llm_call_fn"] = sm._llm_call
        out = asyncio.run(sm._llm_call("text", "title"))
        assert json.loads(out)["summary"] == "ok"
    finally:
        server.shutdown()
        server.server_close()