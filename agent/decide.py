"""The decide half of the discovery loop: one observation in, one action out.

This is the only place in the whole system that talks to a model. Replay never
reaches it, and discovery reaches it exactly once per step. The job is narrow on
purpose: take what the browser can currently see, take what has already been
tried, and come back with a single next action -- not a plan, not a script.

Two design choices are worth stating up front.

*The client is injected.* ``decide`` never constructs an ``anthropic.Anthropic``
of its own. The caller passes one in, which is what makes the loop testable
without network access or an API key: ``agent.check_discover`` passes a fake that
returns canned, Anthropic-shaped responses.

*The response contract is strict.* The model must answer with one JSON object
carrying exactly ``action`` and ``rationale``, and the action must carry exactly
the fields its verb can use -- no more. Unknown keys are rejected rather than
ignored, for the same reason ``artifact/schema.py`` forbids extras: a stale or
misspelled key that disappears quietly is a silent behaviour change, and this
path puts actions into a real back-office UI. Anything malformed raises
``DecisionError`` and stops the run.
"""

import json
from dataclasses import dataclass

__all__ = ["Decision", "DecisionError", "decide"]

# How much of one observation is worth spending context on. An accessibility
# tree for a dense back-office page runs to hundreds of nodes, the great
# majority of them chrome the model will never act on. These bounds are about
# prompt size, not correctness: the refs the model may choose from are exactly
# the ones it is shown, and a truncation is announced in the prompt rather than
# hidden, so the model can navigate instead of guessing at what was cut.
_MAX_NODES = 120
_MAX_DIGEST_CHARS = 2000

# Verbs that BrowserSession can execute, and the terminal verbs that only the
# loop understands.
_STEP_ACTIONS = frozenset({"navigate", "click", "fill", "select", "read"})
_TERMINAL_ACTIONS = frozenset({"done", "stuck"})

# The exact key set each verb may carry, "action" included.
_ACTION_KEYS = {
    "navigate": {"action", "url"},
    "click": {"action", "ref"},
    "fill": {"action", "ref", "value"},
    "select": {"action", "ref", "value"},
    "read": {"action", "ref"},
    "done": {"action"},
    "stuck": {"action", "reason"},
}

SYSTEM_PROMPT = """\
You are driving a real web UI through an accessibility-tree interface in order \
to discover, once, how a task is performed. Everything you do here is being \
recorded and compiled into a deterministic script that will later be replayed \
with no model in the loop, so take the shortest correct path and do not explore \
for its own sake.

You see, each turn: the goal, the current page URL and title, the accessibility \
nodes on that page, a text digest of the page, and the history of what has \
already been done. You reply with exactly ONE next action.

Reply with a single JSON object and nothing else -- no prose before or after, no \
markdown fence. It must have exactly two keys:

  "action"    - one action object, from the list below
  "rationale" - one sentence saying why this action moves the goal forward

The action object must use exactly these shapes. Do not add any other key:

  {"action": "navigate", "url": "<absolute url>"}
  {"action": "click",    "ref": "<ref from the CURRENT nodes list>"}
  {"action": "fill",     "ref": "<ref>", "value": "<text to type>"}
  {"action": "select",   "ref": "<ref>", "value": "<option to choose>"}
  {"action": "read",     "ref": "<ref>"}
  {"action": "done"}
  {"action": "stuck",    "reason": "<what is blocking you>"}

Rules:
- A "ref" is only valid for the page you are looking at right now. Never reuse a \
ref from an earlier turn, and never invent one.
- Read back every value the goal asks for, with a "read" action each, BEFORE \
you answer "done".
- Answer "done" only when the goal is fully achieved and its values have been read.
- Answer "stuck" when the page needs something you do not have or cannot do -- a \
login, a human decision, a CAPTCHA, an option that is not on the page. Being \
stuck is a legitimate answer and is far better than guessing; a human is \
brought in to take over.
"""


@dataclass
class Decision:
    """One action the model chose, and its stated reason for choosing it."""

    action: dict
    rationale: str


class DecisionError(Exception):
    """The model's response was not a usable decision.

    Raised on anything that cannot be turned into exactly one well-formed
    action: no text content, no JSON, missing keys, an unknown verb, or a verb
    carrying fields it cannot use.
    """


def _render_nodes(nodes: list[dict]) -> str:
    """The accessibility nodes as one line each, in the order the page gave them."""
    if not nodes:
        return "(no accessibility nodes on this page)"

    lines = []
    for node in nodes[:_MAX_NODES]:
        line = f"[{node['ref']}] {node.get('role') or '?'}"
        if node.get("name"):
            line += f' name="{node["name"]}"'
        if node.get("value"):
            line += f' value="{node["value"]}"'
        if node.get("focusable"):
            line += " focusable"
        lines.append(line)

    if len(nodes) > _MAX_NODES:
        lines.append(
            f"... {len(nodes) - _MAX_NODES} further nodes not shown. If what you "
            f"need is not above, navigate or narrow the page rather than guessing "
            f"at a ref."
        )
    return "\n".join(lines)


def _render_history(history: list[dict]) -> str:
    """What has already been tried, most recent last.

    Entries are the loop's own record: the action dict, whether it worked, and
    whatever the session said about it. Failures stay in here on purpose -- the
    model needs to see that a click did nothing in order to try something else.
    """
    if not history:
        return "(nothing done yet -- this is the first step)"

    lines = []
    for entry in history:
        outcome = "ok" if entry.get("ok") else "FAILED"
        line = f"{entry.get('index', '?')}. {json.dumps(entry.get('action', {}))} -> {outcome}"
        if entry.get("detail"):
            line += f" ({entry['detail']})"
        lines.append(line)
    return "\n".join(lines)


def _render_inputs(inputs: dict[str, str] | None) -> str:
    """Render the values the caller wants typed during this run.

    The model has to be told these: it is being asked to drive a form, and a
    value it invents is simply the wrong value. They are shown verbatim because
    they must be typed verbatim. What keeps them out of the saved capability is
    the compiler, which turns each one back into a ``{{param}}`` reference, and
    what keeps them out of the logs is the redaction filter.
    """
    if not inputs:
        return "(none -- this capability takes no input values)"
    return "\n".join(f"- {name}: {value}" for name, value in sorted(inputs.items()))


def _build_prompt(goal: str, observation, history: list[dict],
                  inputs: dict[str, str] | None = None) -> str:
    """Assemble the per-turn user message from goal, observation and history."""
    digest = observation.text_digest or ""
    if len(digest) > _MAX_DIGEST_CHARS:
        digest = digest[:_MAX_DIGEST_CHARS] + "\n... (digest truncated)"

    return (
        f"GOAL\n{goal}\n\n"
        f"INPUT VALUES TO USE (type these exactly; do not invent your own)\n"
        f"{_render_inputs(inputs)}\n\n"
        f"CURRENT PAGE\nurl: {observation.url}\ntitle: {observation.title}\n\n"
        f"ACCESSIBILITY NODES (refs are valid for this page only)\n"
        f"{_render_nodes(observation.nodes)}\n\n"
        f"PAGE TEXT DIGEST\n{digest}\n\n"
        f"HISTORY SO FAR\n{_render_history(history)}\n\n"
        f"Reply with one JSON object: the single next action, and your rationale."
    )


def _response_text(response) -> str:
    """Concatenate the text blocks of an Anthropic Messages response.

    Tolerant about block shape (SDK object or plain dict) because the fake
    client in the self-check builds these by hand, but intolerant about there
    being no text at all -- a response with nothing to parse is a failure, not
    an empty decision.
    """
    parts = []
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if kind != "text":
            continue
        text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
        if text:
            parts.append(text)

    if not parts:
        raise DecisionError(
            f"model response carried no text block to parse; got content={getattr(response, 'content', None)!r}"
        )
    return "\n".join(parts)


def _parse_json_object(text: str) -> dict:
    """Pull the one JSON object out of the model's reply.

    The prompt asks for bare JSON, but a stray markdown fence or a sentence of
    preamble is the single most common deviation and is not worth failing a
    live run over, so we scan forward for the first thing that parses as an
    object. Anything beyond that -- no object at all, or a non-object -- raises.
    """
    decoder = json.JSONDecoder()
    for start in range(len(text)):
        if text[start] != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value

    raise DecisionError(f"no JSON object found in model response: {text!r}")


def _validate_action(action) -> dict:
    """Reject any action the loop cannot execute verbatim.

    Shape errors are caught here rather than at ``session.act`` time so the
    message names the model's mistake instead of surfacing as a browser error
    three frames away.
    """
    if not isinstance(action, dict):
        raise DecisionError(f"'action' must be a JSON object, got {type(action).__name__}: {action!r}")

    verb = action.get("action")
    if verb not in _STEP_ACTIONS | _TERMINAL_ACTIONS:
        raise DecisionError(
            f"unknown action {verb!r}; expected one of {sorted(_STEP_ACTIONS | _TERMINAL_ACTIONS)}"
        )

    expected = _ACTION_KEYS[verb]
    actual = set(action)
    if actual != expected:
        raise DecisionError(
            f"action {verb!r} must carry exactly {sorted(expected)}, got {sorted(actual)} "
            f"(missing {sorted(expected - actual)}, unexpected {sorted(actual - expected)})"
        )

    for key in expected - {"action"}:
        if not isinstance(action[key], str) or not action[key].strip():
            raise DecisionError(
                f"action {verb!r} field {key!r} must be a non-empty string, got {action[key]!r}"
            )
    return action


def decide(client, goal: str, observation, history: list[dict],
           model: str = "claude-opus-5", inputs: dict[str, str] | None = None) -> Decision:
    """Ask the model for the single next action, given what the page shows now.

    ``client`` is an injected Anthropic client (anything exposing
    ``messages.create``). ``observation`` is a ``BrowserSession.observe()``
    result. ``history`` is the loop's record of previous steps, failures
    included.

    Raises ``DecisionError`` if the response cannot be read as exactly one
    well-formed action. There is deliberately no retry and no fallback action
    here: a model that cannot answer the format is a condition the caller
    should see, not one this function should paper over.
    """
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(goal, observation, history, inputs)}],
    )

    payload = _parse_json_object(_response_text(response))

    unexpected = set(payload) - {"action", "rationale"}
    if unexpected:
        raise DecisionError(
            f"response object must carry exactly 'action' and 'rationale', "
            f"found extra key(s) {sorted(unexpected)}"
        )

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise DecisionError(f"'rationale' must be a non-empty string, got {rationale!r}")

    return Decision(action=_validate_action(payload.get("action")), rationale=rationale.strip())
