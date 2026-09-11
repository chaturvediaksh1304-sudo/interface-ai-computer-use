# interface.ai Computer-Use Automation — Memory

Live state of the project. Updated after every phase.

**Last updated:** 2026-09-11 — Phases 3 and 4 complete; outputs now return values rather than labels. A live LLM-driven discovery run produces
an artifact that replays deterministically three times running, with no model in the replay path.
Phases 1-2 committed (`2773bca`, `2462511`); Phases 3-4 are **uncommitted**. No remote exists.

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

**Phase 2 — Artifact schema.** Both done-criteria met, verified independently rather than on
the subagents' word.

- `artifact/schema.py` — Pydantic v2 models: `A11yLocator`, `Locator`, `InputParam`,
  `OutputField`, `Step`, `Checkpoint`, `CapabilityArtifact`, plus `save_artifact` /
  `load_artifact`. Seven validation rules enforced (see decisions below).
- `artifacts/example_member_lookup.v1.json` — hand-written 8-step search → detail → read
  capability, 2 inputs, 2 outputs, accessibility-tree-first locators with css fallbacks.
- `artifact/check_schema.py` — 4 lossless round-trips with byte-identical re-serialization,
  the example artifact validating clean, and 24 rejection cases across all 7 rules.
- Mutation-tested: neutering rules 1, 2, 3 and 6 in turn each made the check fail, so it has
  teeth. `schema.py` restored byte-identical afterwards.

**Phase 3 — Discovery loop.** Built: `agent/browser.py` (a11y observe + act), `agent/decide.py`
(the LLM call), `agent/discover.py` (loop + artifact compiler), `agent/llm.py` (backend adapter).
A genuine LLM-driven run against the live sandbox completed the goal end-to-end — logged in,
selected an account, read the balance — and compiled into a schema-valid artifact at
`artifacts/altoro.account_balance.v1.json`. Both stated done-criteria are literally met.

**Phase 4 — Replay + error taxonomy.** Built: `replay/outcomes.py`, `replay/engine.py`. Both
done-criteria pass against the Phase 2 example artifact: clean 8-step replay returning two
typed outputs, identical on re-run, bad input classified as a business outcome rather than a
crash, fallback recovery reported, exhausted fallbacks hard-failing with a screenshot on disk.

## In progress

Nothing in flight. Phases 3 and 4 both meet their stated done-criteria:

- A genuine LLM-driven run (local qwen2.5:14b via Ollama) signs in to the live sandbox, selects
  an account, reads the balance, and compiles to a schema-valid artifact.
- That artifact replays with no LLM anywhere, returning its declared outputs, and gave an
  identical result on three consecutive runs.
- A deliberately bad input is classified as a HARD_FAILURE at step 5 with expected/observed and
  a screenshot at `evidence/replay_altoro.png` — a structured result, not a crash.

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
- **Work split across subagents by module boundary** per Rules.md, with the import contract
  fixed in advance so parallel agents couldn't collide on the same file — `logging_setup →
  redaction` in Phase 1, and the full model contract in Phase 2. Zero contract mismatches in
  both phases.
- **`extra="forbid"` on every artifact model.** Pydantic's default silently drops unknown
  keys, which is precisely the data loss the "no data loss" criterion exists to catch.
- **One artifact-level checkpoint, no per-step expectations.** PRD says "checkpoint/success
  condition" singular. Per-step assertions would help determinism but belong to Phase 4's
  error taxonomy; adding them now would be guessing. Expect a `schema_version` bump if Phase 4
  wants them.
- **Step values carry `{{param}}` templates, never literal input values, and a secret param
  may appear only as a bare `{{name}}`** — never composed into a larger string. This makes it
  structurally impossible to bake a secret into a saved artifact, rather than relying on
  redaction to catch it after the fact.
- **Unknown `schema_version` is rejected loudly with no migration machinery.** Migrations are
  for when a second version exists.
- **Pydantic install was approved explicitly** as a one-off PyPI fetch. Playwright and the
  Anthropic SDK are still uninstalled and need separate approval.

## Deviations from plan

- **Repo root directory name has a trailing space** (`interface.ai `). Not a choice — it is
  how the folder already exists on disk. Every shell path must stay quoted.
- Planning docs live in `PRD_files/`, not at the repo root as the docs' own examples imply.
- No deviation from Phases.md scope through Phase 2: no Playwright, no Anthropic SDK, no agent
  loop, no replay engine.
- **Standing user constraint, added after Phase 1:** no browser, no Playwright, no live network
  request against the sandbox target, and no git operation touching a remote — each requires
  explicit go-ahead, every time, even mid-phase. Local edits, local test runs and local commits
  are fine. This gates Phase 3 and Phase 6, both of which need a live browser.
- The example artifact's element names and css fallbacks are plausible but **not verified
  against the live sandbox** — the site was never visited, per the constraint above. Phase 3's
  real discovery run produces the authoritative artifact.

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

## Known issues, not yet fixed

- **Replay of this capability is intermittent, but it fails honestly.** Roughly one run in five
  the sandbox does not complete the login click and the browser stays on `login.jsp`; the
  account page never appears, so there is genuinely no GO button and replay returns a
  HARD_FAILURE at step 4 with a screenshot. That is the correct classification for the state
  the page is actually in, and it is environmental rather than a locator defect — but it does
  mean the capability is not reliable end-to-end against this host.

  A bounded recovery now exists for it: when a locator goes missing immediately after a click
  that left the page where it was, the engine re-runs that click once and retries the step,
  counted against the taxonomy's existing retry ledger so it cannot loop. It is proven by
  `replay.check_engine` check (i) against a session whose clicks only land when repeated.
  **It has not yet been seen to fire against the live site** — six consecutive live runs all
  passed without the flaky condition occurring, so there is no live evidence either way.
- **Duplicate outputs.** In the current artifact `cell` and `available_balance` both resolve to
  the same cell (`nth=13`), because the model read it once directly and once via a label the
  retargeting then followed to the same place. Harmless but untidy; the compiler does not
  notice two outputs pointing at one element.
- `cell` is still a weak output name — it comes from a read that landed straight on a value
  with no label to borrow a name from.
- **`BUSINESS_SIGNALS` is still the 8 generic phrases** written before the real UI was known.
  It should be replaced with Altoro's actual empty-state wording.
- The example artifact from Phase 2 still describes a member-search flow that was guessed, not
  observed. It validates and replays in tests, but it is not a real capability.

## Next

Phase 5 (evidence/logging) is largely satisfied already by work done in passing — structured
logs exist for every discovery and replay run and a screenshot is captured on failure; it needs
a review pass against its criteria rather than new building. Phase 6 (human escalation) is
untouched, and `StuckError` already carries the goal/step/url/reason payload an intervention
request needs.

Superseded plan — Phase 3 — Claude-driven discovery loop: observe (a11y tree) → decide → act (Playwright) against
the sandbox, completing one real multi-step goal and compiling the transcript into a valid
`CapabilityArtifact`. Done-criteria: one genuine LLM-driven run end-to-end against a live
sandbox, and a transcript that compiles to a valid artifact.

**Phase 3 is blocked pending explicit approval** for three things the standing constraint
covers: installing Playwright and the Anthropic SDK (PyPI fetch), downloading Playwright's
browser binaries, and actually driving a browser against `demo.testfire.net`. It also needs an
`ANTHROPIC_API_KEY`. Nothing about Phase 3 can proceed offline.
