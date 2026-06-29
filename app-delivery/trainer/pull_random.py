"""
CLI: pull N random NEW shipments into the labeling pool (scrape VINs off the SD web
list -> fetch their photos via the official SD API, dedup on shipment guid).

Runs in app-delivery's venv; it orchestrates the tesla-reconcile (scrape) and
shipment-creator (API fetch) venvs under the hood — see puller.pull_batch.

    python trainer/pull_random.py --n 20
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import puller                                            # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="how many NEW shipments to pull")
    ap.add_argument("--pool", default=puller.POOL)
    ap.add_argument("--db", default=puller.DB)
    ap.add_argument("--rounds", type=int, default=4)
    a = ap.parse_args()
    res = puller.pull_batch(a.n, pool=a.pool, db=a.db, rounds=a.rounds, log=print)
    print(f"DONE: {res['new']} new shipment(s) pulled")


if __name__ == "__main__":
    main()
