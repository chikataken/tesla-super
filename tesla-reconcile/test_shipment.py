"""
End-to-end prototype for ONE shipment (BOL/photos deferred).

Given a SuperDispatch shipment ID, Playwright:
  1. looks it up in SuperDispatch,
  2. extracts every VIN,
  3. checks each VIN on Tesla (Approved payment + Filed claims),
  4. decides the flag and applies it to the shipment.

Flag logic (without BOL yet):
  - any VIN with blank payment  -> no flag, leave for manual review
  - any VIN with a damage claim  -> 'Damage claim'
  - otherwise (all paid, no claims) -> 'Delivery confirmed'

Usage:
    python test_shipment.py A3YH673-1
    python test_shipment.py A3YH673-1 --dry-run     # decide but don't write
"""
import argparse

import config
import superdispatch as sd
import tesla
from auth import browser_context


def decide_flag(results):
    """results: list of (vin, PaymentResult, ClaimResult)."""
    if any(not pay.ok for _, pay, _ in results):
        return None, "a VIN has blank payment -> SKIP & log (manual review)"
    if any(claim.has_claim for _, _, claim in results):
        return config.TAG_DAMAGE_CLAIM, "a VIN has a damage claim"
    return config.TAG_DELIVERY_CONFIRMED, "all VINs paid, no claims (BOL deferred)"


def main(order_id, dry_run):
    with browser_context() as ctx:
        sd_page = ctx.new_page()
        tesla_page = ctx.new_page()
        claims_page = ctx.new_page()

        print(f"Shipment: {order_id}   dry_run={dry_run}\n" + "=" * 54)
        row = sd.find_order_by_id(sd_page, order_id)
        if not row:
            print("Shipment not found.")
            return

        detail = sd.open_order_detail(sd_page, row)
        vins = [v.vin for v in detail.vehicles]
        print(f"Found {row.order_id}: {len(vins)} VIN(s): {vins}\n" + "-" * 54)
        if not vins:
            print("No VINs extracted.")
            return

        # Tesla: set up once, then every VIN (tabs/filters carried over)
        tesla.setup_claims_filters(claims_page)
        tesla.ensure_approved(tesla_page)
        results = []
        for vin in vins:
            pay = tesla.payment_check(tesla_page, vin)
            claim = tesla.claims_check(claims_page, vin)
            print(f"  {vin}  payment={pay.ok}/{pay.status!r}  "
                  f"claim={claim.has_claim}({claim.record_count})")
            results.append((vin, pay, claim))

        tag, why = decide_flag(results)
        print("-" * 54)
        print(f"Decision: {why}")

        if tag is None:
            print("No flag applied (left for manual review).")
        else:
            edit_url = sd.edit_url_for(row.detail_url)
            existing = sd.read_edit_tags(sd_page, edit_url)
            if tag in existing:
                print(f"'{tag}' already applied — no change.")
            elif dry_run:
                print(f"[dry-run] would flag '{tag}'.")
            else:
                sd.add_tags(sd_page, edit_url, [tag])
                after = sd.read_edit_tags(sd_page, edit_url)
                ok = tag in after
                print(f"Flagged '{tag}' -> {'PASS' if ok else 'FAIL'}  (tags now: {after})")

        print("=" * 54)
        input("\nPress Enter to close the browser...")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("order_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    main(args.order_id, args.dry_run)
