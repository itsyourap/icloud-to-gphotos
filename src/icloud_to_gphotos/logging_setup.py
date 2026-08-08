"""Logging configuration: a human-readable console stream plus a per-run file."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-38s %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Third-party loggers that are far too chatty at DEBUG level.
_NOISY = ("urllib3", "requests", "httpx", "httpcore", "pyicloud.session")


def configure(level: str = "INFO", log_file: Path | None = None) -> Path | None:
    """Install handlers on the root logger and return the log file path.

    Console output honours ``level``; the file always captures DEBUG so a failed
    unattended run leaves enough detail to diagnose it after the fact.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_TIME_FORMAT))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_TIME_FORMAT))
        root.addHandler(file_handler)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    return log_file
