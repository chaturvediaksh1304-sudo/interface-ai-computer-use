"""Typed, versioned capability artifact — the contract between discovery and replay.

A ``CapabilityArtifact`` is what a one-time, LLM-driven discovery run compiles
down to: the ordered steps, the accessibility-tree locators those steps use,
the inputs the capability accepts, the outputs it returns, and the checkpoint
that proves the run actually succeeded. Replay executes one of these with no
model in the loop, so an artifact that is internally inconsistent is a bug that
must surface here, at load time, rather than halfway through a browser session.

Everything in this module therefore validates eagerly and raises loudly.
Per Rules.md there is no silent coercion and no defaulting around bad input:
unknown fields are rejected rather than dropped, and an unrecognised
``schema_version`` is an error rather than a migration. There is exactly one
schema version today; when a second one exists, that is the moment to write
migration code, not before.

The validation rules enforced here, and where each lives:

1. ``schema_version`` must equal ``SCHEMA_VERSION``  -- CapabilityArtifact._check_version
2. per-action step invariants (locator/value/url)    -- Step._check_action_shape
3. step indices contiguous from 0, in order          -- CapabilityArtifact._check_internal_consistency
4. every ``{{param}}`` resolves to a declared input   -- CapabilityArtifact._check_internal_consistency
5. every output reads from a real ``read`` step      -- CapabilityArtifact._check_internal_consistency
6. locator strategy implies its required field       -- A11yLocator._check_strategy_fields
7. secret params referenced only as bare templates   -- CapabilityArtifact._check_internal_consistency
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

__all__ = [
    "SCHEMA_VERSION",
    "A11yLocator",
    "Locator",
    "InputParam",
    "OutputField",
    "Step",
    "Checkpoint",
    "CapabilityArtifact",
    "save_artifact",
    "load_artifact",
]

SCHEMA_VERSION = "1.0"

# A parameter reference inside a Step.value, e.g. "{{member_id}}". Deliberately
# strict about the inside: no whitespace, no dotted paths, no expressions. A
# template is a name lookup and nothing more, so replay never has to evaluate
# anything.
_TEMPLATE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

# Strategies that identify a node by its accessible name rather than by role or
# a raw selector, and therefore require `name` to be set.
_NAME_STRATEGIES = frozenset({"label", "text", "placeholder", "testid"})


class _Base(BaseModel):
    """Shared config: reject unknown fields instead of quietly discarding them.

    Pydantic's default is to ignore extras, which would let a typo'd or
    stale key disappear silently on load -- exactly the kind of soft failure
    Rules.md forbids on this path.
    """

    model_config = ConfigDict(extra="forbid")


class A11yLocator(_Base):
    """One way to find a node, expressed against the accessibility tree.

    ``css`` is the escape hatch for surfaces whose a11y tree is too thin to
    address; it is intentionally the only strategy that talks about the DOM.
    """

    strategy: Literal["role", "label", "text", "placeholder", "testid", "css"]
    name: str | None = None
    role: str | None = None
    selector: str | None = None
    exact: bool = False
    nth: int | None = None

    @model_validator(mode="after")
    def _check_strategy_fields(self) -> "A11yLocator":
        """Rule 6: a strategy is only meaningful with the field it looks up."""
        if self.strategy == "css" and not self.selector:
            raise ValueError("strategy 'css' requires 'selector'")
        if self.strategy == "role" and not self.role:
            raise ValueError("strategy 'role' requires 'role'")
        if self.strategy in _NAME_STRATEGIES and not self.name:
            raise ValueError(f"strategy {self.strategy!r} requires 'name'")
        return self


class Locator(_Base):
    """A primary locator plus ordered fallbacks, tried in sequence by replay."""

    primary: A11yLocator
    fallbacks: list[A11yLocator] = []


class InputParam(_Base):
    """One argument the capability accepts when invoked."""

    name: str
    type: Literal["string", "number", "bool"]
    required: bool = True
    description: str
    # A secret value must never be written into an artifact or a log. The
    # artifact stores the *reference*; the caller supplies the value at
    # replay time. See CapabilityArtifact._check_internal_consistency.
    secret: bool = False


class OutputField(_Base):
    """One value the capability returns, and where on the page it is read from."""

    name: str
    type: Literal["string", "number", "bool"]
    description: str
    from_step: int
    locator: Locator


class Step(_Base):
    """A single deterministic action in the capability.

    ``value`` is either a literal or a ``"{{param_name}}"`` reference to a
    declared input.
    """

    index: int
    action: Literal["navigate", "click", "fill", "select", "read"]
    description: str
    locator: Locator | None = None
    value: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _check_action_shape(self) -> "Step":
        """Rule 2: each action carries exactly the fields it can actually use.

        A click with a value, or a fill with no locator, means the compiler
        that produced this artifact got something wrong; replay would either
        ignore the field or fail at runtime, and both are worse than failing
        here.
        """
        if self.action == "navigate":
            if not self.url:
                raise ValueError("action 'navigate' requires 'url'")
            if self.locator is not None:
                raise ValueError("action 'navigate' must not carry a 'locator'")
        else:
            if self.locator is None:
                raise ValueError(f"action {self.action!r} requires a 'locator'")
            if self.url is not None:
                raise ValueError(f"action {self.action!r} must not carry a 'url'")

        if self.action in ("fill", "select"):
            if self.value is None:
                raise ValueError(f"action {self.action!r} requires 'value'")
        elif self.value is not None:
            raise ValueError(f"action {self.action!r} must not carry a 'value'")
        return self


class Checkpoint(_Base):
    """The success condition replay asserts before declaring the run complete."""

    kind: Literal["a11y_node_present", "url_matches", "text_present"]
    expected: str
    locator: Locator | None = None


class CapabilityArtifact(_Base):
    """A complete, replayable capability.

    ``version`` is the revision of this particular capability (bumped when the
    underlying UI changes); ``schema_version`` is the revision of the artifact
    format itself. They move independently.
    """

    schema_version: str = SCHEMA_VERSION
    capability_id: str
    version: int = 1
    goal: str
    target: str
    created_at: datetime
    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    checkpoint: Checkpoint

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        """Rule 1: refuse anything but the one version that exists today.

        No migration machinery on purpose. An artifact written by a different
        schema version is not something this code can honestly interpret, and
        guessing would put a wrong action into a real back-office UI.
        """
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {v!r}; this build only reads {SCHEMA_VERSION!r}"
            )
        return v

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> "CapabilityArtifact":
        """Rules 3, 4, 5 and 7 -- the cross-field invariants.

        These cannot live on the individual models because each one needs to
        see the whole artifact: a step cannot know which params are declared,
        and an output cannot know what the step it points at actually does.
        """
        # Rule 3: indices must be contiguous from 0 and in order. Replay walks
        # steps positionally, so a gap or a duplicate means the artifact and the
        # execution order disagree about what step N is.
        actual = [s.index for s in self.steps]
        if actual != list(range(len(self.steps))):
            raise ValueError(
                f"step indices must be contiguous from 0 and in order, got {actual}"
            )

        declared = {p.name: p for p in self.inputs}
        secrets = {name for name, p in declared.items() if p.secret}

        for step in self.steps:
            if step.value is None:
                continue
            referenced = set(_TEMPLATE.findall(step.value))

            # Rule 4: a template that resolves to nothing is a dangling
            # reference -- replay would either substitute nothing or write the
            # literal braces into the page.
            unknown = referenced - declared.keys()
            if unknown:
                raise ValueError(
                    f"step {step.index} references undeclared param(s) "
                    f"{sorted(unknown)}; declared: {sorted(declared)}"
                )

            # Rule 7: no secret literals in artifacts. We cannot check a value
            # we were never given, but we can require that any step touching a
            # secret param does so as a bare "{{name}}" and nothing else. The
            # moment a secret is concatenated into a larger string, part of
            # that string is a literal that someone typed next to a credential
            # -- and the whole point of `secret` is that nothing adjacent to it
            # gets persisted.
            secret_refs = referenced & secrets
            if secret_refs and step.value not in {f"{{{{{n}}}}}" for n in secret_refs}:
                raise ValueError(
                    f"step {step.index} embeds secret param(s) {sorted(secret_refs)} "
                    f'in a composed value; a secret may only appear as a bare "{{{{name}}}}" '
                    f"template, never concatenated with anything else"
                )

        # Rule 5: an output must be read by a step that actually reads.
        for out in self.outputs:
            if not 0 <= out.from_step < len(self.steps):
                raise ValueError(
                    f"output {out.name!r} points at step {out.from_step}, which does not exist"
                )
            action = self.steps[out.from_step].action
            if action != "read":
                raise ValueError(
                    f"output {out.name!r} points at step {out.from_step}, "
                    f"whose action is {action!r}, not 'read'"
                )
        return self


def save_artifact(artifact: CapabilityArtifact, dir: str = "artifacts") -> Path:
    """Write the artifact to ``<dir>/<capability_id>.v<version>.json``.

    Pretty-printed with sorted keys so that two runs producing the same
    capability produce byte-identical files, and so a revision shows up as a
    readable diff rather than a reshuffle.
    """
    path = Path(dir) / f"{artifact.capability_id}.v{artifact.version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = artifact.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_artifact(path) -> CapabilityArtifact:
    """Read and fully validate an artifact from disk.

    Every rule above is enforced here, including the schema version: an
    artifact this build does not understand raises rather than being migrated
    or partially read.
    """
    return CapabilityArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))
