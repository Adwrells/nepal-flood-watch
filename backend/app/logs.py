"""File logging. Rotating, UTF-8, and split by severity.

Two files rather than one: flood-watch.log carries the full cycle narrative for
debugging, errors.log carries only WARNING and above so an operator checking
"did anything break overnight" does not have to read 10 MB of HTTP lines.

Nepali station names and Devanagari headlines go through these handlers, so the
encoding is pinned to UTF-8 -- the Windows default (cp1252) raises on them.
"""
import logging
import logging.handlers

from .config import ROOT

LOG_DIR = ROOT / "logs"
FMT = "%(asctime)s %(levelname)-7s %(name)-12s %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5


def setup(level: int = logging.INFO) -> None:
    """Attach rotating file handlers plus a console handler. Idempotent."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return                                    # already configured
    root.setLevel(level)
    formatter = logging.Formatter(FMT)

    full = logging.handlers.RotatingFileHandler(
        LOG_DIR / "flood-watch.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    full.setFormatter(formatter)

    errors = logging.handlers.RotatingFileHandler(
        LOG_DIR / "errors.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    for h in (full, errors, console):
        root.addHandler(h)

    # httpx logs a full URL per request; at INFO that buries the cycle summary.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
