# Packaging the Shipment Creator as a Windows app

Goal: a normal user double-clicks an installer, gets a Desktop/Start-Menu icon, and
double-clicks it to run the app — no PowerShell, no Python, no `pip`.

## What the build produces

- `dist\ShipmentCreator\` — the **portable app** folder. `ShipmentCreator.exe` inside
  starts the local server and opens the GUI in the default browser. You can zip and
  share this folder as-is.
- `installer\ShipmentCreatorSetup.exe` — a **one-click installer** (Desktop +
  Start-Menu shortcuts, clean uninstall).

## Build it (on a dev machine)

Prerequisites (dev machine only — the end user needs none of this):

1. The project venv (`web.bat` creates it on first run).
2. **Inno Setup** for the installer step — https://jrsoftware.org/isdl.php
   (skip it if you only want the portable folder).

Then, from this folder:

```bat
build.bat
```

That installs build deps, runs PyInstaller, and — if `ISCC.exe` is on PATH — compiles
the installer. If Inno Setup isn't installed, it stops after the portable folder and
tells you.

## How it works (the non-obvious bits)

- **Writable data.** An installed app folder under `Program Files` is read-only, so all
  mutable data — staged orders, BOLs, spares, settings — is written to
  `%LOCALAPPDATA%\TFI Shipment Creator\`. See `paths.py`. In a dev checkout nothing
  changes; data still lands beside the source.
- **Running the pipeline.** The GUI's **Run** shells out to the pipeline. There's no
  `python`/`main.py` on an end-user machine, so when frozen the app re-invokes its own
  exe with a `--pipeline` flag, which `app.py` routes to `main.main()`. Same streamed
  progress as dev.
- **Settings & credentials.** A normal user can't edit `.env`, so the **Settings** tab
  writes config to `settings.json` (in the data dir) and the SuperDispatch client
  secret to the **Windows Credential Manager** (via `keyring`). `config.py` layers
  these under real env vars, so a `.env` still wins for developers.
- **One-time login.** The **Settings → Sign in to Tesla** button opens the shared
  Chrome profile so the user logs in once; cookies persist for later runs. (The app
  attaches to the real installed Chrome over CDP — so Playwright's own Chromium is
  **not** bundled, keeping the build ~180 MB.)

## First-run checklist for the end user

1. Install / launch — the GUI opens in the browser.
2. **Settings** → enter the SuperDispatch environment + client id/secret, and the
   default Excel path → **Save**.
3. **Settings → Sign in to Tesla** → log in once in the Chrome window that opens.
4. **Run** → start a pipeline.

## Running / stopping

The app runs **windowed** — no terminal. It lives in the **system tray** (bottom-right,
near the clock): right-click the icon for **Open Shipment Creator** / **Quit**. There's
no console, so logs go to `%LOCALAPPDATA%\TFI Shipment Creator\app.log` if you need to
debug. (Toggle back to a console build by setting `console=True` in
`shipment_creator.spec`.)

## Notes / future polish

- Real BOL download + SuperDispatch posting need valid credentials + a completed Tesla
  login; smoke-test those on a machine that has them before shipping a release.
