"""Self-check for the command-line entry points.

Run from the repo root:  .venv/bin/python check_cli.py

Needs no network, no browser and no API key. Every case here is one the CLI must get
right before anyone can trust the exit code it reports, and each drives the real
``cli.py`` as a subprocess rather than calling its functions -- an exit code is only
worth checking through the door a shell uses.

  (a) --help works for the top level and every subcommand, and documents the exit codes
  (b) a --param that is not name=value is misuse: exit 2, a message, no traceback
  (c) replay against a missing artifact file is misuse: exit 2, and it says which path
  (d) replay missing a required param fails cleanly: exit 1, names the missing param,
      no traceback -- and does it without launching a browser, which is what makes a
      wrong invocation cheap rather than a ten-second round trip
  (e) a --param value never reaches stdout, stderr or the evidence log

What is deliberately not covered: a successful discovery or replay. Both need a live
sandbox and (for discovery) a model, so they are proven by the evidence runs under
evidence/, not from here.
"""

import json
import pathlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARTIFACT = "artifacts/altoro.account_balance.v1.json"

# A value that looks like a password, so its presence anywhere in the output is
# unmistakable. Nothing the CLI prints or logs may contain it.
SECRET = "hunter2-not-in-any-output"

# Named so a leftover file is obviously from this check, and removed at the end.
RUN_ID = "check-cli-scratch"
LOG = ROOT / "evidence" / f"{RUN_ID}.jsonl"


def run(*args, timeout: int = 120):
    """Invoke cli.py the way a reviewer would, and hand back the finished process."""
    return subprocess.run(
        [sys.executable, "cli.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def both(proc) -> str:
    return proc.stdout + proc.stderr


# -- (a) help, everywhere, documenting the exit codes --------------------------------

for argv in ([], ["discover"], ["replay"], ["handoff-demo"]):
    proc = run(*argv, "--help")
    where = " ".join(argv) or "(top level)"
    assert proc.returncode == 0, f"--help for {where} exited {proc.returncode}"
    for expected in ("exit codes:", "0  success", "1  failure", "2  misuse"):
        assert expected in proc.stdout, f"--help for {where} does not document {expected!r}"

# The contract's flags are all really there, and spelled the way the README documents.
usage = run("discover", "--help").stdout
for flag in ("--goal", "--target", "--param", "--capability-id", "--backend", "--model",
             "--max-steps", "--run-id", "--headed"):
    assert flag in usage, f"discover --help does not mention {flag}"

usage = run("replay", "--help").stdout
for flag in ("ARTIFACT_PATH", "--param", "--run-id", "--headed"):
    assert flag in usage, f"replay --help does not mention {flag}"

# No subcommand at all is misuse, not a traceback.
proc = run()
assert proc.returncode == 2, f"bare invocation exited {proc.returncode}, expected 2"
assert "Traceback" not in proc.stderr

# -- (b) a --param that is not name=value is misuse ----------------------------------

proc = run("replay", ARTIFACT, "--param", "member_id")
assert proc.returncode == 2, f"malformed --param exited {proc.returncode}, expected 2"
assert "name=value" in proc.stderr, f"malformed --param message is unclear: {proc.stderr!r}"
assert "Traceback" not in proc.stderr, "malformed --param produced a traceback"

# -- (c) a missing artifact file is misuse -------------------------------------------

proc = run("replay", "artifacts/no-such-capability.v1.json")
assert proc.returncode == 2, f"missing artifact exited {proc.returncode}, expected 2"
assert "no-such-capability" in proc.stderr, "the error does not say which path was missing"
assert "Traceback" not in proc.stderr, "missing artifact produced a traceback"

# -- (d) a missing required param fails cleanly, and before any browser --------------

started = time.monotonic()
proc = run("replay", ARTIFACT, "--param", f"password={SECRET}", "--run-id", RUN_ID)
elapsed = time.monotonic() - started

assert proc.returncode == 1, f"missing required param exited {proc.returncode}, expected 1"
assert "Traceback" not in both(proc), "missing required param produced a traceback"
for missing in ("username", "account_number"):
    assert f"required param {missing!r} is missing" in proc.stderr, (
        f"the error does not name the missing param {missing!r}: {proc.stderr!r}"
    )
# The engine rejects params before it touches the browser; the CLI must not have
# launched one anyway. browser.start is the log line a launch would have written.
assert "browser.start" not in both(proc), "a browser was launched despite invalid params"
assert elapsed < 20, f"rejecting invalid params took {elapsed:.1f}s; nothing should be that slow"

# -- (e) the param value is nowhere in the output ------------------------------------

assert SECRET not in both(proc), "a --param value was printed"
log_text = LOG.read_text(encoding="utf-8") if LOG.exists() else ""
assert SECRET not in log_text, "a --param value reached the evidence log"
assert "replay.rejected" in log_text, "the rejection was not recorded in the evidence log"

LOG.unlink(missing_ok=True)

# -- (f) the summaries themselves, without a live run --------------------------------
#
# Every printer above this line is only reached on a path that needs a browser, so a
# broken format string in one of them would not show up until a live demo. They are
# driven here against a real artifact and constructed results instead.

import cli  # noqa: E402 -- imported here so the subprocess cases above stay end-to-end
from artifact.schema import load_artifact  # noqa: E402
from escalation.intervention import Handoff, InterventionRequest, OperatorAction  # noqa: E402
from replay.outcomes import Outcome, ReplayResult  # noqa: E402

from contextlib import redirect_stdout  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from io import StringIO  # noqa: E402


def rendered(printer, *args) -> str:
    buffer = StringIO()
    with redirect_stdout(buffer):
        printer(*args)
    return buffer.getvalue()


artifact = load_artifact(ROOT / ARTIFACT)
text = rendered(cli._print_artifact, artifact, Path(ARTIFACT), "discover-x", "some-model")
# Derived from the artifact, not hardcoded: re-running discovery produces a
# different but equally valid capability, and a literal count here would make this
# check fail for a reason that has nothing to do with the CLI.
_art = json.loads(pathlib.Path(ARTIFACT).read_text(encoding="utf-8"))
for _label, _count in (
    ("Steps", len(_art["steps"])),
    ("Inputs", len(_art["inputs"])),
    ("Outputs", len(_art["outputs"])),
):
    assert f"{_label} ({_count})" in text, f"summary should report {_label} ({_count})"
assert "password" in text and "secret" in text, "a secret input is not flagged as one"
assert "{{password}}" in text, "a step's templated value is not shown"

text = rendered(
    cli._print_result,
    ReplayResult(
        outcome=Outcome.HARD_FAILURE,
        capability_id="altoro.account_balance",
        outputs={},
        detail="no locator matched at step 4",
        step_index=4,
        expected="button named 'GO'",
        observed="still on login.jsp",
        evidence="evidence/replay_altoro.png",
        retries=[{"step_index": 4, "condition": "stale_element"}],
    ),
    "replay-x",
    {"username": "jsmith", "password": SECRET},
)
# A hard failure is only actionable with all four of these, so all four must be printed.
for expected in ("hard_failure", "step", "expected", "observed", "evidence/replay_altoro.png"):
    assert expected in text, f"a hard failure summary omits {expected!r}"
assert SECRET not in text, "a --param value was printed in the replay summary"
assert "password (25 chars)" in text, "param shapes are not printed as name plus length"

request = InterventionRequest(
    request_id="iv-test", raised_at=datetime.now(timezone.utc), goal="Read a balance.",
    capability_id="altoro.account_balance", step_index=1, reason="needs credentials",
    url="http://demo.testfire.net/login.jsp", page_title="Altoro Mutual",
    observation_digest="...", screenshot="evidence/interventions/iv-test.png", param_names=[],
)
now = datetime.now(timezone.utc)
text = rendered(
    cli._print_handoff,
    Handoff(
        request=request,
        actions=[
            OperatorAction("fill", f"filled {len(SECRET)} chars", True, now),
            OperatorAction("navigate", "blocked by allowlist, not executed", False, now),
            OperatorAction("resume", "operator returned control to automation", True, now),
        ],
        outcome="resumed",
        operator="dana.ops",
    ),
    "handoff-demo",
)
assert "REFUSED" in text and "1 refused" in text, "a refused operator command is not visible"
assert "resumed" in text, "the handoff outcome is not reported"

print(
    f"PASS check_cli: help+exit codes on 4 parsers, misuse exits 2 (bad --param, "
    f"missing artifact, no subcommand), missing required param exits 1 in {elapsed:.1f}s "
    f"with no browser and no traceback, the param value appears in neither output nor "
    f"log, and all three summaries render from a real artifact without leaking a value."
)
