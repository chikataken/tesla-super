"""Pull a vehicle's SuperDispatch inspection photos via the OFFICIAL Shipper API.

Uses the documented OAuth API (your SUPERDISPATCH_CLIENT_ID/SECRET via sd_api) —
no browser, no scraping, no session token:
  * sd_api.find_by_vin(vin)        -> orders the VIN sits on (GET /v1/public/orders/find_by_vin/{vin})
  * sd_api.get_order(guid)         -> full order; vehicles[].photos[] each =
        {photo_url (storage.googleapis JPG), photo_type: 'Pickup'|'Delivery',
         latitude, longitude, created_at, guid}

Defaults to DELIVERY photos (the VIN shot lives there; we never need Pickup). Some
vehicles genuinely have no VIN photo — the caller then falls back to a plain shot of
the car. `photo_type` does NOT encode camera angle and photos are SD-watermarked +
EXIF-rotated, so angle/VIN selection is a separate vision step.

CLI:  python sd_photos.py <VIN> [--type Delivery|Pickup|all] [--out DIR]
"""
from __future__ import annotations
import argparse, os
import requests

import sd_api


def order_for_vin(vin: str) -> dict | None:
    orders = sd_api.find_by_vin(vin) or []
    if not orders:
        return None
    guid = (orders[0] or {}).get("guid")
    return sd_api.get_order(guid) if guid else None


def photos(order: dict, vin: str | None, ptype: str | None) -> list[dict]:
    out = []
    for v in order.get("vehicles") or []:
        if vin and (v.get("vin") or "").upper() != vin.upper():
            continue
        for p in v.get("photos") or []:
            if ptype and (p.get("photo_type") or "").lower() != ptype.lower():
                continue
            url = p.get("photo_url") or p.get("rendered_photo_url")
            if url:
                out.append({"url": url, "type": p.get("photo_type"), "guid": p.get("guid"),
                            "lat": p.get("latitude"), "lng": p.get("longitude"),
                            "created_at": p.get("created_at"), "vin": v.get("vin")})
    return out


def download(items: list[dict], dest: str) -> list[str]:
    os.makedirs(dest, exist_ok=True)
    saved = []
    for i, p in enumerate(items):
        try:
            r = requests.get(p["url"], timeout=60)
            if r.ok:
                fn = os.path.join(dest, f"{i:02d}_{p['type']}_{(p['guid'] or '')[:8]}.jpg")
                with open(fn, "wb") as f:
                    f.write(r.content)
                saved.append(fn)
        except Exception as e:                       # noqa: BLE001
            print(f"  download failed [{i}]: {e}")
    return saved


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("vin")
    ap.add_argument("--type", default="Delivery", help="Delivery (default) | Pickup | all")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    ptype = None if a.type.lower() == "all" else a.type

    order = order_for_vin(a.vin)
    if not order:
        print(f"no SD order found for VIN {a.vin}"); return
    items = photos(order, a.vin, ptype)
    print(f"order {order.get('number')} (guid {order.get('guid')}): "
          f"{len(items)} {ptype or 'all'} photo(s) for {a.vin}")
    if not items:
        print("  (no photos of that type — caller should fall back to a plain car shot)")
        return
    dest = a.out or os.path.join("sd_photos", a.vin)
    saved = download(items, dest)
    print(f"downloaded {len(saved)} -> {dest}/")


if __name__ == "__main__":
    main()
