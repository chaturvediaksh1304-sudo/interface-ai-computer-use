"""Self-check for the replay error taxonomy.

Run from the repo root:  python3 -m replay.check_outcomes

Covers exactly what Rules.md's Testing section asks of this module: "the replay engine's
error-classification logic (business outcome vs. recoverable vs. hard failure, given
mocked step outcomes)". Every step outcome here is a literal -- no browser, no network,
no model, no artifact on disk. The classifier is pure logic and this file proves it by
never leaving the process.

The cases, in the order they appear below:

  (a) each of the three outcomes is produced from a representative mocked step outcome
  (b) a "no such member" empty-result state classifies BUSINESS and is RETURNED, not raised
  (c) locator-not-found-after-all-fallbacks is HARD_FAILURE and carries step_index,
      expected, observed and evidence
  (d) constructing a HARD_FAILURE without evidence or step_index raises -- the enforcement
      is real, not a docstring
  (e) a recoverable condition inside the bound proceeds and records the retry; exceeding
      the bound escalates to HARD_FAILURE
  (f) a checkpoint failure with NO recognised business signal is HARD_FAILURE, not BUSINESS

(f) is the one that matters. Every other case here would still pass if the classifier
simply called every failed checkpoint a business outcome -- which would report real
breakage as a confident domain answer, the single conflation Rules.md forbids.
"""

from replay.outcomes import (
    BUSINESS_SIGNALS,
    RETRY_LIMIT,
    Condition,
    Outcome,
    ReplayHardFailure,
    ReplayResult,
    business_signal,
    classify,
    success,
)

CAP = "altoro.member_account_lookup"
SHOT = "evidence/shots/step7.png"


def raises(fn, *args, **kwargs) -> str:
    """Assert the call raises ValueError, and hand back the complaint for inspection."""
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"expected ValueError from {getattr(fn, '__name__', fn)}")


# ---------------------------------------------------------------------------------------
# (a) All three outcomes are reachable from a representative mocked step outcome.
# ---------------------------------------------------------------------------------------

# Happy path.
ok = success(CAP, {"account_balance": "1,204.55", "account_status": "Open"})
assert ok.outcome is Outcome.BUSINESS, ok.outcome
assert ok.capability_id == CAP
assert ok.outputs == {"account_balance": "1,204.55", "account_status": "Open"}
# A success needs none of the hard-failure fields, and carries no retries.
assert ok.step_index is None and ok.evidence is None and ok.retries == []

# Business: checkpoint missed, page affirmatively said there was nothing.
biz = classify(
    Condition.CHECKPOINT_FAILED,
    step_index=4,
    expected="Account summary",
    observed="Search results: No such member with that ID.",
    evidence=SHOT,
    capability_id=CAP,
)
assert biz.outcome is Outcome.BUSINESS, biz.outcome

# Recoverable: a known interstitial, first sighting.
rec = classify(
    Condition.INTERSTITIAL,
    step_index=2,
    expected="Member search form",
    observed="Scheduled maintenance notice - dismiss to continue",
    evidence=SHOT,
    capability_id=CAP,
    retries=[],
)
assert rec.outcome is Outcome.RECOVERABLE, rec.outcome

# Hard failure: a timeout past the bound.
hard = classify(
    Condition.TIMEOUT,
    step_index=5,
    expected="Account detail loaded",
    observed="still on the search page after 30s",
    evidence=SHOT,
    capability_id=CAP,
)
assert hard.outcome is Outcome.HARD_FAILURE, hard.outcome

assert {ok.outcome, biz.outcome, rec.outcome, hard.outcome} == set(Outcome), (
    "all three outcomes must be reachable"
)


# ---------------------------------------------------------------------------------------
# (b) "No such member" is a RETURNED value, never an exception.
# ---------------------------------------------------------------------------------------

# classify itself returns rather than raising -- proven by the assignment above already
# having succeeded, and by the outcome and its provenance.
assert isinstance(biz, ReplayResult)
assert biz.outcome is Outcome.BUSINESS
assert biz.capability_id == CAP
assert biz.step_index == 4
assert "no such member" in biz.detail, biz.detail
assert biz.outputs == {}, "an empty-result business outcome declares no outputs"

# The caller can tell "found it" from "the bank says no" without catching anything:
# same outcome, different outputs.
assert ok.outcome is biz.outcome and bool(ok.outputs) != bool(biz.outputs)

# Every phrase in the shipped signal list is recognised, and each one routes to BUSINESS
# rather than to a hard failure.
for phrase in BUSINESS_SIGNALS:
    page = f"Results\n{phrase.capitalize()}.\nTry another search."
    assert business_signal(page) == phrase, phrase
    assert (
        classify(
            Condition.CHECKPOINT_FAILED,
            step_index=4,
            expected="Account summary",
            observed=page,
            evidence=SHOT,
            capability_id=CAP,
        ).outcome
        is Outcome.BUSINESS
    ), phrase

# Matching is case-insensitive and substring-based, because real UIs shout and pad.
assert business_signal("MEMBER NOT FOUND") == "member not found"
assert business_signal("  ...No Results Found for 999...  ") == "no results found"
assert business_signal(None) is None
assert business_signal("") is None

# A hard failure result may not be smuggled out as a business outcome, and the
# exception refuses to carry anything but a hard failure.
assert raises(ReplayHardFailure, biz)
assert raises(ReplayHardFailure, rec)
raised = ReplayHardFailure(hard)
assert raised.result is hard
assert str(raised) == hard.detail

# An already-classified hard failure unwinding through the engine keeps its verdict and
# its original step -- re-classifying it would lose the step that actually broke.
assert classify(raised, step_index=99, capability_id=CAP) is hard


# ---------------------------------------------------------------------------------------
# (c) Locator not found after all fallbacks: HARD_FAILURE with full debugging payload.
# ---------------------------------------------------------------------------------------

lost = classify(
    Condition.LOCATOR_NOT_FOUND,
    step_index=3,
    expected="role=button name='Look Up Member' (primary), 2 fallbacks",
    observed="a11y tree at step 3 exposes no matching node; 0 of 3 locators resolved",
    evidence="evidence/shots/locator_not_found_step3.png",
    capability_id=CAP,
)
assert lost.outcome is Outcome.HARD_FAILURE, lost.outcome
assert lost.step_index == 3
assert lost.expected and lost.observed and lost.evidence
assert lost.evidence == "evidence/shots/locator_not_found_step3.png"
assert "after every declared fallback" in lost.detail, lost.detail

# A locator failure is never a business outcome, even when the page happens to be showing
# an empty-result phrase. The signal only ever qualifies a failed *checkpoint*: if we
# could not find the element we were told to act on, we did not complete the interaction
# that the phrase would be an answer to.
assert (
    classify(
        Condition.LOCATOR_NOT_FOUND,
        step_index=3,
        expected="role=button name='Look Up Member'",
        observed="No results found",
        evidence=SHOT,
        capability_id=CAP,
    ).outcome
    is Outcome.HARD_FAILURE
)


# ---------------------------------------------------------------------------------------
# (d) The required-field enforcement is real.
# ---------------------------------------------------------------------------------------

FULL = dict(
    outcome=Outcome.HARD_FAILURE,
    capability_id=CAP,
    outputs={},
    detail="step 3: locator not found after every declared fallback.",
    step_index=3,
    expected="role=button name='Look Up Member'",
    observed="no matching node",
    evidence=SHOT,
)

# The complete form constructs fine -- so the rejections below are about the missing
# field, not about the shape of the call.
ReplayResult(**FULL)

for missing in ("step_index", "expected", "observed", "evidence"):
    partial = dict(FULL)
    partial[missing] = None
    message = raises(ReplayResult, **partial)
    assert missing in message, (missing, message)
    assert "evidence pointer" in message, message

# Whitespace is not evidence.
assert "evidence" in raises(ReplayResult, **{**FULL, "evidence": "   "})
# ...but step_index 0 is a real answer and must survive the check.
assert ReplayResult(**{**FULL, "step_index": 0}).step_index == 0

# Two fields missing at once are both named, so one fix-and-retry cycle finds them all.
both = raises(ReplayResult, **{**FULL, "evidence": None, "step_index": None})
assert "evidence" in both and "step_index" in both, both

# The other two outcomes carry no such requirement -- they are not debugging payloads.
ReplayResult(outcome=Outcome.BUSINESS, capability_id=CAP, outputs={}, detail="no such member")
ReplayResult(outcome=Outcome.RECOVERABLE, capability_id=CAP, outputs={}, detail="dismissed")

# The outcome set is closed: an invented fourth bucket is refused at construction.
assert raises(ReplayResult, outcome="partial_success", capability_id=CAP, outputs={}, detail="x")


# ---------------------------------------------------------------------------------------
# (e) Bounded retries: inside the bound it proceeds and records; past it, it escalates.
# ---------------------------------------------------------------------------------------

log: list[dict] = []
for attempt in range(1, RETRY_LIMIT + 1):
    result = classify(
        Condition.SLOW_LOAD,
        step_index=6,
        expected="Account detail loaded",
        observed="spinner still present",
        evidence=SHOT,
        capability_id=CAP,
        retries=log,
    )
    assert result.outcome is Outcome.RECOVERABLE, (attempt, result.outcome)
    assert f"attempt {attempt} of {RETRY_LIMIT}" in result.detail, result.detail
    # The retry is recorded -- "logged, then proceed", not silently swallowed.
    assert len(result.retries) == attempt, result.retries
    assert result.retries[-1]["condition"] == "slow_load"
    assert result.retries[-1]["step_index"] == 6
    assert result.retries[-1]["attempt"] == attempt

assert len(log) == RETRY_LIMIT, log

# One more sighting of the same condition at the same step is past the bound: escalate.
escalated = classify(
    Condition.SLOW_LOAD,
    step_index=6,
    expected="Account detail loaded",
    observed="spinner still present",
    evidence=SHOT,
    capability_id=CAP,
    retries=log,
)
assert escalated.outcome is Outcome.HARD_FAILURE, escalated.outcome
assert f"bound is {RETRY_LIMIT}" in escalated.detail, escalated.detail
# The escalated failure still carries the full debugging payload plus the retry history,
# so the log shows what was papered over before it gave up.
assert escalated.step_index == 6 and escalated.evidence == SHOT
assert len(escalated.retries) == RETRY_LIMIT
# Escalating consumed no further budget -- a hard failure is not a retry.
assert len(log) == RETRY_LIMIT, log

# The bound is per (step, condition): a different step, and a different condition at the
# same step, each start fresh rather than inheriting a spent budget.
assert (
    classify(
        Condition.SLOW_LOAD, step_index=7, observed="spinner", evidence=SHOT, retries=log
    ).outcome
    is Outcome.RECOVERABLE
)
assert (
    classify(
        Condition.STALE_ELEMENT, step_index=6, observed="node detached", evidence=SHOT, retries=log
    ).outcome
    is Outcome.RECOVERABLE
)

# All three recoverable conditions behave the same way on first sighting.
for condition in (Condition.INTERSTITIAL, Condition.SLOW_LOAD, Condition.STALE_ELEMENT):
    assert (
        classify(condition, step_index=1, observed="x", evidence=SHOT, retries=[]).outcome
        is Outcome.RECOVERABLE
    ), condition


# ---------------------------------------------------------------------------------------
# (f) ANTI-CONFLATION: a failed checkpoint with no recognised signal is breakage.
# ---------------------------------------------------------------------------------------

# The realistic breakage: the page rendered, the checkpoint missed, and the page says
# nothing that means "we looked and found nothing". A classifier that shrugged and called
# this a business outcome would hand the caller a confident "no such member" for what is
# actually a renamed heading or a half-rendered table.
broken = classify(
    Condition.CHECKPOINT_FAILED,
    step_index=7,
    expected="Account summary for member 800000",
    observed="Member Overview\nBalance unavailable at this time.",
    evidence=SHOT,
    capability_id=CAP,
)
assert broken.outcome is Outcome.HARD_FAILURE, (
    "a failed checkpoint with no recognised business signal must be breakage, "
    f"got {broken.outcome}"
)
assert "no recognised business signal" in broken.detail, broken.detail
assert broken.step_index == 7 and broken.expected and broken.observed and broken.evidence

# The same shape across the states that most tempt a false "the domain said no": an empty
# page, a rendering error, an auth wall, a generic failure, and a near-miss phrase that is
# deliberately not in the signal list.
for label, page in [
    ("empty page", ""),
    ("whitespace only", "   \n  "),
    ("server error", "500 Internal Server Error"),
    ("auth wall", "Your session has expired. Please sign in again."),
    ("generic failure", "An error occurred processing your request."),
    ("near miss", "No such luck."),
    ("wrong page entirely", "Online Banking Home"),
]:
    verdict = classify(
        Condition.CHECKPOINT_FAILED,
        step_index=7,
        expected="Account summary",
        observed=page,
        evidence=SHOT,
        capability_id=CAP,
    )
    assert verdict.outcome is Outcome.HARD_FAILURE, (label, verdict.outcome)
    # A blank page is a real observation, so it survives into the result as a readable
    # marker rather than as an empty string that looks like a dropped field.
    if not page.strip():
        assert verdict.observed == "<no readable content on the page>", (label, verdict.observed)

# The distinction that keeps the above from weakening the enforcement: a blank page is
# something the caller observed, whereas None means the caller never said. classify does
# not paper over the second -- the result refuses to be constructed.
assert "observed" in raises(
    classify, Condition.CHECKPOINT_FAILED, step_index=7, expected="Account summary", evidence=SHOT
)

# Unknown means loud: an unrecognised condition, an arbitrary exception, and a garbage
# string all land in HARD_FAILURE rather than being quietly absorbed.
for label, thing in [
    ("unexpected state", Condition.UNEXPECTED_STATE),
    ("arbitrary exception", RuntimeError("browser crashed")),
    ("unknown string", "banana"),
    ("None", None),
]:
    verdict = classify(
        thing, step_index=8, expected="anything", observed="nothing", evidence=SHOT
    )
    assert verdict.outcome is Outcome.HARD_FAILURE, (label, verdict.outcome)

# A browser timeout caught as an exception maps to the same bucket as the named condition.
timed_out = classify(
    TimeoutError("waiting for locator"),
    step_index=8,
    expected="Account detail loaded",
    observed="navigation never settled",
    evidence=SHOT,
)
assert timed_out.outcome is Outcome.HARD_FAILURE
assert "timed out past the bound" in timed_out.detail, timed_out.detail


print(
    f"PASS: replay outcome taxonomy self-check - 3 closed outcomes all reachable; "
    f"{len(BUSINESS_SIGNALS)} business signals each returned (never raised) as BUSINESS; "
    f"locator-not-found and 11 unexplained/unknown states classified HARD_FAILURE with "
    f"step_index+expected+observed+evidence enforced at construction (4 missing-field "
    f"rejections + whitespace + closed-set); retries bounded at {RETRY_LIMIT} per "
    f"(step, condition), recorded, then escalated"
)
