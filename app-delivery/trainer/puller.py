"""
Orchestrate a random training pull: scrape VINs off the SD web list (tesla-reconcile
venv) -> fetch their Delivery photos via the official API (shipment-creator venv),
dedup on the order/shipment GUID. Loops over fresh random windows until it has N new
shipments (or runs out of rounds).

Runs in app-delivery's venv (only subprocess + json + sqlite). Used by the labeler's
auto-top-up and by `./train.sh pull N` (pull_random.py).
"""
from __future__ import annotations
import json
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
TR_PY = os.path.join(REPO_ROOT, "tesla-reconcile", ".venv", "bin", "python")
TR_DIR = os.path.join(REPO_ROOT, "tesla-reconcile")
SC_PY = os.path.join(REPO_ROOT, "shipment-creator", ".venv", "bin", "python")
SC_DIR = os.path.join(REPO_ROOT, "shipment-creator")
SCRAPE = os.path.join(HERE, "scrape_vins.py")
FETCH = os.path.join(HERE, "fetch_api.py")

POOL = os.path.join(HERE, "pool")
DB = os.path.join(HERE, "seen.db")


def _need_venvs() -> str | None:
    if not os.path.exists(TR_PY):
        return f"tesla-reconcile venv missing ({TR_PY}) — needed to scrape VINs off the SD orders list."
    if not os.path.exists(SC_PY):
        return f"shipment-creator venv missing ({SC_PY}) — needed to fetch photos via the SD API."
    return None


def pull_batch(n: int = 20, pool: str = POOL, db: str = DB,
               rounds: int = 4, scrape_factor: int = 4, timeout: int = 1800,
               log=lambda m: None) -> dict:
    """Pull up to `n` NEW shipments. Returns {"new", "log"}."""
    err = _need_venvs()
    if err:
        return {"new": 0, "log": [err], "error": err}
    os.makedirs(pool, exist_ok=True)
    total, logs = 0, []
    with tempfile.TemporaryDirectory() as tmp:
        for rnd in range(rounds):
            if total >= n:
                break
            vins_json = os.path.join(tmp, f"vins{rnd}.json")
            fetch_out = os.path.join(tmp, f"fetch{rnd}.json")
            # 1) scrape VINs (tesla-reconcile venv, fresh random window each round)
            try:
                subprocess.run([TR_PY, SCRAPE, "--n", str(max(40, n * scrape_factor)),
                                "--out", vins_json], cwd=TR_DIR, timeout=timeout,
                               capture_output=True, text=True)
                vins = json.load(open(vins_json)).get("vins", []) if os.path.exists(vins_json) else []
            except Exception as e:                      # noqa: BLE001
                logs.append(f"round {rnd + 1}: scrape error: {e}")
                break
            logs.append(f"round {rnd + 1}: scraped {len(vins)} VIN(s)")
            log(logs[-1])
            if not vins:
                continue
            # 2) fetch photos via the official API (shipment-creator venv), dedup on guid
            try:
                subprocess.run([SC_PY, FETCH, "--pool", pool, "--db", db,
                                "--max-new", str(n - total), "--vins-json", vins_json,
                                "--out", fetch_out], cwd=SC_DIR, timeout=timeout,
                               capture_output=True, text=True)
                res = json.load(open(fetch_out)) if os.path.exists(fetch_out) else {}
            except Exception as e:                      # noqa: BLE001
                logs.append(f"round {rnd + 1}: fetch error: {e}")
                break
            new = res.get("new", 0)
            total += new
            logs.append(f"  fetched {new} new shipment(s) (checked {res.get('checked')})")
            log(logs[-1])
    logs.append(f"DONE: {total} new shipment(s)")
    return {"new": total, "log": logs}
