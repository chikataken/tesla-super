"""
Run logging: mirror everything a program prints (stdout + stderr, including
tracebacks) to a timestamped file under ./output/logs/, while still showing it
on the console. Call runlog.start("<name>") once at the top of a program's main().
"""
from __future__ import annotations
import datetime as _dt
import os
import sys

_LOG_DIR = os.getenv("LOG_DIR", "./output/logs")


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(sys.__stdout__, "isatty", lambda: False)()


def start(name: str = "run", log_dir: str | None = None) -> str:
    """Begin teeing stdout/stderr to ./output/logs/<name>_<timestamp>.log.
    Returns the log file path. Safe to call once per process."""
    # Make the console accept the non-ASCII characters we print (—, ✅, etc.).
    # No-op on macOS/Linux (already UTF-8); fixes the legacy Windows console.
    for _stream in (sys.__stdout__, sys.__stderr__):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    d = log_dir or _LOG_DIR
    os.makedirs(d, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, f"{name}_{ts}.log")
    f = open(path, "a", buffering=1, encoding="utf-8")
    f.write(f"# {name} log started {_dt.datetime.now().isoformat()}\n")
    f.write(f"# argv: {' '.join(sys.argv)}\n")
    sys.stdout = _Tee(sys.__stdout__, f)
    sys.stderr = _Tee(sys.__stderr__, f)
    return path
