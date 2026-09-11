"""Structured JSON-lines logging with redaction wired in at the source.

Every record emitted through a logger built here is written as one JSON object per line,
to stdout and to ``evidence/<run_id>.jsonl``. Before anything is formatted, the record
passes through a redaction filter, so an unredacted line cannot reach either sink.

Filter, not Formatter: a filter attached to the logger runs once per record and mutates
the record in place, so both handlers are covered by a single pass -- a formatter would
have to be attached (and kept in sync) on every handler, and each new sink would be one
more chance to forget it.

Caller-supplied structured fields go in via the standard ``extra=`` mechanism:

    log.info("member lookup", extra={"member_id": "123", "query": "ssn 123-45-6789"})

Both the message and the values of those fields are redacted.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from guardrails.redaction import redact, redact_obj

# Attribute names the logging module itself puts on every record. Anything on a record
# that is not in this set came from the caller's `extra=` and is treated as a structured
# field. Derived from a real record so it stays correct across Python versions.
_BUILTIN_ATTRS = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _structured_fields(record: logging.LogRecord) -> dict:
    """The caller's `extra=` fields, separated from logging's own record attributes."""
    return {k: v for k, v in record.__dict__.items() if k not in _BUILTIN_ATTRS}


class _RedactFilter(logging.Filter):
    """Rewrites the record's message and structured fields in place before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Interpolate %-args now; after this the record carries a single safe string.
        record.msg = redact(record.getMessage())
        record.args = ()
        record.__dict__.update(redact_obj(_structured_fields(record)))
        return True


class _JsonLinesFormatter(logging.Formatter):
    """One JSON object per record: timestamp, level, logger, event, plus caller fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_structured_fields(record))
        return json.dumps(payload, default=str)


def setup_logging(run_id: str, evidence_dir: str = "evidence") -> logging.Logger:
    """Return a logger for this run, writing JSON lines to stdout and evidence/<run_id>.jsonl.

    Calling it again with the same run_id returns the already-configured logger rather
    than stacking a second pair of handlers onto it.
    """
    logger = logging.getLogger(f"interface_ai.{run_id}")
    if logger.handlers:
        return logger

    path = Path(evidence_dir)
    path.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.INFO)
    logger.propagate = False  # stdout is handled here; don't let the root logger double it
    logger.addFilter(_RedactFilter())

    formatter = _JsonLinesFormatter()
    for handler in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(path / f"{run_id}.jsonl", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
