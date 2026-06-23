"""
Local VIN reader — runs ENTIRELY on-device. easyOCR (default) or Tesseract reads
the VIN off the pickup inspection photos; photos are NEVER sent to Claude or any
external API. Copied from tesla-reconcile/ocr.py.

easyOCR is the default engine (deep-learning scene-text, best on photographed VIN
stickers/windshield strips). Set OCR_ENGINE=tesseract for the lighter fallback,
which needs the system Tesseract binary (`brew install tesseract`) + `pytesseract`.
"""
from __future__ import annotations
import os
import re
import shutil
import warnings
from io import BytesIO

import pytesseract
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageFilter

# Silence EasyOCR/PyTorch's harmless MPS notice on Mac ("'pin_memory' ... not
# supported on MPS now") — its internal DataLoader requests pinned memory that
# Apple's GPU backend ignores. Cosmetic only; nothing to do about it upstream.
warnings.filterwarnings(
    "ignore", message=r".*pin_memory.*not supported on MPS.*")

load_dotenv()   # allow TESSERACT_CMD / OCR_ENGINE in .env

# Engine: "easyocr" (default — deep-learning scene-text reader, far better on
# photographed VIN stickers / windshield strips / screens) or "tesseract" (the
# classic document OCR; faster but weak on photos). EasyOCR needs `pip install
# easyocr` (in requirements.txt). Override with OCR_ENGINE=tesseract in .env.
OCR_ENGINE = os.getenv("OCR_ENGINE", "easyocr").strip().lower()
OCR_GPU = os.getenv("OCR_GPU", "false").strip().lower() in {"1", "true", "yes"}
# EasyOCR internal detection upscale. 1.0 = off (fastest); the BOL photos are
# already ~1440px so this is usually plenty. Raise (1.5-2.0) only for stubborn
# faint VINs — note 2.0 ~= 4x the detection work, so it's much slower on CPU.
OCR_MAG_RATIO = float(os.getenv("OCR_MAG_RATIO", "1.0"))
# Sideways/rotated/flipped stickers: angles to RETRY when an upright pass finds
# nothing (the "smart" fallback — costs nothing on the common case). Tried ONE AT A
# TIME in THIS ORDER, stopping at the first angle that reads the VIN, so the most
# common orientation should come first. Door-jamb VIN stickers are usually shot
# from the right-hand side (the sticker ends up reading correctly after a 270°
# clockwise rotation), so 270 leads; then 90 (left side) and 180 (upside-down).
# Set OCR_ROTATIONS="" to disable.
OCR_ROTATIONS = [int(a) for a in os.getenv("OCR_ROTATIONS", "270,90,180").split(",")
                 if a.strip().lstrip("-").isdigit()]
# A VIN (and the labels around it) is only digits + UPPERCASE letters. Restricting
# EasyOCR to this set speeds the decoder and avoids lowercase/punctuation noise.
_VIN_ALLOWLIST = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_easy_reader = None

# Point pytesseract at the binary even if it isn't on the venv's PATH. You can
# force a path with the TESSERACT_CMD env var (run `which tesseract` to find it).
for _cand in (os.getenv("TESSERACT_CMD"), "tesseract",
              "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
              "/usr/bin/tesseract",
              r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
    if not _cand:
        continue
    # A bare command name (no path separator) is looked up on PATH; an explicit
    # path (POSIX "/" or Windows "\") is used only if it actually exists.
    _has_sep = ("/" in _cand) or ("\\" in _cand)
    _p = (_cand if os.path.exists(_cand) else None) if _has_sep else shutil.which(_cand)
    if _p:
        pytesseract.pytesseract.tesseract_cmd = _p
        break

_ALNUM = re.compile(r"[^A-Z0-9]")
# Standalone label words that flag a VIN-bearing photo: "VIN" and "MFD" (the
# Mfd-date / manufacturer line on the door-jamb VIN sticker). Bounded so substrings
# like "driving" or "MFDX" don't match.
_VIN_WORD = re.compile(r"(?<![A-Z])(?:VIN|MFD)(?![A-Z])")
NGRAM = 3                    # match on any N consecutive VIN chars (in order)
# Minimum run of CONSECUTIVE VIN characters a photo's OCR text must contain for it
# to count as a possible-VIN shot. 3 (the old partial threshold) matched incidental
# numbers in exterior scenes — license plates, signs, addresses, the VIN's own digit
# triples like "505"/"626" — so every order sent 3 random exterior photos and never
# reached the real VIN shot. 5 makes a coincidental match unlikely while still
# catching a partly-read VIN. Tune via OCR_MIN_RUN in .env.
OCR_MIN_RUN = int(os.getenv("OCR_MIN_RUN", "5"))
FOOTER_CROP = 0.13          # drop the bottom 13% (the "Delivery Condition ..." footer
                            # stamp incl. the zip) so it can't be OCR'd / matched
# Tesseract reads small text far better when it's enlarged (~20-30px glyphs). Before
# OCR we upscale each photo so its LONGEST edge reaches OCR_UPSCALE_TO pixels (only
# enlarges, never shrinks). The BOL photos arrive ~1440px, so the default 2800 is a
# ~2x bump that resolves faint windshield/sticker VINs without much slowdown. Raise
# it (e.g. 3600) for stubborn faint VINs; set 0 to disable upscaling.
UPSCALE_TARGET = int(os.getenv("OCR_UPSCALE_TO", "2800"))
_PSMS = (11, 6)             # 11 = sparse text anywhere (best for VIN in a scene)


def tesseract_ok() -> str | None:
    """Return the Tesseract version string, or None if the binary isn't reachable."""
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def check_engine() -> tuple[bool, str]:
    """Verify the CONFIGURED OCR engine is actually usable. Returns (ok, message).

    Guards the silent-failure trap: if OCR_ENGINE=easyocr but easyocr/numpy aren't
    installed in this venv, every photo's OCR throws and the scan swallows it, so
    every VIN comes back 'No VIN photo' instantly. This surfaces that as a clear
    message instead. (run.bat only installs deps when it first creates .venv, so a
    newly-added engine must be pip-installed into an existing venv by hand.)"""
    if OCR_ENGINE == "easyocr":
        try:
            import numpy            # noqa: F401  (easyocr needs it)
            import easyocr          # noqa: F401
        except Exception as e:
            return False, (f"OCR_ENGINE=easyocr but it isn't importable "
                           f"({type(e).__name__}: {e}). Install it into THIS venv: "
                           f"  pip install easyocr   "
                           f"(run.bat does NOT auto-install into an existing .venv).")
        return True, "easyocr"
    v = tesseract_ok()
    if v:
        return True, f"tesseract {v}"
    return False, ("tesseract binary not found — install Tesseract-OCR, or set "
                   "OCR_ENGINE=easyocr in .env.")


def _crop_footer(im: Image.Image) -> Image.Image:
    w, h = im.size
    return im.crop((0, 0, w, int(h * (1 - FOOTER_CROP))))


def _upscale(im: Image.Image) -> Image.Image:
    """Enlarge so the longest edge reaches UPSCALE_TARGET (only upscales). High-quality
    LANCZOS resample so faint VIN characters become legible to the OCR engine."""
    if not UPSCALE_TARGET:
        return im
    w, h = im.size
    longest = max(w, h)
    if longest >= UPSCALE_TARGET:
        return im
    s = UPSCALE_TARGET / longest
    return im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def _prep_tesseract(raw: bytes) -> Image.Image:
    # Crop the footer, UPSCALE small text, boost contrast, sharpen.
    im = _upscale(_crop_footer(Image.open(BytesIO(raw)).convert("L")))
    im = ImageOps.autocontrast(im)
    return im.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))


def _get_easy_reader():
    global _easy_reader
    if _easy_reader is None:
        import easyocr
        _easy_reader = easyocr.Reader(["en"], gpu=OCR_GPU)   # set OCR_GPU=true on NVIDIA
    return _easy_reader


def _ocr_text(raw: bytes, rotations: list[int] | None = None) -> str:
    """OCR a photo to text at the given angle(s). `rotations` is the list of angles
    to read at (e.g. [270]); None means upright only ([0]). Each angle rotates the
    WHOLE IMAGE and runs a full detection pass (for BOTH engines) — the only reliable
    way to read a sideways / upside-down VIN, since the text detectors miss
    non-horizontal text. Callers that want a priority order pass one angle at a
    time, so this reads EXACTLY the angles given (it does not auto-add upright)."""
    angles = list(rotations) if rotations else [0]
    if OCR_ENGINE == "easyocr":
        import numpy as np
        base = _crop_footer(Image.open(BytesIO(raw)).convert("RGB"))
        kwargs = dict(
            detail=0, paragraph=False,
            allowlist=_VIN_ALLOWLIST,  # only VIN chars (digits + UPPERCASE) — faster + cleaner
            mag_ratio=OCR_MAG_RATIO,   # upscale internally for detection (small text)
            text_threshold=0.4,   # lower => detect more text (default 0.7)
            low_text=0.3,         # default 0.4
            link_threshold=0.3,   # default 0.4
            contrast_ths=0.05,    # pick up low-contrast text
            adjust_contrast=0.7,
            min_size=3,           # allow small text regions
        )
        # High-sensitivity settings so faint / angled / low-contrast VINs still
        # get detected (defaults are tuned for clean document text).
        reader = _get_easy_reader()
        # Rotate the WHOLE IMAGE per angle and run a FULL detection pass on each.
        # EasyOCR's built-in `rotation_info` only rotates text boxes it ALREADY
        # detected — but its detector misses truly sideways / upside-down text (a
        # 90°-rotated windshield or door-jamb VIN), so there's no box to rotate and
        # the VIN is never read. Rotating the image first makes the VIN upright for
        # detection, which is the only reliable way to read a sideways VIN. Angle 0
        # is the cheap common case; the extra angles run only in the rotated retry.
        texts = []
        for angle in angles:
            im = base.rotate(-angle, expand=True) if angle else base   # -angle = clockwise
            try:
                texts.append(" ".join(reader.readtext(np.array(im), **kwargs)))
            except Exception:
                pass
        return " ".join(texts)
    im = _prep_tesseract(raw)
    out = ""
    for angle in angles:
        rim = im.rotate(-angle, expand=True) if angle else im   # clockwise
        for psm in _PSMS:
            try:
                out += " " + pytesseract.image_to_string(rim, config=f"--oem 1 --psm {psm}")
            except Exception:
                pass
    return out


def _ngrams(vin: str) -> set[str]:
    f = _ALNUM.sub("", vin.upper())
    return {f[i:i + NGRAM] for i in range(max(0, len(f) - NGRAM + 1))}


def ocr_images(images: list[bytes]) -> list[tuple[str, str]]:
    """OCR every photo ONCE -> list of (alnum_text, raw_text). Reuse across all
    VINs in a multi-VIN order instead of re-OCRing per VIN."""
    out = []
    for raw in images:
        try:
            rt = _ocr_text(raw).upper()
            out.append((_ALNUM.sub("", rt), rt))
        except Exception:
            out.append(("", ""))
    return out


def match_vin(ocr_results: list[tuple[str, str]], expected_vin: str,
              sibling_vins: list[str] | None = None,
              max_send: int = 6, debug: bool = False) -> list[int]:
    """Indices of photos to send to the API for `expected_vin`. A photo matches
    if its OCR text contains any run of NGRAM consecutive VIN chars (or the word
    "VIN"). For multi-VIN orders, only ngrams NOT shared with the sibling VINs
    are used, so a sibling's photo doesn't cross-match on the common prefix."""
    sibling_vins = sibling_vins or []
    shared = set()
    for sv in sibling_vins:
        shared |= _ngrams(sv)
    use = (_ngrams(expected_vin) - shared) or _ngrams(expected_vin)
    word_ok = not sibling_vins              # the word "VIN" can't tell siblings apart
    cands: list[int] = []
    for i, (text, raw) in enumerate(ocr_results):
        num_hit = any(g in text for g in use)
        word_hit = word_ok and bool(_VIN_WORD.search(raw))
        if debug:
            why = "VINngram" if num_hit else ("VINword" if word_hit else "miss")
            print(f"      photo {i}: {why:8} ...{text[-44:]}")
        if num_hit or word_hit:
            cands.append(i)
            if len(cands) >= max_send and not debug:
                break
    return cands[:max_send]


def find_vin_candidates(images: list[bytes], expected_vin: str,
                        max_send: int = 6, debug: bool = False) -> list[int]:
    """Convenience: OCR + match for a single VIN (no siblings)."""
    return match_vin(ocr_images(images), expected_vin, [], max_send, debug)


def _longest_vin_run(full: str, text: str) -> int:
    """Length of the longest CONTIGUOUS substring of the VIN (`full`) that appears
    in `text`. A real VIN shot yields a long run (often the whole VIN); incidental
    scene numbers yield only 2-3. Both args are alnum-normalized, uppercase."""
    best = 0
    n = len(full)
    for i in range(n):
        for j in range(i + best + 1, n + 1):     # only try runs longer than best so far
            if full[i:j] in text:
                best = j - i
            else:
                break
    return best


def scan_for_vin(images: list[bytes], expected_vin: str,
                 max_send: int = 10, debug: bool = False) -> list[int]:
    """Pick which photos to send to the API for `expected_vin`.

    A photo qualifies as a possible-VIN shot only if its OCR text contains a run
    of >= OCR_MIN_RUN CONSECUTIVE VIN characters, OR a standalone "VIN"/"MFD"
    label (the door-jamb sticker). ALL photos in the section are scanned and the
    qualifiers are RANKED by how much of the VIN they actually read, so the real
    VIN shot — which usually sits LATE in the sequence — isn't crowded out by
    earlier exterior photos. The top `max_send` qualifiers are returned; if NONE
    qualify, [] is returned and the caller sends nothing (-> No VIN photo).

    A photo that reads the FULL VIN short-circuits to just that one photo.

    SMART ROTATION FALLBACK: scan upright first (cheap, common). If nothing
    qualifies — e.g. a VIN sticker shot sideways — retry the OCR_ROTATIONS angles
    ONE AT A TIME, in order, stopping at the first angle that finds the VIN. The
    order is a real priority: the orientation door-jamb stickers usually read at
    (270° = shot from the right-hand side) is tried first, so it wins and the other
    angles aren't paid for once we have a hit."""
    full = _ALNUM.sub("", expected_vin.upper())

    def _pass(rotations: list[int] | None) -> list[int]:
        tag = f" @{rotations[0]}°" if rotations else ""
        scored: list[tuple[int, int]] = []           # (score, photo index)
        for i, raw in enumerate(images):
            try:
                raw_text = _ocr_text(raw, rotations=rotations).upper()
                text = _ALNUM.sub("", raw_text)
                run = _longest_vin_run(full, text) if full else 0
                has_word = bool(_VIN_WORD.search(raw_text))
            except Exception:
                continue
            if full and run >= len(full):            # whole VIN read -> that's the shot
                if debug:
                    print(f"      photo {i}: FULL VIN -> use this photo{tag}")
                return [i]
            score = max(run, OCR_MIN_RUN if has_word else 0)
            if debug:
                print(f"      photo {i}: run={run}"
                      f"{' +VINword' if has_word else ''} "
                      f"{'KEEP' if score >= OCR_MIN_RUN else 'skip'}{tag}")
            if score >= OCR_MIN_RUN:
                scored.append((score, i))
        scored.sort(key=lambda t: (-t[0], t[1]))     # most VIN read first, then by order
        return [i for _, i in scored[:max_send]]

    cand = _pass(None)                       # upright first — cheap, common case
    if cand or not OCR_ROTATIONS:
        return cand
    # Try each rotation in priority order; stop at the first that finds the VIN.
    for angle in OCR_ROTATIONS:
        if debug:
            print(f"      no match yet -> retry rotated {angle}°")
        cand = _pass([angle])
        if cand:
            return cand
    return []


# --------- Delivered ZIP from the Super Dispatch footer stamp ---------
# Every photo taken in the Super Dispatch carrier app carries a translucent
# black band at the very bottom with white text:
#     "Delivery Condition: 5/23/2026, Santa Clarita, CA 91355"     (left)
#     "Super Dispatch <logo>"                                      (right)
# Verified live 2026-06-12 on two different carriers' BOLs: identical format
# and position (the app burns it in, so it's uniform). The text occupies
# roughly the bottom 4% of the frame — the same stamp FOOTER_CROP cuts off
# before VIN matching; here we read it on purpose.
FOOTER_BAND = 0.07           # OCR strip: bottom 7% (stamp band + safety margin)
_ZIP_AFTER_STATE_RE = re.compile(r"\b[A-Z]{2}\W{0,3}(\d{5})(?:-\d{4})?\b")
_ZIP_ANY_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_DATE_PART_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def read_footer_zip(raw: bytes, debug: bool = False) -> str | None:
    """OCR the footer stamp of ONE photo and return the delivered ZIP, or None.

    The stamp text is pure white over a darkened band, so a high luminance
    threshold (>=200) isolates it cleanly from any background; upscaling 3x
    first makes the ~30px glyphs readable to Tesseract (validated: reads the
    stamp verbatim). Prefers a ZIP right after the 2-letter state ("CA 91355");
    falls back to the last 5-digit group, with the date masked out first."""
    try:
        im = Image.open(BytesIO(raw)).convert("L")
    except Exception:
        return None
    w, h = im.size
    strip = im.crop((0, int(h * (1 - FOOTER_BAND)), w, h))
    strip = strip.resize((strip.width * 3, strip.height * 3), Image.LANCZOS)
    loose: list[str] = []
    for thr in (200, 215, 185):              # white-text isolation, best first
        prep = strip.point(lambda p, t=thr: 0 if p >= t else 255)
        for psm in (7, 6, 11):               # 7 = single line (the stamp), then looser
            try:
                txt = pytesseract.image_to_string(
                    prep, config=f"--oem 1 --psm {psm}").upper()
            except Exception:
                continue
            txt = _DATE_PART_RE.sub(" ", txt)
            m = _ZIP_AFTER_STATE_RE.search(txt)
            if m:
                if debug:
                    print(f"      footer zip {m.group(1)} (thr{thr}/psm{psm})")
                return m.group(1)
            loose += _ZIP_ANY_RE.findall(txt)
    if loose and debug:
        print(f"      footer zip {loose[-1]} (loose fallback)")
    return loose[-1] if loose else None


def footer_zip(images: list[bytes], max_try: int = 6,
               debug: bool = False) -> str | None:
    """Delivered ZIP from the first readable footer among `images` (any single
    delivery-inspection photo carries the same stamp)."""
    for raw in images[:max_try]:
        z = read_footer_zip(raw, debug=debug)
        if z:
            return z
    return None
