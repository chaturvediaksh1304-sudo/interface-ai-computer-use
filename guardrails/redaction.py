"""Secret/PII redaction for log lines and structured payloads.

Used on the logging path and (later) on artifact serialization, so it is
deliberately defensive: it never raises, whatever it is handed.

Replacements preserve the *kind* of value removed (``[REDACTED:SSN]``,
``[REDACTED:EMAIL]``, ...) so logs stay debuggable.

Patterns are applied in order of decreasing specificity. Order matters: a
16-digit card number would otherwise be partially consumed by a looser
numeric pattern, and a bearer token would be split by the generic
key=value rule. See PATTERNS below for the ordering rationale.
"""

import re

__all__ = ["redact", "redact_obj"]

# Keys whose value is a secret regardless of what the value looks like.
_SECRET_KEY = r"(?:password|passwd|pwd|api[_-]?key|access[_-]?key|secret|token|auth)"

# Value in a key=value pair: everything up to whitespace or a common delimiter.
_SECRET_VALUE = r"""[^\s,;&"']+"""


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum. Used to tell a card number (PAN) apart from any other
    13-19 digit run, e.g. a reference id or a long account number."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_sub(m: "re.Match[str]") -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[REDACTED:CARD]"
    return m.group(0)


# (compiled pattern, replacement) applied in this exact order.
#
# Ordering rationale, most specific first:
#  1. TOKEN  - 'Authorization: Bearer <jwt>'. Must precede the generic
#              key=value rule, which would otherwise only catch 'auth'
#              and leave the token body in the log.
#  2. SECRET - key=value secrets. Runs before every value-shaped pattern
#              so that e.g. api_key=4111111111111111 is redacted as a
#              secret rather than as a card number.
#  3. EMAIL  - consumed before the numeric patterns, because an address
#              may contain digit runs that the phone pattern would bite.
#  4. SSN    - fixed 3-2-4 shape; must precede PHONE, whose separator
#              handling would otherwise chew the same separators.
#  5. ACCOUNT- keyword-anchored account/IBAN numbers. Precedes CARD so a
#              number explicitly labelled as an account is reported as an
#              account even when it happens to pass the Luhn check.
#  6. CARD   - 13-19 digits, Luhn-validated. Must precede PHONE: a
#              grouped PAN like 4111 1111 1111 1111 contains substrings
#              the phone pattern would match, splitting the redaction.
#  7. PHONE  - last, as the loosest numeric pattern.
PATTERNS: list[tuple[re.Pattern[str], object]] = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]+"), r"\1 [REDACTED:TOKEN]"),
    (
        re.compile(rf"""(?i)\b({_SECRET_KEY})(["']?\s*[=:]\s*["']?){_SECRET_VALUE}"""),
        r"\1\2[REDACTED:SECRET]",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED:EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    (
        re.compile(
            r"(?i)\b(?:acct|account|iban)[\s#:.]*(?:no\.?|number|num)?[\s#:.]*"
            r"(?:[A-Z]{2}\d{2}[A-Z0-9]{10,28}|\d{6,19})\b"
        ),
        "[REDACTED:ACCOUNT]",
    ),
    (re.compile(r"\b\d{4}(?:[ -]?\d{4}){2,4}\b|\b\d{13,19}\b"), _card_sub),
    (
        re.compile(r"(?:\+?1[-.\s]?)?\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
        "[REDACTED:PHONE]",
    ),
]


def redact(text: str) -> str:
    """Strip secrets and PII from a string, tagging each by kind.

    Idempotent: the ``[REDACTED:KIND]`` markers it emits are not themselves
    matched by any pattern (or re-match to the identical output).

    Never raises. Non-string input is coerced with ``str()``; anything whose
    ``__str__`` explodes yields a marker rather than taking down the run,
    because this sits directly in the logging path.
    """
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return "[REDACTED:UNPRINTABLE]"
    try:
        for pattern, repl in PATTERNS:
            text = pattern.sub(repl, text)  # type: ignore[arg-type]
    except Exception:
        return "[REDACTED:REDACTION-FAILED]"
    return text


def redact_obj(obj):
    """Recursively redact string values inside dicts/lists/tuples/sets.

    Dict keys are left alone (they are field names, not data), but a value
    under a secret-looking key is redacted wholesale — a bare ``"hunter2"``
    under ``"password"`` has no shape for the regexes to recognise.

    Non-string scalars pass through untouched so structured payloads keep
    their types. Never raises.
    """
    try:
        if isinstance(obj, str):
            return redact(obj)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and re.fullmatch(_SECRET_KEY, k, re.IGNORECASE):
                    out[k] = "[REDACTED:SECRET]"
                else:
                    out[k] = redact_obj(v)
            return out
        if isinstance(obj, (list, tuple, set)):
            return type(obj)(redact_obj(v) for v in obj)
        return obj
    except Exception:
        return "[REDACTED:REDACTION-FAILED]"
