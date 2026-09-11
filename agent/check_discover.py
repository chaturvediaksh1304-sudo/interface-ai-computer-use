"""Self-check for the decide layer, the discovery loop and the artifact compiler.

Run from the repo root:  python3 -m agent.check_discover

No API key and no browser are involved, by design. The Anthropic client and the
``BrowserSession`` are both injected into ``discover``, so both are replaced here
by fakes defined in this file: a ``FakeClient`` that returns canned,
Anthropic-shaped responses, and a ``FakeSession`` that serves canned
observations.

The fake session deliberately reissues **different ref ids on every snapshot**,
including for the same unchanged page. That mirrors the real Playwright
``aria_snapshot(mode="ai")`` behaviour, where a ``[ref=eN]`` marker is valid only
for the one snapshot that produced it. Any dependence on ref stability -- in the
loop or in the compiler -- fails here rather than on a live UI.

Covered, in order:
  (a) a scripted navigate -> fill -> click -> read -> done run drives the loop to
      completion, and the compiled artifact validates against artifact/schema.py,
      round-tripping through save_artifact/load_artifact
  (b) the artifact holds {{param}} templates, not the literal discovered values
  (c) no ref leaks anywhere into the serialized artifact
  (d) {"action": "stuck"} terminates the loop and surfaces the reason
  (e) exhausting max_steps is a loud failure, not a silent truncation
  (f) decide() parses a well-formed response and raises loudly on malformed ones
"""

import contextlib
import io
import json
import re
import tempfile
from types import SimpleNamespace

from pydantic import ValidationError

from agent.decide import Decision, DecisionError, decide
from agent.discover import (
    DiscoveryError,
    MaxStepsError,
    StuckError,
    compile_artifact,
    discover,
)
from artifact.schema import CapabilityArtifact, load_artifact, save_artifact
from guardrails.logging_setup import setup_logging


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeSession:
    """Canned observations, with fresh ref ids on every single snapshot.

    ``pages`` are page states; ``transitions`` gives the page index to move to
    after each *successful* act, in order. ``fail_acts`` names act call numbers
    that report ``ok=False`` -- a failed act changes nothing and consumes no
    transition, exactly as a misfired click on a real page would not.
    """

    def __init__(self, pages, transitions, fail_acts=()):
        self.pages = pages
        self.transitions = list(transitions)
        self.fail_acts = set(fail_acts)
        self.page = 0
        self.snapshot = 0
        self.acts = 0
        self.issued_refs: set[str] = set()

    def observe(self):
        self.snapshot += 1
        page = self.pages[self.page]
        nodes = []
        for i, node in enumerate(page["nodes"]):
            # The ref is a function of the snapshot, never of the node. Two
            # snapshots of the same page therefore disagree about every ref.
            ref = f"e{self.snapshot * 10 + i}"
            self.issued_refs.add(ref)
            nodes.append({"ref": ref, "value": None, "focusable": True, **node})
        return SimpleNamespace(
            url=page["url"], title=page["title"], nodes=nodes, text_digest=page["digest"]
        )

    def act(self, action):
        self.acts += 1
        if self.acts in self.fail_acts:
            return SimpleNamespace(
                ok=False, action=action, detail="element not interactable", observation=self.observe()
            )
        if self.transitions:
            self.page = self.transitions.pop(0)
        return SimpleNamespace(ok=True, action=action, detail="done", observation=self.observe())


class FakeClient:
    """Returns scripted decisions, resolving refs the way a real model would.

    A script entry writes its target as ``"@Account holder"`` (or
    ``"@Search#1"`` for the second node of that name). The client reads the ref
    back out of the prompt it was just handed, which is the same thing the real
    model does -- and means this check fails if the prompt ever stops carrying
    the refs, or carries stale ones.
    """

    def __init__(self, script, repeat_last=False):
        self.script = list(script)
        self.repeat_last = repeat_last
        self.prompts: list[str] = []

    def _resolve(self, action, prompt):
        ref = action.get("ref", "")
        if not ref.startswith("@"):
            return action
        target, _, nth = ref[1:].partition("#")
        found = re.findall(rf'\[(\w+)\] \S+ name="{re.escape(target)}"', prompt)
        assert found, f"no node named {target!r} in the prompt the loop built"
        return {**action, "ref": found[int(nth) if nth else 0]}

    @property
    def messages(self):
        return self

    def create(self, *, model, max_tokens, system, messages):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        entry = self.script[0] if (self.repeat_last and len(self.script) == 1) else self.script.pop(0)
        payload = {**entry, "action": self._resolve(entry["action"], prompt)}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def quiet_logger(name):
    """A real logger from guardrails.logging_setup, writing nowhere visible.

    Built inside a redirected stdout so its StreamHandler binds to a throwaway
    buffer, and pointed at a temporary evidence dir. Using the real thing rather
    than a stub keeps this check honest about the structured fields the loop
    emits -- a field name that collided with a LogRecord attribute would blow up
    here.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        return setup_logging(name, evidence_dir=tempfile.mkdtemp())


def raises(exc_type, fn, *args, **kwargs) -> str:
    """Assert the call raises ``exc_type``, and hand back the message."""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return str(exc)
    raise AssertionError(f"expected {exc_type.__name__} from {getattr(fn, '__name__', fn)}")


# --------------------------------------------------------------------------
# The scripted run: navigate -> fill -> click -> (click retried) -> read -> done
# --------------------------------------------------------------------------

MEMBER_ID = "800000"
GOAL = "Look up a member by member id and read back the account holder name."
TARGET = "https://demo.testfire.net/bank/members.jsp"

PAGES = [
    {
        "url": "https://demo.testfire.net/",
        "title": "Altoro Mutual",
        "digest": "Online Banking landing page.",
        "nodes": [{"role": "link", "name": "Member Search"}, {"role": "link", "name": "Home"}],
    },
    {
        "url": "https://demo.testfire.net/bank/members.jsp",
        "title": "Member Search",
        "digest": "Search for a member by id.",
        "nodes": [
            {"role": "textbox", "name": "Member ID"},
            # Two identically named buttons: the compiler must pin nth so that
            # replay presses the same one discovery pressed.
            {"role": "button", "name": "Search"},
            {"role": "button", "name": "Search"},
            {"role": "link", "name": "Home"},
        ],
    },
    {
        "url": "https://demo.testfire.net/bank/member.jsp?id=800000",
        "title": "Member Detail",
        "digest": "Account holder: Jane Q. Public",
        "nodes": [
            {"role": "heading", "name": "Member Detail"},
            {"role": "definition", "name": "Account holder", "value": "Jane Q. Public"},
        ],
    },
]

SCRIPT = [
    # No leading navigate: discover() seeds step 0 with a navigation to TARGET
    # itself, so the model is never asked to guess the address it was given.
    {"action": {"action": "fill", "ref": "@Member ID", "value": MEMBER_ID},
     "rationale": "Type the member id into the Member ID field."},
    {"action": {"action": "click", "ref": "@Search#1"},
     "rationale": "Submit the search form using its Search button."},
    {"action": {"action": "click", "ref": "@Search#1"},
     "rationale": "Submit the search form using its Search button."},
    {"action": {"action": "read", "ref": "@Account holder"},
     "rationale": "Read the account holder name off the detail view."},
    {"action": {"action": "done"}, "rationale": "The goal value has been read back."},
]


def run_scripted():
    session = FakeSession(PAGES, transitions=[1, 1, 2, 2], fail_acts={3})
    client = FakeClient(SCRIPT)
    artifact = discover(
        GOAL, TARGET,
        client=client, session=session, logger=quiet_logger("check-a"),
        run_inputs={"member_id": MEMBER_ID},
        capability_id="altoro.member_lookup",
    )
    return artifact, session, client


# --------------------------------------------------------------------------
# (a) the loop completes and the artifact validates against the real schema
# --------------------------------------------------------------------------

artifact, session, client = run_scripted()

assert isinstance(artifact, CapabilityArtifact)
assert [s.action for s in artifact.steps] == ["navigate", "fill", "click", "read"], (
    f"the failed click must not become a step; got {[s.action for s in artifact.steps]}"
)
assert session.acts == 5, f"session saw {session.acts} acts (4 recorded + 1 failed retry)"
assert [s.index for s in artifact.steps] == [0, 1, 2, 3]

# Round-trip through the real save/load path: load_artifact re-runs every one of
# the schema's seven rules on the JSON that actually hit disk.
with tempfile.TemporaryDirectory() as tmp:
    path = save_artifact(artifact, dir=tmp)
    reloaded = load_artifact(path)
assert reloaded.model_dump(mode="json") == artifact.model_dump(mode="json")

# Rule 5 in practice: the declared output reads from a read step.
assert len(artifact.outputs) == 1
assert artifact.steps[artifact.outputs[0].from_step].action == "read"

# A real checkpoint, taken from the final observation -- not a placeholder, and
# not the member's data either.
assert artifact.checkpoint.kind == "a11y_node_present"
assert artifact.checkpoint.expected == "Account holder"
assert artifact.checkpoint.locator is not None
assert "Jane Q. Public" not in json.dumps(artifact.model_dump(mode="json"))

# The ambiguity case: two buttons were named "Search", so the locator pins which.
click_step = artifact.steps[2]
assert click_step.locator.primary.nth == 1, (
    f"expected nth pinned on an ambiguous name, got {click_step.locator.primary!r}"
)

# compile_artifact is pure over its inputs: an empty transcript is a loud error,
# never an empty artifact.
assert "recorded no steps" in raises(DiscoveryError, compile_artifact, GOAL, TARGET, "x", [], [])

print("PASS (a) scripted run drives the loop to a 4-step artifact that validates against artifact/schema.py")


# --------------------------------------------------------------------------
# (b) values are generalised into {{param}} templates
# --------------------------------------------------------------------------

blob = json.dumps(artifact.model_dump(mode="json"))

assert MEMBER_ID not in blob, f"the literal discovered value {MEMBER_ID!r} was baked into the artifact"
assert artifact.steps[1].value == "{{member_id}}", artifact.steps[1].value
assert [p.name for p in artifact.inputs] == ["member_id"]
assert artifact.inputs[0].type == "string" and artifact.inputs[0].required
assert artifact.inputs[0].secret is False

# A value the caller did not supply stays a literal: generalising it would
# invent a parameter nobody asked for.
literal = FakeSession(PAGES, transitions=[1, 1, 2, 2], fail_acts={3})
ungeneralised = discover(
    GOAL, TARGET, client=FakeClient(SCRIPT), session=literal,
    logger=quiet_logger("check-b1"), run_inputs={}, capability_id="altoro.literal",
)
assert ungeneralised.steps[1].value == MEMBER_ID
assert ungeneralised.inputs == []

# A secret-looking param name is declared secret, and rule 7 of the schema then
# requires it to appear only as a bare template -- which it does, because the
# compiler replaces the whole value and never composes one.
secret_script = [dict(e) for e in SCRIPT]
secret_script[0] = {"action": {"action": "fill", "ref": "@Member ID", "value": "hunter2"},
                    "rationale": "Type the passphrase into the field."}
secret = discover(
    GOAL, TARGET, client=FakeClient(secret_script),
    session=FakeSession(PAGES, transitions=[1, 1, 2, 2], fail_acts={3}),
    logger=quiet_logger("check-b2"), run_inputs={"account_password": "hunter2"},
    capability_id="altoro.secret",
)
assert secret.inputs[0].secret is True
assert secret.steps[1].value == "{{account_password}}"
assert "hunter2" not in json.dumps(secret.model_dump(mode="json"))

print("PASS (b) filled values become {{param}} templates with declared inputs; literals and secrets handled")


# --------------------------------------------------------------------------
# (c) no ref leaks anywhere into the artifact
# --------------------------------------------------------------------------

payload = artifact.model_dump(mode="json")


def walk(node, path="artifact"):
    """Yield every (path, key, value) pair in the serialized artifact."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield f"{path}[{i}]", None, v
            yield from walk(v, f"{path}[{i}]")


assert session.issued_refs, "the fake session never handed out a ref; this check would be vacuous"
assert len(session.issued_refs) > len(PAGES[1]["nodes"]), (
    "refs must be reissued per snapshot for this check to mean anything"
)

for path, key, value in walk(payload):
    assert key != "ref", f"a 'ref' key survived into the artifact at {path}"
    if isinstance(value, str):
        assert value not in session.issued_refs, f"ref {value!r} leaked into the artifact at {path}"
        stray = re.findall(r"\be\d+\b", value)
        assert not stray, f"ref-shaped token(s) {stray} leaked into the artifact at {path}"

# What replaced them: role/name locators straight off the accessibility tree,
# with css never used as a primary (Architecture.md: css is the escape hatch).
assert artifact.steps[1].locator.primary.strategy == "label"     # form control
assert artifact.steps[2].locator.primary.strategy == "role"      # button
assert artifact.steps[2].locator.primary.role == "button"
assert artifact.steps[2].locator.primary.name == "Search"
assert [f.strategy for f in artifact.steps[2].locator.fallbacks] == ["label", "text", "role"]
assert all(s.locator is None or s.locator.primary.strategy != "css" for s in artifact.steps)

print(f"PASS (c) no ref leaked into the artifact; {len(session.issued_refs)} refs were reissued across snapshots")


# --------------------------------------------------------------------------
# (d) stuck terminates the loop and is surfaced, not swallowed
# --------------------------------------------------------------------------

stuck_script = [
    SCRIPT[0],
    {"action": {"action": "stuck", "reason": "the form demands a one-time passcode I do not have"},
     "rationale": "No node on this page accepts a passcode, so a human must take over."},
]
stuck_session = FakeSession(PAGES, transitions=[1, 1])
try:
    discover(GOAL, TARGET, client=FakeClient(stuck_script), session=stuck_session,
             logger=quiet_logger("check-d"), capability_id="altoro.stuck")
    raise AssertionError("stuck was swallowed: discover returned instead of raising")
except StuckError as exc:
    assert "one-time passcode" in exc.reason
    assert exc.step_index == 2 and exc.goal == GOAL and exc.target == TARGET
    assert exc.url == PAGES[1]["url"], exc.url
    assert "one-time passcode" in str(exc)
assert stuck_session.acts == 2, "the loop kept acting after the model said it was stuck"

print("PASS (d) stuck raises StuckError carrying goal/step/url/reason, and stops the loop")


# --------------------------------------------------------------------------
# (e) max_steps is a loud failure, not a silent truncation
# --------------------------------------------------------------------------

forever = FakeClient([{"action": {"action": "click", "ref": "@Home"},
                       "rationale": "Keep going in circles."}], repeat_last=True)
looping = FakeSession(PAGES, transitions=[])
message = raises(
    MaxStepsError, discover, GOAL, TARGET,
    client=forever, session=looping, logger=quiet_logger("check-e"),
    max_steps=4, capability_id="altoro.loop",
)
assert "budget of 4 steps" in message and "discarded" in message, message
# max_steps bounds the model's decisions; the deterministic step-0 navigation
# is not one of them, so the budget buys 4 decisions plus that one seed act.
assert looping.acts == 5, f"the budget was not honoured: {looping.acts} acts"
assert issubclass(MaxStepsError, DiscoveryError)

# A model that says "done" having done nothing is the same class of failure.
assert "without taking a single action" in raises(
    DiscoveryError, discover, GOAL, TARGET,
    client=FakeClient([{"action": {"action": "done"}, "rationale": "Nothing to do."}]),
    session=FakeSession(PAGES, transitions=[]), logger=quiet_logger("check-e2"),
)

print("PASS (e) exhausting max_steps raises MaxStepsError and discards the partial transcript")


# --------------------------------------------------------------------------
# (f) decide() parses a good response and raises loudly on malformed ones
# --------------------------------------------------------------------------

class Canned:
    """Returns one fixed body, whatever it is asked."""

    def __init__(self, text):
        self.text = text

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


observation = FakeSession(PAGES, transitions=[]).observe()
good = Canned('{"action": {"action": "click", "ref": "e11"}, "rationale": "Follow the search link."}')
decision = decide(good, GOAL, observation, [])
assert isinstance(decision, Decision)
assert decision.action == {"action": "click", "ref": "e11"}
assert decision.rationale == "Follow the search link."
assert good.kwargs["model"] == "claude-opus-5"
assert good.kwargs["system"] and good.kwargs["messages"][0]["role"] == "user"

# The prompt must actually carry the goal, the url and the current refs -- that
# is the whole contract with the model.
prompt = good.kwargs["messages"][0]["content"]
assert GOAL in prompt and observation.url in prompt
assert all(n["ref"] in prompt for n in observation.nodes)

# Preamble and a markdown fence are tolerated; the format is not.
lenient = Canned('Sure!\n```json\n{"action": {"action": "done"}, "rationale": "Finished."}\n```')
assert decide(lenient, GOAL, observation, []).action == {"action": "done"}

malformed = {
    "no JSON at all": "I think we should click the search link.",
    "not an object": "[1, 2, 3]",
    "unknown verb": '{"action": {"action": "scroll", "ref": "e11"}, "rationale": "Scroll down."}',
    "missing rationale": '{"action": {"action": "done"}}',
    "empty rationale": '{"action": {"action": "done"}, "rationale": "   "}',
    "missing required field": '{"action": {"action": "fill", "ref": "e11"}, "rationale": "Type."}',
    "extra field on action": '{"action": {"action": "click", "ref": "e11", "note": "x"}, "rationale": "Click."}',
    "extra top-level key": '{"action": {"action": "done"}, "rationale": "Done.", "plan": "later"}',
    "value not a string": '{"action": {"action": "fill", "ref": "e11", "value": 42}, "rationale": "Type."}',
    "action not an object": '{"action": "done", "rationale": "Done."}',
}
for label, body in malformed.items():
    raises(DecisionError, decide, Canned(body), GOAL, observation, [])

# A response carrying no text block at all is also a failure, not an empty decision.
raises(DecisionError, decide, SimpleNamespace(messages=SimpleNamespace(
    create=lambda **kw: SimpleNamespace(content=[]))), GOAL, observation, [])

# And the artifact schema itself still refuses a broken artifact, so (a)'s
# validation was not a no-op.
try:
    CapabilityArtifact.model_validate({**payload, "schema_version": "9.9"})
    raise AssertionError("the schema accepted an unsupported schema_version")
except ValidationError:
    pass

print(f"PASS (f) decide() parses a well-formed response and raises DecisionError on {len(malformed) + 1} malformed ones")

print(
    f"PASS: agent self-check - {len(artifact.steps)}-step artifact compiled from a faked "
    f"LLM run, validated against artifact/schema.py, refs generalised to a11y locators "
    f"and {MEMBER_ID!r} generalised to {{{{member_id}}}}; stuck, max_steps and malformed "
    f"responses all fail loudly. No network call was made."
)


# --------------------------------------------------------------------------
# (g) an accessible name made of live data must never become a locator
# --------------------------------------------------------------------------
from agent.discover import _name_is_unusable, _stable_part  # noqa: E402

# Names that ARE the value: a locator built from these changes with the data.
for volatile in ("-$2000000000000.00", "1,234.56", "800002 Savings", "9/11/26 2:47 PM"):
    assert _name_is_unusable(volatile), f"{volatile!r} should be rejected as a locator name"

# Real labels, including short ones. "GO" has two letters and must survive: it
# is a genuine button name, and a character-count threshold would discard it.
for label in ("GO", "Go", "OK", "Login", "Available balance", "Member Search"):
    assert not _name_is_unusable(label), f"{label!r} is a usable name and must be kept"

# A label with a timestamp welded on keeps only its stable words.
assert _stable_part("Ending balance as of 9/11/26 2:47 PM") == "Ending balance as of"
assert _stable_part("Available balance") == "Available balance"

print("PASS (g) value-shaped accessible names are rejected as locator anchors, "
      "short real labels are kept, and volatile suffixes are trimmed")


# --------------------------------------------------------------------------
# (h) a filled value must never reach the log
# --------------------------------------------------------------------------
import pathlib  # noqa: E402

from agent.discover import _loggable  # noqa: E402

SECRET = "hunter2-not-a-real-password"
scrubbed = _loggable({"action": "fill", "ref": "e4", "value": SECRET})
assert SECRET not in json.dumps(scrubbed), f"the value survived scrubbing: {scrubbed}"
assert scrubbed["value"] == f"<{len(SECRET)} chars>", scrubbed
assert scrubbed["ref"] == "e4" and scrubbed["action"] == "fill", "scrubbing lost the rest"
# Actions with nothing to hide are passed through untouched.
assert _loggable({"action": "click", "ref": "e9"}) == {"action": "click", "ref": "e9"}
assert _loggable({"action": "navigate", "url": "http://x/y"})["url"] == "http://x/y"

# The guarantee that matters is behavioural, not textual: run a discovery whose
# input is a secret, through the REAL logger into a real file, and assert the
# secret is absent from the bytes on disk. A bare password has no shape any
# redaction pattern can match, so keeping it out at the point of writing is the
# only thing between a real credential and the evidence file.
import tempfile as _tempfile  # noqa: E402

from guardrails.logging_setup import setup_logging as _setup_logging  # noqa: E402

_SECRET = "swordfish-not-a-real-password"
_leak_script = [dict(e) for e in SCRIPT]
_leak_script[0] = {"action": {"action": "fill", "ref": "@Member ID", "value": _SECRET},
                   "rationale": "Type the credential into the field."}
with _tempfile.TemporaryDirectory() as _tmp:
    _log = _setup_logging("check-leak", evidence_dir=_tmp)
    with contextlib.redirect_stdout(io.StringIO()):
        discover(GOAL, TARGET, client=FakeClient(_leak_script),
                 session=FakeSession(PAGES, transitions=[1, 1, 2, 2], fail_acts={3}),
                 logger=_log, run_inputs={"account_password": _SECRET},
                 capability_id="altoro.leakcheck")
    _written = pathlib.Path(_tmp, "check-leak.jsonl").read_text(encoding="utf-8")
assert _SECRET not in _written, "the filled value reached the log file"
assert '"<29 chars>"' in _written, "the value's length should be logged in its place"

print("PASS (h) a filled value never reaches the log file; only its length is recorded")


# --------------------------------------------------------------------------
# (i) a read that lands on a label is retargeted to the value beside it
# --------------------------------------------------------------------------
from agent.discover import _retarget_label_read  # noqa: E402


class _Obs:
    def __init__(self, nodes):
        self.nodes = nodes


class _ReadSession:
    """Returns each node's own name as the text read from it, like a real table."""

    def __init__(self, nodes):
        self.by_ref = {n["ref"]: n for n in nodes}
        self.reads = []

    def act(self, action):
        self.reads.append(action["ref"])
        node = self.by_ref[action["ref"]]
        return SimpleNamespace(ok=True, action=action, detail=node["name"], observation=None)


_ROW = [
    {"ref": "r1", "role": "cell", "name": "Available balance"},
    {"ref": "r2", "role": "cell", "name": "-$1,234.56"},
    {"ref": "r3", "role": "cell", "name": "Account holder"},
]
_label, _value = _ROW[0], _ROW[1]

# Reading the label cell must hand back the value cell next to it.
_session = _ReadSession(_ROW)
_swap = _retarget_label_read(_Obs(_ROW), _label, "Available balance", _session,
                             7, quiet_logger("check-i1"))
assert _swap is not None, "a read that returned its own label should have been retargeted"
assert _swap[0]["ref"] == "r2", _swap[0]
assert _session.reads == ["r2"], "the value must be actually read, never inferred"

# A read that returned real data is left alone -- it already found the value.
assert _retarget_label_read(_Obs(_ROW), _value, "-$1,234.56", _ReadSession(_ROW),
                            7, quiet_logger("check-i2")) is None

# A read whose text differs from the node's name is content, not a label.
assert _retarget_label_read(_Obs(_ROW), _label, "some other text", _ReadSession(_ROW),
                            7, quiet_logger("check-i3")) is None

# A label with no value cell after it stays as it is rather than grabbing something wrong.
_tail = [{"ref": "t1", "role": "cell", "name": "Account holder"},
         {"ref": "t2", "role": "cell", "name": "Jane Q Public"}]
assert _retarget_label_read(_Obs(_tail), _tail[0], "Account holder", _ReadSession(_tail),
                            7, quiet_logger("check-i4")) is None

print("PASS (i) a read returning its own label is retargeted to the adjacent value cell, "
      "which is really read; genuine data reads are left alone")
