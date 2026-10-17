"""Structured logs in pino's format: one JSON object per line.

Every line carries pino's standard fields, so pino tooling (``pino-pretty``,
log shippers that parse pino) reads them as is::

    {"level":30,"time":1760731200000,"pid":4242,"hostname":"box","name":"spot2ytm","msg":"..."}

Levels and their numbers are pino's: trace 10, debug 20, info 30, warn 40,
error 50, fatal 60, plus "silent" to turn logging off. Extra context goes in
as top-level keys (``log.info("search", extra={"transferId": 3})``) and
exceptions in ``err`` with ``type``, ``message`` and ``stack``, as pino's
standard error serializer does.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

LEVELS = {  # pino name -> (python level, pino number)
    "trace": (TRACE, 10),
    "debug": (logging.DEBUG, 20),
    "info": (logging.INFO, 30),
    "warn": (logging.WARNING, 40),
    "error": (logging.ERROR, 50),
    "fatal": (logging.CRITICAL, 60),
}
SILENT = logging.CRITICAL + 10

# Attributes every LogRecord has; anything else came in through ``extra``.
_RECORD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "taskName",
    "color_message",  # uvicorn's copy of msg with terminal colors
}


def pino_level(levelno: int) -> int:
    for python_level, number in sorted(LEVELS.values(), reverse=True):
        if levelno >= python_level:
            return number
    return 10


class PinoFormatter(logging.Formatter):
    def __init__(self, name: str = "spot2ytm"):
        super().__init__()
        self.base = {"pid": os.getpid(), "hostname": socket.gethostname(), "name": name}

    def format(self, record: logging.LogRecord) -> str:
        line: dict = {"level": pino_level(record.levelno), "time": int(record.created * 1000), **self.base}
        if record.name != self.base["name"]:
            line["module"] = record.name.removeprefix("spot2ytm.")
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                line[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            line["err"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "stack": "".join(traceback.format_exception(*record.exc_info)).rstrip(),
            }
        line["msg"] = record.getMessage()
        return json.dumps(line, ensure_ascii=False, default=str, separators=(",", ":"))


def parse_level(value: str | None) -> int:
    """pino level name (or number) -> python logging level."""
    value = (value or "info").strip().lower()
    if value == "silent":
        return SILENT
    if value in LEVELS:
        return LEVELS[value][0]
    if value.isdigit():
        number = int(value)
        for python_level, pino_number in LEVELS.values():
            if number <= pino_number:
                return python_level
        return logging.CRITICAL
    raise ValueError(f"unknown log level {value!r}: use trace, debug, info, warn, error, fatal or silent")


def setup(level: str | None = None, file: str | Path | None = None) -> None:
    """Send every log (ours, uvicorn's, libraries') as pino lines to stdout and optionally a file."""
    formatter = PinoFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(parse_level(level))
    # Libraries stay at warn unless asked for more, so debug shows our own detail.
    library_level = max(root.level, logging.WARNING) if root.level > TRACE else root.level
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "multipart", "yt_dlp"):
        logging.getLogger(noisy).setLevel(library_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our request log


class Timer:
    """Milliseconds since creation, for ``responseTime``/``durationMs`` fields."""

    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)
