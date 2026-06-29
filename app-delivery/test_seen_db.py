import os, tempfile
import trainer.seen_db as db

def test_dedup_by_shipment_not_vin():
    d = tempfile.mkdtemp(); p = os.path.join(d, "s.db")
    con = db.connect(p)
    assert db.count(con) == 0
    db.mark(con, "order/AAA", ["VIN1", "VIN2"], 20)
    assert db.is_seen(con, "order/AAA") is True
    assert db.is_seen(con, "order/BBB") is False     # different shipment not seen
    db.mark(con, "order/BBB", ["VIN1"], 18)          # same VIN, different shipment -> ok
    assert db.count(con) == 2

def test_mark_is_idempotent():
    d = tempfile.mkdtemp(); con = db.connect(os.path.join(d, "s.db"))
    db.mark(con, "k", ["V"], 5); db.mark(con, "k", ["V"], 5)
    assert db.count(con) == 1
