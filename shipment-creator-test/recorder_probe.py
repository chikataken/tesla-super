"""READ-ONLY discovery for the shipment recorder backfill.

Confirms the shared Chrome has a live SuperDispatch session, enumerates the real
order-status tab routes from the /orders nav, and verifies that order cards parse
on a history tab (invoiced/delivered). Navigation only — writes nothing to SD.

    python recorder_probe.py
"""
import sys, json
import auth, config, sd_login, sd_scrape

NAV_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('a[href*="/orders/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const txt = (a.textContent || '').replace(/\s+/g,' ').trim();
    // status-tab links point at /orders/<slug> (no /view/, no query id)
    if (/\/orders\/[a-z_]+($|\?)/.test(href) && !href.includes('/view/')) {
      out.push({href, txt});
    }
  });
  // de-dup by href
  const seen = new Set(); const uniq = [];
  out.forEach(o => { if (!seen.has(o.href)) { seen.add(o.href); uniq.push(o); } });
  return uniq;
}
"""

def main():
    with auth.browser_context() as ctx:
        page = ctx.new_page()
        try:
            status = sd_login.ensure_session(page)
            print(f"[login] ensure_session -> {status}")
            page.goto(config.SD_WEB_BASE + "/orders", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            print(f"[url] {page.url}")
            nav = page.evaluate(NAV_JS)
            print(f"[nav] {len(nav)} order-status tab link(s):")
            for n in nav:
                print(f"    {n['href']:40} | {n['txt'][:40]}")
            # Try the invoiced (delivered) tab page 1 and count cards
            inv = config.SD_WEB_BASE + "/orders/invoiced?size=100&page=1"
            page.goto(inv, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            try:
                page.locator(sd_scrape.ORDER_LINK).first.wait_for(timeout=10000)
            except Exception:
                print(f"[invoiced] no order links found at {page.url}")
            cards = page.evaluate(sd_scrape._CARDS_JS)
            print(f"[invoiced] {len(cards)} cards on page 1; sample:")
            for c in cards[:3]:
                print("    ", json.dumps({k: c.get(k) for k in ('id','href','vins','text')})[:300])
        finally:
            try: page.close()
            except Exception: pass

if __name__ == "__main__":
    sys.exit(main())
