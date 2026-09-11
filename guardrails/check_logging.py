"""Self-check for the structured logging setup.

Run from the repo root:  python3 -m guardrails.check_logging
Covers the Phase 1 logging scaffolding: JSON-lines shape, redaction of both the message
and the caller's structured fields, and handler idempotency across repeated setup calls.

The criterion that actually matters is the last one: the raw secret must not appear
anywhere in the bytes of the file we wrote.
"""

import io
import json
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from guardrails.logging_setup import setup_logging

RUN_ID = "selfcheck-logging"
EVIDENCE_DIR = Path("evidence")
LOG_PATH = EVIDENCE_DIR / f"{RUN_ID}.jsonl"

# Raw sensitive samples. None of these may survive into the log file.
SSN = "123-45-6789"
EMAIL = "member.jane@example-credit-union.org"
TOKEN = "sk-ant-api03-9f8e7d6c5b4a3210"
CARD = "4111111111111111"
SECRETS = [SSN, EMAIL, TOKEN, CARD]

# Start from a clean file so line counts below mean what they say.
LOG_PATH.unlink(missing_ok=True)

# The stdout sink is a real handler; capture it so we can check it too and so the
# self-check's own output stays readable.
captured = io.StringIO()
with redirect_stdout(captured):
    log = setup_logging(RUN_ID)
    # Secret in the message AND in the structured fields, including a nested value and
    # a value whose only tell is its field name.
    log.info(
        "lookup for %s failed",
        SSN,
        extra={
            "member_email": EMAIL,
            "api_key": TOKEN,
            "context": {"card": CARD, "step": 3},
            "step_index": 3,
        },
    )

    # Second setup with the same run_id must reuse the configured logger, not stack a
    # second pair of handlers onto it.
    log2 = setup_logging(RUN_ID)
    log2.info("second call", extra={"note": "no duplicate handlers"})

assert log2 is log, "setup_logging must return the same logger for the same run_id"
assert len(log.handlers) == 2, f"expected stdout + file handler, got {len(log.handlers)}"

raw_bytes = LOG_PATH.read_bytes()
lines = LOG_PATH.read_text(encoding="utf-8").splitlines()

# Idempotency: two log calls, two lines. A duplicated handler set would give four.
assert len(lines) == 2, f"expected 2 lines after 2 log calls, got {len(lines)}"

# Every line is valid JSON carrying the required keys.
records = []
for line in lines:
    record = json.loads(line)  # raises if a line is not valid JSON
    for key in ("ts", "level", "logger", "event"):
        assert key in record, f"missing required key {key!r} in {record}"
    datetime.fromisoformat(record["ts"])  # raises unless the timestamp is ISO8601
    records.append(record)

assert records[0]["level"] == "INFO"
assert records[0]["logger"] == f"interface_ai.{RUN_ID}"

# THE criterion: no raw sensitive substring anywhere in the file's bytes.
for secret in SECRETS:
    assert secret.encode() not in raw_bytes, f"raw secret {secret!r} leaked into {LOG_PATH}"

# ...and redaction markers are present, so the above passed by redacting rather than by
# quietly dropping the fields.
first = records[0]
assert "[REDACTED:SSN]" in first["event"], first["event"]
assert first["member_email"] == "[REDACTED:EMAIL]", first["member_email"]
assert first["api_key"] == "[REDACTED:SECRET]", first["api_key"]
assert first["context"]["card"] == "[REDACTED:CARD]", first["context"]
# Non-sensitive structured values survive intact, with their JSON types.
assert first["step_index"] == 3
assert first["context"]["step"] == 3

# The stdout sink gets the same redacted JSON lines, not a different format.
stdout_lines = captured.getvalue().splitlines()
assert len(stdout_lines) == 2, f"expected 2 stdout lines, got {len(stdout_lines)}"
assert json.loads(stdout_lines[0]) == first
for secret in SECRETS:
    assert secret not in captured.getvalue(), f"raw secret {secret!r} leaked to stdout"

print(
    f"PASS: logging self-check - 2 JSON lines in {LOG_PATH}, all required keys present, "
    f"{len(SECRETS)} raw secrets absent from file bytes and stdout, no duplicate handlers"
)
