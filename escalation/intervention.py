"""Human-in-the-loop escalation: raise an intervention, hand the live session over, take it back.

When the discovery loop cannot proceed it raises ``agent.discover.StuckError``. This module
turns that into an ``InterventionRequest`` -- a JSON document carrying everything a human
needs to decide what to do (the goal, which step stopped, the page as it stands right now,
and a screenshot) -- and then executes the commands that human sends back.

Three properties are the whole point of the module, and each one is load-bearing:

*The session is never rebuilt.* ``apply_operator_commands`` acts through the same
``BrowserSession`` object that got stuck. There is no re-navigation to "restore" state and
no second browser: the cookies, the form the agent half-filled, the frame that is already
open are all still there, which is exactly what makes this a handoff rather than a restart.
Automation resumes from wherever the human left the page.

*The human is not exempt from the guardrails.* Every operator command goes through
``BrowserSession.act``, so the allowlist checks a human's click the same way it checks the
agent's. A blocked command is recorded as a failed ``OperatorAction`` and the handoff
carries on -- the operator gets told "not allowed" instead of watching the session die.

*Every human action is logged.* Each command produces one log line carrying the request_id,
so a reviewer can pull the whole handoff out of the run log by that one field. Phases.md
Phase 6 requires "the human's actions logged"; this is where that happens.

Assumptions, stated because they are not pinned down by the phase brief:

- ``param_names`` is derived from the input controls visible on the stuck page. The stuck
  state itself carries no parameter list, and the page's own fields are the honest answer to
  "what will the operator be asked to type". Names only, never values -- see ``_param_names``.
- ``apply_operator_commands`` takes an optional ``operator`` keyword so the console can say
  who took the session. It defaults, so the contract's call shape still works untouched.
- The mock operator console renders ``<evidence_dir>/interventions/<request_id>.json``. The
  ``Handoff`` is returned in-process rather than written to disk: the log already carries
  every operator action, and a second on-disk copy would be one more thing to keep in sync.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import blake2s
from pathlib import Path

from agent.browser import BrowserSession
from agent.discover import StuckError, _loggable
from guardrails.allowlist import AllowlistViolation

# `_loggable` is imported rather than re-implemented. It exists in discover.py for exactly
# the reason it is needed here -- a filled value may be a password, and a bare password has
# no shape any redaction pattern can match, so it has to be kept out of the log at the point
# of writing. One copy means the two paths cannot drift apart later; a private name is a
# smaller price than two half-synchronised redaction rules.

# Roles that represent something a human types or picks. Used only to collect field *names*
# for the console, so the operator knows what the page is asking for before taking over.
_INPUT_ROLES = frozenset(
    {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "spinbutton", "slider"}
)

# Which command keys become part of the browser action, per command kind. A kind that is not
# in here is not something the operator may ask the session to do.
_COMMAND_KEYS = {
    "navigate": ("url",),
    "click": ("ref", "locator"),
    "fill": ("ref", "locator", "value"),
    "select": ("ref", "locator", "value"),
}

# Keys a command of that kind cannot do without. `click` needs one of ref/locator, which is
# checked separately because it is an either-or rather than a required field.
_REQUIRED_KEYS = {"navigate": ("url",), "fill": ("value",), "select": ("value",)}

# The two commands that end a handoff, and the outcome each one means.
_TERMINATORS = {"resume": "resumed", "abort": "abandoned"}

DIGEST_CHARS = 400


@dataclass(frozen=True)
class InterventionRequest:
    """Everything a human needs to take over, and nothing they must not see.

    Frozen because it is evidence: once written to disk it describes the state at the
    moment automation stopped, and nothing downstream should be able to edit that.
    """

    request_id: str
    raised_at: datetime
    goal: str
    capability_id: str | None
    step_index: int
    reason: str
    url: str
    page_title: str
    observation_digest: str
    screenshot: str
    param_names: list[str]

    def to_dict(self) -> dict:
        """JSON-ready form, with the timestamp as ISO 8601.

        This is the exact shape the operator console renders -- no post-processing, no
        nested envelope, no fields that need resolving against something else on disk.
        """
        return {
            "request_id": self.request_id,
            "raised_at": self.raised_at.isoformat(),
            "goal": self.goal,
            "capability_id": self.capability_id,
            "step_index": self.step_index,
            "reason": self.reason,
            "url": self.url,
            "page_title": self.page_title,
            "observation_digest": self.observation_digest,
            "screenshot": self.screenshot,
            "param_names": list(self.param_names),
        }


@dataclass
class OperatorAction:
    """One thing the human did, and whether the session accepted it."""

    kind: str
    detail: str
    ok: bool
    at: datetime


@dataclass
class Handoff:
    """The record of one control transfer: what was asked for, what was done, how it ended.

    ``outcome`` is either "resumed" -- automation continues from the page as the operator
    left it -- or "abandoned", meaning it does not. There is no third value and no default
    that could be mistaken for either: a command list that never terminates is "abandoned",
    because nobody said the session was fit to continue.
    """

    request: InterventionRequest
    actions: list[OperatorAction] = field(default_factory=list)
    outcome: str = "abandoned"
    operator: str = "unknown"

    @property
    def resumed(self) -> bool:
        """True only when the operator explicitly handed control back."""
        return self.outcome == "resumed"


def _request_id(raised_at: datetime, parts: tuple) -> str:
    """A short id: the date plus a hash of the stuck context.

    Deterministic rather than random, for the same reason replay/engine.py derives its
    evidence filenames from the capability and step: the same stuck state raised twice is
    the same request, and should overwrite its own evidence instead of littering the
    directory with near-identical files nobody can tell apart.
    """
    digest = blake2s("|".join(str(p) for p in parts).encode("utf-8"), digest_size=2).hexdigest()
    return f"iv-{raised_at:%Y%m%d}-{digest}"


def _param_names(observation) -> list[str]:
    """Names of the input controls on the page, deduplicated, order preserved.

    Names only. A value on this page may be a member id or a password, and this document is
    written to disk and rendered in a browser, so values never enter it at all.
    """
    names = []
    for node in observation.nodes:
        name = (node.get("name") or "").strip()
        if node.get("role") in _INPUT_ROLES and name and name not in names:
            names.append(name)
    return names


def _capture_screenshot(session: BrowserSession, logger, path: Path, request_id: str) -> str:
    """Screenshot the stuck page, or leave a note at the same stem saying why not.

    Every exception is caught, deliberately, and for the same reason replay/engine.py does
    it: this runs on a path that is already failing. If the session is too broken to
    screenshot, the operator still needs the request -- losing the picture is survivable,
    losing the handoff is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        session.screenshot(str(path))
        return str(path)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        note = path.with_suffix(".txt")
        note.write_text(
            f"screenshot unavailable for {request_id}: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        logger.warning(
            "escalation.screenshot_unavailable",
            extra={"request_id": request_id, "note": str(note), "reason": str(exc)},
        )
        return str(note)


def raise_intervention(
    *,
    goal: str,
    capability_id: str | None,
    step_index: int,
    reason: str,
    session: BrowserSession,
    logger,
    evidence_dir: str = "evidence",
) -> InterventionRequest:
    """Freeze the current state of the live session into an intervention request.

    Observes the session as it stands now (rather than trusting anything the caller passes
    about the page), writes the screenshot and the JSON under
    ``<evidence_dir>/interventions/``, logs the request, and returns it.
    """
    raised_at = datetime.now(timezone.utc)
    observation = session.observe()
    request_id = _request_id(raised_at, (goal, capability_id, step_index, reason, observation.url))

    out_dir = Path(evidence_dir) / "interventions"
    screenshot = _capture_screenshot(
        session, logger, out_dir / f"{request_id}.png", request_id
    )

    request = InterventionRequest(
        request_id=request_id,
        raised_at=raised_at,
        goal=goal,
        capability_id=capability_id,
        step_index=step_index,
        reason=reason,
        url=observation.url,
        page_title=observation.title,
        observation_digest=observation.text_digest[:DIGEST_CHARS],
        screenshot=screenshot,
        param_names=_param_names(observation),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{request_id}.json"
    path.write_text(json.dumps(request.to_dict(), indent=2) + "\n", encoding="utf-8")

    logger.warning(
        "escalation.raised",
        extra={
            "request_id": request_id,
            "goal": goal,
            "capability_id": capability_id,
            "step_index": step_index,
            "reason": reason,
            "url": request.url,
            "page_title": request.page_title,
            "param_names": request.param_names,
            "screenshot": screenshot,
            "request_path": str(path),
        },
    )
    return request


def from_stuck(
    exc: StuckError,
    *,
    session: BrowserSession,
    logger,
    capability_id: str | None = None,
    evidence_dir: str = "evidence",
) -> InterventionRequest:
    """Build an intervention request straight from a StuckError.

    StuckError already carries the whole intervention payload -- goal, target, step index,
    url and reason -- so this only unpacks it. The URL on the request comes from observing
    the live session rather than from ``exc.url``: the operator is about to act on the page
    as it is now, and the two are logged together so a drift between them is visible.
    """
    logger.info(
        "escalation.from_stuck",
        extra={
            "goal": exc.goal,
            "target": exc.target,
            "step_index": exc.step_index,
            "stuck_url": exc.url,
            "reason": exc.reason,
        },
    )
    return raise_intervention(
        goal=exc.goal,
        capability_id=capability_id,
        step_index=exc.step_index,
        reason=exc.reason,
        session=session,
        logger=logger,
        evidence_dir=evidence_dir,
    )


def _validate(command: dict) -> str | None:
    """Return a complaint if the console sent something the session cannot execute.

    Commands cross a trust boundary -- they arrive from a UI, not from this codebase -- so
    they are checked before they reach the browser rather than after.
    """
    kind = command.get("kind")
    if kind not in _COMMAND_KEYS:
        return f"unsupported operator command kind {kind!r}"
    missing = [k for k in _REQUIRED_KEYS.get(kind, ()) if k not in command]
    if missing:
        return f"{kind} command is missing {', '.join(missing)}"
    if kind != "navigate" and not ("ref" in command or "locator" in command):
        return f"{kind} command needs a 'ref' or a 'locator'"
    return None


def apply_operator_commands(
    request: InterventionRequest,
    commands: list[dict],
    *,
    session: BrowserSession,
    logger,
    operator: str = "unknown",
) -> Handoff:
    """Execute the operator's commands against the same live session, and log each one.

    Stops at the first ``resume`` or ``abort``; anything after the terminator is ignored and
    logged as ignored, so a console that queues commands optimistically cannot sneak an
    action in after the human said they were done.

    A command the allowlist refuses becomes an ``OperatorAction`` with ``ok=False`` and the
    handoff continues to the next command. A human driving the session is still bound by the
    guardrail, and telling them "not allowed" is more useful than crashing the handoff.
    """
    handoff = Handoff(request=request, operator=operator)
    logger.info(
        "escalation.handoff_start",
        extra={
            "request_id": request.request_id,
            "operator": operator,
            "command_count": len(commands),
        },
    )

    terminated = False
    for command in commands:
        kind = command.get("kind")

        if terminated:
            logger.info(
                "escalation.command_ignored",
                extra={
                    "request_id": request.request_id,
                    "operator": operator,
                    "kind": kind,
                    "reason": f"handoff already ended as {handoff.outcome!r}",
                },
            )
            continue

        if kind in _TERMINATORS:
            handoff.outcome = _TERMINATORS[kind]
            detail = (
                "operator returned control to automation; it continues from this page"
                if kind == "resume"
                else "operator abandoned the run; automation does not continue"
            )
            handoff.actions.append(_record(handoff, logger, kind, detail, True, command))
            terminated = True
            continue

        if kind == "note":
            text = str(command.get("text", "")).strip()
            handoff.actions.append(
                _record(handoff, logger, "note", f"operator note: {text}", True, command)
            )
            continue

        complaint = _validate(command)
        if complaint:
            handoff.actions.append(_record(handoff, logger, str(kind), complaint, False, command))
            continue

        action = {"action": kind, **{k: command[k] for k in _COMMAND_KEYS[kind] if k in command}}
        try:
            # The same session object the agent got stuck in -- no new browser, no reload.
            result = session.act(action)
        except AllowlistViolation as violation:
            logger.warning(
                "escalation.blocked",
                extra={
                    "request_id": request.request_id,
                    "operator": operator,
                    "kind": kind,
                    "reason": str(violation),
                },
            )
            handoff.actions.append(
                _record(
                    handoff,
                    logger,
                    kind,
                    f"blocked by allowlist, not executed: {violation}",
                    False,
                    command,
                )
            )
            continue

        handoff.actions.append(_record(handoff, logger, kind, result.detail, result.ok, command))

    if not terminated:
        # No resume, no abort. "Abandoned" is the only honest reading: nobody said the
        # session was in a state automation should continue from.
        logger.warning(
            "escalation.handoff_unterminated",
            extra={
                "request_id": request.request_id,
                "operator": operator,
                "outcome": handoff.outcome,
            },
        )

    logger.info(
        "escalation.handoff_complete",
        extra={
            "request_id": request.request_id,
            "operator": operator,
            "outcome": handoff.outcome,
            "resumed": handoff.resumed,
            "action_count": len(handoff.actions),
            "failed_count": sum(1 for a in handoff.actions if not a.ok),
        },
    )
    return handoff


def _record(
    handoff: Handoff, logger, kind: str, detail: str, ok: bool, command: dict
) -> OperatorAction:
    """Build the OperatorAction and log it, so the two can never disagree.

    The command is logged through ``_loggable``, which replaces a filled value with its
    length. That is the only reason a password typed by a human during a handoff does not
    end up in the evidence log.
    """
    action = OperatorAction(kind=kind, detail=detail, ok=ok, at=datetime.now(timezone.utc))
    logger.info(
        "escalation.operator_action",
        extra={
            "request_id": handoff.request.request_id,
            "operator": handoff.operator,
            "kind": kind,
            "ok": ok,
            "detail": detail,
            "command": _loggable(dict(command)),
        },
    )
    return action
