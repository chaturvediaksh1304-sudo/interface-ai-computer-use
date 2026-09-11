"""Self-check for the CapabilityArtifact schema.

Run from the repo root:  python3 -m artifact.check_schema

Covers both Phase 2 done-criteria:
  (a) the schema round-trips through serialize/deserialize with no data loss, and
  (b) it validates against the hand-written example artifact on disk.

Plus one rejection case per validation rule the schema is supposed to enforce. A schema
that accepts everything round-trips perfectly and is worth nothing, so the rejection
cases are the part with teeth: each one takes a valid artifact, breaks exactly one
invariant, and asserts the schema refuses it.

Round-trips are done in a temporary directory; nothing is written under artifacts/.
"""

import json
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from artifact.schema import (
    SCHEMA_VERSION,
    CapabilityArtifact,
    load_artifact,
    save_artifact,
)

EXAMPLE_PATH = Path("artifacts/example_member_lookup.v1.json")

A11Y_STRATEGIES = {"role", "label", "text", "placeholder", "testid"}

rejections: list[str] = []


def rejects(bad: dict, why: str) -> str:
    """Assert the schema refuses `bad`, and hand back the complaint for inspection.

    Pydantic wraps validator failures in ValidationError, which is itself a ValueError,
    so catching ValueError also covers a schema that raises plain ValueError directly.
    """
    try:
        CapabilityArtifact.model_validate(bad)
    except ValueError as exc:
        rejections.append(why)
        return str(exc)
    raise AssertionError(f"schema accepted {why}")


def assert_no_loss(actual, expected, path: str = "") -> None:
    """Assert every leaf of `expected` survives into `actual`.

    Used to prove serialization does not silently drop a field: a model that parses the
    source but forgets to emit one of its fields fails here rather than passing quietly.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {actual!r}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} dropped during round-trip"
            assert_no_loss(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected a list, got {actual!r}"
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}"
        for i, value in enumerate(expected):
            assert_no_loss(actual[i], value, f"{path}[{i}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def round_trip(source: dict, label: str) -> CapabilityArtifact:
    """Parse -> save -> load -> save again, asserting equality at every hop."""
    first = CapabilityArtifact.model_validate(source)

    with tempfile.TemporaryDirectory() as tmp:
        path = save_artifact(first, dir=tmp)
        on_disk = path.read_bytes()

        second = load_artifact(path)
        # Full model equality, not a field-by-field spot check.
        assert second == first, f"{label}: model changed across the round-trip"
        assert second.model_dump_json() == first.model_dump_json(), (
            f"{label}: serialized form changed across the round-trip"
        )

        # Byte-identical on disk: serialize(load(serialize(x))) == serialize(x).
        assert save_artifact(second, dir=tmp).read_bytes() == on_disk, (
            f"{label}: re-saved bytes differ from the first save"
        )
        # The file we wrote really is the normalized form of the model.
        assert json.loads(on_disk) == json.loads(first.model_dump_json()), (
            f"{label}: file on disk is not the model's normalized JSON"
        )

    # Nothing we wrote went missing. created_at is compared as an instant rather than a
    # string, since the source spelling and the serialized spelling may differ.
    dumped = json.loads(first.model_dump_json())
    expected = deepcopy(source)
    assert datetime.fromisoformat(expected["created_at"]) == datetime.fromisoformat(
        dumped["created_at"]
    ), f"{label}: created_at changed instants"
    expected["created_at"] = dumped["created_at"]
    assert_no_loss(dumped, expected, label)
    return first


# --------------------------------------------------------------------------------------
# (b) The hand-written example artifact validates clean.
# --------------------------------------------------------------------------------------

assert SCHEMA_VERSION == "1.0", SCHEMA_VERSION
assert EXAMPLE_PATH.exists(), f"missing example artifact at {EXAMPLE_PATH}"

EXAMPLE_SOURCE = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
example = load_artifact(EXAMPLE_PATH)

assert example.schema_version == SCHEMA_VERSION
assert example.capability_id == "altoro.member_account_lookup"
assert example.target == "https://demo.testfire.net"
assert example.inputs and example.outputs, "example must declare inputs and outputs"

# It is a genuine search -> detail -> read flow, not a one-click toy.
actions = [step.action for step in example.steps]
assert len(actions) >= 4, actions
assert {"navigate", "fill", "click", "read"} <= set(actions), actions

# Architecture.md's central claim: the accessibility tree locates, CSS is only the escape
# hatch. Assert that here so the reviewer-facing example cannot quietly drift to CSS-first.
for step in example.steps:
    if step.locator is None:
        continue
    assert step.locator.primary.strategy in A11Y_STRATEGIES, (
        f"step {step.index} leads with a non-a11y locator: {step.locator.primary.strategy}"
    )
    assert any(fb.strategy == "css" for fb in step.locator.fallbacks), (
        f"step {step.index} has no css fallback"
    )

# Outputs point at real read steps.
read_indices = {step.index for step in example.steps if step.action == "read"}
assert {out.from_step for out in example.outputs} <= read_indices


# --------------------------------------------------------------------------------------
# (a) Round-trip with no data loss.
# --------------------------------------------------------------------------------------

round_trip(EXAMPLE_SOURCE, "example")

# An all-defaults artifact proves little, so this one populates every optional field:
# fallbacks, exact, nth, a non-required param, number/bool param types, a secret param
# referenced through the template form, and each of the three checkpoint kinds.
MAXIMAL = {
    "schema_version": "1.0",
    "capability_id": "altoro.member_hold_release",
    "version": 3,
    "goal": "Release a pending hold on a member account, confirming with a staff PIN.",
    "target": "https://demo.testfire.net",
    "created_at": "2026-09-11T14:02:11+00:00",
    "inputs": [
        {
            "name": "member_id",
            "type": "string",
            "required": True,
            "description": "Member identifier to act on.",
            "secret": False,
        },
        {
            "name": "hold_amount",
            "type": "number",
            "required": False,
            "description": "Amount of the hold being released, for confirmation.",
            "secret": False,
        },
        {
            "name": "notify_member",
            "type": "bool",
            "required": False,
            "description": "Whether to tick the notify-member box.",
            "secret": False,
        },
        {
            "name": "staff_pin",
            "type": "string",
            "required": True,
            "description": "Operator PIN authorising the release. Never logged, never inlined.",
            "secret": True,
        },
    ],
    "outputs": [
        {
            "name": "release_reference",
            "type": "string",
            "description": "Reference number the UI prints once the hold is released.",
            "from_step": 4,
            "locator": {
                "primary": {
                    "strategy": "testid",
                    "name": "release-reference",
                    "role": None,
                    "selector": None,
                    "exact": True,
                    "nth": 0,
                },
                "fallbacks": [
                    {
                        "strategy": "css",
                        "name": None,
                        "role": None,
                        "selector": "#releaseReference",
                        "exact": False,
                        "nth": None,
                    }
                ],
            },
        }
    ],
    "steps": [
        {
            "index": 0,
            "action": "navigate",
            "description": "Open the holds queue.",
            "locator": None,
            "value": None,
            "url": "https://demo.testfire.net/bank/main.jsp",
        },
        {
            "index": 1,
            "action": "fill",
            "description": "Identify the member.",
            "locator": {
                "primary": {
                    "strategy": "label",
                    "name": "Member ID",
                    "role": None,
                    "selector": None,
                    "exact": True,
                    "nth": 0,
                },
                "fallbacks": [
                    {
                        "strategy": "placeholder",
                        "name": "Member ID",
                        "role": None,
                        "selector": None,
                        "exact": False,
                        "nth": 1,
                    },
                    {
                        "strategy": "css",
                        "name": None,
                        "role": None,
                        "selector": "input[name='memberId']",
                        "exact": False,
                        "nth": None,
                    },
                ],
            },
            "value": "member {{member_id}} hold {{hold_amount}}",
            "url": None,
        },
        {
            "index": 2,
            "action": "fill",
            "description": "Authorise with the operator PIN. Template form only: the secret "
            "must never be embedded in a larger literal.",
            "locator": {
                "primary": {
                    "strategy": "label",
                    "name": "Staff PIN",
                    "role": None,
                    "selector": None,
                    "exact": True,
                    "nth": None,
                },
                "fallbacks": [
                    {
                        "strategy": "css",
                        "name": None,
                        "role": None,
                        "selector": "input[name='staffPin']",
                        "exact": False,
                        "nth": None,
                    }
                ],
            },
            "value": "{{staff_pin}}",
            "url": None,
        },
        {
            "index": 3,
            "action": "select",
            "description": "Pick the notification preference.",
            "locator": {
                "primary": {
                    "strategy": "role",
                    "name": "Notify member",
                    "role": "combobox",
                    "selector": None,
                    "exact": False,
                    "nth": 0,
                },
                "fallbacks": [
                    {
                        "strategy": "css",
                        "name": None,
                        "role": None,
                        "selector": "select[name='notify']",
                        "exact": False,
                        "nth": None,
                    }
                ],
            },
            "value": "{{notify_member}}",
            "url": None,
        },
        {
            "index": 4,
            "action": "read",
            "description": "Read back the release reference.",
            "locator": {
                "primary": {
                    "strategy": "testid",
                    "name": "release-reference",
                    "role": None,
                    "selector": None,
                    "exact": True,
                    "nth": 0,
                },
                "fallbacks": [
                    {
                        "strategy": "text",
                        "name": "Reference",
                        "role": None,
                        "selector": None,
                        "exact": False,
                        "nth": 0,
                    }
                ],
            },
            "value": None,
            "url": None,
        },
    ],
    "checkpoint": {
        "kind": "text_present",
        "expected": "Hold released",
        "locator": {
            "primary": {
                "strategy": "role",
                "name": "Hold detail",
                "role": "region",
                "selector": None,
                "exact": False,
                "nth": 0,
            },
            "fallbacks": [
                {
                    "strategy": "css",
                    "name": None,
                    "role": None,
                    "selector": "#holdDetail",
                    "exact": False,
                    "nth": None,
                }
            ],
        },
    },
}

CHECKPOINTS = {
    "text_present": "Hold released",
    "url_matches": r"^https://demo\.testfire\.net/bank/.*$",
    "a11y_node_present": "region:Hold detail",
}
for kind, expected_value in CHECKPOINTS.items():
    variant = deepcopy(MAXIMAL)
    variant["checkpoint"]["kind"] = kind
    variant["checkpoint"]["expected"] = expected_value
    round_trip(variant, f"maximal[{kind}]")


# --------------------------------------------------------------------------------------
# Rejection cases: one per validation rule.
# --------------------------------------------------------------------------------------

def mutate(base: dict, fn) -> dict:
    bad = deepcopy(base)
    fn(bad)
    return bad


# Rule 1 - schema_version must be exactly "1.0".
rejects(mutate(EXAMPLE_SOURCE, lambda a: a.update(schema_version="1.1")), "schema_version 1.1")
rejects(mutate(EXAMPLE_SOURCE, lambda a: a.update(schema_version="2")), "schema_version 2")
# ...and it is refused on load from disk too, not only on direct model validation.
with tempfile.TemporaryDirectory() as tmp:
    stale = Path(tmp) / "stale.json"
    stale.write_text(
        json.dumps(mutate(EXAMPLE_SOURCE, lambda a: a.update(schema_version="0.9"))),
        encoding="utf-8",
    )
    try:
        load_artifact(stale)
    except ValueError:
        rejections.append("schema_version 0.9 on load_artifact")
    else:
        raise AssertionError("load_artifact accepted schema_version 0.9")

# Rule 2 - action/field coherence.
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][0].pop("url")), "navigate without url")
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["steps"][0].update(locator=a["steps"][1]["locator"])),
    "navigate carrying a locator",
)
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][1].pop("locator")), "click without locator")
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][2].pop("value")), "fill without value")
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][3].pop("value")), "select without value")
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][1].update(value="x")), "click carrying a value")
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][6].update(value="x")), "read carrying a value")

# Rule 3 - step indices contiguous from 0, in order.
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][2].update(index=1)), "duplicate step index")
rejects(mutate(EXAMPLE_SOURCE, lambda a: a["steps"][7].update(index=9)), "gap in step indices")
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["steps"].insert(0, a["steps"].pop(1))),
    "steps out of index order",
)
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: [s.update(index=s["index"] + 1) for s in a["steps"]]),
    "step indices not starting at 0",
)

# Rule 4 - every {{template}} in a step value resolves to a declared input param.
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["steps"][2].update(value="{{membr_id}}")),
    "value templating an undeclared param",
)
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["steps"][2].update(value="{{member_id}}-{{nope}}")),
    "value mixing a declared and an undeclared param",
)

# Rule 5 - every output must come from a real step whose action is "read".
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["outputs"][0].update(from_step=4)),
    "output sourced from a click step",
)
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["outputs"][0].update(from_step=99)),
    "output sourced from a nonexistent step",
)

# Rule 6 - locator strategies require the field they locate by.
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["steps"][2]["locator"].update(primary={"strategy": "css"})),
    "css locator without a selector",
)
rejects(
    mutate(
        EXAMPLE_SOURCE,
        lambda a: a["steps"][2]["locator"].update(primary={"strategy": "role", "name": "Search"}),
    ),
    "role locator without a role",
)
rejects(
    mutate(EXAMPLE_SOURCE, lambda a: a["steps"][2]["locator"].update(primary={"strategy": "label"})),
    "label locator without a name",
)
rejects(
    mutate(
        EXAMPLE_SOURCE,
        lambda a: a["steps"][1]["locator"]["fallbacks"].append({"strategy": "css"}),
    ),
    "css fallback without a selector",
)

# Rule 7 - a secret param may only be referenced as the whole value, never inlined.
rejects(
    mutate(MAXIMAL, lambda a: a["steps"][2].update(value="pin={{staff_pin}}")),
    "secret param embedded in a larger value",
)
rejects(
    mutate(MAXIMAL, lambda a: a["steps"][2].update(value="{{staff_pin}}{{member_id}}")),
    "secret param concatenated with another template",
)

print(
    f"PASS: schema self-check - example artifact at {EXAMPLE_PATH} validates "
    f"({len(example.steps)} steps, {len(example.inputs)} inputs, {len(example.outputs)} outputs, "
    f"a11y-first locators with css fallbacks); 4 lossless round-trips (example + maximal x3 "
    f"checkpoint kinds) with byte-identical re-serialization; {len(rejections)} rejection cases "
    f"across all 7 validation rules"
)
