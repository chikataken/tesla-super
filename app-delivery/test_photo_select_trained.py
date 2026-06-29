"""
Tests for the trained-head selector's pure selection logic (corner scheme). The CLIP
encoder and joblib model are NOT loaded — we build PhotoScore objects (class
probabilities) and check select_from_scores: reject gate, per-slot argmax, distinct.
"""
from __future__ import annotations

from photo_select_trained import PhotoScore, SLOTS, select_from_scores, best_key

ANGLES = ["front", "rear", "front_left", "front_right", "rear_left", "rear_right"]


def _s(i, **probs):
    p = {a: 0.0 for a in ANGLES}
    p["reject"] = 0.0
    p.update(probs)
    return PhotoScore(index=i, probs=p)


def test_picks_argmax_per_corner_slot():
    scores = [
        _s(0, reject=0.9),
        _s(1, front=0.8),
        _s(2, rear=0.85),
        _s(3, front_left=0.9),
        _s(4, front_right=0.88),
        _s(5, rear_left=0.7),
        _s(6, rear_right=0.75),
    ]
    sel = select_from_scores(scores)
    assert sel.picks == {"front": 1, "rear": 2, "front_left": 3,
                         "front_right": 4, "rear_left": 5, "rear_right": 6}
    assert sel.complete() is True


def test_reject_top_class_excluded():
    scores = [_s(0, front=0.4, reject=0.6), _s(1, front=0.5, reject=0.1)]
    sel = select_from_scores(scores)
    assert sel.picks["front"] == 1


def test_distinct_picks_when_overlap():
    # one photo is the best for two corners; slots stay distinct.
    scores = [_s(0, front_left=0.6, rear_left=0.55), _s(1, rear_left=0.5)]
    sel = select_from_scores(scores)
    assert sel.picks["front_left"] == 0
    assert sel.picks["rear_left"] == 1


def test_missing_slot_reported():
    sel = select_from_scores([_s(0, front=0.9), _s(1, rear=0.9)])
    assert sel.picks["front"] == 0 and sel.picks["rear"] == 1
    assert sel.complete() is False
    assert "front_left" in sel.missing()


def test_top_and_is_car():
    assert _s(0, front=0.7, reject=0.2).is_car is True
    assert _s(0, reject=0.7, front=0.2).is_car is False


def test_best_key_recovered_when_reject_edges_it_out():
    # A real key card that the model scores reject-top, but with a strong key prob,
    # is still recovered (we rank by key prob, not argmax).
    scores = [_s(0, front=0.9),
              _s(1, black_key=0.42, white_key=0.25, reject=0.45)]   # top class = reject
    i, c, p = best_key(scores)
    assert i == 1 and c == "black_key" and p == 0.42


def test_best_key_none_below_threshold():
    # Junk frames with only a little key signal are NOT posted as a key.
    scores = [_s(0, reject=0.6, black_key=0.20), _s(1, front=0.7, black_key=0.10)]
    assert best_key(scores) == (None, None, 0.0)


def test_best_key_picks_strongest_and_color():
    scores = [_s(0, black_key=0.40), _s(1, white_key=0.55, black_key=0.10)]
    i, c, p = best_key(scores)
    assert i == 1 and c == "white_key" and p == 0.55
