"""
Structured logging to stdout, captured by journald when run under systemd.

The sibling tools print + tee to files (runlog.py); these are long-running
daemons instead, so they log via the stdlib `logging` module to stdout. systemd
routes stdout to journald (`journalctl -u direct-pickup-worker`). Default format
is single-line JSON for machine parsing; set LOG_FORMAT=text for a readable dev
console.

    from logging_setup import setup, get_logger
    setup("worker")
    log = get_logger(__name__)
    log.info("started", extra={"order_guid": guid})   # extra keys -> JSON fields
"""
from __future__ import annotations
import json
import logging
import sys

import config

# Reserved LogRecord attributes — anything NOT in here that a caller passes via
# `extra=` is emitted as a structured field.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class _TextFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items()
                  if k not in _RESERVED and not k.startswith("_")}
        return f"{base}  {extras}" if extras else base


def setup(service: str) -> None:
    """Configure the root logger once. `service` labels every line (listener/worker)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    for h in list(root.handlers):              # idempotent: clear uvicorn's defaults
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    fmt = _JsonFormatter(service) if config.LOG_FORMAT == "json" else _TextFormatter(service)
    handler.setFormatter(fmt)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
