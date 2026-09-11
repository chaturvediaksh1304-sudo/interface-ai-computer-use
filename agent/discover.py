"""The discovery loop, and the compiler that turns one run into a capability.

``discover`` is the observe -> decide -> act loop: it shows the model what the
page looks like, executes the one action it chooses, and records what happened.
``compile_artifact`` is the part that matters -- it turns that recording into a
``CapabilityArtifact`` that can be replayed with no model anywhere in the loop.

Two transformations do all the work in the compiler, and both exist because a
transcript is not a script:

*Concrete values become parameters.* A run that filled "800000" into a member-id
field discovered how to look up **a** member, not how to look up member 800000.
The compiler rewrites such a value to ``"{{member_id}}"`` and declares a
matching ``InputParam``. The detection rule is deliberately not a heuristic over
the value's shape: **a filled value is a parameter exactly when the caller
supplied it as a run input** (``run_inputs``). Everything else the model typed
or selected is a property of the UI -- a dropdown option, a canned search mode --
and stays a literal, because generalising it would invent a parameter the
caller never asked for.

*Refs become locators.* ``session.act`` addresses nodes by ``ref`` ("n12"), which
is a handle into one page snapshot and is meaningless a second later, let alone
next week. An artifact full of refs cannot replay at all. For every step the
compiler therefore keeps the *observed node* behind the ref and emits an
accessibility-first ``Locator`` from its role and accessible name, with ordered
fallbacks -- see ``_locator_for`` for the strategy ladder and why it is ordered
the way it is.

The loop and the compiler are separate functions on purpose: the compiler is
pure over its inputs, takes no session and no client, and can be exercised on a
hand-written transcript.

Transcript entries -- what the loop records and the compiler consumes -- are
dicts with these keys, one per successfully executed action:

    index       position in the run, contiguous from 0; becomes Step.index
    action      the executed action dict, e.g. {"action": "fill", "ref": ..., "value": ...}
    rationale   the model's stated reason, which becomes Step.description
    node        the observed a11y node the ref resolved to, or None for navigate
    name_nth    the node's position among same role+name nodes in that snapshot
    role_nth    the node's position among same-role nodes in that snapshot
    ambiguous   True when role+name did not uniquely identify the node
    param       name of the run input this value came from, or None
    url_before  the page URL observed before the action ran
    after       {"url", "title"} observed after the action ran
"""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from guardrails.allowlist import AllowlistViolation
from artifact.schema import (
    A11yLocator,
    CapabilityArtifact,
    Checkpoint,
    InputParam,
    Locator,
    OutputField,
    Step,
)
from agent.decide import decide

__all__ = [
    "DiscoveryError",
    "MaxStepsError",
    "StuckError",
    "discover",
    "compile_artifact",
]

# Roles whose node is a form control the user types into or picks from. These
# are addressed by their *label* first, because that is the string a form
# actually guarantees; a text input's accessible name comes from the label
# anyway, and label lookup survives the input being swapped for a different
# control type. Everything else -- buttons, links, headings, cells -- is
# addressed by role + accessible name, which is the more precise pair for
# things whose visible text is their identity.
_FORM_ROLES = frozenset(
    {"textbox", "searchbox", "combobox", "listbox", "spinbutton", "checkbox", "radio", "slider"}
)

# Param names that must never have their value written to disk or to a log.
# Matched against the *name* of the run input, not its value: by the time the
# compiler runs, the value has already been replaced by a bare "{{name}}"
# template, so there is nothing value-shaped left to inspect.
_SECRET_NAME_HINTS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "pin", "ssn", "otp", "cvv", "credential",
)


class DiscoveryError(Exception):
    """A discovery run could not be completed. Hard failure; nothing is saved."""


class MaxStepsError(DiscoveryError):
    """The run used its whole step budget without the model declaring the goal done.

    Explicitly not a partial success. A transcript that never reached "done"
    describes a path that was never shown to work, and compiling it would
    produce a capability whose checkpoint asserts something nobody verified.
    """


class StuckError(DiscoveryError):
    """The model reported it cannot proceed without help.

    Carries the full context a human needs to take the session over: the goal,
    the target, which step it stopped on, the URL it stopped on, and the reason
    the model gave. Raised rather than returned so it cannot be mistaken for a
    completed run.
    """

    def __init__(self, goal: str, target: str, step_index: int, url: str, reason: str):
        self.goal = goal
        self.target = target
        self.step_index = step_index
        self.url = url
        self.reason = reason
        super().__init__(
            f"stuck at step {step_index} on {url!r}: {reason} (goal: {goal!r})"
        )


def _slug(text: str, max_words: int = 6) -> str:
    """A lowercase identifier-safe slug, used for capability and output names."""
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:max_words]
    return "_".join(words) or "unnamed"


def _default_capability_id(goal: str, target: str) -> str:
    """``<host label>.<goal slug>``, e.g. ``demo.look_up_a_member_by_member``."""
    host = urlsplit(target).hostname or "unknown"
    return f"{host.split('.')[0]}.{_slug(goal)}"


_VOLATILE = re.compile(
    r"""(?x)
    [$£€]\s?-?[\d,]+(?:\.\d+)?        # a currency amount
    | \b\d{1,2}/\d{1,2}/\d{2,4}\b     # a date
    | \b\d{1,2}:\d{2}(?::\d{2})?\b    # a time
    | \b\d{4,}\b                       # a long digit run: ids, account numbers, balances
    """
)


def _stable_part(name: str) -> str:
    """The leading piece of an accessible name that is not live data.

    A name like ``"Ending balance as of 9/11/26 2:47 PM"`` is part label and
    part timestamp; the timestamp changes on every run, so only the words
    before it can anchor anything. ``"-$2000000.00"`` has no stable part at all
    and yields an empty string.
    """
    match = _VOLATILE.search(name)
    return (name[: match.start()] if match else name).strip(" \t:-\u2013\u2014")


def _name_is_unusable(name: str) -> bool:
    """True when the accessible name is (or starts as) live data.

    Building a locator out of the value you are reading is self-defeating: the
    name is the data, so it changes exactly when the page does. This is the
    check that keeps such a name from becoming a locator.
    """
    stable = _stable_part(name)
    # Any letter at all is enough to anchor on: short labels like "GO" and "OK"
    # are perfectly good names, and a character-count threshold would reject them.
    return not any(c.isalpha() for c in stable)


def _locator_for(entry: dict) -> Locator:
    """Build a durable, accessibility-first locator from one transcript entry.

    This is the transformation that makes an artifact replayable. The ref the
    step was executed with is discarded entirely; what survives is what the
    accessibility tree said about the node -- its role, its accessible name, and
    its position among its peers on that page.

    The ladder, primary first:

    1. **role + name** (or **label** for form controls, see ``_FORM_ROLES``).
       The strongest pair the a11y tree offers, and the one Playwright's
       ``get_by_role(role, name=...)`` / ``get_by_label(...)`` map onto directly.
       Matched exactly, so "Search" does not also match "Search history".
    2. **the other of the two.** A control whose label lookup fails because the
       markup pairs them loosely is usually still reachable by role + name, and
       vice versa; trying both costs nothing at replay time.
    3. **text**. Catches buttons and links whose accessible name comes from
       their text content when the role has drifted (a link restyled as a
       button, say).
    4. **role + nth**. The last resort, and the only one that survives the name
       changing entirely: the same role in the same position on the page. Kept
       last precisely because position is the most brittle thing to depend on --
       but it is strictly better than failing when a label has been reworded.

    When the accessible name did not uniquely identify the node in the snapshot
    the step ran against, ``nth`` is pinned on the primary too. That is a
    correctness requirement, not a nicety: replay must act on the same one of
    three identically-named "View detail" links that discovery acted on.
    """
    node = entry["node"]
    role = (node.get("role") or "").strip()
    name = (node.get("name") or "").strip()

    # An accessible name that is (or begins as) live data cannot anchor a
    # locator: it changes precisely when the value does. Keep only the stable
    # leading words, matched loosely, and fall through to position when there
    # are none -- a positional locator is brittle, but a value-shaped one is
    # guaranteed to miss on the very next run.
    if name and _name_is_unusable(name):
        return Locator(
            primary=A11yLocator(strategy="role", role=role or "generic", nth=entry["role_nth"]),
            fallbacks=[],
        )
    if name and _stable_part(name) != name:
        stable = _stable_part(name)
        return Locator(
            primary=A11yLocator(strategy="role", role=role or "generic",
                                name=stable, exact=False, nth=entry.get("prefix_nth")),
            fallbacks=[
                A11yLocator(strategy="text", name=stable, exact=False,
                            nth=entry.get("prefix_nth")),
                A11yLocator(strategy="role", role=role or "generic", nth=entry["role_nth"]),
            ],
        )

    # nth is only meaningful when the name was not unique on the page.
    name_nth = entry["name_nth"] if entry.get("ambiguous") else None

    by_role = A11yLocator(strategy="role", role=role or "generic", name=name or None,
                          exact=bool(name), nth=name_nth)

    if not name:
        # Nothing to look the node up by. Position within its role is all the
        # a11y tree gave us, so it is the primary rather than a fallback.
        return Locator(
            primary=A11yLocator(strategy="role", role=role or "generic", nth=entry["role_nth"]),
            fallbacks=[],
        )

    by_label = A11yLocator(strategy="label", name=name, exact=True, nth=name_nth)
    by_text = A11yLocator(strategy="text", name=name, exact=True, nth=name_nth)
    by_position = A11yLocator(strategy="role", role=role or "generic", nth=entry["role_nth"])

    # A positional fallback matches whatever happens to sit at that index, so on
    # a page that is not the one we recorded it silently hits the wrong element.
    # For an action that changes state -- a click, a fill, a select -- that is
    # worse than failing: a wrong click submits a wrong form. When the named
    # locators above have all missed, the page is not what this capability was
    # built against, and stopping is the honest outcome.
    #
    # This only removes position as a LAST RESORT. Where a node had no
    # accessible name at all, position is the primary above and stays: there it
    # is what discovery actually recorded, not a guess made after better
    # locators failed.
    changes_state = entry["action"]["action"] in ("click", "fill", "select")
    tail = [] if changes_state else [by_position]

    if role in _FORM_ROLES:
        return Locator(primary=by_label, fallbacks=[by_role, by_text, *tail])
    return Locator(primary=by_role, fallbacks=[by_label, by_text, *tail])


def _step_for(entry: dict) -> Step:
    """One transcript entry as a schema-valid ``Step``.

    Carries exactly the fields the action can use, because ``Step`` rejects the
    rest: navigate gets a url and no locator, fill and select get a value,
    click and read get neither.
    """
    action = entry["action"]
    verb = action["action"]
    description = entry["rationale"]

    if verb == "navigate":
        return Step(index=entry["index"], action="navigate", description=description, url=action["url"])

    locator = _locator_for(entry)
    if verb in ("fill", "select"):
        # The generalisation: a caller-supplied value becomes a template, and
        # only the template is ever persisted.
        value = f"{{{{{entry['param']}}}}}" if entry.get("param") else action["value"]
        return Step(index=entry["index"], action=verb, description=description,
                    locator=locator, value=value)

    return Step(index=entry["index"], action=verb, description=description, locator=locator)


def _inputs_for(transcript: list[dict]) -> list[InputParam]:
    """Declare one ``InputParam`` per distinct run input the transcript used.

    Ordered by first use so the artifact reads in the order a caller fills the
    form. Every param is typed ``string``: what goes into a form field is text,
    and deciding that "800000" is really a number -- or that leading zeros are
    droppable -- is the caller's call to make, not ours.
    """
    inputs: list[InputParam] = []
    for entry in transcript:
        name = entry.get("param")
        if not name or any(p.name == name for p in inputs):
            continue
        node = entry.get("node") or {}
        label = (node.get("name") or "").strip() or node.get("role") or "the field"
        inputs.append(
            InputParam(
                name=name,
                type="string",
                required=True,
                description=(
                    f"Supplied by the caller at discovery time and templated into step "
                    f"{entry['index']}, where it is {entry['action']['action']}ed into "
                    f"{label!r}."
                ),
                secret=any(hint in name.lower() for hint in _SECRET_NAME_HINTS),
            )
        )
    return inputs


def _checkpoint_for(transcript: list[dict], steps: list[Step]) -> Checkpoint:
    """The success condition, taken from the run's final observation.

    Preference is for ``a11y_node_present`` anchored on the last thing the run
    read: if that node is on the page, the run genuinely arrived at the view it
    was supposed to arrive at. The expected value is the node's *accessible
    name* -- the UI's own label for the field -- never the value read out of it,
    which is exactly the member data that must not be baked into an artifact.

    With no read step there is nothing on the page to anchor to, so the
    checkpoint falls back to the URL the run finished on.
    """
    last_read = next(
        (e for e in reversed(transcript) if e["action"]["action"] == "read"), None
    )
    if last_read is not None:
        node = last_read["node"]
        return Checkpoint(
            kind="a11y_node_present",
            expected=(node.get("name") or "").strip() or node.get("role") or "",
            locator=steps[last_read["index"]].locator,
        )
    return Checkpoint(kind="url_matches", expected=transcript[-1]["after"]["url"])


def compile_artifact(
    goal: str,
    target: str,
    capability_id: str,
    transcript: list[dict],
    outputs_read: list[dict],
) -> CapabilityArtifact:
    """Turn one discovery run into a replayable, schema-valid capability.

    ``transcript`` is the loop's record (see the module docstring for the entry
    shape); ``outputs_read`` declares the values the capability returns, each as
    ``{"name", "type", "description", "from_step"}`` where ``from_step`` is the
    index of a ``read`` entry in the transcript.

    The returned artifact is validated by ``CapabilityArtifact`` itself, so a
    compiler bug -- a dangling template, an output pointing at a click, a gap in
    the step indices -- surfaces here rather than mid-replay in a live UI.
    """
    if not transcript:
        raise DiscoveryError(f"nothing to compile: the run for goal {goal!r} recorded no steps")

    steps = [_step_for(entry) for entry in transcript]
    outputs = [
        OutputField(
            name=spec["name"],
            type=spec["type"],
            description=spec["description"],
            from_step=spec["from_step"],
            locator=steps[spec["from_step"]].locator,
        )
        for spec in outputs_read
    ]

    return CapabilityArtifact(
        capability_id=capability_id,
        goal=goal,
        target=target,
        created_at=datetime.now(timezone.utc),
        inputs=_inputs_for(transcript),
        outputs=outputs,
        steps=steps,
        checkpoint=_checkpoint_for(transcript, steps),
    )


class UnknownRefError(DiscoveryError):
    """The model named a ref that is not in the observation it was shown.

    Recoverable during discovery: nothing was executed, and the loop can show
    the model the refs that do exist and let it choose again. It stays a
    subclass of DiscoveryError so a caller that does not care about the
    distinction still catches it.
    """


def _retarget_label_read(observation, node, read_text, session, step_index, logger):
    """If a read landed on a label, read the value cell beside it instead.

    A table pairs a label cell with a value cell, and a model asked for "the
    balance" often reads the cell that says "Available balance" rather than the
    one holding the number. The tell is exact: the text read back IS the node's
    own accessible name, and that name is words rather than data.

    The neighbouring value is then actually read, not inferred, so the artifact
    only ever records a step that was really performed. Returns the replacement
    ``(node, name_nth, role_nth, ambiguous, prefix_nth, detail)`` or None to keep
    what the model chose.
    """
    name = (node.get("name") or "").strip()
    if not name or read_text.strip() != name or _name_is_unusable(name):
        return None

    nodes = observation.nodes
    position = next((i for i, n in enumerate(nodes) if n.get("ref") == node.get("ref")), None)
    if position is None:
        return None

    # Look only a short way ahead: the value belonging to a label is adjacent to
    # it, and anything further off is a different row's data.
    for candidate in nodes[position + 1: position + 5]:
        candidate_name = (candidate.get("name") or "").strip()
        if not candidate.get("ref") or not candidate_name or not _name_is_unusable(candidate_name):
            continue
        attempt = session.act({"action": "read", "ref": candidate["ref"]})
        if not attempt.ok or not (attempt.detail or "").strip():
            return None
        logger.info(
            "read retargeted from a label to the value beside it",
            extra={"phase": "discover", "step": step_index, "label": name,
                   "value_role": candidate.get("role")},
        )
        resolved = _index_node(observation, candidate["ref"], step_index)
        return (*resolved, attempt.detail)
    return None


def _loggable(action: dict) -> dict:
    """An action safe to write to a log: its value replaced by a description.

    A filled value may be a password, and a bare password has no shape any
    redaction pattern can recognise, so it has to be kept out of the log at the
    point of writing rather than caught downstream. The length and the parameter
    name are enough to debug with; the value itself never is.
    """
    if "value" not in action or action["value"] is None:
        return action
    safe = dict(action)
    safe["value"] = f"<{len(str(action['value']))} chars>"
    return safe


def _index_node(observation, ref: str, step_index: int) -> tuple[dict, int, int, bool]:
    """Find the node a ref points at, plus the positional facts the compiler needs.

    Resolved here, while the snapshot the model actually looked at is still in
    hand: once the page moves on, there is no way to recover how many
    same-named nodes there were.
    """
    nodes = observation.nodes
    node = next((n for n in nodes if n.get("ref") == ref), None)
    if node is None:
        raise UnknownRefError(
            f"step {step_index}: model chose ref {ref!r}, which is not in the current "
            f"observation of {observation.url!r} (refs seen: {[n.get('ref') for n in nodes]})"
        )

    same_role = [n for n in nodes if n.get("role") == node.get("role")]
    same_name = [n for n in same_role if (n.get("name") or "") == (node.get("name") or "")]
    # Position among nodes whose stable prefix matches this one's. A prefix
    # locator is matched loosely, so it can hit several nodes; this is the index
    # that disambiguates it, and it cannot be recovered once the page moves on.
    prefix = _stable_part(node.get("name") or "")
    same_prefix = [n for n in same_role if _stable_part(n.get("name") or "") == prefix]
    prefix_nth = same_prefix.index(node) if len(same_prefix) > 1 else None
    return node, same_name.index(node), same_role.index(node), len(same_name) > 1, prefix_nth


def _output_name(node: dict, taken: set[str], label: str = "") -> str:
    """A stable output name derived from what the read node is called on screen.

    ``label`` is set when the read was retargeted off a label onto the value
    beside it. That label is precisely what the screen calls this value, so it
    names the output far better than the value cell's own name, which is the
    data itself.
    """
    raw = (label or node.get("name") or "").strip()
    # Never name an output after its own value: "2000000000_00" tells a caller
    # nothing and differs every run. Use the stable words if there are any, and
    # otherwise fall back to the node's role.
    label = _stable_part(raw) if raw else ""
    if not any(c.isalpha() for c in label):
        label = node.get("role") or "value"
    base = _slug(label, max_words=4)
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def discover(
    goal: str,
    target: str,
    *,
    client,
    session,
    logger,
    max_steps: int = 25,
    run_inputs: dict[str, str] | None = None,
    capability_id: str | None = None,
    model: str | None = None,
) -> CapabilityArtifact:
    """Drive the observe -> decide -> act loop until the goal is done, then compile it.

    ``client`` (an Anthropic client) and ``session`` (a ``BrowserSession``) are
    injected; the caller owns the session's ``start``/``stop`` lifecycle, since
    escalation in a later phase needs to hand the *same* live session to a human
    rather than have this function tear it down.

    ``run_inputs`` maps parameter name to the value to use for this run --
    ``{"member_id": "800000"}``. Those are the values that become ``{{param}}``
    templates in the compiled artifact; see the module docstring for why that,
    and nothing else, is the rule.

    Terminates three ways, all of them explicit:
      * the model answers ``done``      -> the transcript is compiled and returned
      * the model answers ``stuck``     -> ``StuckError`` with the handoff context
      * the budget runs out             -> ``MaxStepsError``

    An action that the session reports as failed is not a terminal condition: it
    is fed back into the model's history so it can try something else, and it is
    left out of the transcript, because only the path that actually worked
    belongs in a capability. Every such failure is logged at WARNING, so nothing
    is swallowed.
    """
    run_inputs = run_inputs or {}
    capability_id = capability_id or _default_capability_id(goal, target)
    by_value = {value: name for name, value in run_inputs.items()}

    def _param_for(typed: str) -> str | None:
        """Which declared input, if any, the model actually typed.

        Exact match first. Failing that, a select whose option label carries the
        value ("800002 Savings" for an account_number of "800002") still counts:
        the model picks the label it sees on screen, and refusing to generalise
        it would hardcode the one account the run happened to use -- which is
        exactly what a reusable capability must not do. The value must appear as
        a whole token, so "800002" does not match "8000021".
        """
        if typed in by_value:
            return by_value[typed]
        for value, name in by_value.items():
            if value and re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", typed):
                return name
        return None

    transcript: list[dict] = []
    outputs_read: list[dict] = []
    history: list[dict] = []

    logger.info(
        "discovery started",
        extra={"phase": "discover", "capability_id": capability_id, "goal": goal,
               "target": target, "max_steps": max_steps, "params": sorted(run_inputs)},
    )

    # Step 0 is always a navigation to the given target. The caller supplied it,
    # so there is nothing for the model to decide here -- and letting it guess
    # invites it to head for a site it recognises by name rather than the
    # sandbox it was pointed at, which the allowlist would then (correctly) block.
    before = session.observe()
    seed = {"action": "navigate", "url": target}
    seed_result = session.act(seed)
    if not seed_result.ok:
        raise DiscoveryError(f"could not open target {target!r}: {seed_result.detail}")
    transcript.append({
        "index": 0,
        "action": seed,
        "rationale": "Open the capability's target page.",
        "node": None, "name_nth": None, "role_nth": None, "ambiguous": False,
        "param": None,
        "url_before": before.url,
        "after": {"url": seed_result.observation.url, "title": seed_result.observation.title},
    })
    history.append({"index": -1, "action": seed, "ok": True, "detail": "target opened"})

    last_good_url = target

    for attempt in range(max_steps):
        observation = session.observe()

        # A failed or blocked navigation parks the tab on a browser error page,
        # where there is nothing to click and no way forward. Recover
        # deterministically to the last page that worked rather than spending
        # model turns asking it to find its own way off a dead end.
        if observation.url.startswith(("chrome-error://", "about:blank")):
            logger.warning(
                "on a browser error page, recovering to last good url",
                extra={"phase": "discover", "attempt": attempt,
                       "error_url": observation.url, "recover_to": last_good_url},
            )
            # Drop the step that got us here. It looked successful at the time --
            # a click returns before the navigation it starts resolves, and a
            # blocked navigation inside a frame settles later still -- but it
            # provably leads to a dead end, and a capability containing it cannot
            # replay. Only the path that actually worked belongs in an artifact.
            dead_end = transcript.pop() if transcript else None
            if dead_end is not None:
                outputs_read[:] = [o for o in outputs_read if o["from_step"] != dead_end["index"]]
                logger.warning(
                    "dropping the step that led to the error page",
                    extra={"phase": "discover", "dropped_step": dead_end["index"],
                           "dropped_action": _loggable(dead_end["action"])},
                )

            recovery = session.act({"action": "navigate", "url": last_good_url})
            history.append({"index": attempt, "action": {"action": "navigate", "url": last_good_url},
                            "ok": recovery.ok,
                            "detail": "recovered from a browser error page; the previous action "
                                      "led nowhere, so do not repeat it"})
            if not recovery.ok:
                raise DiscoveryError(
                    f"stranded on {observation.url!r} and could not return to {last_good_url!r}"
                )
            continue
        last_good_url = observation.url
        decision = (decide(client, goal, observation, history, model=model, inputs=run_inputs)
                    if model else decide(client, goal, observation, history, inputs=run_inputs))
        action = decision.action
        verb = action["action"]
        step_index = len(transcript)

        logger.info(
            "decision",
            extra={"phase": "discover", "attempt": attempt, "step": step_index,
                   "action": _loggable(action), "rationale": decision.rationale,
                   "url": observation.url},
        )

        if verb == "stuck":
            raise StuckError(goal, target, step_index, observation.url, action["reason"])

        if verb == "done":
            # Step 0 is the seeded navigation, which every run gets for free. A
            # capability that consists only of "open the target page" did nothing
            # and is worth nothing, so the bar is at least one decided action.
            if len(transcript) <= 1:
                raise DiscoveryError(
                    f"model declared the goal {goal!r} done without taking a single action"
                )
            logger.info(
                "discovery complete",
                extra={"phase": "discover", "steps": len(transcript),
                       "outputs": [o["name"] for o in outputs_read]},
            )
            return compile_artifact(goal, target, capability_id, transcript, outputs_read)

        node = name_nth = role_nth = prefix_nth = None
        ambiguous = False
        if verb != "navigate":
            try:
                node, name_nth, role_nth, ambiguous, prefix_nth = _index_node(
                    observation, action["ref"], step_index
                )
            except UnknownRefError as exc:
                # Nothing was executed. Tell the model which refs actually exist
                # and let it choose again -- the resolver stays strict, this only
                # decides how the loop reacts to a miss.
                live = [n.get("ref") for n in observation.nodes if n.get("ref")]
                logger.warning(
                    "model chose an unknown ref, feeding back",
                    extra={"phase": "discover", "attempt": attempt,
                           "chosen_ref": action.get("ref"), "live_ref_count": len(live)},
                )
                history.append({
                    "index": attempt, "action": action, "ok": False,
                    "detail": (f"NO SUCH REF {action.get('ref')!r} on this page. "
                               f"Valid refs right now: {', '.join(live[:40])}"),
                })
                continue

        try:
            result = session.act(action)
        except AllowlistViolation as exc:
            # The guardrail refused the action, so nothing was executed. During
            # discovery that is a navigational mistake by the model, not a fatal
            # condition: tell it the destination is out of scope and let it
            # re-plan. Logged at WARNING so a blocked action is never silent, and
            # the allowlist itself is unchanged -- this only decides how the loop
            # reacts to a block, never whether the block happens.
            logger.warning(
                "action blocked by allowlist, feeding back to model",
                extra={"phase": "discover", "attempt": attempt,
                       "action": _loggable(action), "reason": str(exc)},
            )
            history.append({"index": attempt, "action": action, "ok": False,
                            "detail": f"BLOCKED by allowlist: {exc}"})
            continue

        if not result.ok:
            # Informative, not fatal: this is the one place a model in the loop
            # earns its keep, so hand the failure back and let it re-plan.
            logger.warning(
                "action failed, feeding back to model",
                extra={"phase": "discover", "attempt": attempt,
                       "action": _loggable(action), "detail": result.detail},
            )
            history.append({"index": attempt, "action": action, "ok": False, "detail": result.detail})
            continue

        after = result.observation
        transcript.append({
            "index": step_index,
            "action": action,
            "rationale": decision.rationale,
            "node": node,
            "name_nth": name_nth,
            "role_nth": role_nth,
            "ambiguous": ambiguous,
            "prefix_nth": prefix_nth,
            "param": _param_for(action["value"]) if verb in ("fill", "select") else None,
            "url_before": observation.url,
            "after": {"url": after.url, "title": after.title},
        })
        history.append({"index": attempt, "action": action, "ok": True, "detail": result.detail})

        if verb == "read":
            swap = _retarget_label_read(
                observation, node, result.detail or "", session, step_index, logger
            )
            if swap is not None:
                # Only the transcript's node changes. The output's VALUE is read
                # from the locator at replay time, so there is nothing else here
                # that needs the text this read returned.
                label_name = (node.get("name") or "").strip()
                node, name_nth, role_nth, ambiguous, prefix_nth, _detail = swap
                transcript[-1].update({
                    "node": node, "name_nth": name_nth, "role_nth": role_nth,
                    "ambiguous": ambiguous, "prefix_nth": prefix_nth,
                    # The label this read moved off is the best name the output
                    # will ever have: it is what the screen calls this value.
                    "label": label_name,
                })

            name = _output_name(node, {o["name"] for o in outputs_read},
                                label=transcript[-1].get("label", ""))
            outputs_read.append({
                "name": name,
                "type": "string",
                "description": (
                    f"Read at step {step_index} from the "
                    f"{node.get('role') or 'node'} labelled "
                    f"{(node.get('name') or '').strip() or '(unnamed)'!r}. Kept as a string: "
                    f"the artifact returns what the screen said, and interpreting it is the "
                    f"caller's decision."
                ),
                "from_step": step_index,
            })

    raise MaxStepsError(
        f"discovery for goal {goal!r} used its whole budget of {max_steps} steps without "
        f"the model reporting the goal done; {len(transcript)} step(s) were recorded and "
        f"are being discarded rather than compiled into an unverified capability"
    )
