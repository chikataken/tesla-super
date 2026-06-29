# app-delivery

Android-emulator side of the Tesla delivery workflow: drive the carrier Android
app (sideloaded APK) to upload BOL photos and mark VINs delivered, fed by the
web side (Tesla portal VIN lookup/assign + SuperDispatch BOL photos).

This folder currently covers **Spike A: emulator feasibility** — can the app
install, log in, and accept uploaded photos on an emulator at all?

## Layout
```
env.sh                     # SDK/JDK paths + AVD config (sourced by scripts)
session_env.sh             # imports the desktop's DISPLAY/WAYLAND env (headed window)
scripts/setup_emulator.sh  # one-time, no-sudo: JDK + SDK + emulator + AVD
scripts/start_emulator.sh  # boot the AVD (HEADED by default; --headless for automation)
scripts/stop_emulator.sh   # clean shutdown (saves snapshot → login persists)
scripts/install_app.sh     # install the carrier APK + the arm64-lib workaround
scripts/screenshot.sh      # grab the screen → screenshots/latest.png

serve.sh                   # 24/7 SERVICE: keep emulator+app alive, poll In-Transit, drop off
tesla-delivery.service     # systemd USER unit for serve.sh (no sudo)
dashboard.py / run_web.sh  # status site (app.wastake.com): live log + history, stdlib-only
app-delivery-web.service   # systemd USER unit for the dashboard (port 8011)
app_drive.py               # drive the emulator app: decode -> push -> pick -> Drop Off -> Confirm
decode.sh / decode_vin.py  # VIN -> the 3 app photo sections (sides / vin_plate / key)
fetch_latest.py            # fetch the VIN's latest DELIVERED shipment photos (shipment-creator venv)
ocr_vin.py                 # on-device OCR: which photos show the VIN plate (direct-pickup venv)
dropoffs.db                # ledger of every VIN dropped off (sqlite)
run.sh                     # ./run.sh --vin <VIN> -> 4 corner photos in out/<VIN>/ (selector only)
config.py                  # selector settings (CLIP model, ANTHROPIC_API_KEY) from ../secrets/.env
photo_select_trained.py    # BEST: LOCAL trained head (CLIP features -> learned classifier)
photo_select_clip.py       # LOCAL CLIP zero-shot (default until a model is trained)
photo_select_yolo.py       # alt: LOCAL car-parts detector + geometry rules (no API)
photo_select.py            # alt: Claude vision (sends to API)
train.sh                   # trainer launcher: ./train.sh [label | pull N | train]
trainer/label_app.py       # web labeler (paste VIN or auto-pull batches) + click/keyboard label
trainer/scrape_vins.py     # scrape VINs off the SD orders list (tesla-reconcile venv)
trainer/fetch_api.py       # fetch Delivery photos via the official SD API (shipment-creator venv)
trainer/puller.py          # orchestrates scrape->fetch, dedup on shipment guid (seen_db)
trainer/train.py           # CLIP-embed labels + fit the head -> trainer/model.joblib
trainer/labels.json        # your hand labels (the training set — keep this)
test_photo_select_*.py     # selector logic tests (every model mocked)
requirements.txt           # deps for the selectors + trainer; install into .venv
```

## Picking the delivery corner photos
The Tesla app's Drop-Off upload wants 4 exterior shots — **front, rear, and both
sides**. SuperDispatch hands us ~20 messy inspection photos (night shots, lots of
3/4 angles, VIN-sticker / key-card close-ups, junk frames) and **`photo_type` does
not encode camera angle** (see the `sd-inspection-photos-api` note), so we need a
vision step to choose. Three interchangeable selectors, same output shape
(`{front, rear, left_side, right_side}` indices + a green-boxed contact sheet). The
**CLIP** one is the default — its semantic prior generalizes across cars/lighting far
better than the YOLO part-geometry rules (which overfit one car).

### `./run.sh --vin <VIN>` — the one-command entry
Fetches the VIN's Delivery photos and writes the 4 corner shots to a folder. Local
end to end (the only network call is pulling the SD photos):
```bash
./run.sh --vin 7SAXCDE55PF381263 [--out DIR]    # DIR defaults to ./out
SELECTOR=yolo ./run.sh --vin <VIN>              # use the YOLO selector instead
# ->  out/<VIN>/front.jpg  rear.jpg  left_side.jpg  right_side.jpg
#     out/<VIN>/picks.png  (annotated sheet)   picks.json   _source/ (raw photos)
```
First run auto-creates `.venv`, installs deps, and (for CLIP) downloads the model on
first use. The fetch step uses the sibling `shipment-creator`'s venv (so set that up
too). Under the hood it runs `sd_photos.py` then `<selector> --copy-dir`.

### Train your own head (best accuracy — `trainer/`)
Zero-shot (CLIP) and the part-geometry rules both *guess* the angle from a generic
prior and are unreliable on this narrow domain. Training a tiny classifier on a few
hundred labeled photos fixes that — and it still runs 100% local (a logistic head on
frozen CLIP features; the CLIP encoder is reused, so the head is tiny + trains in
seconds). This is the recommended path once you've labeled some photos.

Use `./train.sh` (bootstraps the venv on first run):
```bash
# 1) LABEL: open the web app and just label — it AUTO-PULLS shipments in the
#    background (tops up the pool in batches of 20 as you go). No buttons.
./train.sh                              # -> http://localhost:8095   (== ./train.sh label)
#    label each photo by camera CORNER (inspection photos are mostly corner shots):
#    front, rear, front_left(FL), front_right(FR), rear_left(RL), rear_right(RR), reject.
#    spatial keys:  q FL · w front · e FR   |   a RL · s rear · d RR   |   x reject · u unset
#    ←/→ move · pressing/clicking a set label again unmarks it.
#    The "Next batch" button serves 20 fresh (untouched) VINs; labeled/skipped ones never return.
#    labels save to trainer/labels.json as you go.

# (optional) pre-pull a batch from the CLI (the labeler does this automatically too):
./train.sh pull 20                      # 20 random unseen shipments into the pool

# 2) TRAIN: CLIP-embed the labeled photos + fit the head; prints held-out accuracy.
./train.sh train                        # -> trainer/model.joblib

# 3) USE: run.sh auto-selects the trained head once trainer/model.joblib exists.
./run.sh --vin <VIN>                    # (or SELECTOR=trained ./run.sh --vin <VIN>)
```

**Random pulls** are a two-step pipeline (orchestrated by `trainer/puller.py`, looping
over random windows until it has N new shipments):
1. **Scrape VINs only** (`scrape_vins.py`, tesla-reconcile venv) — reads VIN strings
   off the SD Invoiced/delivered orders list over a randomized window (the public API
   has no list endpoint). No photos are downloaded here.
2. **Fetch photos via the official SD API** (`fetch_api.py`, shipment-creator venv) —
   for each VIN, `find_by_vin → get_order → Delivery photos` (same API path the other
   tools use), saved to `pool/<VIN>__<guid8>/`.

A sqlite ledger (`trainer/seen.db`) records every shipment pulled, **keyed on the
order/shipment GUID**, so the **same shipment is never repeated** (the same VIN on a
*different* shipment is fine — different photos). The labeler **auto-tops-up** the pool
in batches of 20 (`TRAINER_BATCH`) whenever unlabeled photos fall below `TRAINER_MIN_UNLABELED`,
so it pulls as you label. Requires both sub-venvs; VIN scraping needs a valid SD web
login (run tesla-reconcile's login once). API fetch + dedup verified live; the scrape
step needs that SD session.
The trained head detects **6 corner/face classes** (front, rear, FL, FR, RL, RR) —
inspection photos are consistently corner shots, so corners are a more honest target
than pure side profiles. `photo_select_trained.py` then outputs the best photo per
corner. Label more VINs → retrain → better.

### `photo_select_clip.py` — LOCAL CLIP zero-shot (default until you train)
Scores each photo against text prompts with **open_clip ViT-L-14 / laion2b** (weights
auto-download from HF, no account). No training. Calibrated zero-shot: prompt
ensembling + the model's `logit_scale` softmax over {front, rear, side, reject} (raw
cosine sims are too compressed to rank on). The `reject` class gates VIN-sticker /
key-card / interior / junk frames out; the two strongest `side` shots are ordered by a
left-vs-right prompt.
- ⚠️ **driver-vs-passenger (left/right) is best-effort** — CLIP can't reliably tell
  them apart, so the two side picks are distinct/opposite flanks but which is literally
  the driver side isn't guaranteed (same limitation as the other selectors).

```bash
uv venv .venv && uv pip install -r requirements.txt   # open_clip pulls torch
python ../shipment-creator/sd_photos.py <VIN> --type Delivery --out /tmp/<VIN>
python photo_select_clip.py /tmp/<VIN> --out picks.json --sheet picks.png --copy-dir /tmp/<VIN>/picks
```
GPU is used automatically if available; on the RTX 5060 install the `+cu130` torch the
siblings use. CPU is fine for one-off selection (a 20-photo set is seconds).

### `photo_select_yolo.py` — LOCAL car-parts detector (alt)
A **YOLO11n car-parts segmentation** model + geometry rules (front/rear from
hood/front_light vs trunk/back_light; side from wheels+doors; a full-vehicle gate
drops close-ups). Accurate on the validation car but the rules generalize worse than
CLIP. Needs the weights:
```bash
mkdir -p models && curl -L -o models/carparts_yolo11n_seg.pt \
  https://huggingface.co/konst22/yolo11n-carparts-seg/resolve/main/best.pt
SELECTOR=yolo ./run.sh --vin <VIN>
```

### `photo_select.py` — Claude vision (alt)
Sends the (downscaled) photos to Claude in one call to classify + select. Same CLI;
needs `ANTHROPIC_API_KEY` (read from `../secrets/.env`).

```bash
python -m pytest -q       # 24 tests; trained/CLIP/YOLO/Claude all mocked (no weights/key/network)
```

All three validated 2026-06-27 on the real 20-photo Delivery set for Model X VIN
7SAXCDE55PF381263, each picking a correct front, the clean rear [06], and two
opposite-flank sides while skipping every VIN/key-card/junk frame (CLIP: front [10],
rear [06], sides [04]+[09]). CLIP is the default because its picks hold up across
different cars where the YOLO geometry rules did not.

## End-to-end drop-off + the 24/7 service
Spike A is closed: the chosen photos now flow all the way into a completed Drop-Off,
and the whole thing runs as a continuous service.

**`decode_vin.py` (`./decode.sh --vin <VIN>`)** — turns one VIN into the 3 app
sections. Fetches the VIN's *latest delivered* shipment's Delivery photos via the SD
API (`fetch_latest.py`, shipment-creator venv), picks the 4 exterior shots with the
trained corner model, finds the VIN-plate photo by on-device OCR (`ocr_vin.py`,
direct-pickup-checks venv), and picks the **key card** (white/black-key class). Writes
`out/<VIN>/{sides,vin_plate,key}/` + `manifest.json`. All local, no Claude.
- The key is chosen by `best_key()`: ranked on `max(P(white_key), P(black_key))` with a
  0.30 floor, so a real key card is posted even when `reject` narrowly out-scores it
  (hand-on-paperwork / dark interior shots) — but junk never is.
- If a set has **no VIN-plate close-up** (common on short photo sets), the VIN section
  falls back to a car photo and the manifest flags `vin_fallback: true`.

**`app_drive.py`** — drives the emulator (adb + uiautomator). Each cycle does BOTH:
- **Pick Up** (photo-free): for each Pick Up shipment, verify each unit (Verify → Can't
  Scan QR → Yes/No tap-through, no scanner on the emulator), Start Loading, **wait out
  the ~2-minute loading timer** (Finish Loading is disabled until it elapses), set a
  formality ETA (today, late hour — bumped a day if the app rejects it as past for the
  destination timezone), then Ready-to-Depart → Confirm. Ledgers to `pickups`.
- **Drop Off**: for each In-Transit shipment / pending unit: `decode_vin` → push photos →
  select in the PictureSelector → Add → upload; then Drop Off → "Subject to Inspection"
  → Confirm. Ledgers to `dropoffs`.

`process_cycle()` ties them together: one pull-to-refresh, then drain Pick Up, then
drain In Transit (a freshly picked-up shipment lands in In Transit and is dropped off a
later cycle). Multi-VIN aware; never uses hardware BACK (it crashes the RN app). Debug
frames go to `out/_debug/` (bounded to the newest 400).
```bash
python app_drive.py                 # DRY RUN: everything except the final Confirm
python app_drive.py --confirm       # LIVE: actually drop off the current queue
python app_drive.py --watch 60 --confirm   # SERVICE: poll every 60s, forever
```

**`serve.sh` / `tesla-delivery.service`** — the 24/7 service. `serve.sh` sets the
GPU/offline env, then runs `app_drive.py --watch`, which keeps the **emulator + app
alive** (boots the AVD via `start_emulator.sh` if it's down — snapshot restore keeps
the login), polls In-Transit on an interval, and drops off whatever has arrived. It
survives emulator crashes, app crashes, and per-cycle errors (each logged, loop
continues); `serve.sh` additionally restarts on a hard Python crash.

**Auto-login.** The app logs itself out after inactivity and then hangs on an infinite
spinner. When `app_drive` sees nothing actionable for several polls it force-restarts
the app (`restart_app`) and, if it lands on the login screen, signs back in
(`login_app`): in-app *Next* → `auth.tesla.com` password → *select role*. Set the app
credentials in `secrets/.env` (SEPARATE from the website login):
`TESLA_APP_EMAIL`, `TESLA_APP_PASSWORD` (and `TESLA_APP_ROLE`, default `Outbound
Driver`). No 2FA on this account; `TESLA_APP_TOTP_SECRET` stays unset.
```bash
./serve.sh                 # DRY RUN, poll every 60s
./serve.sh --confirm 30    # LIVE, poll every 30s
# unattended: install the systemd USER unit (no sudo) — see tesla-delivery.service
```

**Status dashboard — `app.wastake.com`.** `dashboard.py` (stdlib only, served by
`app-delivery-web.service` on `127.0.0.1:8011`) shows whether the service is running,
the VIN it's marking right now, a live tail of `out/service.log`, and the full history
of past marks from `dropoffs.db`. The shared cloudflared tunnel routes
`app.wastake.com` → `:8011` (ingress in `~/.cloudflared/config.yml`; DNS routed via
`cloudflared tunnel route dns`). After editing the tunnel ingress, restart it so it
takes effect: `sudo systemctl restart cloudflared-shipment-creator`.

## Running the ARM-only app on the x86_64 emulator
The carrier app `com.tesla.logisticsmobile` (React Native) ships **arm64-only**
native libs and sets `extractNativeLibs=false`. The x86_64 `google_apis` image
translates ARM fine, BUT the installer leaves the app's `lib/arm64` dir empty and
SoLoader then looks for libs under the process ABI (`x86_64`) — which the APK lacks —
crashing with `couldn't find DSO: libreactnative.so`. `install_app.sh` fixes this by
extracting the APK's arm64 `.so` files into that dir (the rooted google_apis image
allows it). The original APK signature is untouched (no repackaging/re-signing).
The fix lives on the persistent userdata disk, so it survives reboots; only an APK
re-install needs it re-applied (just re-run `install_app.sh`). CodePush JS updates
don't touch native libs. NOTE: an **arm64 system image does NOT work** — the emulator
refuses arm64 on an x86_64 host ("not supported by QEMU2 emulator").
The Android SDK + a portable JDK install under `~/Android` (not in this repo).

## Prerequisites (one-time)
The emulator needs hardware acceleration. Grant your user access to `/dev/kvm`:

```bash
# immediate (no re-login), but resets on reboot:
sudo setfacl -m u:$USER:rw /dev/kvm
# OR persistent (takes effect next login):
sudo usermod -aG kvm $USER
```

No other sudo/apt is required — `setup_emulator.sh` uses a portable JDK and the
Android command-line tools.

## Usage
```bash
./scripts/setup_emulator.sh      # download + create the AVD (~2GB, one-time)
./scripts/start_emulator.sh      # boot HEADED (visible window); waits for boot_completed
./scripts/start_emulator.sh --headless   # no window (automation only)
./scripts/stop_emulator.sh       # clean shutdown

# then, with the SDK on PATH (source env.sh):
. ./env.sh
adb install -r /path/to/app.apk  # sideload the carrier app
adb shell pm list packages | grep <vendor>
```
If the headed window glitches on the NVIDIA/Wayland combo, fall back to software GL:
`EMU_GPU=swiftshader_indirect ./scripts/start_emulator.sh`.

## Spike A — what we need to learn
1. Does the app **run + log in** on an emulator, or does Play Integrity /
   attestation block it? (If blocked → physical device or a rooted+Magisk image.)
2. Is photo upload **gallery-based** (we `adb push` SD photos and pick them) or
   **camera-only** (would need feeding the emulator's virtual camera)? — the
   single most important question for the whole pipeline.
3. Are its UI elements automatable (stable resource-ids via `uiautomator dump`)?

## Notes
- Image: `system-images;android-34;google_apis;x86_64` — rootable, has Google
  Play Services, no Play Store (we sideload). Swap to `_playstore` only if the
  app strictly requires a Play-Store install.
- Headed by default so you can drive the app by hand; `session_env.sh` imports the
  desktop display env so it opens even when launched from a tty/cron. For automation
  later, `--headless` runs windowless (grab frames with `adb exec-out screencap -p`).
