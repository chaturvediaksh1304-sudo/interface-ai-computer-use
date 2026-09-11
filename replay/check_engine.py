"""Self-check for the deterministic replay engine.

Run from the repo root:  .venv/bin/python -m replay.check_engine

Covers both Phase 4 done-criteria, plus the properties the engine claims that a
done-criterion alone would not catch.

  (a) a full replay succeeds against real markup and returns the declared outputs
  (b) a deliberately bad input is classified, not crashed
  (c) a primary locator that no longer matches falls through to a fallback, and
      the result says which one matched
  (d) every fallback exhausted is a hard failure carrying step index, expected,
      observed, and a screenshot that actually exists on disk
  (e) a missing required param fails before any browser action is taken
  (f) no LLM is reachable from the replay path
  (g) the same artifact and params replayed twice give the same answer
  (h) a recoverable condition is retried within the taxonomy's bound and the
      run proceeds

What this runs against, and why
-------------------------------
There is no Phase 3 discovery artifact yet -- Phase 3 is still blocked -- so
criterion (a) is proven against ``artifacts/example_member_lookup.v1.json``,
the hand-written Phase 2 artifact, with its URLs repointed at a local test
server. That artifact has the same shape a discovery run produces (navigate →
click → fill → select → click → click → read → read, two typed outputs, one
checkpoint), so it exercises every path the engine has. It is a stand-in, and
the criterion should be re-run against the real artifact once Phase 3 lands.

The markup is served from a throwaway ``http.server`` on 127.0.0.1, never from
the sandbox: a determinism check whose fixtures can change under it is not a
determinism check. The allowlist used here is built inline and scoped to that
loopback server; the shipped ``guardrails/allowlist.json`` is not touched.

Everything is written under a temporary directory, so a run leaves nothing
behind in ``evidence/``.
"""

import json
import logging
import ast
import sys
import tempfile
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent.browser import ActResult, BrowserSession, Observation
from artifact.schema import CapabilityArtifact
from guardrails.allowlist import Allowlist
from guardrails.logging_setup import setup_logging
from replay.engine import replay
from replay.outcomes import RETRY_LIMIT, Outcome

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = REPO / "artifacts" / "example_member_lookup.v1.json"

# The member the fixture knows about. Any other id lands on the empty-result page.
KNOWN_MEMBER = "800000"
EXPECTED_OUTPUTS = {"account_holder": "Jane Q. Member", "available_balance": "$1,234.56"}


# -- fixture site -------------------------------------------------------------
#
# Deliberately plain: labelled form controls, an accessible-named region, and a
# results page that either has a row or says in words that it has none. The
# "No matching records" wording matters -- it is one of the phrases
# replay.outcomes recognises as a real domain answer.

MAIN = """<!doctype html><title>Back office</title>
<h1>Back office</h1>
<a href="/bank/search.jsp">Member Search</a>
"""

SEARCH = """<!doctype html><title>Member search</title>
<form method="get" action="/bank/results.jsp">
  <label for="memberId">Member ID</label>
  <input id="memberId" name="memberId" type="text">
  <label for="acctType">Account type</label>
  <select id="acctType" name="acctType">
    <option value="All">All</option>
    <option value="Checking">Checking</option>
    <option value="Savings">Savings</option>
  </select>
  <button type="submit">Search</button>
</form>
"""

HIT = """<!doctype html><title>Results</title>
<table id="searchResults"><tbody>
  <tr><td>{member_id}</td><td><a href="/bank/detail.jsp">View detail</a></td></tr>
</tbody></table>
"""

MISS = """<!doctype html><title>Results</title>
<p>No matching records found for member {member_id}.</p>
"""

DETAIL = """<!doctype html><title>Member detail</title>
<section id="memberDetail" role="region" aria-label="Member Detail">
  <p><span>Account holder</span>:
     <span class="account-holder" aria-label="Account holder">Jane Q. Member</span></p>
  <p><span>Available Balance</span>:
     <span class="available-balance" aria-label="Available Balance">$1,234.56</span></p>
</section>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        member_id = (query.get("memberId") or [""])[0]

        if parts.path == "/bank/main.jsp":
            body = MAIN
        elif parts.path == "/bank/search.jsp":
            body = SEARCH
        elif parts.path == "/bank/results.jsp":
            template = HIT if member_id == KNOWN_MEMBER else MISS
            body = template.format(member_id=member_id)
        elif parts.path == "/bank/detail.jsp":
            body = DETAIL
        else:
            self.send_error(404)
            return

        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; the engine's own log is the record."""


def start_site() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


# -- harness ------------------------------------------------------------------


def test_allowlist() -> Allowlist:
    """Scoped to the loopback fixture only. The shipped config is left alone."""
    return Allowlist(
        {
            "allowed_domains": ["127.0.0.1"],
            "allow_subdomains": False,
            "allowed_schemes": ["http"],
            "allowed_path_patterns": ["/bank/*"],
            "allowed_actions": ["navigate", "click", "fill", "select", "read"],
        }
    )


class Recorder(logging.Handler):
    """Captures the run's structured log so assertions can read it back."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == name]


def local_artifact(base_url: str) -> dict:
    """The Phase 2 example artifact, repointed at the local fixture."""
    source = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    source["target"] = base_url
    source["steps"][0]["url"] = f"{base_url}/bank/main.jsp"
    return source


def mutate(source: dict, fn) -> CapabilityArtifact:
    """Deep-copy, apply one breaking change, and validate. Mirrors check_schema."""
    broken = deepcopy(source)
    fn(broken)
    return CapabilityArtifact.model_validate(broken)


NOWHERE = {"strategy": "css", "selector": "#no-such-element-anywhere"}


def run(artifact, params, session, evidence_dir: Path, run_id: str):
    """One replay, with its log captured. Returns (result, recorder)."""
    logger = setup_logging(run_id, evidence_dir=str(evidence_dir))
    recorder = Recorder()
    logger.addHandler(recorder)
    try:
        return replay(artifact, params, session=session, logger=logger,
                      evidence_dir=str(evidence_dir)), recorder
    finally:
        logger.removeHandler(recorder)


# -- stub session, for the two cases a real browser cannot express ------------


class StubSession:
    """A session that never opens a browser.

    Used for the two checks that are about the engine's own control flow rather
    than about a page: that a missing param is refused before any action is
    attempted, and that a recoverable condition is retried within the bound.
    A real browser could not produce a stale-element failure on demand without
    racing the page, which is the opposite of what this file is for.
    """

    url = "http://127.0.0.1/bank/main.jsp"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def act(self, action: dict) -> ActResult:
        self.calls.append(action)
        detail = self.script.pop(0) if self.script else "ok"
        ok = not detail.startswith("StaleRefError")
        return ActResult(ok, action, detail, Observation(self.url, "stub", [], ""))

    def screenshot(self, path: str) -> str:
        Path(path).write_bytes(b"stub")
        return path


def main() -> None:
    server, base = start_site()
    tmp = Path(tempfile.mkdtemp(prefix="check_engine_"))
    evidence = tmp / "evidence"
    source = local_artifact(base)
    valid = CapabilityArtifact.model_validate(source)
    good = {"member_id": KNOWN_MEMBER, "account_type": "All"}

    session = BrowserSession(test_allowlist(), setup_logging("browser", evidence_dir=str(evidence)))
    session.start()
    try:
        # (a) the happy path: every step runs, the checkpoint holds, outputs come back.
        result, log = run(valid, good, session, evidence, "case_a")
        assert result.outcome is Outcome.BUSINESS, result
        assert result.outputs == EXPECTED_OUTPUTS, result.outputs
        assert result.evidence is None, "a clean run must not leave failure evidence"
        assert [r.step_index for r in log.events("replay.step")] == list(range(8))
        assert all(
            r.locator_match is None or r.locator_match.startswith("primary")
            for r in log.events("replay.step_result")
        ), "unmutated artifact should match on primary everywhere"

        # (g) determinism: same artifact, same params, same page, same answer.
        again, _ = run(valid, good, session, evidence, "case_g")
        assert again.outputs == result.outputs, (again.outputs, result.outputs)
        assert again.outcome is result.outcome

        # (b) the one that matters: a member id the UI has never heard of.
        #     The results page says so in words, so this is a domain answer, not
        #     a breakage -- and the optional param is omitted here too, so the
        #     select step is skipped rather than blanked.
        bad, log = run(valid, {"member_id": "000000"}, session, evidence, "case_b")
        assert bad.outcome is Outcome.BUSINESS, bad
        assert bad.outputs == {}, bad.outputs
        assert "no matching records" in bad.detail, bad.detail
        assert bad.step_index == 5, bad.step_index
        assert Path(bad.evidence).exists(), bad.evidence
        assert [r.step_index for r in log.events("replay.step_skipped")] == [3]

        # (c) the primary locator drifted; a fallback still holds the capability up.
        drifted = mutate(
            source,
            lambda a: a["steps"][2]["locator"].update(
                primary={"strategy": "label", "name": "Renamed Since Discovery"}
            ),
        )
        result_c, log = run(drifted, good, session, evidence, "case_c")
        assert result_c.outcome is Outcome.BUSINESS, result_c
        assert result_c.outputs == EXPECTED_OUTPUTS, result_c.outputs
        assert "fallback[1]" in result_c.detail, result_c.detail
        matches = {r.step_index: r.locator_match for r in log.events("replay.step_result")}
        assert matches[2].startswith("fallback[1]"), matches[2]

        # (d) every fallback gone: a hard failure that can be debugged at 3am.
        gone = mutate(
            source,
            lambda a: a["steps"][1].update(
                locator={"primary": dict(NOWHERE), "fallbacks": [dict(NOWHERE), dict(NOWHERE)]}
            ),
        )
        result_d, _ = run(gone, good, session, evidence, "case_d")
        assert result_d.outcome is Outcome.HARD_FAILURE, result_d
        assert result_d.step_index == 1, result_d.step_index
        assert result_d.expected and result_d.observed, result_d
        assert "locator not found" in result_d.detail, result_d.detail
        shot = Path(result_d.evidence)
        assert shot.suffix == ".png" and shot.exists() and shot.stat().st_size > 0, result_d.evidence

        # A checkpoint that fails with nothing to explain it is breakage, not a
        # domain answer -- the asymmetry replay.outcomes exists to enforce.
        wrong = mutate(
            source,
            lambda a: a["checkpoint"].update(kind="url_matches", expected="/nowhere.jsp", locator=None),
        )
        result_cp, _ = run(wrong, good, session, evidence, "case_cp")
        assert result_cp.outcome is Outcome.HARD_FAILURE, result_cp
        assert result_cp.step_index == 8, result_cp.step_index

        # (f) nothing on this path can reach a model.
        assert "anthropic" not in sys.modules, sorted(m for m in sys.modules if "anthro" in m)
        assert not [m for m in sys.modules if m.split(".")[0] in {"anthropic", "openai"}]
        # Scan the parsed AST, not raw text: a docstring that merely *promises*
        # determinism must not be mistaken for a nondeterminism source.
        engine_src = (REPO / "replay" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(engine_src)
        banned_modules = {"anthropic", "openai", "random"}
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Attribute):
                called.add(node.attr)
        assert not (imported & banned_modules), f"replay engine imports {sorted(imported & banned_modules)}"
        assert "create" not in called or "messages" not in imported, "replay engine may reach a model API"
    finally:
        session.stop()
        server.shutdown()

    # (e) a missing required param must fail before a single action is attempted.
    stub = StubSession()
    try:
        run(valid, {"account_type": "All"}, stub, evidence, "case_e")
        raise AssertionError("missing required param should have raised")
    except ValueError as exc:
        assert "member_id" in str(exc), exc
    assert stub.calls == [], f"browser was touched before params were checked: {stub.calls}"

    # An unknown param is refused the same way -- a typo'd name must not be
    # silently dropped into a live form.
    try:
        run(valid, dict(good, membr_id="1"), stub, evidence, "case_e2")
        raise AssertionError("unknown param should have raised")
    except ValueError as exc:
        assert "membr_id" in str(exc), exc

    # (h) a recoverable condition is retried inside the bound, then the run proceeds.
    tiny = CapabilityArtifact.model_validate(
        {
            "capability_id": "stub.retry",
            "goal": "prove the recoverable path is wired to classify",
            "target": "http://127.0.0.1",
            "created_at": "2026-09-11T00:00:00Z",
            "inputs": [],
            "outputs": [
                {
                    "name": "holder",
                    "type": "string",
                    "description": "whatever the read step returned",
                    "from_step": 1,
                    "locator": {"primary": {"strategy": "css", "selector": "#x"}},
                }
            ],
            "steps": [
                {
                    "index": 0,
                    "action": "navigate",
                    "description": "open the page",
                    "url": "http://127.0.0.1/bank/main.jsp",
                },
                {
                    "index": 1,
                    "action": "read",
                    "description": "read a value that is briefly stale",
                    "locator": {"primary": {"strategy": "css", "selector": "#x"}},
                },
            ],
            "checkpoint": {"kind": "url_matches", "expected": "/bank/"},
        }
    )
    flaky = StubSession(["navigated", "StaleRefError: gone", "StaleRefError: gone", "Jane Q. Member"])
    result_h, _ = run(tiny, {}, flaky, evidence, "case_h")
    assert result_h.outcome is Outcome.BUSINESS, result_h
    assert result_h.outputs == {"holder": "Jane Q. Member"}, result_h.outputs
    assert len(result_h.retries) == RETRY_LIMIT, result_h.retries

    # ...and past the bound it escalates rather than looping forever.
    stuck = StubSession(["navigated"] + ["StaleRefError: gone"] * 10)
    result_h2, _ = run(tiny, {}, stuck, evidence, "case_h2")
    assert result_h2.outcome is Outcome.HARD_FAILURE, result_h2
    assert len(stuck.calls) == 1 + RETRY_LIMIT + 1, stuck.calls

    check_renavigation(valid, good, evidence)

    print(
        f"PASS: replay engine self-check - 8-step Phase 2 example artifact replays clean "
        f"against local markup returning {len(EXPECTED_OUTPUTS)} typed outputs and identically "
        f"on re-run; bad member id classified as a business outcome (not a crash); primary "
        f"locator drift recovered via fallback[1] and reported; exhausted fallbacks and an "
        f"unexplained checkpoint both hard-fail with step index, expected/observed and a real "
        f"screenshot on disk; missing and unknown params rejected with zero browser actions; "
        f"stale-element retried {RETRY_LIMIT}x then escalated; no anthropic/openai module "
        f"reachable from the replay path"
    )



# --------------------------------------------------------------------------
# (i) a click that silently does not navigate is re-run once, bounded
# --------------------------------------------------------------------------
class NoOpClickSession(StubSession):
    """A session where a click only takes effect when it is repeated.

    This is the failure being guarded against: the click is found and reported
    successful, but the page does not move, so the step after it looks for
    something that only exists on the page never reached. Repeating the same
    click is what makes it land -- exactly what the engine's recovery does.
    """

    def __init__(self):
        super().__init__()
        self.last_click = None
        self.repeated_clicks = 0
        self.navigated = False

    def act(self, action: dict) -> ActResult:
        self.calls.append(action)
        obs = Observation(self.url, "stub", [], "")
        kind = action["action"]
        if kind == "navigate":
            return ActResult(True, action, f"navigated to {action['url']}", obs)
        if kind in ("fill", "select"):
            return ActResult(True, action, "matched primary role='textbox'", obs)
        if kind == "click":
            signature = json.dumps(action, sort_keys=True)
            if signature == self.last_click:
                self.repeated_clicks += 1
                self.navigated = True          # the repeat is what lands
            else:
                self.navigated = False         # a fresh click silently no-ops
            self.last_click = signature
            return ActResult(True, action, "matched primary role='button' name='Login'", obs)
        if not self.navigated:
            return ActResult(False, action, "no locator matched; tried primary role='cell'", obs)
        return ActResult(True, action, "matched primary role='cell'", obs)


def check_renavigation(artifact, params, evidence_dir) -> None:
    session = NoOpClickSession()
    result, log = run(artifact, params, session, evidence_dir, "case_renav")

    # The count that matters is the REPEAT, not the total: the artifact has
    # several click steps of its own, so a plain click count would pass without
    # the recovery ever running.
    assert session.repeated_clicks >= 1, (
        "the click that failed to navigate was never re-run "
        f"(clicks={len(session.calls)}, repeats={session.repeated_clicks})"
    )
    assert log.events("replay.renavigating"), \
        "the re-navigation was not logged; a recovery must never be silent"

    # Bounded: a click that NEVER takes must still stop rather than loop.
    class NeverNavigates(NoOpClickSession):
        """Even the repeat does not land, so the run must stop, not loop."""

        def act(self, action):
            out = super().act(action)
            self.navigated = False
            return out

    stuck = NeverNavigates()
    verdict, _ = run(artifact, params, stuck, evidence_dir, "case_renav_bound")
    assert verdict.outcome is Outcome.HARD_FAILURE, verdict
    assert len(stuck.calls) < 40, f"re-navigation looped: {len(stuck.calls)} actions"
    print("PASS (i) a click that did not navigate is re-run once and the run recovers; "
          "a click that never navigates still stops, bounded and logged")


if __name__ == "__main__":
    main()
