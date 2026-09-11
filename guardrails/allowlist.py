"""Deny-by-default allowlist guardrail.

Every navigation and every action the agent (or the replay engine) wants to take is
checked against a small JSON config. Anything not explicitly allowed is blocked with a
loud exception -- per Rules.md there is no boolean return a caller can quietly ignore.
"""

import json
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = Path(__file__).with_name("allowlist.json")


class AllowlistViolation(Exception):
    """Raised when a URL or action type is not explicitly allowed."""


class Allowlist:
    def __init__(self, config: dict):
        # Hosts are compared case-insensitively; everything else is compared as written.
        self.domains = [d.lower() for d in config["allowed_domains"]]
        self.allow_subdomains = config["allow_subdomains"]
        self.schemes = [s.lower() for s in config["allowed_schemes"]]
        self.path_patterns = config["allowed_path_patterns"]
        self.actions = config["allowed_actions"]

    def check_navigation(self, url: str) -> None:
        """Allow the URL, or raise AllowlistViolation explaining exactly what failed."""
        parts = urlsplit(url)

        if parts.scheme.lower() not in self.schemes:
            raise AllowlistViolation(
                f"scheme {parts.scheme!r} not allowed (allowed: {self.schemes}) in {url!r}"
            )

        # .hostname is already lowercased and has the port stripped off. It is None for
        # inputs with no authority component, which is itself a block-worthy case.
        host = parts.hostname
        if host is None:
            raise AllowlistViolation(f"no host in URL {url!r}")

        # Exact host match, or -- only when allow_subdomains is on -- a true subdomain.
        # The leading dot is what stops "evil-example.com" from matching "example.com".
        allowed_host = any(
            host == d or (self.allow_subdomains and host.endswith("." + d))
            for d in self.domains
        )
        if not allowed_host:
            raise AllowlistViolation(
                f"domain {host!r} not allowed (allowed: {self.domains}, "
                f"subdomains={'on' if self.allow_subdomains else 'off'}) in {url!r}"
            )

        path = parts.path or "/"
        if not any(fnmatch(path, pattern) for pattern in self.path_patterns):
            raise AllowlistViolation(
                f"path {path!r} not allowed (allowed: {self.path_patterns}) in {url!r}"
            )

    def check_action(self, action_type: str, url: str) -> None:
        """Allow the action on that URL, or raise AllowlistViolation.

        The URL is re-checked here too: an allowed action type on an out-of-scope page
        is still out of scope.
        """
        if action_type not in self.actions:
            raise AllowlistViolation(
                f"action {action_type!r} not allowed (allowed: {self.actions})"
            )
        self.check_navigation(url)


def load_allowlist(path=None) -> Allowlist:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    return Allowlist(json.loads(config_path.read_text()))
