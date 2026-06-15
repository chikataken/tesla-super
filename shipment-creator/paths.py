"""Where the app reads and writes at runtime.

Two cases:
- **Dev checkout** (running `python app.py`): everything stays next to the source,
  exactly as before — `./output/...`, `./settings.json` — so nothing changes for
  development.
- **Frozen build** (the installed .exe): the program folder lives under Program
  Files and is READ-ONLY, so all mutable data — staged orders, downloaded BOLs,
  spares, settings, the active-excel marker — goes under
  `%LOCALAPPDATA%\\TFI Shipment Creator` instead. Bundled read-only assets (the
  static frontend) are read from PyInstaller's unpack dir.

Override the data location with the `SC_DATA_DIR` environment variable.
"""
import os
import sys

APP_NAME = "TFI Shipment Creator"


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


# Folder holding code / bundled resources. When frozen, PyInstaller extracts data
# files to sys._MEIPASS; in dev it's just this source folder.
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _default_data_dir() -> str:
    if is_frozen():
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    # dev: keep writing beside the source tree (unchanged behavior)
    return os.path.dirname(os.path.abspath(__file__))


# Root of all mutable runtime data.
DATA_DIR = os.getenv("SC_DATA_DIR", _default_data_dir())
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")


def data_path(*parts: str) -> str:
    """Absolute path under DATA_DIR, creating the parent folder on demand."""
    p = os.path.join(DATA_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def output_path(*parts: str) -> str:
    """Absolute path under DATA_DIR/output, creating the parent folder on demand."""
    p = os.path.join(OUTPUT_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def resource_path(*parts: str) -> str:
    """Absolute path to a bundled, read-only resource (e.g. the static frontend)."""
    return os.path.join(RESOURCE_DIR, *parts)
