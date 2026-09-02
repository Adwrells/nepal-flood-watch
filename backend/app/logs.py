"""File logging. Rotating, UTF-8, and split by severity.

Two files rather than one: flood-watch.log carries the full cycle narrative for
debugging, errors.log carries only WARNING and above so an operator checking
"did anything break overnight" does not have to read 10 MB of HTTP lines.

Nepali station names and Devanagari headlines go through these handlers, so the
encoding is pinned to UTF-8 -- the Windows default (cp1252) raises on them.
"""
import json
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone

from .config import ROOT

NPT = timezone(timedelta(hours=5, minutes=45))

LOG_DIR = ROOT / "logs"
# Errors are also kept as structured JSON lines. errors.log is for a human
# skimming "did anything break overnight"; errors.jsonl is for counting them --
# which spider fails most, whether a fault is new or chronic, whether a fix
# actually stopped it. A prose log cannot answer those without grepping.
FMT = "%(asctime)s %(levelname)-7s %(name)-12s %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5


class JsonlHandler(logging.handlers.RotatingFileHandler):
    """One JSON object per WARNING+ record, for machine analysis."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "ts": datetime.fromtimestamp(record.created, NPT).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "module": record.module,
                "line": record.lineno,
            }
            if record.exc_info:
                payload["exception"] = logging.Formatter().formatException(record.exc_info)[-2000:]
            with open(self.baseFilename, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + chr(10))
        except Exception:      # noqa: BLE001 - logging must never raise
            self.handleError(record)


def recent_errors(limit: int = 100) -> list[dict]:
    """Read back the structured error log, newest first.

    Served by /api/errors so a fault is visible in the console rather than only
    to whoever thinks to open a file on the server.
    """
    path = LOG_DIR / "errors.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-2000:]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def error_summary(limit: int = 500) -> dict:
    """Counts by logger and message, so chronic faults stand out from one-offs."""
    from collections import Counter
    rows = recent_errors(limit)
    by_logger = Counter(r.get("logger", "?") for r in rows)
    # Message text carries station names and URLs, so group on a coarse prefix
    # rather than the whole string, or every occurrence looks unique.
    by_kind = Counter(" ".join(r.get("message", "").split()[:6]) for r in rows)
    return {
        "total": len(rows),
        "newest": rows[0]["ts"] if rows else None,
        "oldest": rows[-1]["ts"] if rows else None,
        "by_logger": dict(by_logger.most_common(10)),
        "by_kind": dict(by_kind.most_common(10)),
    }


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

    structured = JsonlHandler(
        LOG_DIR / "errors.jsonl", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    structured.setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    for h in (full, errors, structured, console):
        root.addHandler(h)

    # httpx logs a full URL per request; at INFO that buries the cycle summary.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # Prediction verification gets its own file, not mixed into the cycle
    # narrative: "did alert X get echoed by real news" is a different question
    # from "did the cycle run cleanly", and answering it later means grepping
    # one small file instead of months of flood-watch.log. propagate=False so
    # it does not ALSO duplicate into the core log.
    predictions = logging.handlers.RotatingFileHandler(
        LOG_DIR / "predictions.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    predictions.setFormatter(formatter)
    pred_logger = logging.getLogger("prediction_log")
    pred_logger.setLevel(level)
    pred_logger.addHandler(predictions)
    pred_logger.propagate = False
