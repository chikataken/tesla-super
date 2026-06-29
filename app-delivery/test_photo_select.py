"""
Tests for the photo-angle selector's parsing/coercion logic. The Claude call is
mocked (no anthropic SDK, no API key, no network) — we test how a tool response is
turned into a Selection, not the model's judgment.
"""
from __future__ import annotations

import photo_select
from photo_select import SLOTS, Selection


class _Block:
    type = "tool_use"
    name = "report"

    def __init__(self, inp):
        self.input = inp


class _Resp:
    def __init__(self, content):
        self.content = content


class _Client:
    def __init__(self, resp):
        self.messages = type("M", (), {"create": lambda _s, **k: resp})()


def _mock(monkeypatch, tool_input, content=None):
    resp = _Resp(content if content is not None else [_Block(tool_input)])
    monkeypatch.setattr(photo_select, "_get_client", lambda: _Client(resp))


def _imgs(n=5):
    return [b"img%d" % i for i in range(n)]          # prep_image tolerates non-images


def test_valid_selection_is_parsed(monkeypatch):
    _mock(monkeypatch, {
        "selection": {"front": 0, "rear": 1, "left_side": 2, "right_side": 3},
        "photos": [{"index": 0, "angle": "front"}],
        "reasoning": "ok",
    })
    sel = photo_select.select_corner_photos(_imgs())
    assert sel.picks == {"front": 0, "rear": 1, "left_side": 2, "right_side": 3}
    assert sel.complete() is True
    assert sel.missing() == []
    assert sel.reasoning == "ok"


def test_out_of_range_and_negative_indices_coerce_to_minus1(monkeypatch):
    _mock(monkeypatch, {
        "selection": {"front": 0, "rear": 99, "left_side": -1, "right_side": 2},
        "reasoning": "",
    })
    sel = photo_select.select_corner_photos(_imgs(5))     # valid indices are 0..4
    assert sel.picks == {"front": 0, "rear": -1, "left_side": -1, "right_side": 2}
    assert sel.complete() is False
    assert set(sel.missing()) == {"rear", "left_side"}


def test_duplicate_pick_is_not_complete(monkeypatch):
    _mock(monkeypatch, {
        "selection": {"front": 0, "rear": 0, "left_side": 1, "right_side": 2},
        "reasoning": "",
    })
    sel = photo_select.select_corner_photos(_imgs())
    assert sel.complete() is False                        # 0 used twice


def test_no_tool_use_yields_all_missing(monkeypatch):
    _mock(monkeypatch, {}, content=[])                    # empty response, no tool_use
    sel = photo_select.select_corner_photos(_imgs())
    assert sel.picks == {s: -1 for s in SLOTS}
    assert sel.complete() is False


def test_only_first_max_photos_are_sent(monkeypatch):
    seen = {}
    def fake_create(_s, **k):
        seen["n_images"] = sum(1 for b in k["messages"][0]["content"]
                               if b.get("type") == "image")
        return _Resp([_Block({"selection": {s: -1 for s in SLOTS}, "reasoning": ""})])
    monkeypatch.setattr(photo_select.config, "VISION_MAX_PHOTOS", 3)
    monkeypatch.setattr(photo_select, "_get_client",
                        lambda: type("C", (), {"messages": type("M", (), {"create": fake_create})()})())
    photo_select.select_corner_photos(_imgs(10))
    assert seen["n_images"] == 3                          # capped at VISION_MAX_PHOTOS


def test_selection_complete_helper():
    assert Selection(picks={"front": 0, "rear": 1, "left_side": 2, "right_side": 3}).complete()
    assert not Selection(picks={"front": 0, "rear": 1, "left_side": 2, "right_side": -1}).complete()
