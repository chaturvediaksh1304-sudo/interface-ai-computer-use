# interface.ai Computer-Use Automation — PRD

## One-liner
A system that lets an LLM discover how to complete a task inside a legacy UI with no API, then locks that discovery into a deterministic, replayable capability an AI agent can invoke without the model in the loop.

## Problem
Banks and credit unions run legacy back-office apps with no API. AI agents need a way to act inside them reliably and cheaply — re-reasoning with an LLM on every invocation is slow, expensive, and non-deterministic. Nothing today turns a one-time LLM-driven UI walkthrough into a reusable, typed, safely replayable capability.

## Target user
Not an end user — this is infrastructure. The consumer is an AI agent (or the engineer wiring one up) that needs to invoke bank-back-office actions as a typed function call instead of reasoning about a UI from scratch each time.

## MVP scope (must-have)
- Goal-driven agent loop: natural-language goal + target → LLM-driven observe/decide/act loop against a real sandbox UI via Playwright + accessibility tree
- Structured, versioned, typed artifact schema capturing steps, locators, input params, output shape, and checkpoint/success condition
- Deterministic replay engine: runs the artifact with no LLM in the loop, using accessibility-tree-based locators with fallbacks
- Explicit error taxonomy in the replay contract: business outcome vs. recoverable condition vs. hard failure
- Safety guardrails: configurable allowlist (domains/routes/action types), risky-action handling, secret/PII redaction in logs and artifacts
- Structured logging + a richer failure signal (screenshot) for observability
- Human-in-the-loop escalation: detect stuck state, raise an intervention request with context, hand off control of the *same* live session to a human, resume after
- Evidence: one real discovery run + one replay run + one replay hitting an error/exceptional state, all under `/evidence/`

## Explicitly out of scope (for now)
- Multi-tenant plumbing, queues, clusters, or any scaling infrastructure — only the design story, not the build
- Desktop-app support — design story only
- Real-time co-browsing operator console — mocked handoff surface is sufficient
- Agent-facing capability catalog/API, code generation from artifacts, confidence scoring, assisted LLM fallback, multi-run stability testing — stretch goals, only if time remains after the core is solid

## Success looks like
A single command runs the agent against a sandbox banking-style flow (search → detail → action), produces a versioned artifact, and a second command replays that artifact deterministically with no LLM call — including one replay run that deliberately hits a bad input/not-found/simulated failure and reports it correctly through the error taxonomy. REPORT.md defends every design decision.
