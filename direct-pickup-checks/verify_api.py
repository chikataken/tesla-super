"""
One-shot, READ-ONLY probe to settle the open API questions before we build the
tagging step. It does NOT create, modify, or tag anything by default.

It answers, against YOUR live/sandbox tenant:
  1. Do the OAuth credentials work?                              (auth)
  2. What are the real webhook action names?                     (confirms our set)
  3. Does the ORDER object expose a `tags` field we could write? (the key question)
  4. Does the inspection-photos endpoint shape match our assumptions?

Setup: put credentials in ../secrets/.env (shared) or this folder's .env:
    SUPERDISPATCH_CLIENT_ID=...
    SUPERDISPATCH_CLIENT_SECRET=...
    SD_ENV=test            # or production

Run:
    python verify_api.py                       # auth + webhook actions only (safest)
    python verify_api.py --order <ORDER_GUID>  # also GET that order + its photos
    python verify_api.py --vin <VIN>           # find an order guid by VIN, then probe it

The OPTIONAL write test (only if you explicitly ask) checks whether PATCH accepts a
tags field. It is OFF unless you pass --try-tag-write WITH an order guid, and it
prints exactly what it would send and asks nothing else of you:
    python verify_api.py --order <GUID> --try-tag-write
"""
from __future__ import annotations
import argparse
import json

import config
import sd_client


def _print_keys(label: str, obj: dict) -> None:
    print(f"\n{label} — top-level keys:")
    for k in sorted(obj.keys()):
        v = obj[k]
        kind = type(v).__name__
        preview = "" if isinstance(v, (dict, list)) else f" = {v!r}"
        print(f"    {k:24} ({kind}){preview}")


def probe_tags(order: dict) -> None:
    print("\n" + "=" * 64)
    print("TAGS — does the order object expose a writable tags field?")
    candidates = [k for k in order if "tag" in k.lower() or "label" in k.lower()]
    if candidates:
        print(f"  FOUND tag-like field(s): {candidates}")
        for k in candidates:
            print(f"    {k} = {order[k]!r}")
        print("  -> If a create/PATCH accepts this field, we can tag FULLY via API.")
    else:
        print("  No `tags`/`label` field on the order object.")
        print("  -> Strong sign the public API can't write tags (matches tesla-reconcile,")
        print("     which applies tags by scraping the web edit page). Confirm by checking")
        print("     the create/PATCH request schema in the API reference.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", help="order GUID to GET and inspect")
    ap.add_argument("--vin", help="find an order GUID by VIN, then inspect it")
    ap.add_argument("--try-tag-write", action="store_true",
                    help="attempt a PATCH with a tags field (mutates — off by default)")
    args = ap.parse_args()

    print(f"SD_ENV={config.SD_ENV}  base={config.SD_API_BASE}")

    # 1. auth
    try:
        tok = sd_client.get_token()
        print(f"[1] AUTH OK — token starts {tok[:16]}…")
    except Exception as e:                                   # noqa: BLE001
        print(f"[1] AUTH FAILED: {e}")
        return 1

    # 2. webhook actions (confirm the names we subscribe to are real)
    try:
        actions = sd_client.list_webhook_actions()
        names = sorted({str(a.get('action') or a.get('name') or a)
                        if isinstance(a, dict) else str(a) for a in actions})
        print(f"\n[2] WEBHOOK ACTIONS ({len(actions)}):")
        for n in names:
            print(f"    {n}")
        want = set(config.SUBSCRIBE_ACTIONS)
        have = set(names)
        print(f"    we want: {sorted(want)}")
        miss = want - have
        print("    MISSING from live list: " + (str(sorted(miss)) if miss else "none ✅"))
    except Exception as e:                                   # noqa: BLE001
        print(f"\n[2] could not list webhook actions: {e}")

    # 3. order + tags
    guid = args.order
    if args.vin and not guid:
        try:
            hits = sd_client.find_by_vin(args.vin) if hasattr(sd_client, "find_by_vin") else []
            guid = (hits[0].get("guid") or hits[0].get("id")) if hits else None
            print(f"\n[3] find_by_vin({args.vin}) -> guid={guid}")
        except Exception as e:                               # noqa: BLE001
            print(f"\n[3] find_by_vin failed: {e}")

    if guid:
        try:
            order = sd_client.get_order(guid)
            _print_keys("[3] ORDER", order)
            probe_tags(order)
        except Exception as e:                               # noqa: BLE001
            print(f"\n[3] get_order failed: {e}")
            order = {}

        # 4. inspection photos shape
        try:
            photos = sd_client.get_inspection_photos(guid)
            print(f"\n[4] INSPECTION PHOTOS: {len(photos)} record(s)")
            if photos:
                print("    first record raw:")
                print("   ", json.dumps(photos[0], indent=2)[:800])
                print("    normalized ->", sd_client.normalize_photo(photos[0]))
        except Exception as e:                               # noqa: BLE001
            print(f"\n[4] get_inspection_photos failed (verify endpoint): {e}")

        if args.try_tag_write:
            body = {"tags": ["CLAUDE"]}
            print("\n[5] TAG-WRITE TEST (mutates) — sending PATCH merge-patch:")
            print(f"    {json.dumps(body)}")
            try:
                # uses the same merge-patch content type the sibling client uses
                resp = sd_client._request(
                    "PATCH", sd_client.PATH_ORDER.format(guid=guid), json=body)
                print(f"    PATCH accepted. Response tags-like fields: "
                      f"{[k for k in (resp.get('data',{}).get('object',{}) if isinstance(resp,dict) else {}) if 'tag' in k.lower()]}")
                print("    -> Re-run with --order to confirm the tag actually stuck.")
            except Exception as e:                           # noqa: BLE001
                print(f"    PATCH rejected/ignored: {e}")
                print("    -> Confirms tags are NOT writable via the API.")
    else:
        print("\n[3] (no --order/--vin given) skipping order + tags probe.")
        print("    Re-run with --order <GUID> to inspect an order's fields for `tags`.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
