"""Wells Fargo statement parsing + check reconciliation against the wells-check DB.

parse_statement(pdf_bytes) pulls every CHECK entry (check number + the
Withdrawals/Debits amount) out of a monthly WF statement PDF. Two layouts are
recognized, applied line-by-line over the whole document:

  * the "Checks paid" summary section — rows of `number [*^] date amount`
    triplets, up to several per printed line (gated on the section header so a
    stray `1234 6/05 500.00` elsewhere can't fake a check);
  * "Transaction history" rows whose description mentions a check —
    `date  number  ...Check...  amount` (the amount is the Withdrawals/Debits
    column; WF prints the running balance after it on some rows, so the FIRST
    amount after the description is taken).

Entries are deduped by check number (the Checks-paid section wins). The debug
block reports what each strategy saw so a mis-parsed statement is tunable.

reconcile(db_path) compares the uploaded checks (wf_checks) with SuperDispatch
(paid_orders): a WF check matches on the check number ↔ SD payment reference —
compared on DIGITS ONLY, so a hand-typed ref like 'VV21851' still matches check
21851 — and its statement amount must equal SUM(price) across every SD order
sharing that reference (checks pay bundles of shipments).
"""
from __future__ import annotations
import io
import re
import sqlite3

_AMT = r"([\d,]+\.\d{2})"
# Checks-paid triplet: number, optional footnote mark, M/D date, amount.
_TRIPLET = re.compile(rf"\b(\d{{3,7}})\s*[*^#]?\s+(\d{{1,2}}/\d{{1,2}})\s+{_AMT}")
# Transaction-history check row: date first, then the check number, a description
# containing "check", then the Withdrawals/Debits amount.
_TXN = re.compile(
    rf"^\s*(\d{{1,2}}/\d{{1,2}})\s+(\d{{3,7}})\s+.*?\bcheck\b\S*.*?\s{_AMT}(?:\s|$)", re.I)
_SECTION_START = re.compile(r"\bchecks\s+paid\b", re.I)
_SECTION_END = re.compile(r"^\s*(total\b|transaction\s+history|daily\s+ledger|summary\s+of)",
                          re.I)


def _amount(s: str) -> float:
    return float(s.replace(",", ""))


def parse_lines(lines: list[str]) -> dict:
    """Pure core of parse_statement (unit-testable without a PDF)."""
    checks: dict[str, dict] = {}
    dbg = {"lines": len(lines), "checks_paid_hits": 0, "txn_hits": 0,
           "unparsed_check_lines": []}
    in_section = False
    for line in lines:
        # END before START: "Total checks paid …" CONTAINS "checks paid", so checking
        # start first would re-open the section on the very line that closes it.
        if in_section and _SECTION_END.match(line):
            in_section = False
            continue
        if _SECTION_START.search(line):
            in_section = True
            continue
        got = False
        if in_section:
            for num, date, amt in _TRIPLET.findall(line):
                checks[num] = {"check_number": num, "amount": _amount(amt),
                               "date": date, "source": "checks_paid"}
                dbg["checks_paid_hits"] += 1
                got = True
        if not got:
            m = _TXN.match(line)
            if m:
                date, num, amt = m.group(1), m.group(2), m.group(3)
                checks.setdefault(num, {"check_number": num, "amount": _amount(amt),
                                        "date": date, "source": "transaction_history"})
                dbg["txn_hits"] += 1
            elif "check" in line.lower() and re.search(r"\d{3,7}", line) \
                    and len(dbg["unparsed_check_lines"]) < 12:
                dbg["unparsed_check_lines"].append(line.strip()[:140])
    return {"checks": sorted(checks.values(), key=lambda c: int(c["check_number"])),
            "debug": dbg}


def parse_statement(data: bytes) -> dict:
    import pdfplumber
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = len(pdf.pages)
        for pg in pdf.pages:
            lines.extend((pg.extract_text() or "").splitlines())
    out = parse_lines(lines)
    out["debug"]["pages"] = pages
    return out


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or "")).lstrip("0")


def reconcile(db_path: str) -> dict:
    """Compare wf_checks against paid_orders. Groups:
    matched / mismatched (amount differs) / wf_only (check not found in SD) /
    sd_only (SD checks inside the statement's check-number range but absent
    from it — checks are sequential, so the range scopes the month)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        sd_by_digits: dict[str, dict] = {}
        for r in con.execute(
                "SELECT reference_number ref, COUNT(*) n, SUM(price) total,"
                " GROUP_CONCAT(order_id, ', ') orders"
                " FROM paid_orders WHERE reference_number IS NOT NULL"
                " AND reference_number != '' GROUP BY reference_number"):
            key = _digits(r["ref"])
            if not key:
                continue
            agg = sd_by_digits.setdefault(
                key, {"refs": [], "n": 0, "total": 0.0, "orders": []})
            agg["refs"].append(r["ref"])
            agg["n"] += r["n"]
            agg["total"] = round(agg["total"] + (r["total"] or 0), 2)
            agg["orders"].append(r["orders"] or "")
        try:
            wf = [dict(r) for r in con.execute(
                "SELECT check_number, amount, date, source_file FROM wf_checks")]
        except sqlite3.OperationalError:                  # table absent: nothing uploaded
            wf = []
        matched, mismatched, wf_only = [], [], []
        wf_keys = set()
        for c in sorted(wf, key=lambda x: int(_digits(x["check_number"]) or 0)):
            key = _digits(c["check_number"])
            wf_keys.add(key)
            agg = sd_by_digits.get(key)
            if not agg:
                wf_only.append({"check_number": c["check_number"], "wf_amount": c["amount"],
                                "date": c["date"]})
                continue
            diff = round((c["amount"] or 0) - agg["total"], 2)
            item = {"check_number": c["check_number"], "wf_amount": c["amount"],
                    "sd_total": agg["total"], "diff": diff, "orders_n": agg["n"],
                    "orders": ", ".join(o for o in agg["orders"] if o),
                    "sd_refs": ", ".join(agg["refs"]), "date": c["date"]}
            (matched if abs(diff) <= 0.005 else mismatched).append(item)
        sd_only = []
        nums = [int(_digits(c["check_number"])) for c in wf if _digits(c["check_number"])]
        if nums:
            lo, hi = min(nums), max(nums)
            for key, agg in sorted(sd_by_digits.items(),
                                   key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
                if key in wf_keys or not key.isdigit():
                    continue
                if lo <= int(key) <= hi:
                    sd_only.append({"sd_refs": ", ".join(agg["refs"]), "sd_total": agg["total"],
                                    "orders_n": agg["n"],
                                    "orders": ", ".join(o for o in agg["orders"] if o)})
        cov = con.execute("SELECT MIN(sent_date), MAX(sent_date), COUNT(*) FROM paid_orders"
                          " WHERE reference_number IS NOT NULL AND reference_number != ''"
                          ).fetchone()
        return {
            "summary": {
                "wf_checks": len(wf),
                "matched": len(matched), "matched_total": round(sum(m["wf_amount"] or 0 for m in matched), 2),
                "mismatched": len(mismatched),
                "wf_only": len(wf_only), "wf_only_total": round(sum(w["wf_amount"] or 0 for w in wf_only), 2),
                "sd_only": len(sd_only),
                "sd_coverage_from": cov[0], "sd_coverage_to": cov[1], "sd_checks": cov[2],
                "statement": (wf[0].get("source_file") if wf else None),
            },
            "matched": matched, "mismatched": mismatched,
            "wf_only": wf_only, "sd_only": sd_only,
        }
    finally:
        con.close()
