"""
Tests for the LOCAL YOLO car-parts selector's logic. The detector itself is not run
(no torch/weights needed): we test `_analyze` (one YOLO result -> PhotoStat) with a
fake result, and `select_from_stats` (PhotoStats -> picks) with synthetic stats.
"""
from __future__ import annotations

import photo_select_yolo as ps
from photo_select_yolo import PhotoStat, SLOTS, select_from_stats


# ----- fakes for _analyze -----
class _Box:
    def __init__(self, cls, conf, xyxy):
        self.cls = cls
        self.conf = conf
        self.xyxy = [xyxy]          # ultralytics: b.xyxy[0] -> [x1,y1,x2,y2]


class _Result:
    def __init__(self, names, boxes):
        self.names = names
        self.boxes = boxes


def _result(parts):
    """parts: list of (class_name, conf, x_center). Builds a fake YOLO result on a
    1000px-wide image with small boxes centered at x_center."""
    names = {i: n for i, n in enumerate(sorted({p[0] for p in parts}))}
    rev = {n: i for i, n in names.items()}
    boxes = [_Box(rev[n], c, [x - 10, 100, x + 10, 200]) for (n, c, x) in parts]
    return _Result(names, boxes)


def test_analyze_front_shot():
    r = _result([("hood", 0.9, 500), ("front_bumper", 0.8, 500),
                 ("front_glass", 0.7, 500), ("front_left_door", 0.6, 200)])
    st = ps._analyze(0, r, img_w=1000)
    assert st.is_full is True
    assert st.front > st.rear
    assert st.angle in ("front", "front_quarter")


def test_analyze_rejects_closeup():
    r = _result([("front_light", 0.8, 500)])      # single part -> not a full vehicle
    st = ps._analyze(3, r, img_w=1000)
    assert st.is_full is False
    assert st.angle == "other"


def test_analyze_facing_from_front_vs_rear_x():
    # front parts on the LEFT, rear parts on the RIGHT -> nose faces left in-frame.
    r = _result([("hood", 0.9, 200), ("front_bumper", 0.8, 250),
                 ("back_bumper", 0.8, 800), ("back_glass", 0.7, 820), ("wheel", 0.6, 500)])
    st = ps._analyze(0, r, img_w=1000)
    assert st.facing == "left"


# ----- select_from_stats -----
def _stat(i, angle, front=0.0, rear=0.0, wheels=0, doors=0.0, spread=0.6,
          facing=None, full=True, ndist=5):
    return PhotoStat(index=i, angle=angle, front=front, rear=rear, wheels=wheels,
                     doors=doors, spread=spread, facing=facing, is_full=full,
                     n_distinct=ndist)


def test_full_selection_picks_one_per_slot():
    stats = [
        _stat(0, "other", full=False, ndist=0),               # junk
        _stat(1, "front", front=2.3, rear=0.0),               # best front
        _stat(2, "front_quarter", front=2.1, wheels=2, doors=1.0),
        _stat(3, "rear", rear=1.4, front=0.5),                # best rear (canonical)
        _stat(4, "rear_quarter", rear=2.5, wheels=3, doors=0.8),  # higher score but a quarter
        _stat(5, "side", front=0.5, rear=0.4, wheels=2, doors=1.3, facing="right"),
        _stat(6, "side", front=0.4, rear=1.0, wheels=3, doors=1.0, facing="left"),
    ]
    sel = select_from_stats(stats)
    assert sel.picks["front"] == 1
    assert sel.picks["rear"] == 3                 # canonical rear beats the quarter (4)
    assert set([sel.picks["left_side"], sel.picks["right_side"]]) == {5, 6}
    assert sel.complete() is True
    # opposite facing -> mapped to the matching slots
    assert sel.picks["left_side"] == 6 and sel.picks["right_side"] == 5


def test_junk_never_selected():
    stats = [_stat(0, "other", front=5.0, full=False), _stat(1, "front", front=1.0)]
    sel = select_from_stats(stats)
    assert sel.picks["front"] == 1                # the non-full high-front junk is ignored


def test_quarter_fallback_when_no_clean_front():
    stats = [
        _stat(0, "front_quarter", front=1.5, wheels=2, doors=1.0),  # only a quarter
        _stat(1, "rear", rear=1.2),
    ]
    sel = select_from_stats(stats)
    assert sel.picks["front"] == 0                # falls back to the front quarter


def test_missing_sides_reported():
    stats = [_stat(0, "front", front=2.0), _stat(1, "rear", rear=2.0)]
    sel = select_from_stats(stats)
    assert sel.picks["front"] == 0 and sel.picks["rear"] == 1
    assert sel.complete() is False
    assert "left_side" in sel.missing() and "right_side" in sel.missing()
