# interface.ai Computer-Use Automation — Phases

Each phase is a loop: implement → verify against done-criteria → fix → re-check → proceed. Never skip ahead.

## Phase 1: Setup + guardrail scaffolding
- Goal: repo skeleton, allowlist config format, redaction utility, logging setup
- Done-criteria:
  - [ ] Allowlist blocks an out-of-scope domain/action in a manual test
  - [ ] Redaction utility strips a sample secret/PII string from a log line
- Depends on: none

## Phase 2: Artifact schema
- Goal: typed, versioned `CapabilityArtifact` (steps, locators, input params, output shape, checkpoint)
- Done-criteria:
  - [ ] Schema round-trips through serialize/deserialize with no data loss
  - [ ] Schema validated against at least one hand-written example artifact
- Depends on: Phase 1

## Phase 3: Discovery agent loop
- Goal: Claude-driven observe (a11y tree) → decide → act (Playwright) loop against the chosen sandbox target, completing one real multi-step goal
- Done-criteria:
  - [ ] One genuine LLM-driven run completes a real goal end-to-end against a live sandbox
  - [ ] Run transcript compiles into a valid `CapabilityArtifact` per Phase 2's schema
- Depends on: Phase 2

## Phase 4: Deterministic replay + error taxonomy
- Goal: replay engine that executes a saved artifact with no LLM calls, using a11y-tree locators with fallback strategy, classifying outcomes into business/recoverable/hard-failure
- Done-criteria:
  - [ ] Replay of the Phase 3 artifact succeeds and returns declared outputs
  - [ ] A deliberately bad input produces a correctly classified business-outcome or hard-failure result, not a crash
- Depends on: Phase 3

## Phase 5: Evidence/logging
- Goal: structured log of every discovery and replay step; screenshot captured on any failure
- Done-criteria:
  - [ ] Discovery run and replay run each produce a complete structured log under `/evidence/`
  - [ ] The error-case replay from Phase 4 has an accompanying screenshot
- Depends on: Phase 4

## Phase 6: Human escalation + handoff
- Goal: detect a stuck state, raise an intervention request with context, expose the live session for manual control (mock operator surface), resume after
- Done-criteria:
  - [ ] A simulated stuck condition triggers an intervention request carrying goal/step/state/reason
  - [ ] Control transfer to the mock operator surface and back to automation is demonstrated on the *same* session, with the human's actions logged
- Depends on: Phase 5

## Phase 7: Documentation + final evidence package
- Goal: README.md (setup + demo commands), REPORT.md (all 7 required headings), final `/evidence/` package
- Done-criteria:
  - [ ] README's documented commands run clean on a fresh clone
  - [ ] REPORT.md covers Architecture, Artifact schema, Determinism & error handling, Heterogeneity & multi-tenant, Escalation & handoff, Safety, Cuts
  - [ ] `/evidence/` contains: saved artifact, discovery-run log, replay-run log, one error-case replay log
- Depends on: Phase 6
