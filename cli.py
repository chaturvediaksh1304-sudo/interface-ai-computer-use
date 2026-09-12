"""Command-line entry points: the two commands Architecture.md promises, plus a demo.

    discover      one LLM-driven run against a live target, compiled into a saved artifact
    replay        that artifact executed deterministically, with no model anywhere
    handoff-demo  the Phase 6 escalation, reproducible in one command

This file is wiring and printing, nothing else. Every decision it appears to make is
made somewhere else: the allowlist decides what may be visited, ``agent.discover``
decides when a run is done or stuck, ``replay.engine`` decides what a failure means,
``escalation.intervention`` decides what an operator is allowed to do. If behaviour
needs changing, it does not change here.

Three conventions worth knowing before reading further:

*The exit code is the result.* A reviewer runs these from a shell, and a shell reads
exit codes, so the outcome is carried there as well as in the printed summary --
0 success, 1 a failure the system models and expects, 2 misuse. See ``EXIT_*`` below.

*Parameter values are never printed.* A ``--param`` value may be a password. Names and
lengths are printed; values are not, anywhere, on any path. This follows the precedent
set by ``agent.discover._loggable``.

*Parameters are passed as strings.* ``--param k=v`` comes off a shell command line, so
every value reaches the engine as a string. A capability declaring a ``number`` or
``bool`` input would have to be invoked from Python rather than from here. No artifact
in this repo declares one, so converting them would be guessing at a rule nobody has
needed yet.
"""

import argparse
import logging
import sys
from pathlib import Path

from agent.browser import BrowserSession
from agent.discover import (
    MaxStepsError,
    StuckError,
    _default_capability_id,
    discover,
)
from agent.llm import build_client
from artifact.schema import load_artifact, save_artifact
from escalation.intervention import apply_operator_commands, raise_intervention
from guardrails.allowlist import load_allowlist
from guardrails.logging_setup import setup_logging
from replay.engine import _check_params, replay
from replay.outcomes import Outcome

# The three exit codes, documented in --help and asserted by check_cli.py.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_MISUSE = 2

EXIT_CODES = """exit codes:
  0  success        -- discovery compiled an artifact; replay returned a business outcome
  1  failure        -- a failure the system models: replay hard-failure or rejected params,
                       discovery stuck or out of steps. Expected, reported, never a traceback
  2  misuse         -- bad arguments, a missing artifact file, a --param that is not k=v
"""

# The Phase 6 handoff is demonstrated against the same sandbox page the discovery run
# got stuck on: a login form the capability had no credentials for. Held here rather
# than read from an artifact so the demo still runs when no artifact has been recorded.
DEMO_TARGET = "http://demo.testfire.net/login.jsp"
DEMO_GOAL = "Read the account balance for the given account number."
DEMO_CAPABILITY_ID = "altoro.account_balance"
DEMO_REASON = (
    "The sign-in form needs credentials this capability was not given. "
    "Automation cannot proceed without a human supplying them."
)


def _textbox(nth: int) -> dict:
    """A locator for the nth textbox on the page, in the shape ``BrowserSession`` takes.

    The sandbox's login inputs carry no accessible name of their own, so position is
    the only thing there is to address them by. These are the same two fields the
    recorded capability fills at its steps 1 and 2.
    """
    return {
        "primary": {"strategy": "role", "role": "textbox", "nth": nth, "exact": False},
        "fallbacks": [],
    }


# -- printing -----------------------------------------------------------------------
#
# Everything below writes a summary a person can read in a terminal. The structured
# record of the same run is the JSON-lines log under evidence/; neither is a substitute
# for the other, and neither is printed in the other's format.


def _clip(text, limit: int = 96) -> str:
    """One line, shortened, for values that may be long or may contain newlines."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _field(label: str, value, width: int = 14) -> None:
    print(f"  {label:<{width}} {value}")


def _readable(record) -> str:
    """A retry record as ``key=value`` pairs rather than as a printed Python dict."""
    if not isinstance(record, dict):
        return str(record)
    return ", ".join(f"{key}={value}" for key, value in record.items())


def _param_shapes(params: dict) -> str:
    """Parameter names and value lengths -- never values. A value may be a password."""
    if not params:
        return "(none)"
    return ", ".join(f"{name} ({len(str(value))} chars)" for name, value in sorted(params.items()))


def _print_artifact(artifact, path: Path, run_id: str, model: str) -> None:
    print()
    print(f"Discovery complete: {artifact.capability_id} v{artifact.version}")
    _field("artifact", path)
    _field("target", artifact.target)
    _field("model", model)
    _field("log", f"evidence/{run_id}.jsonl")

    print(f"\n  Steps ({len(artifact.steps)}):")
    for step in artifact.steps:
        where = step.url if step.action == "navigate" else _describe_locator(step.locator)
        value = f"  value={step.value}" if step.value is not None else ""
        print(f"    {step.index:>2}  {step.action:<9} {_clip(where, 60):<60}{value}".rstrip())

    print(f"\n  Inputs ({len(artifact.inputs)}):")
    for param in artifact.inputs:
        flags = "required" if param.required else "optional"
        if param.secret:
            flags += ", secret"
        print(f"    {param.name:<20} {param.type:<7} {flags}")

    print(f"\n  Outputs ({len(artifact.outputs)}):")
    for output in artifact.outputs:
        print(f"    {output.name:<20} {output.type:<7} read at step {output.from_step}")

    checkpoint = artifact.checkpoint
    print(f"\n  Checkpoint: {checkpoint.kind} expecting {_clip(checkpoint.expected, 60)!r}")
    print(f"\nReplay it with:  .venv/bin/python cli.py replay {path}")


def _describe_locator(locator) -> str:
    """How a step's locator reads in a summary line: its primary strategy and target.

    A role locator shows both its role and its accessible name, because the two are
    what a reader has to compare against the page -- ``role='button'`` alone does not
    say which button, and ``'Login'`` alone does not say what kind of thing it is.
    """
    if locator is None:
        return ""
    primary = locator.primary
    described = f"{primary.strategy}={primary.role or primary.selector or primary.name!r}"
    if primary.strategy == "role" and primary.name:
        described += f" name={primary.name!r}"
    if primary.nth is not None:
        described += f"[{primary.nth}]"
    if locator.fallbacks:
        described += f" (+{len(locator.fallbacks)} fallback)"
    return described


def _print_result(result, run_id: str, params: dict) -> None:
    print()
    headline = {
        Outcome.BUSINESS: "Replay succeeded",
        Outcome.RECOVERABLE: "Replay ended in a recoverable state",
        Outcome.HARD_FAILURE: "Replay FAILED",
    }[result.outcome]
    print(f"{headline}: {result.capability_id} -> {result.outcome.value}")
    _field("params", _param_shapes(params))
    _field("detail", _clip(result.detail, 120))

    if result.outputs:
        print("\n  Outputs:")
        for name, value in result.outputs.items():
            print(f"    {name:<20} {_clip(value, 70)}")
    else:
        print("\n  Outputs: (none returned)")

    if result.outcome is Outcome.HARD_FAILURE:
        print("\n  Failure:")
        for label, value in (
            ("step", result.step_index),
            ("expected", _clip(result.expected, 100)),
            ("observed", _clip(result.observed, 100)),
            ("evidence", result.evidence),
        ):
            print(f"    {label:<10} {value}")

    if result.retries:
        print(f"\n  Recovered conditions ({len(result.retries)}):")
        for retry in result.retries:
            print(f"    {_clip(_readable(retry), 100)}")

    print(f"\n  Full log: evidence/{run_id}.jsonl")


def _print_handoff(handoff, run_id: str) -> None:
    request = handoff.request
    print()
    print(f"Intervention {request.request_id} -- {handoff.outcome}")
    _field("goal", _clip(request.goal, 90))
    _field("stopped at", f"step {request.step_index} on {request.url}")
    _field("reason", _clip(request.reason, 90))
    _field("operator", handoff.operator)
    _field("screenshot", request.screenshot)
    _field("request", f"evidence/interventions/{request.request_id}.json")

    print(f"\n  Operator actions ({len(handoff.actions)}):")
    for action in handoff.actions:
        mark = "ok " if action.ok else "REFUSED"
        print(f"    {mark:<8} {action.kind:<9} {_clip(action.detail, 90)}")

    refused = sum(1 for action in handoff.actions if not action.ok)
    print(
        f"\n  {len(handoff.actions)} action(s), {refused} refused by the allowlist. "
        # Deliberately no URL here. request.url is where the intervention was
        # RAISED, and after a successful handoff the session has moved on -- the
        # caller prints where it actually ended up, and two different URLs under
        # one heading reads like a contradiction.
        f"Session {'resumed' if handoff.resumed else 'abandoned'}."
    )
    print(f"  Full log: evidence/{run_id}.jsonl")


# -- commands ------------------------------------------------------------------------


def cmd_discover(args) -> int:
    """Run one LLM-driven discovery against a live target and save the artifact."""
    params = dict(args.param)
    capability_id = args.capability_id or _default_capability_id(args.goal, args.target)
    run_id = args.run_id or f"discover-{capability_id}"

    logger = setup_logging(run_id)
    client, model = build_client(args.backend)
    model = args.model or model

    print(f"Discovering {capability_id!r} against {args.target}")
    print(f"  model {model}, params {_param_shapes(params)}, log evidence/{run_id}.jsonl")

    session = BrowserSession(load_allowlist(), logger, headless=not args.headed)
    session.start()
    try:
        artifact = discover(
            args.goal,
            args.target,
            client=client,
            session=session,
            logger=logger,
            max_steps=args.max_steps,
            run_inputs=params,
            capability_id=capability_id,
            model=model,
        )
    except StuckError as exc:
        # Not a crash: the model said it cannot proceed without a human. The context it
        # carries is what `handoff-demo` turns into an intervention request.
        print(
            f"\nDiscovery stopped: the agent is stuck at step {exc.step_index} "
            f"on {exc.url}\n  reason: {exc.reason}\n"
            f"  Nothing was saved -- a path that was never shown to work is not a capability.\n"
            f"  Full log: evidence/{run_id}.jsonl",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    except MaxStepsError as exc:
        print(
            f"\nDiscovery stopped: the step budget of {args.max_steps} ran out before "
            f"the goal was done.\n  {exc}\n"
            f"  Nothing was saved. Re-run with a larger --max-steps, or a narrower goal.\n"
            f"  Full log: evidence/{run_id}.jsonl",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    finally:
        session.stop()

    _print_artifact(artifact, save_artifact(artifact), run_id, model)
    return EXIT_OK


def cmd_replay(args) -> int:
    """Replay a saved artifact deterministically. No model is loaded on this path."""
    path = Path(args.artifact)
    if not path.is_file():
        print(f"cli.py replay: no artifact file at {str(path)!r}", file=sys.stderr)
        return EXIT_MISUSE

    artifact = load_artifact(path)
    params = dict(args.param)
    run_id = args.run_id or f"replay-{artifact.capability_id}"
    logger = setup_logging(run_id)

    # The engine validates params before it touches the browser. Calling that check
    # here too means a bad invocation costs no browser launch at all -- and it is the
    # engine's own check, imported rather than restated, so the two cannot disagree
    # about what a valid call looks like. The quiet logger keeps the one `replay.param`
    # block in the evidence log coming from the real run rather than from this rehearsal.
    try:
        _check_params(artifact, params, logging.getLogger("interface_ai.cli.preflight"))
    except ValueError as exc:
        logger.error("replay.rejected", extra={"reason": str(exc)})
        print(f"\nReplay not attempted: {exc}", file=sys.stderr)
        print(
            "  Declared inputs: "
            + ", ".join(f"{p.name} ({p.type}{', secret' if p.secret else ''})" for p in artifact.inputs),
            file=sys.stderr,
        )
        return EXIT_FAILURE

    print(f"Replaying {artifact.capability_id} v{artifact.version} ({len(artifact.steps)} steps)")
    print(f"  params {_param_shapes(params)}, log evidence/{run_id}.jsonl")

    session = BrowserSession(load_allowlist(), logger, headless=not args.headed)
    session.start()
    try:
        result = replay(artifact, params, session=session, logger=logger)
    finally:
        session.stop()

    _print_result(result, run_id, params)
    return EXIT_FAILURE if result.outcome is Outcome.HARD_FAILURE else EXIT_OK


def cmd_handoff_demo(args) -> int:
    """Reproduce the Phase 6 escalation end-to-end against one live session.

    The point of the exercise is that the session is never rebuilt: the intervention is
    raised on the live page, the operator's commands run through that same session, and
    automation carries on from wherever the operator left it. One of the canned commands
    is deliberately out of bounds, to show that a human is no more exempt from the
    allowlist than the agent is.
    """
    run_id = args.run_id or "handoff-demo"
    logger = setup_logging(run_id)

    print(f"Handoff demo against {DEMO_TARGET} (log evidence/{run_id}.jsonl)")

    session = BrowserSession(load_allowlist(), logger, headless=not args.headed)
    session.start()
    try:
        opened = session.act({"action": "navigate", "url": DEMO_TARGET})
        if not opened.ok:
            print(f"\nCould not open {DEMO_TARGET}: {opened.detail}", file=sys.stderr)
            return EXIT_FAILURE

        request = raise_intervention(
            goal=DEMO_GOAL,
            capability_id=DEMO_CAPABILITY_ID,
            step_index=1,
            reason=DEMO_REASON,
            session=session,
            logger=logger,
        )

        # What the mock operator console sends back. The credentials default to the
        # sandbox's published demo login and are overridable on the command line, so the
        # demo survives them changing. They are filled through the session like any other
        # operator command, so only their length reaches the log.
        commands = [
            {"kind": "note", "text": "Using the shared read-only demo login for this account."},
            {"kind": "fill", "locator": _textbox(1), "value": args.username},
            {"kind": "fill", "locator": _textbox(2), "value": args.password},
            # Out of bounds on purpose: the allowlist refuses it and the handoff carries on.
            {"kind": "navigate", "url": "https://www.chase.com/"},
            {
                "kind": "click",
                "locator": {
                    "primary": {"strategy": "role", "role": "button", "name": "Login", "exact": True},
                    "fallbacks": [{"strategy": "text", "name": "Login", "exact": True}],
                },
            },
            {"kind": "resume"},
            # Queued after the operator said they were done; must be ignored, not run.
            {"kind": "fill", "locator": _textbox(1), "value": "ignored-after-resume"},
        ]

        handoff = apply_operator_commands(
            request, commands, session=session, logger=logger, operator="dana.ops"
        )
        resumed_on = session.url
    finally:
        session.stop()

    _print_handoff(handoff, run_id)
    print(f"  Automation would continue from: {resumed_on}")
    return EXIT_OK if handoff.resumed else EXIT_FAILURE


# -- argument parsing ----------------------------------------------------------------


def _param(text: str) -> tuple:
    """Parse one ``--param name=value``. Anything else is misuse and exits 2."""
    name, separator, value = text.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(
            f"expected name=value, got {text!r}  (for example: --param member_id=800000)"
        )
    return name.strip(), value


def _add_common(parser) -> None:
    """The options every command shares: where the log goes, and whether to watch."""
    parser.add_argument(
        "--run-id",
        help="names the structured log written to evidence/<run-id>.jsonl "
        "(default: derived from the command and the capability it is acting on)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="run the browser visibly instead of headless",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Turn one LLM-driven walkthrough of a legacy UI into a capability, "
        "then replay that capability deterministically with no model in the loop.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    discover_parser = subcommands.add_parser(
        "discover",
        help="drive a goal with the model and save the resulting capability artifact",
        description="Drive a natural-language goal against a live target with the model in "
        "the loop, then compile the run into a versioned capability artifact under "
        "artifacts/. Needs a browser and a model backend.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    discover_parser.add_argument("--goal", required=True, help="what to accomplish, in plain English")
    discover_parser.add_argument("--target", required=True, help="URL to start from; must be on the allowlist")
    discover_parser.add_argument(
        "--param",
        action="append",
        default=[],
        type=_param,
        metavar="NAME=VALUE",
        help="a value this run should use, repeatable. These become the capability's "
        "declared inputs; values are never printed or logged, only their names and lengths",
    )
    discover_parser.add_argument("--capability-id", help="name for the capability (default: derived from the goal)")
    discover_parser.add_argument(
        "--backend",
        choices=("auto", "anthropic", "ollama"),
        default="auto",
        help="which model backend to use; auto picks Anthropic when ANTHROPIC_API_KEY "
        "is set and a local Ollama server otherwise (default: auto)",
    )
    discover_parser.add_argument("--model", help="model name (default: the backend's own default)")
    discover_parser.add_argument(
        "--max-steps", type=int, default=25, help="step budget before the run gives up (default: 25)"
    )
    _add_common(discover_parser)
    discover_parser.set_defaults(handler=cmd_discover)

    replay_parser = subcommands.add_parser(
        "replay",
        help="execute a saved artifact deterministically, with no model call",
        description="Execute a saved capability artifact step by step using its "
        "accessibility-tree locators, assert its checkpoint, and return its declared "
        "outputs. No model is loaded on this path.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    replay_parser.add_argument("artifact", metavar="ARTIFACT_PATH", help="path to a saved artifact JSON")
    replay_parser.add_argument(
        "--param",
        action="append",
        default=[],
        type=_param,
        metavar="NAME=VALUE",
        help="a value for one of the capability's declared inputs, repeatable. Values are "
        "never printed or logged, only their names and lengths",
    )
    _add_common(replay_parser)
    replay_parser.set_defaults(handler=cmd_replay)

    handoff_parser = subcommands.add_parser(
        "handoff-demo",
        help="reproduce the human escalation and handoff end-to-end",
        description="Raise an intervention against a live session, apply a canned list of "
        "operator commands -- including one the allowlist must refuse -- resume automation "
        "on the same session, and report what the operator did.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    handoff_parser.add_argument(
        "--username",
        default="jsmith",
        help="username the operator types during the handoff (default: the sandbox's published demo login)",
    )
    handoff_parser.add_argument(
        "--password",
        default="demo1234",
        help="password the operator types during the handoff (default: the sandbox's published demo login)",
    )
    _add_common(handoff_parser)
    handoff_parser.set_defaults(handler=cmd_handoff_demo)

    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
