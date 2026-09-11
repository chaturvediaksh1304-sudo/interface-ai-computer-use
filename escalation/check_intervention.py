"""Self-check for the escalation / control-handoff module.

Run from the repo root:  .venv/bin/python -m escalation.check_intervention

Covers both Phase 6 done-criteria without a browser and without a single network call. The
session here is a fake defined in this file: it records every call made on it, and it runs
every action through the *real* allowlist, so "the human is bound by the guardrail too" is
proved against the shipped config rather than against a stub that always says yes.

The cases, in the order they appear below:

  (a) a simulated stuck condition produces an InterventionRequest whose goal, step index,
      url, title, digest and reason are all populated -- done-criterion 1
  (b) `from_stuck` builds the same thing from a real agent.discover.StuckError
  (c) the request JSON lands under evidence/interventions/ and round-trips
  (d) operator commands run against the SAME session object -- asserted by identity, and by
      the fake's own call log -- and each one produces a logged OperatorAction
  (e) an allowlist-violating operator command is recorded ok=False and the handoff CONTINUES
  (f) a fill command's secret value never reaches the log file, asserted against the bytes
  (g) resume and abort produce different unambiguous outcomes, and commands queued after the
      terminator are ignored

(d) and (f) are the two that matter. (d) is the difference between a handoff and a restart:
if the commands went to a new browser the run would still "pass" every other assertion here
while silently losing the session state that made the handoff worth doing. (f) is the one
that cannot be fixed after the fact -- a secret written to evidence is written.
"""

import json
from pathlib import Path

from agent.browser import ActResult, Observation
from agent.discover import StuckError
from escalation.intervention import (
    Handoff,
    InterventionRequest,
    OperatorAction,
    apply_operator_commands,
    from_stuck,
    raise_intervention,
)
from guardrails.allowlist import AllowlistViolation, load_allowlist
from guardrails.logging_setup import setup_logging

RUN_ID = "phase6-escalation-selfcheck"
EVIDENCE = Path("evidence")
LOG_PATH = EVIDENCE / f"{RUN_ID}.jsonl"

# A value with no shape any redaction pattern could recognise -- which is the whole point.
SECRET = "hunter2-Tr0ub4dor-correct-horse"

STUCK_URL = "https://demo.testfire.net/bank/transfer.jsp"
NODES = [
    {"role": "heading", "name": "Transfer Funds", "ref": "e1"},
    {"role": "combobox", "name": "From Account", "ref": "e2"},
    {"role": "textbox", "name": "Amount", "ref": "e3"},
    {"role": "textbox", "name": "Transaction Password", "ref": "e4"},
    {"role": "button", "name": "Transfer Money", "ref": "e5"},
    {"role": "textbox", "name": "Amount", "ref": "e6"},  # duplicate name, must not repeat
]
DIGEST = "Transfer Funds. This account requires a one-time transaction password to continue."


class FakeSession:
    """A BrowserSession stand-in: no browser, no network, and a log of everything asked of it.

    It enforces the real allowlist on every action, exactly where BrowserSession.act does,
    so an operator command that goes out of scope fails here for the same reason it would
    fail live.
    """

    def __init__(self, url: str):
        self._url = url
        self.allowlist = load_allowlist()
        self.calls = []  # every call, in order: used to prove the same object was driven

    @property
    def url(self) -> str:
        return self._url

    def observe(self) -> Observation:
        self.calls.append(("observe", self._url))
        return Observation(self._url, "Altoro Mutual: Transfer Funds", NODES, DIGEST)

    def screenshot(self, path: str) -> str:
        self.calls.append(("screenshot", path))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # A real PNG header, so the file the console links to is a real (if tiny) image.
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n<fake screenshot for self-check>")
        return path

    def act(self, action: dict) -> ActResult:
        self.calls.append(("act", action["action"]))
        target = action["url"] if action["action"] == "navigate" else self._url
        self.allowlist.check_action(action["action"], target)  # raises AllowlistViolation
        if action["action"] == "navigate":
            self._url = action["url"]
        detail = {
            "navigate": lambda: f"navigated to {self._url}",
            "click": lambda: f"matched ref {action.get('ref')!r}",
            "fill": lambda: f"matched ref {action.get('ref')!r}; filled {len(action['value'])} chars",
            "select": lambda: f"matched ref {action.get('ref')!r}; selected {action['value']}",
        }[action["action"]]()
        return ActResult(True, action, detail, self.observe())


def fresh_logger():
    """A logger writing to a log file that starts empty, so (f) inspects only this run."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    LOG_PATH.unlink(missing_ok=True)
    return setup_logging(RUN_ID, str(EVIDENCE))


logger = fresh_logger()

# ---------------------------------------------------------------------------------------
# (a) A simulated stuck condition produces a fully populated InterventionRequest.
#     Phase 6 done-criterion 1: goal / step / state / reason must all actually be there.
# ---------------------------------------------------------------------------------------

session = FakeSession(STUCK_URL)
request = raise_intervention(
    goal="transfer 100 from checking to savings",
    capability_id="altoro.transfer_funds",
    step_index=7,
    reason="the page asks for a one-time transaction password the agent does not have",
    session=session,
    logger=logger,
    evidence_dir=str(EVIDENCE),
)

assert isinstance(request, InterventionRequest)
assert request.goal == "transfer 100 from checking to savings"
assert request.capability_id == "altoro.transfer_funds"
assert request.step_index == 7
assert "transaction password" in request.reason
assert request.url == STUCK_URL, request.url
assert request.page_title == "Altoro Mutual: Transfer Funds", request.page_title
assert request.observation_digest.startswith("Transfer Funds"), request.observation_digest
assert request.request_id.startswith("iv-") and len(request.request_id) == 16, request.request_id
assert request.raised_at.tzinfo is not None  # evidence timestamps are never naive

# The state came from observing the live session, not from anything the caller passed.
assert ("observe", STUCK_URL) in session.calls

# Screenshot exists, sits under evidence/, and is pointed at by the request.
shot = Path(request.screenshot)
assert shot.exists() and shot.suffix == ".png", request.screenshot
assert EVIDENCE in shot.parents, request.screenshot

# param_names: names of the page's input controls, deduplicated, and NOTHING else.
assert request.param_names == ["From Account", "Amount", "Transaction Password"], request.param_names
assert "Transfer Money" not in request.param_names  # a button is not a parameter
assert SECRET not in json.dumps(request.to_dict())

# The same stuck state raised twice is the same request, not two files to reconcile.
again = raise_intervention(
    goal="transfer 100 from checking to savings",
    capability_id="altoro.transfer_funds",
    step_index=7,
    reason="the page asks for a one-time transaction password the agent does not have",
    session=FakeSession(STUCK_URL),
    logger=logger,
    evidence_dir=str(EVIDENCE),
)
assert again.request_id == request.request_id, (again.request_id, request.request_id)

# ---------------------------------------------------------------------------------------
# (b) from_stuck builds the same thing out of a real StuckError.
# ---------------------------------------------------------------------------------------

stuck = StuckError(
    goal="transfer 100 from checking to savings",
    target="https://demo.testfire.net/",
    step_index=7,
    url=STUCK_URL,
    reason="the page asks for a one-time transaction password the agent does not have",
)
from_exc = from_stuck(
    stuck,
    session=FakeSession(STUCK_URL),
    logger=logger,
    capability_id="altoro.transfer_funds",
    evidence_dir=str(EVIDENCE),
)
assert from_exc.goal == stuck.goal
assert from_exc.step_index == stuck.step_index
assert from_exc.reason == stuck.reason
assert from_exc.url == stuck.url
assert from_exc.page_title and from_exc.observation_digest and from_exc.param_names
assert from_exc.request_id == request.request_id  # same stuck state, same request

# ---------------------------------------------------------------------------------------
# (c) The JSON is written under evidence/interventions/ and round-trips unchanged.
# ---------------------------------------------------------------------------------------

path = EVIDENCE / "interventions" / f"{request.request_id}.json"
assert path.exists(), path
loaded = json.loads(path.read_text(encoding="utf-8"))
# Every raise above described the same stuck state, so each one overwrote this file rather
# than leaving three near-identical documents behind. What is on disk is the last of them.
assert loaded == from_exc.to_dict(), loaded
assert loaded["request_id"] == request.request_id

# Exactly the keys a console renders -- no envelope, no dangling references.
assert set(loaded) == {
    "request_id", "raised_at", "goal", "capability_id", "step_index", "reason",
    "url", "page_title", "observation_digest", "screenshot", "param_names",
}, sorted(loaded)
assert loaded["raised_at"] == from_exc.raised_at.isoformat()
assert Path(loaded["screenshot"]).exists()

# ---------------------------------------------------------------------------------------
# (d) Operator commands run against the SAME session object, and each one is logged.
# (e) An allowlist violation is recorded ok=False and the handoff carries on.
# (f) The filled secret never reaches the log.
# ---------------------------------------------------------------------------------------

before = len(session.calls)
handoff = apply_operator_commands(
    request,
    [
        {"kind": "note", "text": "reading the OTP off the hardware token"},
        {"kind": "fill", "ref": "e4", "value": SECRET},
        {"kind": "navigate", "url": "https://chase.com/transfer"},   # out of scope: blocked
        {"kind": "select", "ref": "e2", "value": "800002 Savings"},
        {"kind": "click", "ref": "e5"},
        {"kind": "teleport", "ref": "e5"},                           # not a command we execute
        {"kind": "fill", "ref": "e3"},                               # malformed: no value
        {"kind": "resume"},
        {"kind": "click", "ref": "e1"},                              # after the terminator
    ],
    session=session,
    logger=logger,
    operator="dana.ops",
)

assert isinstance(handoff, Handoff)
assert handoff.request is request                       # the request it was raised from
assert handoff.operator == "dana.ops"

# (d) The commands were executed on the object that got stuck -- not a rebuilt session.
assert len(session.calls) > before, "operator commands never reached the session"
acted = [c for c in session.calls[before:] if c[0] == "act"]
assert [c[1] for c in acted] == ["fill", "navigate", "select", "click"], acted
assert all(isinstance(a, OperatorAction) and a.at.tzinfo is not None for a in handoff.actions)

kinds = [(a.kind, a.ok) for a in handoff.actions]
assert kinds == [
    ("note", True),
    ("fill", True),
    ("navigate", False),    # (e) blocked by the allowlist
    ("select", True),       # (e) ...and the handoff continued past it
    ("click", True),
    ("teleport", False),
    ("fill", False),
    ("resume", True),
], kinds

blocked = handoff.actions[2]
assert "blocked by allowlist" in blocked.detail and "chase.com" in blocked.detail, blocked.detail
assert session.url == STUCK_URL, "a blocked navigate must not move the session"
assert "unsupported operator command kind" in handoff.actions[5].detail
assert "missing value" in handoff.actions[6].detail

# (g, first half) resume ends the handoff, and the queued click after it is ignored.
assert handoff.outcome == "resumed" and handoff.resumed
assert len(handoff.actions) == 8, "a command after the terminator was executed"
assert ("act", "click") not in session.calls[len(session.calls) - 1:]

# Every operator action produced a log line carrying the request_id -- done-criterion 2.
lines = [json.loads(line) for line in LOG_PATH.read_text(encoding="utf-8").splitlines()]
operator_lines = [
    line for line in lines
    if line["event"] == "escalation.operator_action" and line["request_id"] == request.request_id
]
assert len(operator_lines) == len(handoff.actions), (len(operator_lines), len(handoff.actions))
assert [line["kind"] for line in operator_lines] == [a.kind for a in handoff.actions]
assert any(line["event"] == "escalation.raised" for line in lines)
assert any(line["event"] == "escalation.blocked" and line["level"] == "WARNING" for line in lines)

# (f) The secret is nowhere in the bytes on disk; only its length survives.
raw = LOG_PATH.read_bytes()
assert SECRET.encode() not in raw, "the filled value reached the evidence log"
assert b"hunter2" not in raw
fill_line = operator_lines[1]
assert fill_line["command"]["value"] == f"<{len(SECRET)} chars>", fill_line["command"]
assert f"filled {len(SECRET)} chars" in fill_line["detail"], fill_line["detail"]

# ---------------------------------------------------------------------------------------
# (g) abort is unambiguously different from resume, and so is a handoff nobody terminated.
# ---------------------------------------------------------------------------------------

aborted = apply_operator_commands(
    request,
    [
        {"kind": "note", "text": "this member needs a manual review, not automation"},
        {"kind": "abort"},
        {"kind": "fill", "ref": "e4", "value": SECRET},  # ignored
    ],
    session=session,
    logger=logger,
    operator="dana.ops",
)
assert aborted.outcome == "abandoned" and not aborted.resumed
assert [a.kind for a in aborted.actions] == ["note", "abort"], aborted.actions
assert "does not continue" in aborted.actions[1].detail
assert aborted.outcome != handoff.outcome

# No terminator at all is "abandoned": nobody said the session was fit to continue.
silent = apply_operator_commands(
    request, [{"kind": "click", "ref": "e5"}], session=session, logger=logger, operator="dana.ops"
)
assert silent.outcome == "abandoned" and not silent.resumed

# The secret was never re-logged by the ignored command either.
assert SECRET.encode() not in LOG_PATH.read_bytes()

# The allowlist is the real one: this is what it is protecting against.
try:
    load_allowlist().check_action("navigate", "https://chase.com/transfer")
except AllowlistViolation:
    pass
else:
    raise AssertionError("the allowlist used in this check is not actually enforcing")

print(
    f"PASS: escalation self-check - {request.request_id} carries goal/step/state/reason, "
    f"round-trips from {path}, {len(handoff.actions)} operator actions ran on the same session "
    f"(1 blocked by allowlist, handoff continued), resume vs abort distinct, secret absent from "
    f"{LOG_PATH}"
)
