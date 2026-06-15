"""
Prototype: flag a SuperDispatch shipment (found by VIN) with a tag, and verify.

Searches the orders page for the VIN, opens the edit form, reads current tags,
adds the requested tag if it's missing, saves, then re-reads to confirm.

Usage:
    python test_flag.py 7G2CEHEE4RA000318
    python test_flag.py 7G2CEHEE4RA000318 "Delivery confirmed"
"""
import sys

import config
import superdispatch as sd
from auth import browser_context


def main(vin, tag):
    with browser_context() as ctx:
        page = ctx.new_page()
        print(f"VIN: {vin}   tag to apply: '{tag}'\n" + "-" * 48)

        row = sd.find_order_by_vin(page, vin)
        if not row:
            print("No order found for that VIN.")
            return
        edit_url = sd.edit_url_for(row.detail_url)
        print(f"Found order {row.order_id}")

        before = sd.read_edit_tags(page, edit_url)
        print(f"Tags before: {before}")

        if tag in before:
            print(f"'{tag}' already applied — no change needed (check path).")
        else:
            sd.add_tags(page, edit_url, [tag])
            print(f"Applied '{tag}' and saved.")

        after = sd.read_edit_tags(page, edit_url)
        print(f"Tags after:  {after}")
        print("-" * 48)
        print("RESULT:", "PASS ✓" if tag in after else "FAIL ✗")

        input("\nPress Enter to close the browser...")


if __name__ == "__main__":
    vin = sys.argv[1] if len(sys.argv) > 1 else "7G2CEHEE4RA000318"
    tag = sys.argv[2] if len(sys.argv) > 2 else config.TAG_DELIVERY_CONFIRMED
    main(vin, tag)
