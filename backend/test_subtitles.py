"""Tests for subtitle quality fixes: backtracking re-segmentation (no
mid-word cuts) and seam dedup between overlapping audio chunks."""

import backend.main as main_mod
from backend.resegment import resegment_words
from backend.text_clean import seam_overlap
from backend.test_main import make_client, _make_wav_bytes


def _cjk_words(text: str, start: float = 0.0, dur: float = 0.22, gap: float = 0.0):
    """One aligned word per character, uniform pacing (Chinese aligner shape)."""
    out = []
    t = start
    for ch in text:
        out.append({"word": ch, "start": round(t, 3), "end": round(t + dur, 3), "text": ch})
        t += dur + gap
    return out


# ── resegment: backtracking hard breaks ─────────────────────────────────


def test_resegment_cjk_prefers_filler_break():
    # 28 unpunctuated chars; "呢" (index 16) is the only natural break.
    text = "这句话叫做科技资本开支优先于消费呢总共有五段对应五个观点"
    assert text.index("呢") == 16
    result = resegment_words(_cjk_words(text))
    assert len(result) >= 2
    # The hard break must backtrack onto the filler particle, not cut at
    # whatever character position the char limit happened to land on.
    assert result[0]["text"].endswith("呢")
    # Nothing lost or duplicated.
    assert "".join(s["text"] for s in result) == text


def test_resegment_never_splits_latin_run():
    # "GDP" sits right where the naive char-limit cut used to land.
    text = "四是大摩同样跟进下调了全年实际GDP的增速预期从之前说起"
    result = resegment_words(_cjk_words(text))
    joined = "".join(s["text"] for s in result)
    assert joined == text
    for prev, nxt in zip(result, result[1:]):
        l, r = prev["text"][-1], nxt["text"][0]
        assert not (l.isascii() and l.isalnum() and r.isascii() and r.isalnum()), (
            f"latin run split between {prev['text']!r} and {nxt['text']!r}"
        )


def test_resegment_keeps_percent_number_intact():
    text = "增速预期从之前的水平大幅下调到了百分之四点六这一点和高盛类似"
    result = resegment_words(_cjk_words(text))
    assert any("百分之四点六" in s["text"] for s in result), (
        f"百分之四点六 was torn apart: {[s['text'] for s in result]}"
    )


def test_resegment_cjk_max_chars_tightened():
    # 40 continuous chars with no cues must still be broken into readable
    # lines (CJK default max_chars is now 24).
    text = "这是一段完全没有任何标点符号也没有停顿的长句子用来验证中文字幕长度上限收紧了"
    result = resegment_words(_cjk_words(text))
    assert len(result) >= 2
    assert all(len(s["text"]) <= 24 for s in result)


def test_resegment_soft_break_on_pause():
    # A 0.5s pause after char 20 should be taken as a soft break once past
    # the soft thresholds, instead of running to the hard limit.
    head = "前面这一部分已经超过了十八个字符限制了"
    tail = "后面是新的一句话"
    words = _cjk_words(head) + _cjk_words(tail, start=len(head) * 0.22 + 0.5)
    result = resegment_words(words)
    assert result[0]["text"] == head
    assert result[1]["text"].startswith("后面")


# ── seam_overlap ────────────────────────────────────────────────────────


def test_seam_overlap_basic():
    cut, k = seam_overlap("今天我们聊会处于稳定状态", "会处于稳定状态美元汇率")
    assert k == 7
    assert "会处于稳定状态美元汇率"[cut:] == "美元汇率"


def test_seam_overlap_case_and_space_insensitive():
    cut, k = seam_overlap("一是NoBazuka", "nobazuka 也就是没有火箭筒")
    assert k == len("nobazuka")
    assert "nobazuka 也就是没有火箭筒"[cut:].lstrip() == "也就是没有火箭筒"


def test_seam_overlap_none():
    assert seam_overlap("完全不同的内容", "另外一段完全不同") == (0, 0)


def test_seam_overlap_whole_duplicate():
    new = "会处于稳定状态"
    cut, k = seam_overlap("前文会处于稳定状态", new)
    assert cut == len(new) and k == 7


def test_seam_overlap_short_ignored():
    # Overlaps under min_overlap chars are too likely to be coincidence.
    assert seam_overlap("说到了这", "这是新话题完全不同")[0] == 0


# ── endpoint-level: dedup survives coarse segment timestamps ────────────


def test_transcribe_seam_dedup_between_chunks(tmp_path, monkeypatch):
    client = make_client(tmp_path=tmp_path)

    fake_chunks = [
        (tmp_path / "c0.wav", 0.0, 0.0),
        (tmp_path / "c1.wav", 23.5, 23.5),
    ]
    for p, _, _ in fake_chunks:
        p.write_bytes(_make_wav_bytes(0.1))
    monkeypatch.setattr(main_mod, "probe_duration", lambda _p: 48.0)
    monkeypatch.setattr(main_mod, "split_audio", lambda _s, _o, _c, _v, **_k: fake_chunks)

    # Chunk 1 repeats chunk 0's tail, but its segment starts *after*
    # prev_end (coarse timestamps) so the time-based dedup misses it.
    responses = [
        {"text": "人民币汇率下半年会处于稳定状态",
         "segments": [{"start": 0.0, "end": 8.0, "text": "人民币汇率下半年会处于稳定状态"}]},
        {"text": "会处于稳定状态美元对人民币保持稳定",
         "segments": [{"start": 0.5, "end": 8.0, "text": "会处于稳定状态美元对人民币保持稳定"}]},
    ]

    async def fake_chunk(_client, _path, initial_prompt=None):
        return responses.pop(0)

    monkeypatch.setattr(main_mod, "_transcribe_chunk", fake_chunk)

    r = client.post(
        "/api/transcribe",
        files={"file": ("x.wav", _make_wav_bytes(0.1), "audio/wav")},
        data={"align": "false"},
    )
    assert r.status_code == 200
    text = r.json()["text"]
    assert "稳定状态会处于稳定状态" not in text.replace(" ", "")
    assert "美元对人民币保持稳定" in text
