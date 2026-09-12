# interface.ai Computer-Use Automation — Memory

Live state of the project. Updated after every phase.

**Last updated:** 2026-09-11 — **All seven phases complete and submitted-ready.** 19 commits,
pushed to https://github.com/chaturvediaksh1304-sudo/interface-ai-computer-use (public). Working
tree clean, `main` tracking `origin/main`, 11/11 self-checks passing.

---

## Done

All seven phases meet the done-criteria in `PRD_files/Phases.md`, each verified rather than
asserted.

| Phase | Deliverable | Verification |
|---|---|---|
| 1 | Guardrail scaffolding — allowlist, redaction, structured logging | allowlist blocks an out-of-scope domain and action; redaction strips SSN/email/card/token from a log line |
| 2 | `CapabilityArtifact` schema, 7 validation rules | 4 lossless round-trips, byte-identical re-serialisation, 24 rejection cases |
| 3 | LLM-driven discovery loop and artifact compiler | a real run against the live sandbox compiled a schema-valid artifact |
| 4 | Deterministic replay + three-outcome taxonomy | replay returns declared outputs; three consecutive runs identical; both error shapes classified |
| 5 | Evidence and observability | structured logs for every run, screenshot on failure |
| 6 | Human escalation and handoff | intervention raised with full context; operator acted on the *same* live session; control returned |
| 7 | `cli.py`, `README.md`, `REPORT.md`, evidence package | documented commands run clean; REPORT carries the seven mandated headings |

**The shipped capability** is `artifacts/altoro.account_balance.v1.json` — 8 steps, 3 inputs
(`password` secret, `username`, `account_number`), 1 output, checkpoint `a11y_node_present`.
Discovered by a live LLM-driven run against `demo.testfire.net`, replays deterministically with no
model in the path.

**Entry points:** `cli.py discover`, `cli.py replay`, `cli.py handoff-demo`. Exit codes carry the
outcome — 0 success, 1 a modelled failure, 2 misuse.

## In progress

Nothing. The project is complete against the brief and pushed. Remaining work is optional polish,
listed under Known issues.

## Key decisions + why

- **Two halves sharing only a data format.** Discovery is expensive and non-deterministic; replay
  costs nothing per invocation and touches no model. The artifact is the entire contract between
  them.
- **Accessibility tree, not DOM selectors or screenshot coordinates.** It is the one mechanism that
  still works when the surface has no clean DOM — the brief's stated common case — and the same
  concept exists on desktop.
- **`aria_snapshot(mode="ai")`, not `page.accessibility.snapshot()`.** The API `PRD_files/Architecture.md`
  names does not exist in Playwright 1.62. The architectural bet survived; the specific call did not.
- **Refs are discarded at compile time.** Accessibility refs do not survive a document change, so an
  artifact storing them cannot replay. `_locator_for` emits a durable role/name ladder instead.
- **A value becomes a `{{param}}` only when the caller supplied it**, never by guessing from the
  value's shape — otherwise the compiler invents parameters nobody asked for.
- **Secrets are structurally excluded, not filtered.** Rule 7 requires a secret to appear only as a
  bare `{{name}}`, so baking one into an artifact is impossible rather than something redaction has
  to catch afterwards. `_generalise_text` extends the same protection to the model's prose.
- **The primary locator gets a longer timeout than any fallback** (10s vs 2s), and bare positional
  fallbacks are removed from state-changing actions. Both exist because a wrong answer that looks
  like success is worse than an honest failure.
- **Signals are split by what they license.** An empty-result phrase explains a failed checkpoint; a
  blocking phrase ("login failed") also explains a missing element. Neither counts when the page was
  never dependably read.
- **Escalation reuses the live session object.** `escalation/intervention.py` contains no
  `BrowserSession(` — handing back a different session would be a restart, not a handoff. The human
  is not exempt from the allowlist, and a refused command is recorded while the handoff continues.
- **Redaction runs as a logging `Filter`, not a `Formatter`**, so every sink is covered by one pass
  and a future sink cannot leak by omission.
- **Work was split across subagents by module boundary** per `PRD_files/Rules.md`, with the import
  contract fixed in advance each time so parallel agents could not collide. Zero contract mismatches
  across all seven phases.

## Deviations from plan

- **The LLM backend is local Ollama (`qwen2.5:14b-instruct`), not Claude.** No `ANTHROPIC_API_KEY`
  was available. The client is injected, so `build_client("auto")` prefers Anthropic the moment a
  key exists; the loop, compiler, schema and replay are identical either way. Documented in REPORT §1.
- **The allowlist was widened once**, from the paths guessed in Phase 2 to include `/doLogin`, after
  the live run showed the real login POST target. Approved explicitly.
- **Redaction was changed once**, to redact query-string values, after a GET form put filled values
  into URLs that were being logged verbatim. Approved explicitly.
- **The capability runs over plain HTTP** because `demo.testfire.net` presents a certificate that
  expired in June 2026. The allowlist permits both schemes.
- **Repo directory name carries a trailing space** (`interface.ai `). Not a choice — quote every
  shell path.
- **`REPORT-long.md` was created and then deleted.** It predated the business-signal and
  description-leak fixes and so contained claims that had become false; shipping it beside a correct
  short report would have been worse than not shipping it. Full text remains in history at `397b148` (6,281 words). Note that hashes predating the
  `filter-branch` that purged the assignment PDF are gone — `a92e3ee`, cited in an earlier
  commit message, is one of them.

## Known issues, not yet fixed

All are stated openly in REPORT §7 rather than hidden.

- **`redact_obj` mangles a boolean field named `secret`** — `"secret": true` becomes
  `"[REDACTED:SECRET]"`, because the rule matches on the key name. Harmless here, lossy in general.
- **Replay against the public sandbox is intermittent** — roughly one run in five leaves the browser
  on `login.jsp` because the login click does not navigate. A bounded re-navigation recovery exists
  and has been observed firing; the one-in-five figure is anecdotal, not measured.
- **`cell` is a poor output name**, inherited from a read that landed on a value with no nearby label
  to borrow a name from. The compiler does not notice an output is unnamed in any useful sense.
- **`param_names` is empty when a page's inputs have no accessible names** — deriving it from the
  artifact's declared inputs rather than the page would be sturdier.
- **Page content the system observes is captured in logs**, so an account number the bank's own
  screen displays appears in `observed`. That is redaction's job, and its keyword-anchored gaps apply.
- **`artifacts/example_member_lookup.v1.json` is a schema fixture, not a real capability** — written
  before the sandbox was ever visited, under a constraint that forbade browsing.

## Next

Nothing is required. If the project is picked up again, in order of value:

1. **Tune the signal lists against more of the real UI.** `BLOCKING_SIGNALS` and
   `EMPTY_RESULT_SIGNALS` now carry the sandbox's actual wording, but only for the paths exercised
   so far.
2. **Derive `param_names` from the artifact's declared inputs** instead of the page's accessible
   names.
3. **Give the compiler a notion of output quality**, so a read landing on an unnamed value is
   flagged at compile time rather than discovered by a caller.
4. **Re-run discovery with Claude** once a key exists, to produce a second artifact alongside the
   locally-discovered one and close the deviation in REPORT §1.

## Working practices that paid off

Recorded because they were load-bearing, not incidental.

- **Verify, do not trust the report.** Several subagent claims were accurate; several were not. Every
  done-criterion in this project was re-checked by running the thing.
- **A mutation test that does not assert it mutated proves nothing.** Two early mutation tests were
  silent no-ops read as findings. Always confirm the injection landed before trusting a green result.
- **Read exit codes from the process, not through a pipe.** `cmd | tail; echo $?` reports `tail`'s
  status and masked a genuinely failing check for two rounds.
- **Regenerating evidence silently invalidates prose written against it.** README and REPORT both
  drifted out of sync with the artifact after re-discovery; a scripted audit of every cited path and
  number caught it.
- **A check that asserts the buggy behaviour is worse than no check.** `check_browser` required a
  select's chosen value to appear in the log — the exact leak that later needed fixing.
