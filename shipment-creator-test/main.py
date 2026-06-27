"""
Parse an Excel sheet -> group VINs into shipments -> preview.

    python main.py --excel sheet.xlsx
    python main.py --excel sheet.xlsx --sheet "Loads"

This stage writes nothing. Later stages (Tesla BOL download, SuperDispatch API
create) plug in after this preview looks right.
"""
import argparse
import sys

# Accept non-ASCII output on a legacy Windows console; no-op on macOS/Linux.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
import excel_ingest
import grouping


def _preview(shipments, report, warnings):
    print("=" * 64)
    print("COLUMN MAPPING (canonical <- your header):")
    for canonical, header in sorted(report.column_mapping.items()):
        print(f"   {canonical:18} <- {header!r}")
    if report.unmapped_headers:
        print(f"   (unmapped columns ignored: {report.unmapped_headers})")
    if report.missing_required:
        print(f"   !! MISSING REQUIRED COLUMNS: {report.missing_required}")

    print("-" * 64)
    print(f"Rows: {report.total_rows} total, {report.good_rows} good, "
          f"{len(report.bad_rows)} with errors")
    for r in report.bad_rows:
        print(f"   row {r.row_number}: {'; '.join(r.errors)}")

    if warnings:
        print("-" * 64)
        print("GROUPING WARNINGS:")
        for w in warnings:
            print(f"   {w}")

    print("-" * 64)
    print(f"SHIPMENTS: {len(shipments)}")
    for s in shipments:
        vins = ", ".join(v.vin for v in s.vehicles)
        pu = f"{s.pickup.get('city','?')}, {s.pickup.get('state','?')} {s.pickup.get('zip','')}"
        do = f"{s.delivery.get('city','?')}, {s.delivery.get('state','?')} {s.delivery.get('zip','')}"
        price = f"${s.price:,.2f}" if s.price is not None else "—"
        print(f"   [{s.group_key}]  {len(s.vehicles)} veh  {pu}  ->  {do}  "
              f"{price}  (rows {s.source_rows})")
        if len(s.vehicles) > 1:
            print(f"        VINs: {vins}")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", default=config.DEFAULT_EXCEL,
                    help="Path to the .xlsx sheet (defaults to config.DEFAULT_EXCEL)")
    ap.add_argument("--sheet", default=None, help="Worksheet name (default: first)")
    ap.add_argument("--download-bols", action="store_true",
                    help="After preview, open Tesla and download a BOL per VIN's "
                         "most-recent shipment (deduped by shipment).")
    ap.add_argument("--limit", type=int, default=0,
                    help="With --download-bols, only process the first N VINs (0 = all).")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel browser tabs for BOL downloads (default 4).")
    ap.add_argument("--create", action="store_true",
                    help="Build SuperDispatch order payloads from the shipments.")
    ap.add_argument("--live", action="store_true",
                    help="With --create, actually POST the orders (otherwise dry-run).")
    ap.add_argument("--headed", action="store_true",
                    help="Force a visible, interactive browser window for this run — "
                         "overrides WINDOW_MODE=ghost / HEADLESS so you can watch it "
                         "and click things yourself.")
    args = ap.parse_args()
    if args.headed:
        config.WINDOW_MODE = "visible"
        config.HEADLESS = False

    import os
    import profiles
    # This run is for ONE dispatcher: the web app passes SC_PROFILE; the CLI falls back
    # to the globally-selected dispatcher. Drives BOTH the state filter and which board
    # (output folder) we write to, so each dispatcher's flow stays independent.
    _pid = (os.getenv("SC_PROFILE") or "").strip() or (profiles.active_id() or "")
    _profile = profiles.get_profile(_pid)
    rows, report = excel_ingest.read_rows(args.excel, args.sheet)
    rows = profiles.filter_rows(rows, _profile)        # only the dispatcher's states
    if _profile:
        kept = profiles.allowed_states(_profile)
        print(f"Dispatcher profile: {_profile.get('name')} "
              f"({'states ' + ','.join(sorted(kept)) if kept else 'no state filter'}) "
              f"-> {len(rows)} VIN(s) after filtering.")
    shipments, warnings = grouping.build_shipments(rows)
    _preview(shipments, report, warnings)

    if args.download_bols:
        import tesla_bol
        vins = [r.vin for r in rows if r.ok]
        if args.limit:
            vins = vins[:args.limit]
        print(f"\nDownloading BOLs for {len(vins)} VIN(s) on {args.workers} tab(s)...")
        results = tesla_bol.download_for(vins, workers=args.workers)
        ok = sum(1 for v in results.values() if v["path"])
        print(f"\nBOLs: {ok}/{len(results)} VIN(s) have a saved BOL.")

    if args.create:
        import json
        import pdf_read
        import sd_api
        import tesla_bol
        import terminals_lookup
        import terminals_db
        dry = not args.live

        import os
        import datetime
        import glob
        import paths
        _pbase = paths.profile_output_dir(_pid)        # this dispatcher's own board folder
        orders_dir = os.path.join(_pbase, "orders")
        os.makedirs(orders_dir, exist_ok=True)

        # Persistence: load the current board (newest staged file) and PRESERVE it.
        # New VINs are appended as their own new orders; manual splits/positions and
        # already-staged VINs are left exactly as they are.
        prior_files = sorted(glob.glob(os.path.join(orders_dir, "*.json")),
                             key=os.path.getmtime, reverse=True)
        base = []
        if prior_files:
            try:
                with open(prior_files[0]) as f:
                    base = json.load(f)
            except (OSError, ValueError):
                base = []
        known_vins = {v.get("vin") for o in base for v in o.get("vehicles", [])
                      if v.get("vin")}
        base_numbers = {o.get("number") for o in base if o.get("number")}

        # VINs set aside in the spare workspace (output/spares.json) live off the
        # board on purpose — treat them as known so a re-run doesn't resurrect them.
        try:
            with open(os.path.join(_pbase, "spares.json")) as f:
                known_vins |= {s.get("vin") for s in json.load(f) if s.get("vin")}
        except (OSError, ValueError):
            pass

        # Excel supplies ONLY vin -> (cost, shipment number), in spreadsheet order.
        # Everything else (route, venue, vehicle) comes from the BOL.
        ordered_all = [(r.vin, r.get("price"), (r.get("group_id") or "").strip())
                       for r in rows if r.ok]
        ordered = [t for t in ordered_all if t[0] not in known_vins]   # skip staged
        skipped = len(ordered_all) - len(ordered)
        # Download/populate order: alphabetical by pickup STATE, city as tiebreaker
        # (from the Excel), so same-state shipments come through together — instead of
        # spreadsheet order. (tesla_bol preserves this order; it no longer re-sorts.)
        _pk = {r.vin: ((r.get("pickup_state") or "").strip().lower(),
                       (r.get("pickup_city") or "").strip().lower())
               for r in rows if r.ok}
        ordered.sort(key=lambda t: _pk.get(t[0], ("zzzz", "zzzz")))
        if args.limit:
            ordered = ordered[:args.limit]               # limit the NEW VINs

        # Terminal names from the Excel (OriginLocation/DestinationLocation) — the ONLY
        # key we match against the local terminal cache. When BOTH a shipment's terminals
        # are already cached we skip the Tesla portal for it; otherwise we fetch the BOL
        # and LEARN the new terminal(s) so next time it's skippable.
        names_by_vin = {r.vin: ((r.get("pickup_name") or "").strip(),
                                (r.get("delivery_name") or "").strip())
                        for r in rows if r.ok}

        stage = os.path.join(orders_dir,
                              f"orders_{datetime.datetime.now():%Y%m%d-%H%M%S}.json")

        rec_by_vin = {}
        bols = set()
        # vin -> 'Need By' date. Seeded from the Excel NeedByDate column so cache-resolved
        # shipments (which skip the dashboard) still get a need-by; the dashboard value
        # overrides it for VINs we actually download (see tesla_bol.download_for).
        need_by = {r.vin: r.get("need_by") for r in rows
                   if r.ok and (r.get("need_by") or "").strip()}

        def _restage():
            """(Re)group the NEW VINs parsed so far and write base + new to the staged
            file, so the GUI fills in live as BOLs arrive while everything already on
            the board stays put. New orders are numbered to avoid existing numbers, so
            same-route new VINs become their OWN orders rather than joining old ones."""
            for vin, rec in rec_by_vin.items():        # stamp Need By onto each record
                if need_by.get(vin):
                    rec["need_by"] = need_by[vin]
            orders = sd_api.group_by_route(ordered, rec_by_vin, reserved=base_numbers)
            payloads = [sd_api.order_payload_from_route(num, chunk) for num, chunk in orders]
            with open(stage, "w") as f:
                json.dump(base + payloads, f, indent=2, default=str)
            return payloads

        # Parse each BOL the moment it lands and restage, so orders appear as they
        # download instead of all at the end.
        def _on_bol(shp, path):
            bols.add(shp)
            routes_here = set()                         # (pickup_zip, delivery_zip) this BOL adds
            for rec in pdf_read.extract_records(path):
                rec_by_vin[rec["vin"]] = rec
                # LEARN any not-yet-cached terminal from this BOL (keyed on the Excel name)
                # so the next run can skip the portal for it.
                pn, dn = names_by_vin.get(rec["vin"], ("", ""))
                terminals_lookup.learn_from_bol_record(rec, pn, dn)
                pz = (rec.get("pickup") or {}).get("zip")
                dz = (rec.get("delivery") or {}).get("zip")
                if pz and dz:
                    routes_here.add((pz, dz))
            # Purge the BOL PDF now that its data has been read — the staged order
            # carries everything we need, so the file is just clutter. Set
            # KEEP_BOLS=true to retain them. (Best-effort: never fail the run on this.)
            if not config.KEEP_BOLS and path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            payloads = _restage()
            # ON THE SPOT: if this BOL's route(s) match a scanned candidate, resolve its
            # GUID + exact address now and post the match — don't wait for the end.
            matched = 0
            if config.SD_SCAN and sd_hits and routes_here:
                try:
                    matched = _reconcile_sd(base + payloads, routes_here)
                except Exception as exc:                # noqa: BLE001
                    print(f"    SD reconcile error: {exc}")
            tail = f"; {matched} SD match(es) so far" if matched else ""
            print(f"    parsed {shp}: {len(rec_by_vin)} new vehicle(s) so far "
                  f"-> {len(payloads)} new order(s) (+{len(base)} kept){tail}")

        # Prepare the SD scan zip-pairs up front so the scan can run CONCURRENTLY
        # with the BOL downloads (the scan only needs the Excel routes, not the
        # BOLs). The exact-address comparison still happens afterwards, once the
        # board's BOL addresses exist.
        sd_scan_pairs = None
        sd_hits: list = []
        if config.SD_SCAN:
            sd_scan_pairs = {(r.get("pickup_zip"), r.get("delivery_zip")) for r in rows
                             if r.ok and r.get("pickup_zip") and r.get("delivery_zip")}
            print(f"Will scan Posted + Accepted + Pending for {len(sd_scan_pairs)} route "
                  f"zip-pair(s), concurrently with the BOL downloads.")

        # Incremental SuperDispatch reconcile state. _on_bol calls _reconcile_sd ON THE
        # SPOT for each BOL's route; a final sweep at the end catches any candidate whose
        # BOL landed before the concurrent scan had finished populating sd_hits.
        sd_resolved = {}          # guid -> resolved SD order (stamped loadboard_status)
        sd_tried_vins = set()     # candidate VINs already looked up (never re-resolved)
        sd_stats = {"auth_error": None}

        def _reconcile_sd(board_now, route_zips=None):
            """Resolve scan candidates -> GUID + EXACT address and (re)write
            output/consolidation_search.json. With route_zips, resolve ONLY the
            candidates on those (pickup_zip, dropoff_zip) routes (per-BOL, on the spot);
            without it, resolve every still-pending candidate (final sweep). Returns the
            count of orders that match a board route exactly (posted/accepted/pending)."""
            if not config.SD_SCAN:
                return 0
            import consolidation
            todo = {h["vin"]: h["loadboard_status"] for h in sd_hits
                    if h["vin"] not in sd_tried_vins
                    and (route_zips is None
                         or (h.get("pickup_zip"), h.get("dropoff_zip")) in route_zips)}
            if todo:
                sd_tried_vins.update(todo)
                try:
                    res = consolidation.find_orders_for_vins(list(todo), throttle_s=0)
                    sd_stats["auth_error"] = res.auth_error or sd_stats["auth_error"]
                    for o in res.orders:                  # stamp which tab found it
                        for v in (o.get("vehicles") or []):
                            vv = v.get("vin")
                            if vv and vv in todo:
                                o["loadboard_status"] = todo[vv]
                                break
                        if o.get("guid"):
                            sd_resolved[o["guid"]] = o
                except Exception as exc:                  # noqa: BLE001
                    print(f"    SD resolve error: {exc}")
            annotated = consolidation.match_against_routes(
                list(sd_resolved.values()), board_now, use_street=True,
                my_vins=[h["vin"] for h in sd_hits]) if sd_resolved else []
            # ROUTE-LEVEL highlights: the scan already knows every posted/accepted/pending
            # SD shipment whose ZIP-PAIR matches an Excel route. Surface those directly so a
            # board route lights up (amber=posted, green=accepted) whenever the loadboard has
            # a shipment on that route — even if the stricter street-level consolidation match
            # (above) didn't fire and even if the per-VIN API resolve got rate-limited.
            # We key by the BOARD order's own routeKey (board_route_key == the frontend's
            # routeKey, byte-for-byte) and link scan hits to board orders by 5-digit-
            # normalized zip pairs, so ZIP+4 vs 5-digit differences between the loadboard and
            # the BOL don't break the match.
            _rank = {"posted": 3, "accepted": 2, "pending": 1}

            def _z5(z):
                return str(z or "").strip()[:5]

            _hit_status: dict = {}                # (pz5, dz5) -> strongest status seen
            for h in sd_hits:
                pz, dz = _z5(h.get("pickup_zip")), _z5(h.get("dropoff_zip"))
                if not (pz and dz):
                    continue
                st = (h.get("loadboard_status") or "").strip().lower()
                if _rank.get(st, 0) > _rank.get(_hit_status.get((pz, dz), ""), 0):
                    _hit_status[(pz, dz)] = st
            _route_status: dict = {}              # board routeKey -> strongest status
            for b in board_now:
                pz = _z5(((b.get("pickup") or {}).get("venue") or {}).get("zip"))
                dz = _z5(((b.get("delivery") or {}).get("venue") or {}).get("zip"))
                st = _hit_status.get((pz, dz))
                if st:
                    bk = consolidation.board_route_key(b)
                    if _rank.get(st, 0) > _rank.get(_route_status.get(bk, ""), 0):
                        _route_status[bk] = st
            route_hits = [{"route": k, "loadboard_status": st} for k, st in _route_status.items()]
            with open(os.path.join(_pbase, "consolidation_search.json"), "w") as f:
                json.dump({"orders": annotated, "route_hits": route_hits,
                           "checked_vins": len(sd_tried_vins),
                           "found_vins": [], "not_found_vins": [], "errors": [],
                           "auth_error": sd_stats["auth_error"]}, f, indent=2, default=str)
            return sum(1 for a in annotated if a.get("is_candidate"))

        if skipped:
            print(f"\nSkipping {skipped} VIN(s) already on the board.")

        import profiles
        is_all = (_pid == profiles.ALL_PROFILE_ID)
        row_by_vin = {r.vin: r for r in rows if r.ok}

        if not is_all:
            # NON-ALL dispatchers NEVER touch Tesla (auth problems). Build every shipment
            # from the Excel row; the posting overlay upgrades any stop whose terminal name
            # matches the DB, and leaves the rest as the Excel's own data (badged 'Excel').
            for vin, cost, shp in ordered:
                r = row_by_vin.get(vin)
                if r:
                    rec_by_vin[vin] = terminals_lookup.build_record_from_excel(vin, r.fields)
            print(f"Built {len(rec_by_vin)} shipment(s) from the terminal DB + Excel — "
                  f"no Tesla portal.")
            _restage()
            if config.SD_SCAN:
                import sd_scrape
                sd_hits = sd_scrape.scan_loadboard(sd_scan_pairs)
            payloads = _restage()                      # final
        else:
            # ALL: the terminal-feeding profile. GATE: skip the Tesla portal for any shipment
            # whose BOTH terminals are already cached (synthesize from cache); the rest hit
            # the portal and seed the cache via learn_from_bol_record.
            portal_vins, from_cache = [], 0
            for vin, cost, shp in ordered:
                pn, dn = names_by_vin.get(vin, ("", ""))
                rec = terminals_lookup.build_synthetic_record(vin, pn, dn)
                if rec:
                    rec_by_vin[vin] = rec
                    from_cache += 1
                else:
                    portal_vins.append(vin)
            if from_cache:
                print(f"Resolved {from_cache} VIN(s) from the terminal cache — no Tesla portal "
                      f"for those.")
            print(f"Fetching BOLs for {len(portal_vins)} new VIN(s) (source of shipment info)...")
            _restage()                                 # preserve base + cache-resolved up front
            if portal_vins:
                # download_for runs the SD loadboard scan CONCURRENTLY (async). The async
                # path can't drive the sync Vaultwarden login, so restore the shared session
                # here (sync) first — the async tabs then inherit it.
                if config.SD_SCAN:
                    import sd_login
                    st = sd_login.ensure_session()
                    if st != sd_login.LOGIN_OK:
                        print(f"⚠ SuperDispatch auto-login could not complete ({st}); the "
                              f"loadboard scan may return no matches this run.")
                tesla_bol.download_for(portal_vins, workers=args.workers, on_bol=_on_bol,
                                       need_by=need_by, sd_scan_pairs=sd_scan_pairs,
                                       sd_hits=sd_hits)
            elif config.SD_SCAN:
                import sd_scrape
                sd_hits = sd_scrape.scan_loadboard(sd_scan_pairs)
            # Link terminals learned THIS run to their original (strict + reasoned).
            if portal_vins:
                try:
                    terminals_db.link_learned_by_address()
                    sm = terminals_db.link_learned_smart()
                    if sm.get("total"):
                        print(f"  linked {sm['total']} newly-learned terminal(s) to originals "
                              f"({sm['linked_addr_multi']} same-addr/name, {sm['linked_zip_fuzzy']} fuzzy)")
                except Exception as exc:               # never fail a run on bookkeeping
                    print(f"  terminal linking skipped: {exc}")
            payloads = _restage()                      # final
        print(f"\n=== {len(payloads)} new order(s) from {len(bols)} BOL(s); "
              f"{skipped} VIN(s) skipped; {len(base) + len(payloads)} order(s) total "
              f"on the board (SD_ENV={config.SD_ENV}) -> {stage} ===")

        # Final SD reconcile sweep — the per-BOL pass (in _on_bol) already matched on the
        # spot; this catches any scan candidate whose BOL landed before the concurrent
        # scan finished populating sd_hits. Best-effort: logs and is skipped on error.
        if config.SD_SCAN:
            try:
                total = _reconcile_sd(base + payloads, None)
                print(f"  SD matches: {total} order(s) match a board route exactly "
                      f"(posted/accepted/pending) -> output/consolidation_search.json")
            except Exception as e:                       # noqa: BLE001
                print(f"  loadboard reconcile skipped (error): {e}")

        if dry:
            print("DRY-RUN — nothing sent. Open the file above to review the exact "
                  "payloads, then re-run with --live to send them.")
        else:
            created = 0
            dispatcher = profiles.dispatcher_phone(_profile)
            for payload in payloads:
                try:
                    body = sd_api.to_sd_order(payload, dispatcher=dispatcher)
                    res = sd_api.create_order(body, dry_run=False)
                    guid = res.get("guid") or (res.get("data") or {}).get("guid")
                    print(f"  [{payload['number']}] created -> guid={guid}")
                    created += 1
                except sd_api.SDError as e:
                    print(f"  [{payload['number']}] ERROR: {e}")
            print(f"\nCreated {created}/{len(payloads)} orders.")


if __name__ == "__main__":
    main()
