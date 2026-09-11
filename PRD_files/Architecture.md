# interface.ai Computer-Use Automation — Architecture

## Platform
Backend/CLI tool — no UI. Two entry-point commands: `discover` (LLM-driven run) and `replay` (deterministic run).

## Stack
- Frontend: none
- Backend: Python (single sync process, no services/queues — Section 7 of the brief explicitly rewards simplicity here)
- Database: none — artifacts persisted as versioned JSON files on disk (`/artifacts/`); no need for a DB at this scale
- Auth: none (no user-facing surface); target sandbox site requires no login, or a throwaway test account if it does
- Hosting/deploy: local only — this is a take-home, not a deployed service
- Third-party APIs/services: Anthropic API (Claude) for the discovery-run agent loop; Playwright for browser automation via the accessibility tree
- Computer-use mechanism: Playwright + accessibility tree, not raw DOM selectors or screenshot+coordinates — chosen because it's the one mechanism in Section 4's list that still works when the surface has no clean DOM (the brief's own stated common case), and it degrades gracefully to desktop apps later (accessibility tree exists there too)

## Folder structure
```
/agent/          — discovery loop: observe (a11y tree snapshot) → decide (Claude call) → act (Playwright)
/artifact/       — schema definition + serialization (Pydantic models, versioned)
/replay/         — deterministic replay executor, locator/fallback strategy, error taxonomy
/guardrails/     — allowlist enforcement, risky-action policy, redaction
/evidence/       — structured logs, screenshots, saved example artifact, one error-case run
/artifacts/      — saved capability artifacts (JSON)
README.md
REPORT.md
```

## Data flow
`discover(goal, target)` → Claude observes the accessibility tree each step, decides an action, Playwright executes it → on goal completion, the transcript is compiled into a typed `CapabilityArtifact` (steps, locators, params, outputs, checkpoint) and saved to `/artifacts/`.
`replay(artifact_id, params)` → loads the artifact, walks steps using accessibility-tree locators with fallback strategy, asserts the checkpoint, classifies any failure into business-outcome / recoverable / hard-failure, returns a structured result — no LLM call anywhere in this path.
