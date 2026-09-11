"""Deterministic replay: execute a saved capability with no model in the loop.

This is the half of the system the PRD actually sells. Discovery is a one-time,
expensive, LLM-driven walkthrough; replay is what an agent invokes afterwards,
thousands of times, as a typed function call. So the defining property of this
module is what it does *not* contain: no Anthropic client, no prompt, no
sampling, no clock-dependent branching, no randomness. Same artifact, same
params, same page means the same result, every time.

What replay does, in order
--------------------------
1. Load and fully validate the artifact (``artifact.schema`` raises on anything
   inconsistent, so a broken artifact fails before the browser is touched).
2. Check the supplied params against the declared inputs -- **before any
   browser action**. A capability that half-executes and then discovers an
   input is missing has already clicked buttons in a real back-office UI.
3. Walk the steps in order, substituting ``{{param}}`` templates, executing
   each through ``BrowserSession.act``.
4. Assert the artifact's checkpoint. Rules.md: never proceed past an unverified
   checkpoint.
5. Read the declared outputs, typed per the artifact, and return them.

Division of labour
------------------
Three things this module deliberately does not own:

* **Locator resolution and fallback.** ``BrowserSession._resolve_locator``
  already walks primary then each fallback in order and reports which one
  matched. Replay hands it the whole ``Locator`` and reads the answer back out
  of ``ActResult.detail``; re-implementing the walk here would put a second,
  divergent copy of the strategy in the tree.
* **The allowlist.** Every action, including the reads done for the checkpoint
  and the outputs, goes through ``session.act``, which checks the guardrail
  before it touches the page. ``AllowlistViolation`` is the one exception this
  module lets propagate untouched: a guardrail a caller can catch as a generic
  step failure is not a guardrail.
* **Classification.** ``replay.outcomes.classify`` owns the mapping from "what
  happened" to business / recoverable / hard-failure. This module reports the
  ``Condition`` it observed and takes the verdict it is given. It cannot
  manufacture a business outcome: ``classify`` independently re-checks for an
  affirmative signal before it will call anything a domain answer.

Never refs, always locators
---------------------------
``BrowserSession.act`` accepts either a ``ref`` from the latest observation or
a durable ``Locator``. Replay uses only the latter. Refs are numbered per
snapshot and were measured re-numbering across two snapshots of an identical,
unchanged page; an artifact recorded on Tuesday would drive the wrong element
on Wednesday. The artifact's role/name locators with their fallback chain are
the whole durability story, so a ref appearing anywhere in this file would be a
bug, not a shortcut.

Assumptions made explicit
-------------------------
* A step whose value references an **optional** param the caller omitted is
  skipped entirely, rather than filled with an empty string. That is what
  "omit to leave the form's own default in place" means in the example
  artifact, and blanking a pre-selected dropdown is a different action from not
  touching it.
* ``checkpoint.kind == "url_matches"`` is a case-sensitive **substring** test,
  not a regex. An artifact is data compiled from a model's transcript; letting
  it carry an expression language into the assertion step is a bigger surface
  than the feature is worth.
* ``checkpoint.kind == "a11y_node_present"`` passes when the checkpoint's
  locator resolves. The locator is the identity of the node; ``expected`` is
  carried through to the result as the human-readable statement of what was
  required.
* Outputs are read from the value their ``from_step`` read step already
  captured, rather than re-read through the ``OutputField``'s own locator. The
  schema guarantees ``from_step`` is a ``read``, so the value is in hand; going
  back to the page would be a second round-trip that can disagree with the
  first, and "the output is whatever the step read" is the more defensible
  story.
* A missing or mistyped param raises ``ValueError`` rather than returning a
  ``ReplayResult``. The taxonomy describes the three ways a *replay* can end;
  being invoked with the wrong arguments is a caller bug that happens before
  the replay starts, and Rules.md says fail loud everywhere outside those
  three.
"""

from pathlib import Path

from artifact.schema import CapabilityArtifact, load_artifact

# The one regex that defines what a parameter reference looks like. Imported
# rather than re-spelled: if the schema's idea of a template and replay's idea
# of a template ever drift apart, the failure mode is a wrong value typed into
# a live back-office form.
from artifact.schema import _TEMPLATE as TEMPLATE
from replay.outcomes import (
    Condition,
    Outcome,
    ReplayHardFailure,
    ReplayResult,
    business_signal,
    classify,
    success,
)

__all__ = ["replay"]

# What a browser-layer failure detail means, in the taxonomy's vocabulary.
# Matched as substrings of ``ActResult.detail``, first hit wins. Anything that
# matches nothing here is an unexpected state, which is a hard failure --
# unknown is loud.
_FAILURE_CONDITIONS = (
    ("no locator matched", Condition.LOCATOR_NOT_FOUND),
    ("StaleRefError", Condition.STALE_ELEMENT),
    ("TimeoutError", Condition.TIMEOUT),
)

# Which Python types a declared input will accept. ``bool`` is checked before
# ``number`` everywhere below because in Python a bool *is* an int, and a caller
# passing True for a numeric field is a mistake worth naming.
_ACCEPTED = {"string": str, "number": (int, float), "bool": bool}

_TRUE = frozenset({"true", "yes", "y", "1"})
_FALSE = frozenset({"false", "no", "n", "0"})


def replay(
    artifact_path_or_obj,
    params: dict,
    *,
    session,
    logger,
    evidence_dir: str = "evidence",
) -> ReplayResult:
    """Execute a capability artifact deterministically and return a typed result.

    Args:
        artifact_path_or_obj: a path to a saved artifact JSON, or an already
            loaded ``CapabilityArtifact``.
        params: values for the artifact's declared inputs, by name.
        session: a started ``BrowserSession``.
        logger: a logger from ``guardrails.logging_setup.setup_logging``.
        evidence_dir: where a failure screenshot is written.

    Returns a ``ReplayResult``: a business outcome (success, or a domain answer
    such as "no such member") or a hard failure carrying step index, expected
    vs. observed state and an evidence pointer. Raises ``ValueError`` for bad
    params, and lets ``AllowlistViolation`` propagate.
    """
    artifact = (
        artifact_path_or_obj
        if isinstance(artifact_path_or_obj, CapabilityArtifact)
        else load_artifact(artifact_path_or_obj)
    )

    # -- pre-flight: everything that can be known without touching the browser -
    _check_params(artifact, params, logger)
    _check_checkpoint_shape(artifact)

    logger.info(
        "replay.start",
        extra={
            "capability_id": artifact.capability_id,
            "capability_version": artifact.version,
            "schema_version": artifact.schema_version,
            "step_count": len(artifact.steps),
            "target": artifact.target,
        },
    )

    state = _RunState(artifact, session, logger, Path(evidence_dir))
    try:
        return _run(state, params)
    except ReplayHardFailure as exc:
        # The engine's own abort channel, per replay.outcomes. Nothing in this
        # file raises it today, but a helper that grows one gets honoured here
        # rather than escaping to the caller as an unclassified crash.
        _log_result(logger, exc.result)
        return exc.result


class _RunState:
    """The handful of things every stage of one replay needs to reach.

    A small object rather than six positional arguments threaded through five
    helpers. ``retries`` is the single list ``classify`` reads and appends to,
    so the retry bound is counted across the whole run rather than per call.
    """

    __slots__ = ("artifact", "session", "logger", "evidence_dir", "retries", "observation", "drift")

    def __init__(self, artifact, session, logger, evidence_dir: Path):
        self.artifact = artifact
        self.session = session
        self.logger = logger
        self.evidence_dir = evidence_dir
        self.retries: list[dict] = []
        self.observation = None  # the most recent post-action Observation
        self.drift: list[str] = []  # steps that only matched via a fallback


def _renavigate_and_retry(state: _RunState, step, prior, params: dict):
    """Re-run the click before this step, then try this step once more.

    A click that starts a navigation can silently do nothing -- the element is
    found and clicked, the click reports success, and the page never moves. The
    next step then looks for something that only exists on the page we should
    have arrived at, fails to find it, and the run stops on a locator error that
    describes a symptom rather than the cause.

    This is the RECOVERABLE shape the taxonomy already names: transient, worth a
    bounded retry, and logged rather than silently absorbed. The bound is the
    shared retry ledger, so this cannot loop -- ``classify`` stops granting
    retries for a (step, condition) pair once the budget is spent.

    Returns the successful ActResult for ``step``, or None to let the original
    verdict stand.
    """
    verdict = classify(
        Condition.SLOW_LOAD,
        step_index=step.index,
        expected=step.description,
        observed=f"still on {state.session.url!r}; the click at step {prior.index} did not navigate",
        capability_id=state.artifact.capability_id,
        retries=state.retries,
    )
    if verdict.outcome is not Outcome.RECOVERABLE:
        return None

    state.logger.warning(
        "replay.renavigating",
        extra={
            "step_index": step.index,
            "reason": f"step {prior.index} clicked but the page did not move",
            "url": state.session.url,
        },
    )

    repeat = state.session.act(_action_for(prior, params))
    if not repeat.ok:
        return None
    if repeat.observation is not None:
        state.observation = repeat.observation

    retry = state.session.act(_action_for(step, params))
    if retry.observation is not None:
        state.observation = retry.observation
    return retry if retry.ok else None


def _run(state: _RunState, params: dict) -> ReplayResult:
    """The linear body: steps, then checkpoint, then outputs."""
    reads: dict[int, str] = {}

    previous = None

    for step in state.artifact.steps:
        skipped = _skip_reason(step, params)
        if skipped:
            state.logger.info(
                "replay.step_skipped",
                extra={"step_index": step.index, "action": step.action, "reason": skipped},
            )
            continue

        url_before = state.session.url
        result, verdict = _execute(state, step, params)

        # A locator that is missing right after a click which left the page where
        # it was usually means the click did not take, not that the element is
        # gone. Re-run that click once and try again before giving up.
        if (
            verdict is not None
            and verdict.outcome is Outcome.HARD_FAILURE
            and "no locator matched" in (verdict.observed or "")
            and previous is not None
            and previous.action == "click"
            and state.session.url == url_before
        ):
            recovered = _renavigate_and_retry(state, step, previous, params)
            if recovered is not None:
                result, verdict = recovered, None

        if verdict is not None:
            return _finish(state, verdict)
        previous = step
        if step.action == "read":
            # For a read, ActResult.detail *is* the text that was read.
            reads[step.index] = result.detail

    verdict = _assert_checkpoint(state)
    if verdict is not None:
        return _finish(state, verdict)

    outputs, verdict = _collect_outputs(state, reads)
    if verdict is not None:
        return _finish(state, verdict)

    final = success(state.artifact.capability_id, outputs)
    final.retries = list(state.retries)
    if state.drift:
        # Phase 4's value is partly in knowing the primary locator drifted: the
        # capability still works, but its artifact wants re-recording before the
        # last fallback goes too. Surfaced on the happy path, not just in logs.
        final.detail += " Locator drift: " + "; ".join(state.drift) + "."
    return _finish(state, final)


# -- pre-flight ---------------------------------------------------------------


def _check_params(artifact, params: dict, logger) -> None:
    """Validate the caller's params against the declared inputs, or raise.

    Runs to completion and reports every problem at once: being told about one
    misspelled param, fixing it, and then being told about the next is a worse
    experience than being handed the whole list.
    """
    declared = {p.name: p for p in artifact.inputs}
    problems = []

    unknown = sorted(set(params) - declared.keys())
    if unknown:
        problems.append(
            f"unknown param(s) {unknown}; this capability declares {sorted(declared)}"
        )

    for name, spec in declared.items():
        value = params.get(name)
        if value is None:
            if spec.required:
                problems.append(f"required param {name!r} is missing")
            continue
        accepted = _ACCEPTED[spec.type]
        # bool is a subclass of int, so a bare isinstance would let True through
        # as a number and 1 through as a bool.
        ok = isinstance(value, bool) if spec.type == "bool" else (
            isinstance(value, accepted) and not isinstance(value, bool)
        )
        if not ok:
            problems.append(
                f"param {name!r} is declared {spec.type!r} but got "
                f"{type(value).__name__}"
            )

    if problems:
        raise ValueError(
            f"cannot replay {artifact.capability_id!r}: " + "; ".join(problems)
        )

    # Log the shape of every param, never a value. A secret param must never be
    # written to a log, and the only way to be sure of that is not to log any of
    # them -- a non-secret member id is PII too.
    for name, spec in declared.items():
        value = params.get(name)
        logger.info(
            "replay.param",
            extra={
                "param": name,
                "type": spec.type,
                "secret": spec.secret,
                "present": value is not None,
                "value_len": len(str(value)) if value is not None else 0,
            },
        )


def _check_checkpoint_shape(artifact) -> None:
    """The one checkpoint invariant the schema cannot express on its own."""
    checkpoint = artifact.checkpoint
    if checkpoint.kind == "a11y_node_present" and checkpoint.locator is None:
        raise ValueError(
            f"artifact {artifact.capability_id!r} has an 'a11y_node_present' "
            f"checkpoint with no locator; there is no node to look for"
        )


# -- steps --------------------------------------------------------------------


def _skip_reason(step, params: dict) -> str | None:
    """Why this step is not run, or None to run it.

    The only reason today: the step's value references an optional param the
    caller omitted. Required params are guaranteed present by ``_check_params``,
    so anything absent here is optional by construction.
    """
    if step.value is None:
        return None
    absent = sorted(n for n in TEMPLATE.findall(step.value) if params.get(n) is None)
    if absent:
        return f"optional param(s) {absent} not supplied"
    return None


def _substitute(value: str, params: dict) -> str:
    """Replace every ``{{name}}`` with its param value, as a string."""
    return TEMPLATE.sub(lambda m: _as_text(params[m.group(1)]), value)


def _as_text(value) -> str:
    """One fixed spelling per type, so the same params always type the same text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _action_for(step, params: dict) -> dict:
    """Turn a Step into the action dict BrowserSession.act consumes."""
    action = {"action": step.action}
    if step.action == "navigate":
        action["url"] = step.url
    else:
        # The whole Locator, primary plus fallbacks: the browser layer owns the
        # walk. Never a ref.
        action["locator"] = step.locator.model_dump(mode="json")
    if step.value is not None:
        action["value"] = _substitute(step.value, params)
    return action


def _execute(state: _RunState, step, params: dict):
    """Run one step, retrying only while ``classify`` says the condition is recoverable.

    Returns ``(ActResult, None)`` on success, or ``(None, ReplayResult)`` with
    the terminal verdict. The retry bound is not enforced here on purpose --
    ``classify`` counts it against the shared retry log and stops granting
    retries when it is spent, so adding a newly-recoverable condition to the
    taxonomy needs no change in this file.
    """
    action = _action_for(step, params)
    state.logger.info(
        "replay.step",
        extra={
            "step_index": step.index,
            "action": step.action,
            "description": step.description,
            "has_value": step.value is not None,
        },
    )

    while True:
        result = state.session.act(action)
        if result.observation is not None:
            state.observation = result.observation

        if result.ok:
            matched = _matched_locator(step, result.detail)
            if matched and not matched.startswith("primary"):
                state.drift.append(f"step {step.index} matched {matched}")
            state.logger.info(
                "replay.step_result",
                extra={
                    "step_index": step.index,
                    "action": step.action,
                    "ok": True,
                    "locator_match": matched,
                },
            )
            return result, None

        condition = _condition_for(result.detail, state.observation)
        verdict = classify(
            condition,
            step_index=step.index,
            expected=step.description,
            observed=_observed(state, result.detail),
            evidence=_capture_evidence(state, step.index),
            capability_id=state.artifact.capability_id,
            retries=state.retries,
        )
        state.logger.info(
            "replay.step_result",
            extra={
                "step_index": step.index,
                "action": step.action,
                "ok": False,
                "condition": condition.value,
                "outcome": verdict.outcome.value,
                "detail": result.detail,
            },
        )
        if verdict.outcome is Outcome.RECOVERABLE:
            continue
        return None, verdict


def _matched_locator(step, detail: str) -> str | None:
    """Which candidate in the locator chain matched, per the browser layer.

    ``_resolve_locator`` reports this as ``"matched primary ..."`` or
    ``"matched fallback[N] ..."`` at the front of the detail string. A ``read``
    is the exception: its detail is the text that was read, so there is nothing
    to parse and nothing to claim.
    """
    if step.action in ("navigate", "read") or not detail.startswith("matched "):
        return None
    return detail[len("matched "):].split(";")[0].strip()


def _condition_for(detail: str, observation) -> Condition:
    """Name what the browser layer reported, in the taxonomy's vocabulary.

    One judgement call lives here. When a step cannot proceed *and* the page is
    affirmatively saying the domain answer is empty -- "no matching records" --
    the truthful report is that the artifact's success condition will not be
    reached, i.e. ``CHECKPOINT_FAILED``, rather than "a locator is missing".
    The distinction matters because a missing row on a no-results page is the
    UI working correctly, not the automation breaking.

    This does not let the engine invent a business outcome: ``classify``
    re-runs the signal check itself and downgrades to a hard failure if the
    marker is not really there. The most this can do is trade a precise
    hard-failure message for a slightly vaguer one.
    """
    text = observation.text_digest if observation is not None else ""
    if business_signal(text):
        return Condition.CHECKPOINT_FAILED
    for marker, condition in _FAILURE_CONDITIONS:
        if marker in detail:
            return condition
    return Condition.UNEXPECTED_STATE


def _observed(state: _RunState, detail: str) -> str:
    """What was actually on the page, for the result and the business-signal scan."""
    page = state.observation.text_digest if state.observation is not None else ""
    return f"{detail} | page text: {page}" if page else detail


# -- checkpoint ---------------------------------------------------------------


def _assert_checkpoint(state: _RunState) -> ReplayResult | None:
    """Assert the artifact's success condition. None means it held.

    Mandatory, and never skipped: Rules.md forbids proceeding past an unverified
    checkpoint, so a capability that ran every step but cannot prove it landed
    somewhere expected does not get to return outputs.
    """
    checkpoint = state.artifact.checkpoint
    step_index = len(state.artifact.steps)  # one past the last, where this runs

    passed, observed = _read_checkpoint(state, checkpoint)
    state.logger.info(
        "replay.checkpoint",
        extra={
            "kind": checkpoint.kind,
            "expected": checkpoint.expected,
            "passed": passed,
            "step_index": step_index,
        },
    )
    if passed:
        return None

    return classify(
        Condition.CHECKPOINT_FAILED,
        step_index=step_index,
        expected=f"{checkpoint.kind}: {checkpoint.expected}",
        observed=observed,
        evidence=_capture_evidence(state, step_index),
        capability_id=state.artifact.capability_id,
        retries=state.retries,
    )


def _read_checkpoint(state: _RunState, checkpoint) -> tuple[bool, str]:
    """Return ``(passed, observed)`` for the checkpoint, without classifying it."""
    page = state.observation.text_digest if state.observation is not None else ""

    if checkpoint.kind == "url_matches":
        url = state.session.url
        return checkpoint.expected in url, f"url: {url} | page text: {page}"

    scoped = None
    if checkpoint.locator is not None:
        result = state.session.act(
            {"action": "read", "locator": checkpoint.locator.model_dump(mode="json")}
        )
        if result.observation is not None:
            state.observation = result.observation
            page = result.observation.text_digest
        scoped = result.detail if result.ok else None
        if not result.ok:
            # The node the checkpoint names is not there at all. That is a
            # failed checkpoint, and the page text still gets to explain why.
            return False, f"checkpoint locator did not resolve: {result.detail} | page text: {page}"

    if checkpoint.kind == "a11y_node_present":
        return True, f"node present | page text: {page}"

    # text_present: inside the checkpoint's locator if it has one, else anywhere
    # on the page.
    haystack = scoped if scoped is not None else page
    return checkpoint.expected in haystack, f"read: {haystack!r} | page text: {page}"


# -- outputs ------------------------------------------------------------------


def _collect_outputs(state: _RunState, reads: dict[int, str]):
    """Build the declared outputs from the read steps. Returns ``(outputs, verdict)``."""
    outputs: dict = {}
    for field in state.artifact.outputs:
        raw = reads.get(field.from_step)
        if raw is None:
            # The schema guarantees from_step is a read step, and reads carry no
            # value so they are never skipped. Reaching here means an invariant
            # broke, which is exactly what a hard failure is for.
            return outputs, _output_failure(
                state, field, f"step {field.from_step} produced no value"
            )
        try:
            outputs[field.name] = _coerce(raw, field.type)
        except ValueError as exc:
            return outputs, _output_failure(state, field, str(exc))

    state.logger.info(
        "replay.outputs",
        extra={
            "names": sorted(outputs),
            # Values can be a member's name or balance. Log the shape only.
            "lengths": {k: len(str(v)) for k, v in outputs.items()},
        },
    )
    return outputs, None


def _output_failure(state: _RunState, field, why: str) -> ReplayResult:
    step_index = field.from_step
    state.logger.info(
        "replay.output_failed", extra={"output": field.name, "step_index": step_index, "reason": why}
    )
    return classify(
        Condition.UNEXPECTED_STATE,
        step_index=step_index,
        expected=f"output {field.name!r} of type {field.type!r}",
        observed=why,
        evidence=_capture_evidence(state, step_index),
        capability_id=state.artifact.capability_id,
        retries=state.retries,
    )


def _coerce(raw: str, declared: str):
    """Type a read value per the artifact's declaration, or raise ValueError.

    No cleverness: no stripping of currency symbols, no thousands separators, no
    locale guessing. An artifact that declares a balance as a ``number`` and
    then reads ``"$1,234.56"`` off the screen has a bug in the *artifact*, and
    quietly repairing it here would hide that from whoever has to trust the
    number. The example artifact declares such fields as ``string`` for exactly
    this reason.
    """
    text = raw.strip()
    if declared == "string":
        return text
    if declared == "number":
        try:
            return float(text) if ("." in text or "e" in text.lower()) else int(text)
        except ValueError:
            raise ValueError(f"read {text!r}, which is not a number") from None
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"read {text!r}, which is not a recognised boolean")


# -- evidence and logging -----------------------------------------------------


def _capture_evidence(state: _RunState, step_index: int) -> str:
    """Screenshot the failure and return the path. Phases.md Phase 5 requires this.

    The filename is derived from the capability and the step, with no timestamp:
    replaying the same failure twice must produce the same artifacts on disk,
    and a timestamp would make every re-run a new file to sift through.

    Every exception is caught, deliberately. Evidence capture runs on a path
    that is already failing; if the session is dead the original failure is the
    interesting one, and it must not be replaced by a screenshot error. When the
    screenshot cannot be taken, a note is written at the same stem instead, so
    the evidence pointer a hard failure requires still points at something real.
    """
    stem = (
        state.evidence_dir
        / f"replay_{state.artifact.capability_id}_v{state.artifact.version}_step{step_index}"
    )
    stem.parent.mkdir(parents=True, exist_ok=True)
    path = stem.with_suffix(".png")
    try:
        state.session.screenshot(str(path))
        return str(path)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        note = stem.with_suffix(".txt")
        note.write_text(
            f"screenshot unavailable at step {step_index}: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        state.logger.warning(
            "replay.evidence_unavailable",
            extra={"step_index": step_index, "note": str(note), "reason": str(exc)},
        )
        return str(note)


def _finish(state: _RunState, result: ReplayResult) -> ReplayResult:
    _log_result(state.logger, result)
    return result


def _log_result(logger, result: ReplayResult) -> None:
    logger.info(
        "replay.result",
        extra={
            "outcome": result.outcome.value,
            "capability_id": result.capability_id,
            "detail": result.detail,
            "step_index": result.step_index,
            "expected": result.expected,
            "observed": result.observed,
            "evidence": result.evidence,
            "retry_count": len(result.retries),
            "output_names": sorted(result.outputs),
        },
    )
