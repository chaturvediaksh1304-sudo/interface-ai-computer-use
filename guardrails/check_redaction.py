"""Self-check for guardrails.redaction. Run: python3 -m guardrails.check_redaction"""

from guardrails.redaction import redact, redact_obj

LOG = (
    "2026-09-11 14:02:11 INFO replay step=4 member ssn=123-45-6789 "
    "email=jane.doe@examplebank.com card 4111 1111 1111 1111 "
    'phone (415) 555-0132 acct no. 000123456789 headers={"Authorization": '
    '"Bearer eyJhbGciOiJIUzI1NiJ9.abc-123_def", "api_key": "sk-live-9f8e7d6c5b4a"}'
)

out = redact(LOG)

# Every sensitive substring is gone.
for leak in (
    "123-45-6789",
    "jane.doe@examplebank.com",
    "4111 1111 1111 1111",
    "555-0132",
    "000123456789",
    "eyJhbGciOiJIUzI1NiJ9.abc-123_def",
    "sk-live-9f8e7d6c5b4a",
):
    assert leak not in out, f"leaked {leak!r} -> {out}"

# Kind is preserved, not blanket-replaced.
for kind in ("SSN", "EMAIL", "CARD", "PHONE", "ACCOUNT", "TOKEN", "SECRET"):
    assert f"[REDACTED:{kind}]" in out, f"missing {kind} -> {out}"

# Idempotent.
assert redact(out) == out, f"not idempotent:\n{out}\n{redact(out)}"

# No over-redaction: a benign operational line survives byte-for-byte.
BENIGN = (
    "2026-09-11 14:02:12 INFO replay step=5 action=click "
    "role=button name='Search' url=/members/search elapsed_ms=412 status=ok"
)
assert redact(BENIGN) == BENIGN, f"over-redacted:\n{BENIGN}\n{redact(BENIGN)}"

# Non-string / None-ish input must not raise.
assert redact(None) == "None"
assert redact(42) == "42"

# Nested structures.
payload = {
    "goal": "look up member",
    "password": "hunter2",
    "steps": [
        {"note": "emailed jane.doe@examplebank.com", "ms": 120},
        {"note": "ssn 123-45-6789 on file"},
    ],
}
red = redact_obj(payload)
assert red["password"] == "[REDACTED:SECRET]"
assert red["steps"][0]["note"] == "emailed [REDACTED:EMAIL]"
assert red["steps"][0]["ms"] == 120, "non-string scalar must keep its type"
assert red["steps"][1]["note"] == "ssn [REDACTED:SSN] on file"
assert red["goal"] == "look up member"
assert redact_obj(red) == red, "redact_obj not idempotent"

print(
    "PASS redaction: 7 kinds redacted from log line, no leaks, idempotent, "
    "benign line unchanged, non-str safe, nested dict/list walked."
)
