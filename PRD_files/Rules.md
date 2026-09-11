# interface.ai Computer-Use Automation — Rules

## Libraries
- Prefer: Playwright (accessibility-tree APIs), Pydantic (typed artifact schema + versioning), Anthropic Python SDK, stdlib `logging` for structured logs
- Avoid: Selenium/Puppeteer (Playwright's a11y-tree support is stronger), any queue/worker framework (Celery, etc. — out of scope per brief Section 7), any ORM/DB (unnecessary at this scale)

## Error handling
Fail loud everywhere except the three explicitly-modeled replay outcomes. The replay contract must distinguish:
- **Business outcome** (e.g. "no such member") — a valid, structured result returned to the caller, not an exception
- **Recoverable condition** (e.g. dismiss a known interstitial, retry a transient load) — handled internally with bounded retries, logged, then proceed
- **Hard failure** (unexpected state, locator not found after fallbacks, timeout past bound) — stop immediately, return a structured error with step index, expected vs. observed state, and evidence pointer (screenshot)
Never silently swallow an error or blindly proceed past an unverified checkpoint.

## Testing
Unit tests for the artifact schema (serialization/versioning round-trips) and the replay engine's error-classification logic (business outcome vs. recoverable vs. hard failure, given mocked step outcomes). The LLM-driven discovery loop and live browser interaction are integration-only and not unit-tested — too much to mock meaningfully; instead they're proven via the real evidence run.

## Requires explicit approval before doing
- Installing any new dependency not already listed in this file
- Widening the allowlist (new domains/routes/action types)
- Any change to how secrets/PII are redacted or logged
- Committing anything under `/evidence/` that might contain unredacted sensitive-looking data — flag for review first

## Build approach (Claude Code execution)
- **Multi-subagent build, entire project.** Split ALL work across subagents by module boundary (guardrails/allowlist, artifact schema, discovery agent loop, replay engine, evidence/logging, escalation/handoff, docs) — not just the UI piece. Each subagent owns its module's Phases.md done-criteria and reports back before integration into the main branch.
- **Token efficiency, entire project.** Run `/caveman` mode across all subagent reasoning, status updates, and scaffolding work for every phase — full technical accuracy required, just compressed. Exception: final user-facing text (README.md, REPORT.md content, code comments meant for human review) stays in normal prose — don't caveman-compress what a reviewer reads.
- **`/karpathy-guidelines` applies to every subagent, every phase** — see Execution discipline below. No subagent is exempt.
- **Mocked operator surface (Phase 6) must not look AI-generated.** Aksh has UI/UX plugins configured in Claude Code — use them for the mock operator console instead of a default unstyled scaffold. This is the one screen a reviewer actually looks at.

## Execution discipline (Karpathy guidelines — always included)
1. **Think before coding** — state assumptions explicitly; if multiple interpretations exist, present them, don't silently pick; stop and ask if genuinely unclear.
2. **Simplicity first** — minimum code that solves the task; no speculative abstraction, no unrequested configurability, no error handling for impossible cases.
3. **Surgical changes** — touch only what the task requires; don't refactor or "improve" adjacent code; match existing style; remove only orphans your own change created.
4. **Goal-driven execution** — every task starts with a stated, checkable success criterion; work in verify-then-proceed loops (implement → check against criterion → fix → re-check) rather than declaring done by feel.

## Looping mandate
Work phase-by-phase per Phases.md. Do not attempt multiple phases in a single pass. Each phase's done-criteria must be verifiably met before starting the next. With subagents, this still holds per-module — a subagent doesn't start its next phase until its own done-criteria are verified.
