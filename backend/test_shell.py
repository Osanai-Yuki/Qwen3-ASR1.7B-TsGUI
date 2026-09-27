"""Tests for the desktop-shell security additions: session-token middleware,
security response headers, and the llama-server API key."""

import backend.main as main_mod
from backend.llamarunner import LlamaRunner
from backend.test_main import make_client


def _enable_shell(monkeypatch, token="test-shell-token-123"):
    monkeypatch.setitem(main_mod._shell_state, "enabled", True)
    monkeypatch.setitem(main_mod._shell_state, "token", token)
    monkeypatch.setitem(main_mod._shell_state, "handshake_done", False)
    return token


def test_shell_guard_inert_by_default():
    client = make_client()
    assert client.get("/api/health").status_code == 200


def test_shell_handshake_and_cookie_gate(monkeypatch):
    client = make_client()
    token = _enable_shell(monkeypatch)

    # Without a session cookie every request is rejected.
    r = client.get("/api/health")
    assert r.status_code == 403
    assert r.json()["detail"] == "missing shell session"

    # Handshake: ?st=<token> -> 302 stripping the token + HttpOnly cookie.
    r = client.get(f"/?st={token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    set_cookie = r.headers["set-cookie"]
    assert "asr_shell_session=" in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie

    # TestClient stored the cookie: API access now works.
    assert client.get("/api/health").status_code == 200

    # The one-time token is spent: replaying it is rejected.
    fresh = make_client()  # new client without the cookie
    _ = fresh  # make_client re-set boot state; shell state persists
    r = fresh.get(f"/?st={token}", follow_redirects=False)
    assert r.status_code == 403


def test_shell_handshake_rejects_wrong_token(monkeypatch):
    client = make_client()
    _enable_shell(monkeypatch)
    r = client.get("/?st=wrong-token", follow_redirects=False)
    assert r.status_code == 403


def test_security_headers_on_responses():
    client = make_client()
    r = client.get("/api/health")
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "media-src 'self' blob:" in csp  # local audio player needs blob:
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_llamarunner_api_key_in_argv(tmp_path):
    with_key = LlamaRunner(
        bin_path=str(tmp_path / "llama-server.exe"), api_key="sekret",
    )
    with_key._model_path = tmp_path / "m.gguf"
    cmd = with_key.build_cmd()
    assert "--api-key" in cmd
    assert cmd[cmd.index("--api-key") + 1] == "sekret"

    without = LlamaRunner(bin_path=str(tmp_path / "llama-server.exe"))
    without._model_path = tmp_path / "m.gguf"
    assert "--api-key" not in without.build_cmd()


def test_transcribe_chunk_sends_bearer(monkeypatch):
    """The internal llama-server client must authenticate every call."""
    captured = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "ok", "segments": []}

    class FakeClient:
        async def post(self, url, **kwargs):
            captured["headers"] = kwargs.get("headers") or {}
            return FakeResp()

    import asyncio
    import wave, io, struct
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * 160)
    p = None
    import tempfile, pathlib
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        fh.write(buf.getvalue())
        p = pathlib.Path(fh.name)
    try:
        asyncio.run(main_mod._transcribe_chunk(FakeClient(), p))
    finally:
        p.unlink(missing_ok=True)
    auth = captured["headers"].get("Authorization", "")
    assert auth == f"Bearer {main_mod.LLAMA_API_KEY}"
