# cleaner-portal

`tesla-reconcile/clean.sh --apply`, repackaged as a **self-contained, double-click app**
that attaches to your real **Chrome over CDP** and runs the Tesla "Dispatch Dashboard
2.0" end-of-day cleanup in its own tab.

This folder is **portable**: it bundles its own copy of the cleanup code and runs on its
own Chrome profile (`.auth`, created inside the folder). Drop it on any Mac with Google
Chrome and it builds and runs on its own.

## What it does (same as `clean.sh --apply`)

- Bumps every **Pickup Date Today / Pickup Date Late** shipment to **tomorrow**
  (reason *Other*), repeating until none remain.
- Assigns a driver (`CLEANUP_DRIVER`, default `JESSICA TFI 2246664226`) to every
  **Driver Needed** unit still showing *No Driver Selected*.
- Leaves **ETAs alone** unless `PROCESS_ETA=true`.

## The Dock apps — install first (important)

There are two clickable apps:

- **Cleaner Portal.app** — a **GUI window** showing the live counts with a **Run** button
  (and a **Dry run** toggle), a **streaming progress log**, a **Stop** button, and a
  **stall watchdog**. It checks your Tesla session first (surfaces the sign-in page if
  you're signed out), confirms before applying, then runs the cleanup with live progress
  so it never looks "stuck."
- **Portal Status.app** — a **read-only** version: the same counts GUI with no Run button.
  Submits nothing.

### Why you must install (not just double-click from the repo)

This repo lives in `~/Documents`, and macOS (TCC privacy) **blocks an app launched from
Finder/Dock from reading `~/Documents`** — so double-clicking the `.app` from here just
hangs/denies. The fix: **double-click `install.command`** once. It copies the apps to
**`~/Applications/Cleaner Portal`** (not a protected location), rebuilds the venv there,
and carries over your saved Tesla login.

Then open `~/Applications/Cleaner Portal` and **drag the two apps to your Dock**.

Re-run `install.command` after you change anything in the repo to update the installed
copy. Use the installed copy (in `~/Applications/Cleaner Portal`) for everyday use —
don't run the repo copy and the installed copy at the same time (they'd both try to
drive Chrome on the same debug port).

> The `.command` files (below) still work directly from the repo because they run under
> Terminal, which already has Documents access.

## First-time setup / use via Terminal

1. **First time only:** double-click **`login-once.command`**. A dedicated Chrome opens
   on the Tesla vendor portal — sign in, then press Return in the Terminal. The session
   is saved into this folder's `.auth` profile and reused from then on.
2. Double-click **`run-cleaner.command`** (or **Cleaner Portal.app**) to run the cleanup.
   Your everyday Chrome can stay open. The Chrome it drives is parked **off-screen**
   ("ghost") by default.
3. Re-run `login-once.command` (or just click the app) if the Tesla session ever expires
   — the app will detect it and reopen the sign-in window for you.

**Requirements on the machine:** Google Chrome. Plus either `uv` or Python ≥ 3.10 — if
neither is present, first run installs `uv` automatically via Homebrew (so Homebrew is
the only thing to have, if you don't have Python 3.10+). First run also needs internet
to install Playwright.

## How it works

It attaches over **CDP** to the real installed Chrome (`AUTH_MODE=cdp`): launches Chrome
on this folder's `.auth` profile with the remote-debugging port, parked off-screen, then
attaches and opens its work in its own tab. Because it's the real browser on a real,
logged-in profile, `navigator.webdriver` stays false and Tesla's bot-detection is
satisfied. The `.auth` profile runs **alongside** (and never touches) your everyday
Chrome.

## Options

- **Watch the browser:** `./run-cleaner.command --headed`
- **Driver / ETA behavior:** create a `.env` in this folder with `CLEANUP_DRIVER=...`
  and/or `PROCESS_ETA=true`.

## Files

| File | Role |
|------|------|
| `install.command` | Installs the apps to `~/Applications/Cleaner Portal` (needed for Dock use). |
| `Cleaner Portal.app` | Dock app — GUI: counts + Run (with dry-run) + live progress. |
| `Portal Status.app` | Dock app — read-only live-counts GUI (the "test" app). |
| `login-once.command` | One-time Tesla login (double-click first). |
| `run-cleaner.command` | Terminal launcher — runs the cleanup `--apply` over Chrome/CDP. |
| `cleaner_app.py` | The Cleaner Portal GUI (Tkinter; runs cleanup as a subprocess). |
| `status_app.py` | The read-only counts GUI + shared `scrape()`. |
| `scrape_counts.py` | Subprocess helper: prints the counts as JSON for the GUI. |
| `preflight_login.py` | Read-only "are we signed in?" check; surfaces the sign-in page if not. |
| `_bootstrap.sh` | Shared venv/Chrome/PATH bootstrap, sourced by the launchers. |
| `tesla_cleanup.py` | The cleanup logic + live-calibrated DOM selectors. |
| `auth.py` | CDP browser attach + login detection. |
| `config.py` / `runlog.py` | Settings (env) + per-run logging. |
| `tesla_login_once.py` | The one-time login helper. |
| `requirements.txt` | Lean deps: Playwright + python-dotenv. |

## Provenance / keeping in sync

`tesla_cleanup.py`, `auth.py`, `config.py`, `runlog.py` are **vendored copies** of the
files in `../tesla-reconcile` (taken from commit `69311cb`). This is a snapshot — if the
main tool's cleanup logic or selectors change later, re-copy those four files to update:

```bash
cp ../tesla-reconcile/{tesla_cleanup,auth,config,runlog}.py .
```

## Dry-run / debug

Preview without changing anything (counts + plan only), no `--apply`:

```bash
AUTH_MODE=cdp CDP_PROFILE_DIR="$PWD/.auth" .venv/bin/python tesla_cleanup.py
```
