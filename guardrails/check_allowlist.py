"""Self-check for the allowlist guardrail.

Run from the repo root:  python3 -m guardrails.check_allowlist
Covers the Phase 1 done-criterion: "Allowlist blocks an out-of-scope domain/action".
"""

from guardrails.allowlist import Allowlist, AllowlistViolation, load_allowlist


def blocked(fn, *args) -> str:
    """Assert the call is blocked, and hand back the violation message."""
    try:
        fn(*args)
    except AllowlistViolation as exc:
        return str(exc)
    raise AssertionError(f"expected AllowlistViolation from {fn.__name__}{args}")


allowlist = load_allowlist()

# Allowed: in-scope domain, in-scope path, in-scope action. These must not raise.
allowlist.check_navigation("https://demo.testfire.net/bank/main.jsp")
allowlist.check_action("click", "https://demo.testfire.net/bank/transfer.jsp")
allowlist.check_action("fill", "http://localhost:8000/")

# Host matching is case-insensitive and port-insensitive.
allowlist.check_navigation("https://DEMO.TestFire.net:443/bank/main.jsp")

# Out-of-scope domain.
assert "not allowed" in blocked(
    allowlist.check_navigation, "https://chase.com/bank/main.jsp"
)

# The lookalike case: "evil-example.com" must not match an entry of "example.com".
lookalike = Allowlist(
    {
        "allowed_domains": ["example.com"],
        "allow_subdomains": True,
        "allowed_schemes": ["https"],
        "allowed_path_patterns": ["*"],
        "allowed_actions": ["navigate"],
    }
)
lookalike.check_navigation("https://example.com/x")
lookalike.check_navigation("https://sub.example.com/x")
assert "'evil-example.com' not allowed" in blocked(
    lookalike.check_navigation, "https://evil-example.com/x"
)
# Suffix-only tricks must fail too.
blocked(lookalike.check_navigation, "https://example.com.attacker.net/x")
blocked(lookalike.check_navigation, "https://notexample.com/x")

# Subdomains are off in the shipped config, so they are blocked there.
assert "not allowed" in blocked(
    allowlist.check_navigation, "https://admin.demo.testfire.net/bank/main.jsp"
)

# Out-of-scope action type on an otherwise-allowed URL.
assert "action 'download' not allowed" in blocked(
    allowlist.check_action, "download", "https://demo.testfire.net/bank/main.jsp"
)

# Allowed action type on an out-of-scope URL is still blocked.
blocked(allowlist.check_action, "click", "https://chase.com/bank/main.jsp")

# Out-of-scope path on an allowed domain.
blocked(allowlist.check_navigation, "https://demo.testfire.net/admin/console")

# Non-http schemes are blocked.
blocked(allowlist.check_navigation, "file:///etc/passwd")
blocked(allowlist.check_navigation, "javascript:alert(1)")

print("PASS: allowlist self-check - deny-by-default holds on domain, subdomain, lookalike, path, scheme, and action cases")
