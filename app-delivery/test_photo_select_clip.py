"""
Tests for the CLIP selector's pure selection logic. The CLIP model is NOT loaded
(no torch/open_clip needed): we build PhotoScore objects directly and check
select_from_scores — the reject gate, per-slot picks, and L/R side ordering.
"""
from __future__ import annotations

import photo_select_clip as ps
from photo_select_clip import PhotoScore, SLOTS, select_from_scores


def _s(i, front=0.0, rear=0.0, side=0.0, reject=0.0, rightness=0.5):
    return PhotoScore(index=i, front=front, rear=rear, side=side,
                      reject=reject, rightness=rightness)


def test_picks_one_per_slot_and_orders_sides_by_rightness():
    scores = [
        _s(0, reject=0.9),                                  # junk -> excluded
        _s(1, front=0.97),                                  # front
        _s(2, rear=1.0),                                    # rear
        _s(3, side=0.95, rightness=0.2),                    # a left-ish side
        _s(4, side=0.90, rightness=0.8),                    # a right-ish side
    ]
    sel = select_from_scores(scores)
    assert sel.picks["front"] == 1
    assert sel.picks["rear"] == 2
    assert sel.picks["right_side"] == 4        # higher rightness -> right slot
    assert sel.picks["left_side"] == 3
    assert sel.complete() is True


def test_reject_gate_excludes_high_reject_photos():
    # A photo that looks front-ish but is mostly 'reject' must not be picked.
    scores = [_s(0, front=0.45, reject=0.55), _s(1, front=0.40, reject=0.10)]
    sel = select_from_scores(scores)
    assert sel.picks["front"] == 1             # the non-rejected one, even if lower front


def test_strongest_of_each_class_wins():
    scores = [_s(0, front=0.6), _s(1, front=0.95), _s(2, rear=0.9)]
    sel = select_from_scores(scores)
    assert sel.picks["front"] == 1             # 0.95 beats 0.6


def test_missing_sides_reported():
    scores = [_s(0, front=0.9), _s(1, rear=0.9)]
    sel = select_from_scores(scores)
    assert sel.picks["front"] == 0 and sel.picks["rear"] == 1
    assert sel.complete() is False
    assert "left_side" in sel.missing() and "right_side" in sel.missing()


def test_top_property_and_is_car():
    assert _s(0, side=0.8, reject=0.1).top == "side"
    assert _s(0, reject=0.9).is_car is False
    assert _s(0, front=0.8, reject=0.2).is_car is True


def test_single_side_fills_one_slot_by_rightness():
    sel = select_from_scores([_s(0, front=0.9), _s(1, rear=0.9), _s(2, side=0.9, rightness=0.9)])
    assert sel.picks["right_side"] == 2 and sel.picks["left_side"] == -1
