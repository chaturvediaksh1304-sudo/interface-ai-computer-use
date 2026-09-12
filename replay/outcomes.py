"""The replay error taxonomy -- the typed outcome contract every replay returns.

Rules.md defines three, and only three, ways a replay can end, and the whole
value of this module is that the set is closed:

* **business outcome** -- the UI worked and told us something true about the
  domain ("no such member", "no results found"). This is a *valid result*
  handed back to the caller. It is never an exception.
* **recoverable** -- a transient or known-benign condition the engine handles
  itself with a small, bounded number of retries, logs, and then proceeds past.
* **hard failure** -- unexpected state, a locator not found after every
  fallback, a timeout past the bound, a checkpoint that failed with nothing to
  explain it. Replay stops immediately and returns a structured error carrying
  the step index, expected vs. observed state, and an evidence pointer.

The distinction that matters most is the first one. A caller invoking a
capability needs to tell "the automation broke" apart from "the bank says there
is no such member". Those two demand completely different responses -- page an
engineer, versus render an answer to a user -- and a system that conflates them
teaches its callers to ignore both. Rules.md forbids the conflation explicitly,
so this module makes it structurally hard rather than a matter of discipline.

How a business outcome is recognised
------------------------------------
The engine does not get to guess. A business outcome requires *two* things at
once:

1. the artifact's checkpoint assertion failed -- meaning the page rendered and
   we could read it, we simply did not land on the success state; and
2. the observed state contains one of a small, explicit list of marker phrases
   in ``BUSINESS_SIGNALS`` -- the things a back-office UI actually prints when
   it looked and found nothing.

A checkpoint failure *without* a recognised signal is a hard failure, not a
business outcome. That asymmetry is the entire point. Treating every failed
checkpoint as "the domain said no" would let a renamed button, a broken page,
or a half-loaded table masquerade as a confident domain answer -- silent
breakage reported as a successful lookup, which is the worst outcome this
system can produce. Requiring an affirmative, human-authored marker phrase
means the taxonomy only claims a domain answer when the UI actually gave one.

The signal list is deliberately a flat list of phrases rather than a clever
heuristic. It is auditable, a reviewer can see exactly what the system will
accept as "the bank said no", and extending it is a reviewed data change rather
than a model tweak.

Retry bound
-----------
``RETRY_LIMIT`` is small and explicit. A recoverable condition is retried at
most that many times *per step*; the (attempt, reason) pair is recorded on the
result so the log shows what was papered over. Once the bound is spent the same
condition escalates to a hard failure -- an unbounded retry loop is just a
slower way of hanging.

This module is pure logic: no I/O, no browser, no network, no model. It is the
one place that decides which bucket a failure falls into, so it is also the one
place that has to be right.
"""

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "RETRY_LIMIT",
    "BUSINESS_SIGNALS",
    "EMPTY_RESULT_SIGNALS",
    "BLOCKING_SIGNALS",
    "Outcome",
    "Condition",
    "ReplayResult",
    "ReplayHardFailure",
    "business_signal",
    "classify",
    "success",
]

# Bounded retries only. Two attempts is enough to ride out a dismissable
# interstitial or a re-resolve of a stale node; anything that needs more than
# that is not transient, it is broken, and pretending otherwise just delays the
# hard failure while burning wall-clock time in a real back-office session.
RETRY_LIMIT = 2

# The complete list of phrases that mean "the UI looked and there was nothing
# there". Matched case-insensitively as substrings of the observed state. Kept
# short and literal on purpose: every entry here is permission for the system to
# report a failed checkpoint as a successful domain answer, so each one should
# be something a reviewer can point at in the real UI.
# Two kinds of domain answer, because they license different conclusions.
#
# EMPTY_RESULT_SIGNALS mean "the query found nothing". They explain a checkpoint
# that did not match, and nothing more. They do NOT explain a missing element:
# a search button should exist whether or not the last search found anything, so
# a locator failure on a page showing one of these is still breakage.
EMPTY_RESULT_SIGNALS = (
    "no such member",
    "member not found",
    "no member found",
    "no results found",
    "no matching records",
    "no records found",
    "no accounts found",
)

# BLOCKING_SIGNALS mean "this flow cannot continue, and here is why". They DO
# explain a missing element: sign in with bad credentials and the button the next
# step wants was never rendered, because the page the capability expected was
# never reached. Reporting that as breakage leaves the caller unable to tell
# "your credentials are wrong" from "the automation is broken", which Rules.md
# names as the worst failure mode of this system.
#
# The first two entries are verbatim from the sandbox. Keep every entry a full,
# unambiguous phrase: a fragment like "failed" would match half the error pages
# in the world and turn this guard into noise.
# Deliberately narrow. Each entry must be a statement about the INPUT the caller
# supplied or the entity it named -- something the caller can act on. Conditions
# about our own plumbing are excluded even though they also block the flow: an
# expired session or a denied permission means the automation's state decayed,
# which is breakage to be reported or escalated, not an answer from the bank.
# `check_outcomes` asserts exactly that for "your session has expired", and it
# caught an earlier version of this list that wrongly included it.
BLOCKING_SIGNALS = (
    "login failed",
    "this username or password was not found in our system",
    "account closed",
)

# Kept as the union so `business_signal` and anything importing this name still
# see every phrase the system recognises.
BUSINESS_SIGNALS = EMPTY_RESULT_SIGNALS + BLOCKING_SIGNALS


class Outcome(str, Enum):
    """The three, and only three, ways a replay can end."""

    BUSINESS = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class Condition(str, Enum):
    """What the engine observed, before it has been bucketed into an Outcome.

    This is the engine's vocabulary for reporting *what happened*; ``classify``
    owns the mapping from these to an ``Outcome``. Anything the engine cannot
    name -- an arbitrary exception, an unrecognised string -- collapses to
    ``UNEXPECTED_STATE``, which is a hard failure. Unknown means loud.
    """

    CHECKPOINT_FAILED = "checkpoint_failed"
    LOCATOR_NOT_FOUND = "locator_not_found"
    TIMEOUT = "timeout"
    INTERSTITIAL = "interstitial"
    SLOW_LOAD = "slow_load"
    STALE_ELEMENT = "stale_element"
    UNEXPECTED_STATE = "unexpected_state"


# Conditions worth a bounded retry: each is transient or known-benign, and each
# has an obvious "try the same thing again" remedy. Everything not listed here
# is a hard failure on first sight.
_RECOVERABLE_CONDITIONS = frozenset(
    {Condition.INTERSTITIAL, Condition.SLOW_LOAD, Condition.STALE_ELEMENT}
)

# Exceptions the engine may catch from the browser layer, mapped by class name
# so this module stays importable without Playwright and testable without a
# browser. Anything absent from the table is UNEXPECTED_STATE.
_EXCEPTION_CONDITIONS = {
    "TimeoutError": Condition.TIMEOUT,
    "PlaywrightTimeoutError": Condition.TIMEOUT,
}

# Fields a hard failure is useless without. Enforced in ReplayResult.__post_init__.
_HARD_FAILURE_FIELDS = ("step_index", "expected", "observed", "evidence")


def _missing(value) -> bool:
    """True when a required hard-failure field was not really supplied.

    ``step_index`` of 0 is a perfectly good answer, so this tests for None and
    for whitespace-only strings rather than for falsiness.
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


@dataclass
class ReplayResult:
    """The structured result of a replay, whichever way it ended.

    A ``HARD_FAILURE`` may not be constructed without ``step_index``,
    ``expected``, ``observed`` and ``evidence``. That is checked here rather
    than left to the engine's good manners: a hard failure without an evidence
    pointer and an expected-vs-observed pair is unactionable to whoever is
    reading it at 3am, and a contract that merely *asks* for those fields is a
    contract that will eventually be handed a result without them.
    """

    outcome: Outcome
    capability_id: str
    outputs: dict[str, str]
    detail: str
    step_index: int | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: str | None = None
    retries: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Accept the wire spelling ("hard_failure") as well as the member, but
        # refuse anything outside the closed set -- Outcome() raises for the rest.
        self.outcome = Outcome(self.outcome)

        if self.outcome is Outcome.HARD_FAILURE:
            missing = [n for n in _HARD_FAILURE_FIELDS if _missing(getattr(self, n))]
            if missing:
                raise ValueError(
                    f"hard failure is missing required field(s) {missing}; a hard "
                    f"failure must carry step_index, expected, observed and an "
                    f"evidence pointer, or it cannot be debugged"
                )


class ReplayHardFailure(Exception):
    """Raised inside the engine to abort a replay, carrying the structured result.

    This exists so an unwinding failure deep in a step can reach the top of the
    replay without every intermediate frame having to thread a result back by
    hand. It is an internal control-flow device: the engine catches it at the
    boundary and returns ``.result`` to the caller. Business outcomes never
    travel this way -- they are returned, not raised.
    """

    def __init__(self, result: ReplayResult):
        if result.outcome is not Outcome.HARD_FAILURE:
            raise ValueError(
                f"ReplayHardFailure carries hard failures only, got {result.outcome.value!r}; "
                f"business and recoverable outcomes are returned, never raised"
            )
        super().__init__(result.detail)
        self.result = result


def business_signal(observed: str | None, signals: tuple[str, ...] = BUSINESS_SIGNALS) -> str | None:
    """Return the recognised domain phrase in `observed`, or None.

    Case-insensitive substring match. Returning the matched phrase rather than a
    bool means the result can name exactly which marker justified calling this a
    domain answer.

    ``signals`` narrows which phrases count, because what a phrase licenses
    depends on what failed: a checkpoint mismatch is explained by any of them,
    while a missing element is explained only by a blocking one. Defaults to
    every phrase the system recognises.
    """
    if not observed:
        return None
    haystack = observed.lower()
    for phrase in signals:
        if phrase in haystack:
            return phrase
    return None


def _as_condition(exc_or_state) -> Condition:
    """Normalise whatever the engine caught into a Condition. Unknown is loud."""
    if isinstance(exc_or_state, Condition):
        return exc_or_state
    if isinstance(exc_or_state, BaseException):
        return _EXCEPTION_CONDITIONS.get(
            type(exc_or_state).__name__, Condition.UNEXPECTED_STATE
        )
    if isinstance(exc_or_state, str):
        try:
            return Condition(exc_or_state)
        except ValueError:
            return Condition.UNEXPECTED_STATE
    return Condition.UNEXPECTED_STATE


def classify(
    exc_or_state,
    *,
    step_index: int | None,
    expected: str | None = None,
    observed: str | None = None,
    evidence: str | None = None,
    capability_id: str = "",
    retries: list[dict] | None = None,
) -> ReplayResult:
    """Decide which of the three buckets a failure falls into.

    This is the single place that decision is made. The engine reports *what it
    saw* -- a ``Condition``, or the exception it caught -- and gets back a typed
    result; it does not get to form its own opinion about what counts as a
    domain answer.

    Arguments:
        exc_or_state: a ``Condition``, its string spelling, or a caught
            exception. Anything unrecognised is treated as an unexpected state,
            which is a hard failure.
        step_index: which step of the artifact this happened at. Required for a
            hard failure; pass it always.
        expected / observed: the checkpoint's expectation and the state actually
            read off the page. ``observed`` is also what the business-signal
            scan reads, so pass the real page text, not a summary.
        evidence: path to the screenshot captured at the failure. Always pass
            it -- this function cannot invent one, and a hard failure without it
            refuses to be constructed.
        capability_id: carried through onto the result for the caller's logs.
        retries: the running retry log for this replay. Pass the *same* list on
            every call within one replay: ``classify`` reads it to see how much
            of the bound is already spent and appends to it when it grants a
            retry, so the bound cannot be forgotten by a caller who neglects to
            record an attempt.

    Returns a ``ReplayResult``. Note that a business outcome is *returned*, like
    every other outcome -- nothing here raises on the caller's behalf. Raising
    is the engine's decision, via ``ReplayHardFailure``.
    """
    # An already-classified failure unwinding through the engine keeps its
    # original verdict and evidence; re-classifying it would lose the step that
    # actually broke.
    if isinstance(exc_or_state, ReplayHardFailure):
        return exc_or_state.result

    condition = _as_condition(exc_or_state)
    if retries is None:
        retries = []

    # 1. Business outcome: the checkpoint failed, but the page affirmatively
    #    told us the domain answer. Both halves are required.
    # A checkpoint that did not match is explained by either kind of signal. A
    # locator that was not found is explained only by a BLOCKING one -- an
    # empty-result phrase says nothing about why an element is absent, and
    # treating it as an explanation would mask real breakage as a domain answer.
    explains = (
        BUSINESS_SIGNALS
        if condition is Condition.CHECKPOINT_FAILED
        else BLOCKING_SIGNALS
        if condition is Condition.LOCATOR_NOT_FOUND
        else ()
    )
    if explains:
        signal = business_signal(observed, explains)
        if signal is not None:
            return ReplayResult(
                outcome=Outcome.BUSINESS,
                capability_id=capability_id,
                outputs={},
                detail=(
                    f"step {step_index}: the step did not complete ({condition.value}), "
                    f"and the page reported the recognised domain signal {signal!r}. "
                    f"This is a valid domain answer, not a breakage."
                ),
                step_index=step_index,
                expected=expected,
                observed=observed,
                evidence=evidence,
                retries=list(retries),
            )
        # No recognised signal: fall through to the hard failure below. A failed
        # checkpoint on an unexplained page is breakage, and calling it a
        # business outcome would hide it.

    # 2. Recoverable: transient and still inside the bound. The bound is counted
    #    per (step, condition) so a retry spent on a slow load at step 3 does not
    #    consume the budget for a stale element at step 7.
    if condition in _RECOVERABLE_CONDITIONS:
        spent = sum(
            1
            for r in retries
            if r.get("step_index") == step_index and r.get("condition") == condition.value
        )
        if spent < RETRY_LIMIT:
            retries.append(
                {
                    "step_index": step_index,
                    "condition": condition.value,
                    "attempt": spent + 1,
                    "limit": RETRY_LIMIT,
                    "observed": observed,
                }
            )
            return ReplayResult(
                outcome=Outcome.RECOVERABLE,
                capability_id=capability_id,
                outputs={},
                detail=(
                    f"step {step_index}: {condition.value} handled, attempt "
                    f"{spent + 1} of {RETRY_LIMIT}; proceeding."
                ),
                step_index=step_index,
                expected=expected,
                observed=observed,
                evidence=evidence,
                retries=list(retries),
            )
        # Bound exhausted -- escalate. Falls through to the hard failure below.

    # 3. Hard failure: everything else, plus the two fall-throughs above.
    return ReplayResult(
        outcome=Outcome.HARD_FAILURE,
        capability_id=capability_id,
        outputs={},
        detail=_hard_detail(condition, step_index, retries),
        step_index=step_index,
        expected=expected,
        observed=_readable(observed),
        evidence=evidence,
        retries=list(retries),
    )


def _readable(observed: str | None) -> str | None:
    """Turn a legitimately blank observation into something a log can show.

    A blank page is a real and informative observation -- but ``observed=""`` in
    a log reads as a missing field rather than as "the page was empty", and the
    hard-failure check would reject it as one. ``None`` is left alone on
    purpose: it means the caller never told us what it saw, which is a genuine
    contract violation and should still be refused at construction.
    """
    if observed is not None and not observed.strip():
        return "<no readable content on the page>"
    return observed


def _hard_detail(condition: Condition, step_index: int | None, retries: list[dict]) -> str:
    """Say why this is a hard failure, in terms a human reading a log can act on."""
    if condition is Condition.CHECKPOINT_FAILED:
        return (
            f"step {step_index}: checkpoint assertion failed and the observed state "
            f"carried no recognised business signal. Treating as breakage rather "
            f"than a domain answer."
        )
    if condition in _RECOVERABLE_CONDITIONS:
        spent = sum(
            1
            for r in retries
            if r.get("step_index") == step_index and r.get("condition") == condition.value
        )
        return (
            f"step {step_index}: {condition.value} did not clear after {spent} "
            f"retries (bound is {RETRY_LIMIT}); escalating to a hard failure."
        )
    if condition is Condition.LOCATOR_NOT_FOUND:
        return f"step {step_index}: locator not found after every declared fallback."
    if condition is Condition.TIMEOUT:
        return f"step {step_index}: timed out past the bound."
    return f"step {step_index}: unexpected state ({condition.value})."


def success(capability_id: str, outputs: dict[str, str]) -> ReplayResult:
    """The happy path: every step ran and the checkpoint asserted clean.

    Modelled as a business outcome because that is what it is -- the UI worked
    and gave us a true answer about the domain. "Found the member, here are
    their accounts" and "there is no such member" are the same kind of result
    from the caller's point of view: a real answer, arrived at without anything
    breaking. The two are told apart by whether ``outputs`` is populated, not by
    a fourth outcome the taxonomy does not have.
    """
    return ReplayResult(
        outcome=Outcome.BUSINESS,
        capability_id=capability_id,
        outputs=dict(outputs),
        detail=f"replay of {capability_id!r} completed; checkpoint asserted clean.",
    )
