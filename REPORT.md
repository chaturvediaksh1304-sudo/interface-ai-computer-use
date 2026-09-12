# REPORT

Every path, symbol and number below was checked against the working tree. Where reality pushed back
on the plan — and it did, repeatedly — this says so, because that is where the judgement lives.

---

## 1. Architecture

Re-reasoning about a UI with a model on every invocation is slow, expensive and non-deterministic.
So the system splits in two, sharing nothing but a data format: **discovery** drives a live surface
with an LLM, one action per turn (`agent/discover.py::discover`); **replay** executes the saved
artifact with **zero** model calls (`replay/engine.py::replay`).

`agent/browser.py` is the only module importing Playwright. Everything above it speaks in
`Observation`s and action dicts whose verbs are exactly `artifact.schema.Step.action`. Replay reuses
`BrowserSession` rather than owning a second browser layer, so the allowlist and the locator ladder
cannot drift between the two paths. Dependencies are injected — `decide(client, ...)` never builds a
client, `discover(..., session=)` never builds a browser — which is what lets the checks run with no
network, lets escalation hand the *same live session* to a human (§5), and made the backend
swappable.

**The accessibility-tree bet survived; the API naming it did not.** `PRD_files/Architecture.md` specifies
`page.accessibility.snapshot()`, which does not exist in the installed Playwright 1.62.0 —
`hasattr(Page, "accessibility")` is `False`. `observe` uses `aria_snapshot(mode="ai")`: the same
browser-computed tree, with a `[ref=eN]` handle per node. Still roles and names computed by the
browser rather than DOM scraping, so the reason for choosing it — it works on legacy surfaces with
no clean DOM, and the same concept exists on desktop — is untouched. Only the call changed.

**Deviation: the LLM is local, not Claude.** With no `ANTHROPIC_API_KEY` available, `agent/llm.py`
adds `OllamaClient` presenting the one method `decide` calls; `build_client("auto")` prefers
Anthropic whenever a key exists. Loop, compiler, schema and replay are identical on both paths, and
replay reaches no model at all. One accommodation: local models flatten `ref`/`value` out of
`action`, so that path constrains generation to a JSON schema at the adapter boundary rather than
loosening the strict validator, which would hide a real class of mistake.

---

## 2. Artifact schema

Seven Pydantic v2 models plus `save_artifact` / `load_artifact`. Every model sets `extra="forbid"`:
Pydantic's default silently drops unknown keys, which is exactly the data loss a round-trip test is
supposed to catch. Seven rules are enforced at load time, before the browser is touched — version
match, per-action field shape, contiguous step indices, every `{{param}}` resolving to a declared
input, every output reading from a real `read` step, each locator strategy implying its required
field, and a secret param appearing only as a bare `{{name}}`.

**A transcript is not a script.** Two rewrites in `compile_artifact` do the work.

*Refs become locators.* Discovery addresses nodes by `ref`, a handle into one snapshot. An artifact
storing refs cannot replay — refs restart in a new document, visible directly in the discovery log
(login controls are `e90`/`e95`/`e99`; after navigation `f1e60`, `f2e63`). `_locator_for` discards
the ref and emits a ladder: role + accessible name (label first for form controls), the other of the
two, then text, then position — position last, and for state-changing actions removed entirely (§3).

*Values become parameters.* A filled value becomes a `{{param}}` exactly when the caller supplied it
in `run_inputs` — deliberately not a heuristic over the value's shape. Anything else the model typed
is a property of the UI and stays literal, since generalising it would invent a parameter nobody
asked for. The whole-token match earned itself live: the model selected the option label
`"800002 Savings"` for an `account_number` of `"800002"`, and the compiler generalised it rather
than hardcoding the one account that run happened to use.

**Secrets are structurally excluded, not filtered.** Rule 7 checks no values — by compile time there
are none left. It requires that a step touching a secret does so as a bare `{{name}}` and nothing
else, because the moment a secret is concatenated into a larger string, part of that string is a
literal typed next to a credential.

The shipped artifact, `artifacts/altoro.account_balance.v1.json`, is eight steps, three inputs, one
output. Its login-form locators are *positional* because this application's login inputs carry no
accessible names, while step 3 is `role=button name='Login'` with label and text fallbacks — one
file showing both ends of the ladder. Its shortcomings are in §7.

---

## 3. Determinism & error handling

`replay/engine.py` holds no client, no prompt, no sampling, no randomness and no clock-dependent
branching; `check_engine` asserts no `anthropic`/`openai` module is even reachable from that path.
Replay never uses a ref: `_action_for` hands the whole `Locator` to the browser layer, so one copy
of the fallback strategy exists in the tree.

`evidence/replay-determinism.jsonl` holds three consecutive replays against the live host (156
records). Comparing `(step_index, action, ok, locator_match)` across all eight steps of all three
runs: **identical**, every step on its primary locator, zero retries.

**A wrong answer that looked like success.** `_resolve_locator` originally gave every candidate the
same short budget. On a slow render the primary was still waiting when its budget expired — and the
next candidate was often *positional* (`role=cell nth=13`), which matches whatever sits at that
index, instantly, on any page. So a slow render let a positional fallback win the race and the run
acted on the **wrong element while reporting success**; the same artifact passed or failed depending
on render speed. Two fixes: the primary now gets `PRIMARY_TIMEOUT_MS = 10_000` against
`CANDIDATE_TIMEOUT_MS = 2_000`, so a fallback can only win by the primary genuinely being absent;
and bare positional fallbacks are dropped from `click`/`fill`/`select`, because if every named
locator missed, the page is not the one this capability was built against and a wrong click submits
a wrong form. Position survives only where discovery actually recorded it — a node with no
accessible name — never as a guess after better locators failed. Relatedly, a click that starts a
navigation returns before it resolves, so the post-action URL check saw the old page and called it
success; the click path now settles on `networkidle`, because this app renders into frames and the
outer document reports itself loaded while the frame is still arriving.

The principle behind all three: **a wrong answer that looks like success is worse than an honest
failure.** A capability that hard-fails gets fixed; one that quietly clicks the wrong row gets
trusted.

**The taxonomy** closes at three outcomes. *Business outcome* — a valid structured result, never an
exception. *Recoverable* — bounded retries only, `RETRY_LIMIT = 2` per `(step, condition)` against a
shared ledger `classify` both reads and appends to, so the bound cannot be forgotten. *Hard
failure* — `ReplayResult.__post_init__` **refuses to construct** one without `step_index`,
`expected`, `observed` and `evidence`, because a contract that merely asks for those will eventually
be handed a result without them.

Recognising a business outcome needs affirmative evidence, and what a phrase licenses depends on
what failed:

| Signal kind | Explains a failed checkpoint | Explains a *missing element* |
|---|---|---|
| `EMPTY_RESULT_SIGNALS` — "no results found" | yes | **no** — a search button exists whether or not the last search found anything |
| `BLOCKING_SIGNALS` — "login failed", "account closed" | yes | **yes** — they say why the flow never reached that page |

No phrase counts when the page was never dependably read. Both shapes are in the evidence.
`replay-error-case.jsonl` replays with bad credentials: the page says *"Login Failed: … this
username or password was not found in our system"*, so the result is a **business outcome** naming
that signal — the bank answering, not the automation breaking. `replay-hard-failure.jsonl` replays
with an unknown account: the dropdown has no such option and the page never says why, so it is a
**hard failure** with expected/observed and a screenshot. No affirmative message, no manufactured
answer.

---

## 4. Heterogeneity & multi-tenant

**Design only.** The PRD puts multi-tenant plumbing and desktop support explicitly out of scope.

The interesting heterogeneity was not between tenants but inside one application: two pages of the
same app address elements completely differently, and the ladder absorbed it without a per-site
branch. A balance cell whose accessible name *is* its data has two defences in `agent/discover.py` —
`_stable_part` keeps only the words before the first currency amount, date or long digit run, and
`_name_is_unusable` rejects a name outright when nothing alphabetic survives, because a locator
built from the value it reads cannot match once the value changes. `_retarget_label_read` handles
the mirror case, a read landing on a label, by reading the adjacent value cell instead and actually
performing that read. All fired in the live run.

The seams that point at multi-tenant already exist: client and session are injected, the allowlist
is per-instance config (`load_allowlist(path)`), artifacts are versioned data keyed by
`capability_id` and `version`, format version is separate from capability version, and `replay` is
pure over its inputs. A tenant would be a configuration record — allowlist file, credential source,
backend choice, artifact namespace — all already constructor arguments, so a factory over existing
seams rather than a rewrite, with artifacts moving to a store keyed `(tenant, capability_id,
version)`.

Two honest limits. Concurrency would be process-level, since Playwright's sync API binds a session
to its thread. And desktop would reuse the `Observation`/action boundary, already expressed in roles
and names that exist in AX and UIA — but **not** `_build_locator`, which maps each strategy onto a
specific Playwright call. That function is the true width of the port, and calling the boundary
platform-agnostic without saying so would be overselling it.

---

## 5. Escalation & handoff

**The session is never rebuilt.** `apply_operator_commands` acts through the same `BrowserSession`
that got stuck — the module contains no `BrowserSession(` anywhere. The cookies, the half-filled
form and the open frame are all still there, which is what makes this a handoff rather than a
restart.

**The human is not exempt from the guardrails.** Every operator command goes through
`BrowserSession.act`, so the allowlist checks a human's click exactly as it checks the agent's. A
blocked command becomes an `OperatorAction` with `ok=False` and the handoff *continues* — the
operator is told "not allowed" rather than watching the session die. Every action is logged with the
`request_id`, so a whole handoff can be pulled from a run log by one field.

`evidence/phase6-handoff.jsonl` holds three live handoffs. The agent stopped on `login.jsp` for want
of credentials; the request captured goal, capability id, step index, reason, URL, page title,
observation digest and a real screenshot. Operator `dana.ops` sent a note, two fills (logged as
`<6 chars>`/`<8 chars>`, never the values), **one navigation to `www.chase.com` refused by the
allowlist** with the handoff carrying on, a click, and `resume` — plus one command **ignored because
it arrived after the terminator**. The session moved from `login.jsp` to `/bank/main.jsp` on the
same object. All three handoffs log one `escalation.blocked` and one `escalation.command_ignored`,
which is itself small evidence that this path is as repeatable as replay.

A command list that never terminates is `abandoned`, not `resumed`: no default may be mistaken for
consent to continue. Commands are validated before reaching the browser since they cross a trust
boundary, and `_loggable` is imported from `agent/discover.py` rather than re-implemented so the two
paths cannot drift. `operator/console.html` is the mocked surface; its README is explicit about what
is real (request shape, command vocabulary, screenshot, allowlist band) and what is not
(co-browsing, backend, operator auth).

---

## 6. Safety

**Allowlist.** Deny-by-default across scheme, host, path glob and action type, raising rather than
returning a boolean a caller could ignore. Subdomain matching is an explicit flag, default off,
matching on a leading dot — the naive `endswith` lets `evil-example.com` match an `example.com`
entry, and the check was mutation-tested against exactly that mistake. Scheme is checked though the
brief did not ask, because `file://` and `javascript:` slip past a host-only check. `_guard_route`
filters what the *page* initiates, not only what this code initiates.

That last one earned its keep live: the model clicked the site's search button, the navigation to
`/search.jsp` was blocked, the tab parked on `chrome-error://`, the loop recovered to the last good
URL — **and dropped the step that led there from the transcript**. The guardrail did not merely
block a navigation; it kept a dead end out of a capability that would otherwise be replayed forever.
It constrained the human too, refusing `www.chase.com` mid-handoff (§5).

**Redaction.** Seven classes of secret and PII, replaced with kind-preserving markers so logs stay
debuggable. It runs as a logging `Filter`, not a `Formatter` — a filter fires once per record so
every sink is covered, whereas a formatter must be attached per handler and one omission leaks. Card
detection is Luhn-gated, separating a real PAN from any other long digit run.

Four leaks were found and closed, all visible in the evidence. A GET form put filled values into the
query string while URLs were logged verbatim (now `listAccounts=[REDACTED:QUERYVAL]` — parameter
name visible, value not). The discovery loop logged whole action dicts, so a password reached an
evidence file (now `_loggable` substitutes a length). A supplied value reached the *artifact*
through `Step.description`, the model's own rationale — rule 7 guards `value`, nothing guarded
prose, so `_generalise_text` now generalises descriptions too. And `select` logged its chosen value
verbatim while `fill` had always logged a length — found because the browser check had been
*asserting* that leak.

**Still open.** `redact_obj` redacts any value under a secret-looking key wholesale, right for
`"password": "hunter2"` and wrong for `"secret": true`. Account and phone patterns are
keyword-anchored on purpose, so a bare account number with no nearby keyword passes through; the
alternative would eat every timestamp and artifact id in the logs, so it is a deliberate
false-negative trade. Page content the system *observes* is still captured as evidence, so an
account number the bank's own screen displays appears in `observed`. And the capability runs over
plain HTTP because the sandbox's certificate is expired under this clock — acceptable for a public
sandbox with a throwaway login, not against a real back office.

---

## 7. Cuts

Cut on purpose, per the PRD's scope: multi-tenant plumbing, queues and clusters (§4 — the seams
exist, the infrastructure does not); desktop support; a real co-browsing console; the stretch goals
(capability catalog, code generation, confidence scoring, assisted LLM fallback); a database; and a
unit-test framework, since `PRD_files/Rules.md` scopes tests to schema round-trips and error classification —
eleven runnable module self-checks stand in, and all eleven pass.

Built, and still imperfect:

- **`cell` is a poor output name**, inherited from a read that landed on a value with no label to
  borrow words from. The compiler does not notice that an output is unnamed in any useful sense.
- **`param_names` is empty when a page's inputs have no accessible names** — exactly the case in the
  live intervention. Deriving it from the artifact's declared inputs rather than the page would be
  sturdier whenever a capability is in play.
- **`artifacts/example_member_lookup.v1.json` is a schema fixture, not a real capability** — written
  before the sandbox was ever visited. It validates and replays against local markup; the
  authoritative artifact is the one compiled from the live run.
- **Replay against the public sandbox is intermittent** — roughly one run in five leaves the browser
  on `login.jsp` because the login click does not navigate. A bounded re-navigation recovery exists
  and is proven by `check_engine` case (i) against a scripted session whose clicks only land when
  repeated. The figure is anecdotal, not measured.
- **The artifact records a click on either side of the select it logically follows**, because the
  compiler records the path that worked rather than the path that reads well. It replays
  deterministically, so it stands — but it is a transcript, not a design.
- **The LLM backend is local Ollama, not Claude** (§1) — documented, not hidden.

Next, in order: tune the signal lists against more of the real UI; derive `param_names` from the
artifact; and give the compiler a notion of output quality, so a read landing on an unnamed value is
flagged at compile time rather than discovered by a caller.

### The evidence package

| File | Records | What it shows |
|---|---|---|
| `evidence/discovery-live-01.jsonl` | 45 | the live LLM-driven run: 8 decisions, 17 observations, 8 actions |
| `evidence/replay-live-01.jsonl` | 52 | one clean replay, no model on the path |
| `evidence/replay-determinism.jsonl` | 156 | three consecutive replays, step for step identical |
| `evidence/replay-error-case.jsonl` | 33 | bad credentials: a business outcome naming the "login failed" signal |
| `evidence/replay-hard-failure.jsonl` | 38 | unknown account: an honest hard failure with a screenshot |
| `evidence/phase6-handoff.jsonl` | 83 | three live human handoffs on the same session, one command refused in each |
| `evidence/phase1-manual-test.jsonl` | 3 | allowlist decisions logged through the redacting logger |
| `evidence/interventions/iv-20260912-4344.json` | — | the live intervention request, with its screenshot beside it |
| `evidence/replay_altoro.png` | — | the failure screenshot the hard-failure result points at |
