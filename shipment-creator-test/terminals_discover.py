"""
ONE-TIME discovery probe for the SuperDispatch Terminals page (read-only).

The terminals scraper needs selectors VERIFIED against the live site (same as
sd_scrape.py). This probe drives the shared logged-in Chrome, finds the Terminals
tab from the SD nav, and dumps what the scraper needs to be written correctly:
the resolved URL, the page <title>, the full HTML, a screenshot, and a guess at
the row/link + pagination structure. It writes NOTHING to SuperDispatch.

Run:  python terminals_discover.py
Out:  output/terminals_discover/*   (html, png, structure.json)
"""
from __future__ import annotations
import json
import os

import auth
import config
import paths

OUT = paths.output_path("terminals_discover", ".keep")
OUT = os.path.dirname(OUT)

# Candidate paths to try in order; the first that doesn't bounce to /login or /orders
# and whose body mentions "terminal" wins. We also follow any visible "Terminals" nav
# link, which is the authoritative source if these guesses are wrong.
CANDIDATE_PATHS = ["/terminals", "/settings/terminals", "/manage/terminals", "/contacts/terminals"]

_LINKS_JS = r"""
() => {
  const a = [...document.querySelectorAll('a[href]')]
    .map(e => ({text: (e.textContent||'').trim().slice(0,40), href: e.href}))
    .filter(x => x.text);
  const termNav = a.find(x => /terminal/i.test(x.text));
  // Heuristic row detection: repeated table rows or list cards.
  const rows = document.querySelectorAll('table tbody tr').length;
  const cards = document.querySelectorAll('[class*="row" i], [class*="card" i], li').length;
  const pager = [...document.querySelectorAll('a[href*="page" i], [class*="pagination" i] a, nav a')]
    .map(e => ({text: (e.textContent||'').trim().slice(0,20), href: e.href})).slice(0, 12);
  return {termNav, tableRows: rows, cardish: cards, pager,
          bodyMentionsTerminal: /terminal/i.test(document.body.innerText)};
}
"""


def _dump(page, tag: str) -> dict:
    os.makedirs(OUT, exist_ok=True)
    page.wait_for_load_state("domcontentloaded")
    info = {"tag": tag, "url": page.url, "title": page.title()}
    try:
        with open(os.path.join(OUT, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        info["html_error"] = str(e)
    try:
        page.screenshot(path=os.path.join(OUT, f"{tag}.png"), full_page=True)
    except Exception as e:
        info["png_error"] = str(e)
    try:
        info["structure"] = page.evaluate(_LINKS_JS)
    except Exception as e:
        info["structure_error"] = str(e)
    return info


def main() -> None:
    findings = []
    with auth.browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 0) Land on the dashboard, capture the nav so we can find the real Terminals link.
        page.goto(config.SD_WEB_BASE)
        home = _dump(page, "00_home")
        findings.append(home)
        if "login" in (page.url or "").lower():
            print("NOT LOGGED IN to SuperDispatch — log into the shared Chrome profile "
                  f"({config.CDP_PROFILE_DIR}) once, then re-run.")
            print(json.dumps(home, indent=2))
            return

        # 1) Prefer the authoritative nav link if the home page exposes one.
        nav = (home.get("structure") or {}).get("termNav")
        if nav and nav.get("href"):
            page.goto(nav["href"])
            findings.append(_dump(page, "01_navlink"))

        # 2) Try the guessed paths too, for completeness.
        for i, path in enumerate(CANDIDATE_PATHS, start=2):
            try:
                page.goto(config.SD_WEB_BASE + path)
                findings.append(_dump(page, f"{i:02d}_guess{path.replace('/', '_')}"))
            except Exception as e:
                findings.append({"tag": f"guess{path}", "error": str(e)})

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "findings.json"), "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2)
    print(f"Discovery written to {OUT}")
    for f in findings:
        s = f.get("structure") or {}
        print(f"  [{f.get('tag')}] url={f.get('url')} title={f.get('title')!r} "
              f"tableRows={s.get('tableRows')} mentionsTerminal={s.get('bodyMentionsTerminal')}")


if __name__ == "__main__":
    main()
