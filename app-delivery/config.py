"""
Settings for app-delivery's vision steps, read from the environment.

Credential resolution mirrors the sibling tools: shared creds live in
<repo-root>/secrets/.env so every tool reads one file; an app-local .env in THIS
folder can override; real environment variables win over both.

    python config.py        # print the resolved config (secrets masked)
"""
from __future__ import annotations
import os

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

# load_dotenv never overrides an already-set var, so this order makes app-local .env
# win over shared secrets, and real environment variables win over everything.
load_dotenv(os.path.join(_HERE, ".env"))                  # app-local override
load_dotenv(os.path.join(_REPO_ROOT, "secrets", ".env"))  # shared authoritative

# --- CLIP zero-shot selector (photo_select_clip.py) -------------------------
# open_clip model + pretrained tag (downloaded from HF on first use, no account).
CLIP_MODEL = os.getenv("CLIP_MODEL", "ViT-L-14").strip()
CLIP_PRETRAINED = os.getenv("CLIP_PRETRAINED", "laion2b_s32b_b82k").strip()
# A photo whose `reject` probability is >= this is treated as non-car (VIN sticker /
# key card / interior / junk) and excluded from selection.
CLIP_REJECT_THRESH = float(os.getenv("CLIP_REJECT_THRESH", "0.5"))

# --- Tesla Logistics APP login (emulator) -----------------------------------
# Credentials for the carrier Android app's SSO sign-in (auth.tesla.com). Used by
# app_drive.py to auto-recover when the app logs itself out after inactivity (it then
# hangs on an infinite spinner -> we restart + sign back in). KEPT SEPARATE from the
# regular Tesla *website* login the sibling tools use (BW_TESLA_ITEM / tesla_login.py):
# this is the delivery-driver app account, which may differ. TESLA_APP_TOTP_SECRET is
# the optional base32 2FA seed (only if the app account has 2FA enabled).
TESLA_APP_EMAIL = os.getenv("TESLA_APP_EMAIL", "").strip()
TESLA_APP_PASSWORD = os.getenv("TESLA_APP_PASSWORD", "")
TESLA_APP_TOTP_SECRET = os.getenv("TESLA_APP_TOTP_SECRET", "").strip()
# After sign-in the app shows a "Please select your role" screen; the drop-off /
# delivery workflow lives under "Outbound Driver". Override only if that changes.
TESLA_APP_ROLE = os.getenv("TESLA_APP_ROLE", "Outbound Driver").strip()

# --- Claude vision ----------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-6").strip()
# Photos are downscaled before upload (cost control). 1568px / q90 is Claude's
# high-fidelity image sweet spot — plenty to judge a vehicle's camera angle.
VISION_MAX_SIDE = int(os.getenv("VISION_MAX_SIDE", "1568"))
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "90"))
# Cap how many photos we send in one selection call (cost guard). A delivery set is
# ~20; if a set is ever huge, only the first N are considered.
VISION_MAX_PHOTOS = int(os.getenv("VISION_MAX_PHOTOS", "40"))


def _mask(s: str) -> str:
    return f"{s[:4]}…({len(s)} chars)" if s else "MISSING"


if __name__ == "__main__":
    print(f"ANTHROPIC_API_KEY     = {_mask(ANTHROPIC_API_KEY)}")
    print(f"VISION_MODEL          = {VISION_MODEL}")
    print(f"VISION_MAX_SIDE       = {VISION_MAX_SIDE}")
    print(f"VISION_MAX_PHOTOS     = {VISION_MAX_PHOTOS}")
    print(f"TESLA_APP_EMAIL       = {TESLA_APP_EMAIL or 'MISSING'}")
    print(f"TESLA_APP_PASSWORD    = {_mask(TESLA_APP_PASSWORD)}")
    print(f"TESLA_APP_TOTP_SECRET = {_mask(TESLA_APP_TOTP_SECRET)}")
