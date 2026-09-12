"""Self-check for the browser layer.

Run from the repo root:  .venv/bin/python -m agent.check_browser

Most of this runs offline against a throwaway ``http.server`` on 127.0.0.1, so the check
works with no network. A ``data:`` or ``file:`` URL would have been simpler, but the
allowlist requires a real host on an http(s) scheme and the shipped config is not
something a self-check gets to weaken -- so the offline pages are served over loopback
and pointed at a permissive Allowlist built here, in this file. ``guardrails/allowlist.json``
is untouched.

What is covered:
  (a) observe() and every action type (navigate/fill/select/read/click) against known markup
  (b) the allowlist blocks an out-of-scope domain and no page load happens
  (c) a locator whose primary cannot match falls through to a fallback, and says so
  (d) three ways a ref goes stale, each failing loudly and promptly
  (e) one live page load against the approved sandbox, skipped cleanly if unreachable
"""

import io
import time
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from agent.browser import BrowserSession, StaleRefError
from guardrails.allowlist import Allowlist, AllowlistViolation, load_allowlist
from guardrails.logging_setup import setup_logging

RUN_ID = "selfcheck-browser"
LOG_PATH = Path("evidence") / f"{RUN_ID}.jsonl"
LIVE_URL = "https://demo.testfire.net/index.jsp"

# A value that looks nothing like a pattern the redactor knows, so if it shows up in the
# log it is because this module logged it, not because redaction missed it.
FILL_SECRET = "hunter2-plaintext-never-log-me"

SEARCH_HTML = """<!doctype html><title>Member Search</title>
<h1>Member Search</h1>
<form action="/results.html" method="get">
  <label for="mid">Member ID</label>
  <input id="mid" name="mid" placeholder="id">
  <select id="branch" name="branch">
    <option value="east">East</option>
    <option value="west">West</option>
  </select>
  <button type="submit">Search</button>
</form>
<p id="status">Ready</p>
"""

RESULTS_HTML = """<!doctype html><title>Results</title>
<h1>Results</h1>
<p id="balance">Balance: 1234.56</p>
<button id="back">Back</button>
"""

# Permissive only about where it points -- loopback, on http, any path. Built here so
# the shipped deny-by-default config stays exactly as it ships.
TEST_ALLOWLIST = Allowlist(
    {
        "allowed_domains": ["127.0.0.1"],
        "allow_subdomains": False,
        "allowed_schemes": ["http"],
        "allowed_path_patterns": ["*"],
        "allowed_actions": ["navigate", "click", "fill", "select", "read"],
    }
)


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler without the per-request stderr chatter."""

    def log_message(self, *args):
        pass


def serve(directory: str) -> str:
    """Start a loopback file server on an ephemeral port; return its base URL."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=directory)
    )
    Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def ref_for(observation, role: str, name: str | None = None) -> str:
    """The ref of the first node with this role (and accessible name, if given)."""
    for node in observation.nodes:
        if node["role"] == role and (name is None or node["name"] == name):
            assert node["ref"], f"node {role}/{name} has no ref to point at"
            return node["ref"]
    raise AssertionError(f"no {role!r} node named {name!r} in {observation.nodes}")


def raises(exc_type, fn, *args):
    """Assert the call raises, and hand back the message."""
    try:
        fn(*args)
    except exc_type as exc:
        return str(exc)
    raise AssertionError(f"expected {exc_type.__name__} from {fn.__name__}{args}")


LOG_PATH.unlink(missing_ok=True)
captured = io.StringIO()
live_status = "skipped"

# Every log line the session emits goes to evidence/<run_id>.jsonl and to stdout; the
# stdout copy is captured so the PASS line at the end is the only thing on screen.
with redirect_stdout(captured):
    log = setup_logging(RUN_ID)

    with TemporaryDirectory() as tmp:
        Path(tmp, "index.html").write_text(SEARCH_HTML)
        Path(tmp, "results.html").write_text(RESULTS_HTML)
        base = serve(tmp)

        session = BrowserSession(TEST_ALLOWLIST, log, headless=True)
        session.start()
        try:
            # (a) observe + every action type ------------------------------------
            result = session.act({"action": "navigate", "url": f"{base}/index.html"})
            assert result.ok, result.detail
            observation = result.observation
            assert observation.title == "Member Search", observation.title
            assert observation.url == f"{base}/index.html", observation.url

            # The a11y tree is the observation: roles and accessible names, with a ref
            # on everything addressable.
            roles = {n["role"] for n in observation.nodes}
            assert {"heading", "textbox", "combobox", "button"} <= roles, roles
            assert any(n["ref"] for n in observation.nodes), "no refs in snapshot"
            textbox = next(n for n in observation.nodes if n["role"] == "textbox")
            assert textbox["name"] == "Member ID", textbox
            assert textbox["focusable"] is True, textbox
            assert "Ready" in observation.text_digest, observation.text_digest

            mid = ref_for(observation, "textbox", "Member ID")
            result = session.act({"action": "fill", "ref": mid, "value": FILL_SECRET})
            assert result.ok, result.detail
            assert FILL_SECRET not in result.detail, "fill value leaked into detail"
            filled = next(
                n for n in result.observation.nodes if n["ref"] == mid or n["role"] == "textbox"
            )
            assert filled["value"] == FILL_SECRET, filled

            branch = ref_for(result.observation, "combobox")
            result = session.act({"action": "select", "ref": branch, "value": "west"})
            # The selected value must NOT appear: a select carries data as
            # sensitive as a fill -- an account number is precisely the regulated
            # data this system must not persist -- so it reports its shape the way
            # a fill reports "filled N chars".
            assert result.ok, result.detail
            assert "selected 1 option(s)" in result.detail, result.detail
            assert "west" not in result.detail, f"the selected value leaked: {result.detail}"

            status = ref_for(result.observation, "paragraph")
            result = session.act({"action": "read", "ref": status})
            assert result.ok and result.detail == "Ready", result.detail

            search = ref_for(result.observation, "button", "Search")
            result = session.act({"action": "click", "ref": search})
            assert result.ok, result.detail
            assert result.observation.url.startswith(f"{base}/results.html"), result.observation.url
            assert "Balance: 1234.56" in result.observation.text_digest

            # (c) locator fallback ------------------------------------------------
            # Primary and first fallback cannot match; the second one can.
            result = session.act(
                {
                    "action": "click",
                    "locator": {
                        "primary": {"strategy": "role", "role": "button", "name": "Nope"},
                        "fallbacks": [
                            {"strategy": "testid", "name": "also-missing"},
                            {"strategy": "role", "role": "button", "name": "Back"},
                        ],
                    },
                }
            )
            assert result.ok, result.detail
            assert result.detail.startswith("matched fallback[1] role='button'"), result.detail

            # A locator chain where nothing matches is a failed action, not a hang.
            started = time.monotonic()
            result = session.act(
                {
                    "action": "click",
                    "locator": {"primary": {"strategy": "css", "selector": "#missing"}},
                }
            )
            assert not result.ok and "no locator matched" in result.detail, result.detail
            # Bound raised from 8s: the primary locator now gets a longer budget
            # than a fallback, so a full miss costs 6s plus one short wait per
            # fallback. Still a hard bound -- the point of this assertion is that
            # nothing waits forever.
            assert time.monotonic() - started < 16, "unbounded wait on an unmatched locator"

            # (d) stale refs ------------------------------------------------------
            observation = session.observe()
            back = ref_for(observation, "button", "Back")

            # d1: a ref that was never in any snapshot.
            result = session.act({"action": "click", "ref": "e9999"})
            assert not result.ok and "StaleRefError" in result.detail, result.detail

            # d2: a ref held across a navigation. act() re-observes after every action,
            # so the ref from the old page is simply not in the current snapshot -- and
            # a ref that is in the current snapshot is, by definition, the live element.
            result = session.act({"action": "navigate", "url": f"{base}/index.html"})
            assert result.ok, result.detail
            result = session.act({"action": "click", "ref": back})
            assert not result.ok, result.detail
            assert "not in the most recent observation" in result.detail, result.detail

            # d3: a ref from the current snapshot whose element has since been removed.
            observation = session.observe()
            gone = ref_for(observation, "button", "Search")
            session._page.evaluate("() => document.querySelector('button').remove()")
            started = time.monotonic()
            result = session.act({"action": "click", "ref": gone})
            assert not result.ok and "no longer resolves" in result.detail, result.detail
            assert time.monotonic() - started < 6, "stale-ref failure was not prompt"

            # d4: the document gate, reached by navigating without re-observing -- the
            # one case where a ref number from the old page could still be live on the
            # new one. act() cannot get here (it always re-observes), so drive the page
            # directly to prove the gate is real.
            observation = session.observe()
            orphan = ref_for(observation, "textbox", "Member ID")
            session._page.goto(f"{base}/results.html", wait_until="domcontentloaded")
            assert "previous document" in raises(StaleRefError, session._resolve_ref, orphan)
        finally:
            session.stop()
            session.stop()  # idempotent

    # (b) allowlist enforcement -----------------------------------------------
    # The shipped config this time -- the real one the agent runs under.
    shipped = BrowserSession(load_allowlist(), log, headless=True)
    shipped.start()
    try:
        before = shipped.url
        message = raises(
            AllowlistViolation,
            shipped.act,
            {"action": "navigate", "url": "https://www.evil-example.com/bank/main.jsp"},
        )
        assert "not allowed" in message, message
        assert shipped.url == before == "about:blank", f"a page loaded anyway: {shipped.url}"

        # An allowed action type on an out-of-scope page is still blocked, and the
        # violation is not downgraded into ActResult(ok=False).
        raises(AllowlistViolation, shipped.act, {"action": "click", "ref": "e1"})

        # (e) one live page load against the approved sandbox. A single page, and a
        # sandbox that will not load is reported rather than retried -- the offline
        # cases above are what make this check meaningful with no network.
        result = shipped.act({"action": "navigate", "url": LIVE_URL})
        if not result.ok:
            live_status = f"SKIPPED - {result.detail.splitlines()[0][:100]}"
        else:
            live_nodes = result.observation.nodes
            assert live_nodes, "live sandbox returned an empty a11y tree"
            assert any(n["ref"] for n in live_nodes), "live sandbox returned no refs"
            live_status = f"ran ({len(live_nodes)} a11y nodes from {result.observation.url})"
    finally:
        shipped.stop()

# (5) every observe and act left a structured line, and no fill value is in any of them.
lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
assert FILL_SECRET not in LOG_PATH.read_text(encoding="utf-8"), "fill value reached the log"
for event in ("browser.start", "browser.observe", "browser.act", "browser.act_result", "browser.stop"):
    assert any(f'"{event}"' in line for line in lines), f"no {event} line in {LOG_PATH}"
assert any('"value_len"' in line for line in lines), "fill was logged without its shape"

print(
    f"PASS: browser self-check - a11y observe + navigate/fill/select/read/click, "
    f"allowlist blocks off-scope navigation with no page load, locator falls back to "
    f"fallback[1], 3 stale-ref cases fail loudly and promptly, {len(lines)} log lines "
    f"written with no fill value; live sandbox {live_status}"
)
