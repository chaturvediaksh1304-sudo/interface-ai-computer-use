# interface.ai Computer-Use Automation — Memory

Live state of the project. Updated after every phase.

**Last updated:** 2026-09-11 — Phase 1 (setup + guardrail scaffolding) complete and verified.
Awaiting confirmation before Phase 2.

---

## Done

**Phase 1 — Setup + guardrail scaffolding.** Both done-criteria met and independently
re-verified from a clean shell (not taken on the subagents' word).

- Repo skeleton per Architecture.md: `agent/ artifact/ replay/ guardrails/ evidence/ artifacts/`,
  plus `requirements.txt`, `.gitignore`, and a `.venv`. `git init` run; **nothing committed yet**.
- `guardrails/allowlist.py` + `allowlist.json` — deny-by-default allowlist over domains,
  URL schemes, path globs, and action types. Raises `AllowlistViolation`; never returns a
  boolean a caller could ignore.
- `guardrails/redaction.py` — `redact(str)` and `redact_obj(obj)`. Seven classes of
  secret/PII, replaced with kind-preserving markers (`[REDACTED:SSN]`) so logs stay debuggable.
- `guardrails/logging_setup.py` — `setup_logging(run_id)`, JSON-lines to stdout and to
  `evidence/<run_id>.jsonl`, with redaction applied as a logging **Filter** on the logger.
- Three runnable self-checks: `python3 -m guardrails.check_{allowlist,redaction,logging}`.
- Integration manual test: allowlist decisions logged through the redacting logger,
  evidence at `evidence/phase1-manual-test.jsonl`, grep confirms zero raw secrets on disk.

## In progress

Nothing. Stopped at the Phase 1 gate by instruction, pending confirmation to start Phase 2
(artifact schema).

## Key decisions + why

- **Phase 1 is stdlib-only.** Allowlist, redaction and logging need nothing beyond `re`,
  `json`, `logging`, `fnmatch`, `urllib.parse`. Playwright/Pydantic/Anthropic are listed in
  `requirements.txt` but deliberately not installed until the phase that first needs them.
- **Allowlist config is JSON, not YAML.** PyYAML is not in Rules.md's approved library list
  and adding it would have required explicit approval for no real gain.
- **Redaction runs as a logging Filter, not a Formatter.** A Filter sits on the logger and
  fires once per record, so every sink is covered by one pass. A Formatter has to be attached
  to each handler individually — add a third sink later, forget it once, and you leak.
- **Subdomain matching is an explicit config flag, default off, and matches on a leading dot.**
  `host == d or host.endswith("." + d)`. This is what stops `evil-example.com` from matching
  an `example.com` entry; the naive `endswith` version is a real vulnerability and the
  self-check was mutation-tested against exactly that mistake.
- **URL scheme is checked, though the brief didn't ask.** Without it `file://` and
  `javascript:` slip past a host-only check. Cheap, and it is a trust boundary.
- **Card detection is Luhn-gated.** It is what separates a real PAN from any other long
  digit run (step ids, timestamps) and keeps the logs from being shredded by false positives.
- **Redaction is keyword-anchored for account numbers and phone numbers.** A bare 6–19 digit
  pattern would eat every timestamp, elapsed_ms and artifact id in the logs. Deliberate
  false-negative tradeoff — see Open items.
- **Work split across three subagents by module boundary** per Rules.md, with the
  `logging_setup → redaction` import contract fixed in advance so parallel agents couldn't
  collide on the same file.

## Deviations from plan

- **Repo root directory name has a trailing space** (`interface.ai `). Not a choice — it is
  how the folder already exists on disk. Every shell path must stay quoted.
- Planning docs live in `PRD_files/`, not at the repo root as the docs' own examples imply.
- No deviation from Phases.md scope: Phase 2+ untouched, no artifact schema, no Playwright,
  no agent loop.

## Open items needing human sign-off

Rules.md requires explicit approval before changing redaction behaviour or widening the
allowlist. Two things are waiting on a decision:

1. **Known redaction gaps**, accepted for now: bare account numbers with no nearby keyword;
   non-Luhn 13–19 digit runs; bare 10-digit phone numbers with no separators; non-US phone
   formats; names, dates of birth and street addresses (not in scope as given); secret values
   under unconventional key names (`"cred": ...`).
2. **Allowlist target is seeded to `demo.testfire.net`** (Altoro Mutual, a public
   IBM-maintained deliberately-vulnerable sandbox bank app, no login required) plus localhost.
   If Phase 3 picks a different sandbox, widening the allowlist needs approval.

## Next

Phase 2 — typed, versioned `CapabilityArtifact` (steps, locators, input params, output shape,
checkpoint) in `artifact/`. Done-criteria: lossless serialize/deserialize round-trip, and
validation against at least one hand-written example artifact. Requires installing Pydantic
(already pre-approved in Rules.md's library list).
