"""Unit tests for the WF-statement parser + check reconciliation (no PDF, no HTTP):
parse_lines gets synthetic statement text in both layouts; reconcile gets a temp
sqlite DB shaped like wells-check's wells.db."""
import sqlite3

import wells_reconcile as wr


def test_checks_paid_section_multi_per_line():
    lines = [
        "Checks paid",
        "Number Date Amount Number Date Amount Number Date Amount",
        "21726 6/05 400.00 21727 * 6/06 1,250.50 21728 6/09 89.99",
        "21730 ^ 6/12 3,820.00",
        "Total checks paid $5,560.49",
        "1234 6/20 999.99",                       # after section end -> must NOT parse
    ]
    out = wr.parse_lines(lines)
    got = {c["check_number"]: c["amount"] for c in out["checks"]}
    assert got == {"21726": 400.0, "21727": 1250.5, "21728": 89.99, "21730": 3820.0}
    assert all(c["source"] == "checks_paid" for c in out["checks"])


def test_transaction_history_check_rows():
    lines = [
        "Transaction history",
        "6/05 21726 Check 400.00 12,345.67",       # balance after amount -> first amt wins
        "6/06 Deposit Branch 500.00",              # not a check
        "6/09 21731 Cashed Check 220.00",
    ]
    out = wr.parse_lines(lines)
    got = {c["check_number"]: c["amount"] for c in out["checks"]}
    assert got == {"21726": 400.0, "21731": 220.0}


def test_checks_paid_wins_dedupe():
    lines = [
        "Checks paid",
        "21726 6/05 400.00",
        "Total",
        "6/05 21726 Check 400.00",
    ]
    out = wr.parse_lines(lines)
    assert len(out["checks"]) == 1
    assert out["checks"][0]["source"] == "checks_paid"


def _mk_db(tmp_path):
    p = str(tmp_path / "wells.db")
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE paid_orders(guid TEXT PRIMARY KEY, order_id TEXT,
        price REAL, reference_number TEXT, sent_date TEXT)""")
    con.execute("""CREATE TABLE wf_checks(check_number TEXT PRIMARY KEY, amount REAL,
        date TEXT, source_file TEXT, uploaded_at REAL)""")
    rows = [  # check 100: two orders summing 600; check 101: mismatch; VV102: prefix match
        ("g1", "A1", 400.0, "100", "2026-06-05"),
        ("g2", "A2", 200.0, "100", "2026-06-05"),
        ("g3", "A3", 999.0, "101", "2026-06-06"),
        ("g4", "A4", 150.0, "VV102", "2026-06-07"),
        ("g5", "A5", 75.0, "104", "2026-06-09"),   # inside 100-106, not on statement -> sd_only
        ("g6", "A6", 80.0, "999", "2026-06-10"),   # outside the statement's range -> ignored
    ]
    con.executemany("INSERT INTO paid_orders VALUES(?,?,?,?,?)", rows)
    wf = [("100", 600.0, "6/05", "s.pdf", 0), ("101", 900.0, "6/06", "s.pdf", 0),
          ("102", 150.0, "6/07", "s.pdf", 0), ("106", 50.0, "6/08", "s.pdf", 0)]
    con.executemany("INSERT INTO wf_checks VALUES(?,?,?,?,?)", wf)
    con.commit(); con.close()
    return p


def test_reconcile_groups(tmp_path):
    r = wr.reconcile(_mk_db(tmp_path))
    assert [m["check_number"] for m in r["matched"]] == ["100", "102"]   # bundle sum + VV prefix
    assert r["matched"][0]["orders_n"] == 2 and r["matched"][0]["sd_total"] == 600.0
    assert [m["check_number"] for m in r["mismatched"]] == ["101"]
    assert r["mismatched"][0]["diff"] == -99.0
    assert [w["check_number"] for w in r["wf_only"]] == ["106"]
    assert [s["sd_refs"] for s in r["sd_only"]] == ["104"]               # 999 out of range
    s = r["summary"]
    assert (s["wf_checks"], s["matched"], s["mismatched"], s["wf_only"], s["sd_only"]) \
        == (4, 2, 1, 1, 1)
