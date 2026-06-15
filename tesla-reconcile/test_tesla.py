"""
Standalone Tesla-only check for one or more VINs (no SuperDispatch needed).
Tabs/filters are set up ONCE, then each VIN just gets typed in — the Approved
tab stays active and the Claims filters stay applied between VINs.

Usage:
    python test_tesla.py 5YJ3E1EA3SF131143 7SAYGDEE5PA100889
    python test_tesla.py 5YJ3E1EA3SF131143
    python test_tesla.py                      # uses DEFAULT_VINS below
"""
import sys

import tesla
from auth import browser_context

DEFAULT_VINS = ["5YJ3E1EA3SF131143"]


def check_one(page, claims_page, vin):
    print(f"\nVIN: {vin}")
    pay = tesla.payment_check(page, vin)
    print(f"  PAYMENT  ok={pay.ok}  status={pay.status!r}  note={pay.note!r}")
    claim = tesla.claims_check(claims_page, vin)
    print(f"  CLAIMS   has_claim={claim.has_claim}  records={claim.record_count}")
    if not pay.ok:
        print("  VERDICT: payment blank -> SKIP & log")
    elif claim.has_claim:
        print("  VERDICT: damage claim -> tag 'Damage claim'")
    else:
        print("  VERDICT: payment ok + no claim -> proceed to BOL/photo check")


def main(vins):
    with browser_context() as ctx:
        page = ctx.new_page()
        claims_page = ctx.new_page()
        print(f"Testing {len(vins)} VIN(s)\n" + "=" * 44)

        # ---- one-time session setup (carried over for every VIN) ----
        tesla.setup_claims_filters(claims_page)   # all statuses + Destination
        tesla.ensure_approved(page)               # open Fleet > Approved once

        for vin in vins:
            check_one(page, claims_page, vin)

        print("\n" + "=" * 44)
        input("Press Enter to close the browser...")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_VINS)
