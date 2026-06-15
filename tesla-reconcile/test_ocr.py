"""
OCR diagnostic: shows exactly what the OCR engine reads from a real order's
delivery photos, so we can tell a setup problem (engine not installed) from an
accuracy one — and confirm the rotated (sideways/upside-down) VIN retry fires.

It runs the SAME path as the real pipeline: ocr.scan_for_vin with debug=True,
which scans every photo upright, and — if nothing qualifies — rescans rotated
(OCR_ROTATIONS, default 90/180/270). Watch for the "retry rotated" line and the
"(rotated)" tags to see the sideways pass working.

Usage:
    python test_ocr.py A3YF156     # a specific order id
    python test_ocr.py             # first qualifying order in the window
"""
import datetime as dt
import sys

import ocr
import superdispatch as sd
from auth import browser_context

WINDOW_START = dt.date(2026, 5, 1)
WINDOW_END = dt.date.today() - dt.timedelta(days=7)


def main(order_id):
    ok, msg = ocr.check_engine()
    print("OCR engine:", msg)
    print(f"Rotations retried when upright finds nothing: {ocr.OCR_ROTATIONS}")
    print("=" * 60)
    if not ok:
        return

    with browser_context() as ctx:
        page = ctx.new_page()
        bol = ctx.new_page()

        if order_id:
            order = sd.find_order_by_id(page, order_id)
        else:
            order = None
            for pageno in range(1, 6):
                page.goto(sd.invoiced_url(WINDOW_START, WINDOW_END,
                                          page=pageno, ascending=True))
                rows = sd.scrape_order_rows(page)
                order = next((r for r in rows if not r.should_skip), None)
                if order:
                    break
        if not order:
            print("No order found.")
            return

        detail = sd.open_order_detail(page, order)
        # get_bol_photos returns (sections, delivered_zip); sections is one
        # {'vin', 'urls'} per Delivery Inspection block (one per vehicle).
        sections, delivered_zip = sd.get_bol_photos(bol, detail.detail_url)
        default_vin = detail.vehicles[0].vin if detail.vehicles else ""
        print(f"{order.order_id}: {len(sections)} photo section(s) | "
              f"delivered zip {delivered_zip}\n")

        for sec in sections:
            vin = sec.get("vin") or default_vin
            images = sd.fetch_images(bol, sec.get("urls", []))
            print(f"-- scanning for {vin}: {len(images)} photos "
                  f"(need a run of >= {ocr.OCR_MIN_RUN} VIN chars) --")
            cand = ocr.scan_for_vin(images, vin, max_send=99, debug=True)
            print(f"   => candidate photo indices: {cand}"
                  f"{'   *** NO VIN PHOTO FOUND ***' if not cand else ''}\n")

        input("Press Enter to close...")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
