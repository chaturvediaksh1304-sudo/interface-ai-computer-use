# REPORT

This document defends the design decisions behind the interface.ai computer-use automation
system. Where a claim is checkable, it points at the file, function, evidence log or number that
backs it. Where reality pushed back on the plan — and it did, repeatedly — the report says so,
because the places where the design had to change under contact are the places where the
engineering judgement actually lives.

Every path, function name, record count and timing in this document was verified against the
working tree before it was written.

---

## 1. Architecture

### The bet

The PRD's premise is that re-reasoning about a UI with an LLM on every invocation is slow,
expensive and non-deterministic. So the system is split into two halves that share almost nothing
but a data format:

| | Discovery | Replay |
|---|---|---|
| Driven by | an LLM, one action per turn | a saved JSON artifact |
| Model calls | one per step | zero |
| Entry point | `agent/discover.py::discover` | `replay/engine.py::replay` |
| Cost of the live run in evidence | 159.4 s, 10 model decisions | 2.8 s, no model |

Those timings are measured, not estimated: `evidence/discovery-live-01.jsonl` spans 159.4 s from
first to last record and contains 10 `decision` events; the six replays in
`evidence/replay-determinism.jsonl` took 2.77–2.81 s each, end to end, against the same live host.
That ratio — and the fact that the second column costs nothing per invocation — is the entire
product thesis, and it is the first thing the evidence had to demonstrate.

### Module map

```
guardrails/   allowlist.py, redaction.py, logging_setup.py   — the trust boundary
artifact/     schema.py                                      — the contract between the halves
agent/        browser.py, decide.py, discover.py, llm.py     — the expensive half
replay/       outcomes.py, engine.py                         — the cheap half
escalation/   intervention.py                                — human handoff
operator/     console.html, README.md                        — the mocked operator surface
```

`agent/browser.py` is the only module in the tree that imports Playwright. Everything above it
speaks in terms of an `Observation` (a flattened accessibility tree) and an action dict whose verbs
are exactly `artifact.schema.Step.action`. Replay does not get its own browser layer; it reuses
`BrowserSession`, which is why the allowlist and the locator ladder cannot drift between the two
paths.

### The accessibility tree, and the API that did not survive

`PRD_files/Architecture.md` specifies `page.accessibility.snapshot()`. That API does not exist in
the installed Playwright. Verified directly: `.venv/bin/pip list` reports `playwright 1.62.0`, and
`hasattr(playwright.sync_api.Page, "accessibility")` is `False`, while
`hasattr(playwright.sync_api.Locator, "aria_snapshot")` is `True`. The tree API was deprecated in
favour of ARIA snapshots and has since been removed from the Python bindings.

The bet survived; the API did not. `BrowserSession.observe` uses
`page.locator("body").aria_snapshot(mode="ai")`, which returns the same browser-computed
accessibility tree serialised as YAML with a `[ref=eN]` handle on every addressable node.
`_parse_snapshot` flattens that into the node dicts the rest of the system reads. This is still
roles and accessible names computed by the browser — it is not DOM scraping — and the reason for
choosing the accessibility tree in the first place (it works on legacy surfaces with no clean DOM,
and the same concept exists on desktop) is untouched. Only the call changed.

This is worth stating plainly, because it is the general shape of the problem: the architectural
bet was right and the specific mechanism it named was wrong, and the code had to be clear about
which of the two it was defending.

### Injected dependencies

`decide(client, goal, observation, history, ...)` never constructs a model client, and
`discover(..., client=, session=)` never constructs a browser. Both are passed in. This buys three
things that were each needed in practice:

- `agent/check_discover.py` drives the entire loop with a fake client returning canned,
  Anthropic-shaped responses, so the compiler is exercised with no network and no API key.
- Escalation can hand the *same live session* to a human, because `discover` does not own the
  session lifecycle (`escalation/intervention.py`).
- The model backend is swappable, which is what made the run in the evidence possible at all.

### Deviation: the LLM backend is local, not Claude

`PRD_files/Architecture.md` specifies the Anthropic API. No `ANTHROPIC_API_KEY` was available, so
`agent/llm.py` adds `OllamaClient`, a small adapter presenting the one method `decide` calls
(`client.messages.create`) and talking to a local Ollama server. `build_client("auto")` prefers
Anthropic whenever the key is set and falls back to Ollama otherwise, so the same command works
either way; `DEFAULT_CLAUDE_MODEL` and `DEFAULT_OLLAMA_MODEL` (`qwen2.5:14b-instruct`) sit side by
side in that file.

This is a documented deviation, not a substitution. The loop, the compiler, the artifact schema and
the replay engine are identical on both paths, and an artifact discovered through one replays
exactly like one discovered through the other — replay never reaches a model at all. The one place
it shows is `llm.py::DECISION_SCHEMA`: local models reliably flatten `ref`/`value` to the top level
instead of nesting them inside `action`, so the Ollama path constrains generation to a JSON schema
and normalises bracketed refs (`"[e103]"` → `"e103"`) rather than loosening the strict validator in
`decide`. Making the resolver tolerant would have hidden a real class of mistake; normalising one
known transcription artefact at the adapter boundary does not.

---

## 2. Artifact schema

`artifact/schema.py` defines seven Pydantic v2 models — `A11yLocator`, `Locator`, `InputParam`,
`OutputField`, `Step`, `Checkpoint`, `CapabilityArtifact` — plus `save_artifact` / `load_artifact`.
Every model sets `extra="forbid"`. Pydantic's default is to silently drop unknown keys, which is
precisely the data loss the Phase 2 done-criterion ("no data loss") exists to catch.

Seven validation rules are enforced, all at load time, before the browser is touched:

| # | Rule | Enforced in |
|---|---|---|
| 1 | `schema_version` must equal `SCHEMA_VERSION` | `CapabilityArtifact._check_version` |
| 2 | each action carries exactly the fields it can use | `Step._check_action_shape` |
| 3 | step indices contiguous from 0, in order | `_check_internal_consistency` |
| 4 | every `{{param}}` resolves to a declared input | `_check_internal_consistency` |
| 5 | every output reads from a real `read` step | `_check_internal_consistency` |
| 6 | a locator strategy implies its required field | `A11yLocator._check_strategy_fields` |
| 7 | a secret param appears only as a bare `{{name}}` | `_check_internal_consistency` |

`artifact/check_schema.py` exercises these with four lossless round-trips (byte-identical
re-serialisation) and 24 rejection cases spanning all seven rules; it passes.

### The transformation that makes an artifact replayable

An execution transcript is not a script. Two rewrites in `agent/discover.py::compile_artifact` do
all the work.

**Refs become locators.** During discovery, actions address nodes by `ref` — Playwright's handle
into one snapshot. An artifact storing refs cannot replay. Two independent reasons, both real:
refs restart in a new document, which is directly visible in `evidence/discovery-live-01.jsonl`
(the login page's controls are `e90`, `e95`, `e99`; after navigation they are `f1e60`, `f2e63`,
`f3e15`), and ref numbering is Playwright's business, not ours — `agent/check_discover.py` case (c)
measures 34 refs being reissued across snapshots and asserts that none of them reaches the
artifact. So `_locator_for` discards the ref entirely and emits a durable locator from what the
accessibility tree said about the node:

1. **role + accessible name** (or **label** first for form controls, per `_FORM_ROLES`), matched
   exactly.
2. **the other of the two** — loose markup breaks label lookup and role lookup in different ways,
   and trying both costs nothing at replay time.
3. **text** — for a link restyled as a button, where the role drifted but the words did not.
4. **role + nth** — position. Last, and for state-changing actions, removed entirely. See §3.

`BrowserSession._resolve_locator` walks that chain and reports which candidate matched, so replay
can surface locator drift on the happy path rather than only in logs.

**Concrete values become parameters.** The detection rule is deliberately not a heuristic over the
value's shape: a filled value becomes a `{{param}}` exactly when the caller supplied it as a run
input (`run_inputs`). Anything else the model typed is a property of the UI — a canned search mode,
a dropdown option — and stays a literal, because generalising it would invent a parameter the
caller never asked for. The one concession is `_param_for`'s whole-token match, and it fired live:
the model selected the option label `"800002 Savings"` (14 characters, per the log) for an
`account_number` of `"800002"`, and the compiler still generalised it rather than hardcoding the
one account this run happened to use.

### Secrets are structurally excluded, not filtered

Rule 7 does not check values — by the time the compiler runs there is no value left to check. It
requires that any step touching a secret param does so as a bare `{{name}}` and nothing else. The
moment a secret is concatenated into a larger string, part of that string is a literal someone
typed next to a credential. This makes baking a secret into an artifact structurally impossible,
rather than something redaction has to catch afterwards. In the live artifact,
`artifacts/altoro.account_balance.v1.json`, `password` is declared `"secret": true` and appears in
step 1 as exactly `"{{password}}"`.

### Versioning

`schema_version` (the format) and `version` (this capability's revision) move independently. An
unrecognised `schema_version` raises loudly with no migration machinery, on the grounds that
migrations are for when a second version exists, and guessing at an artifact this build cannot
honestly interpret would put a wrong action into a real back-office UI.

### What the real artifact looks like, warts included

`artifacts/altoro.account_balance.v1.json` — eight steps, three inputs (`password` secret,
`username`, `account_number`), one output, checkpoint `a11y_node_present`. Three honest
observations:

- **Its login-form locators are positional** (`role=textbox nth=1` / `nth=2`), because this
  application's login inputs have no accessible names at all. Where names exist, the ladder used
  them: step 3 is `role=button name='Login'` with `label` and `text` fallbacks. The ladder degrades
  per element, not per site, and this one file shows both ends of it.
- **The single output is named `cell`.** The read landed straight on a value whose accessible name
  is the figure itself, so there was no neighbouring label to borrow a name from and the compiler
  fell back to the node's role. Accurate, useless to a caller, and the
  compiler does not notice.
- **`cell` is a bad output name**, inherited from a read that landed straight on a value with no
  label to borrow words from.
- **The checkpoint's `expected` is the balance itself** — a 105-character negative currency figure,
  because that cell's accessible name *is* its data. For `a11y_node_present` the assertion is that
  the locator resolves, and `expected` is carried through only as the human-readable statement of
  what was required, so this does not affect replay. It still reads badly, and it is a symptom of
  the same problem discussed in §4.

---

## 3. Determinism & error handling

### No model in the path

`replay/engine.py` contains no client, no prompt, no sampling, no clock-dependent branching and no
randomness. This is asserted rather than merely commented: `replay/check_engine.py` verifies that
no `anthropic` or `openai` module is reachable from the replay path, alongside its functional
cases.

Replay also never uses a ref. `_action_for` always hands `BrowserSession.act` the whole `Locator` —
primary plus fallbacks — and lets the browser layer own the walk, so there is exactly one copy of
the fallback strategy in the tree.

### The determinism evidence

`evidence/replay-determinism.jsonl` holds three consecutive replays of the live artifact against
`demo.testfire.net` (156 records, none unparseable; three `replay.result` records).
Comparing the `(step_index, action, ok, locator_match)` tuple sequence of all eight steps across all
three runs: **identical in every run**. Every step matched on its *primary* locator; no fallback was
used anywhere; `retry_count` is 0 in all six; the checkpoint passed in all six; both outputs came
back at 105 characters each. `evidence/replay-live-01.jsonl` holds three further clean runs of the
same artifact — the JSONL sinks are append-mode, so each demo run accumulates rather than
overwriting — for nine successful live replays in total, every one a `business_outcome` with two
105-character outputs and zero retries. There is no live replay failure anywhere in the evidence
outside the deliberate error case below.

### The best story in the project: a wrong answer that looked like success

Replay was briefly non-deterministic, and the failure mode was the dangerous kind.

`_resolve_locator` walks primary, then each fallback, waiting for each candidate to attach. It
originally gave every candidate the same short budget. On a slow render the primary would still be
waiting when its budget expired — and the next candidate in the chain was often *positional*
(`role=cell nth=13`), which matches whatever happens to sit at that index, instantly, on any page.
So a slow render let a positional fallback win the race, and the run acted on the **wrong element**
while reporting success. The same artifact passed or failed depending on how fast the page happened
to render.

Two changes, both visible in `agent/browser.py` and `agent/discover.py`:

1. **The primary gets a longer budget than any fallback.** `PRIMARY_TIMEOUT_MS = 10_000` against
   `CANDIDATE_TIMEOUT_MS = 2_000`. A fallback can no longer win by being faster; it can only win by
   the primary genuinely not being there. The worst case stays bounded — one primary wait plus one
   short wait per fallback.
2. **Positional fallbacks are removed from state-changing actions.** In `_locator_for`:
   `changes_state = entry["action"]["action"] in ("click", "fill", "select")`, and the positional
   tail is dropped for those. If every named locator has missed, the page is not the one this
   capability was built against, and a wrong click submits a wrong form. Position survives only
   where it was what discovery actually recorded — a node with no accessible name — never as a
   guess made after better locators failed.

A related fix sits in `_perform`: a click that starts a navigation returns before that navigation
resolves, so the post-action URL check would see the old page and call the action a success. The
click path now settles on `networkidle` rather than `domcontentloaded`, because this application
renders into frames and the outer document reports itself loaded while the frame holding the
content is still arriving — which made the *next* step race the render.

The principle behind all three is the same, and it is the one the whole error model is built on:
**a wrong answer that looks like success is worse than an honest failure.** A capability that
hard-fails gets fixed. A capability that quietly clicks the wrong row gets trusted.

### The taxonomy

`replay/outcomes.py` closes the set at three outcomes, and the closure is the point.

- **Business outcome** — a valid, structured result, never an exception. Recognising one requires
  *two* things at once: the checkpoint assertion failed (so the page rendered and was readable),
  **and** the observed state contains one of the explicit marker phrases in `BUSINESS_SIGNALS`. A
  failed checkpoint with no recognised signal is a hard failure. That asymmetry is deliberate:
  treating every failed checkpoint as "the domain said no" would let a renamed button or a
  half-loaded table masquerade as a confident domain answer. The signal list is a flat list of eight
  phrases rather than a clever heuristic, so a reviewer can see exactly what the system will accept
  as "the bank said no", and extending it is a reviewed data change.
- **Recoverable** — bounded retries only. `RETRY_LIMIT = 2`, counted per `(step, condition)` pair
  against a single shared ledger that `classify` both reads and appends to, so the bound cannot be
  forgotten by a caller who neglects to record an attempt. Once spent, the same condition escalates.
- **Hard failure** — everything else. `ReplayResult.__post_init__` **refuses to construct** a hard
  failure without `step_index`, `expected`, `observed` and `evidence`. A contract that merely asks
  for those fields is one that will eventually be handed a result without them, and a hard failure
  with no screenshot and no expected-versus-observed pair is unactionable to whoever reads it at
  3am.

Success is modelled as a business outcome with populated `outputs`, rather than a fourth member.
"Found the account, here is the balance" and "there is no such member" are the same kind of result
from the caller's point of view: a real answer, arrived at without anything breaking. They are told
apart by whether `outputs` is populated.

`replay/check_outcomes.py` verifies that all three outcomes are reachable, that each of the eight
business signals is *returned* rather than raised, that 11 unexplained or unknown states classify
as hard failures, that the four required fields are enforced at construction, and that retries are
bounded at 2 and then escalate. It passes.

### The error-case run, and what it honestly shows

`evidence/replay-error-case.jsonl` (33 records) is the same artifact replayed with bad
credentials. Steps 0–3 succeed — the form fills and the Login button is clicked, all on primary
locators. Step 4 looks for the `GO` button, which does not exist because the login failed. What
happens next is worth reading closely:

1. The bounded re-navigation recovery fires: `replay.renavigating`, `"step 3 clicked but the page
   did not move"`. This is the recovery for a click that silently does nothing, charged against the
   shared retry ledger so it cannot loop.
2. It re-runs the Login click, which succeeds again, and retries step 4, which fails again.
3. The run stops with `outcome: hard_failure`, `step_index: 4`, `detail: "step 4: locator not found
   after every declared fallback."`, the full expected/observed pair, and `evidence/replay_altoro.png`
   on disk.

That is the correct classification for the state the page is actually in, and it is a structured
result rather than a crash. **This corrects one claim in `Memory.md`**, which records the
re-navigation recovery as never having been seen to fire against the live site; it fires here, in
the evidence, and correctly fails to rescue a genuinely wrong state.

Two honest caveats about this run, both readable in the same record:

- **The page gave a real domain answer that the system reported as breakage.** The observed page
  text contains `"Login Failed: We're sorry, but this username or password was not found in our
  system."` That is unambiguously the UI telling us the domain answer — and because no phrase in
  `BUSINESS_SIGNALS` matches it, the taxonomy called it a hard failure. The conservative bias is
  working as designed (it will not invent a domain answer), but the signal list is under-tuned
  against the real UI. The fix is one reviewed data change; it has not been made. See §7.
- **Bad credentials and the environmental flake are indistinguishable in the result.** `Memory.md`
  records that roughly one live replay in five leaves the browser on `login.jsp` because the login
  click does not complete. That produces the *same* signature as this run — no `GO` at step 4, hard
  failure, screenshot. Both are honest failures, but a caller cannot tell "your password is wrong"
  from "the sandbox flaked" without opening the screenshot. All seven live replays in the evidence
  passed, so the one-in-five figure is anecdotal and unmeasured; it is reported here as exactly
  that.

---

## 4. Heterogeneity & multi-tenant

**This section is a design story only.** `PRD_files/PRD.md` puts multi-tenant plumbing, queues,
clusters and scaling infrastructure explicitly out of scope, and desktop support likewise. Nothing
described as "would be" below is built, and this section is careful to keep the two apart.

### What heterogeneity the system already survived

The interesting heterogeneity is not "different tenants" — it is that two pages of the *same*
application address their elements completely differently, and the locator ladder had to absorb
that without a per-site branch:

| Surface | What the accessibility tree offered | What the compiler emitted |
|---|---|---|
| `login.jsp` inputs | no accessible name at all | `role=textbox nth=1` / `nth=2` (positional primary) |
| `login.jsp` submit | accessible name `Login` | `role=button name='Login'` + label + text fallbacks |
| balance cell | the name *is* the data | `role=cell nth=13`; name rejected by `_name_is_unusable` |
| balance label row | a real label beside a value | the read retargeted onto the value cell |

All four came out of one run against one host. The same root cause — this application's inputs
carry no accessible names — also explains why `param_names` was empty in the live intervention
request (§5): the console derives it from the page's input controls, and there was nothing to
derive. That is the honest character of the accessibility-tree bet. It degrades to position when
the markup is poor, per element, and it says so rather than pretending.

Two specific defences against name-shaped data, both in `agent/discover.py`, both required by this
UI. `_stable_part` keeps only the leading words of a name before the first currency amount, date,
time or long digit run; `_name_is_unusable` rejects a name outright when nothing alphabetic
survives that trim. A locator built from an accessible name that *is* the value it reads cannot
match once the value changes, and the balance cell's name has no stable part at all, so position is
used instead. Separately, `_retarget_label_read` handles a read that lands on a label rather than a
value: when the text read back *is* the node's own accessible name, it reads the adjacent value
cell instead — and actually performs that read, so the artifact only ever records a step that
really happened. Both fired in the live run.

### What in the current code already points at multi-tenant

| Seam | Where | Why it matters |
|---|---|---|
| Model client is injected | `decide(client, ...)`, `agent/llm.py::build_client` | per-tenant backend, model or key routing is a constructor argument, not a code change |
| Browser session is injected | `discover(..., session=)`, `replay(..., session=)` | per-tenant browser context, cookies and credentials; also what makes the human handoff possible |
| Allowlist is per-instance config | `load_allowlist(path)`, `BrowserSession(allowlist, ...)` | a tenant's allowlist is a JSON file; `guardrails/allowlist.json` is a default, not a singleton |
| Artifacts are versioned data, not code | `save_artifact` → `<capability_id>.v<version>.json`; `capability_id` is host label + goal slug | capabilities are already namespaced by target host and independently versioned |
| Format version separate from capability version | `schema_version` vs `version` | a fleet-wide format migration and one capability's re-recording are different events |
| Replay is pure over its inputs | `replay(artifact, params, session=, logger=)` | no global state; N concurrent replays are N sessions and N loggers |

### What the design would be

A tenant is a small configuration record: an allowlist file, a credential source, an LLM backend
choice, and an artifact namespace. Every one of those is already an injected argument, so the
tenant object would be a factory over existing constructors rather than a rewrite.

Artifacts would move from a directory to a store keyed `(tenant, capability_id, version)` with the
same JSON as the value — they are data, and nothing about them assumes a filesystem beyond the two
helper functions.

Concurrency would be process-level, not thread-level: Playwright's sync API binds a session to its
thread, so the honest unit of scale is one process per session. That is a direct consequence of
`PRD_files/Rules.md` choosing the sync API for simplicity, and it is the first thing that would
need revisiting under real load.

Desktop would reuse the `Observation` / action-dict boundary, which is already expressed in terms
of roles, names and values that exist in AX and UIA as much as in ARIA. It would **not** reuse
`BrowserSession._build_locator`, which maps each strategy onto a specific Playwright call
(`get_by_role`, `get_by_label`, and so on). That function is the true width of the port, and
calling the boundary platform-agnostic without saying so would be overselling it.

None of this is built. There is no queue, no worker pool, no tenant registry, no credential vault,
no capability catalog and no concurrency control anywhere in the tree.

---

## 5. Escalation & handoff

`escalation/intervention.py` turns a stuck run into a document a human can act on, executes the
commands that human sends back, and hands control to automation again. Three properties carry the
whole module.

**The session is never rebuilt.** `apply_operator_commands` acts through the same `BrowserSession`
object that got stuck. No re-navigation to "restore" state, no second browser. The cookies, the
half-filled form and the already-open frame are all still there, which is what makes this a handoff
rather than a restart.

**The human is not exempt from the guardrails.** Every operator command goes through
`BrowserSession.act`, so the allowlist checks a human's click the same way it checks the agent's. A
blocked command becomes an `OperatorAction` with `ok=False` and the handoff *continues* — the
operator is told "not allowed" rather than watching the session die.

**Every human action is logged**, each line carrying the `request_id`, so a reviewer can pull an
entire handoff out of a run log by that one field.

The live demonstration is `evidence/phase6-handoff.jsonl` with
`evidence/interventions/iv-20260912-4344.json`. The agent stopped on `login.jsp` for want of
credentials; the request captured goal, capability id, step index, reason, URL, page title, a
400-character observation digest and a real screenshot. Operator `dana.ops` then sent seven
commands, of which the log shows: a note, two fills (recorded as `<6 chars>` and `<8 chars>`, never
the values), **one navigation to `www.chase.com` blocked by the allowlist** with the handoff
carrying on, a click, `resume`, and one further command **ignored because it arrived after the
terminator**. Final state: `outcome: "resumed"`, `action_count: 6`, `failed_count: 1` — and the
session's URL moved from `http://demo.testfire.net/login.jsp` to
`http://demo.testfire.net/bank/main.jsp` on the same session object, which is the Phase 6
done-criterion met literally. The log holds three such handoffs from repeated demo runs, each ending
with an identical `action_count: 6`, `failed_count: 1`, `outcome: "resumed"` — which is itself a
small piece of evidence that the handoff path is as repeatable as the replay path.

Design choices worth defending:

- **A command list that never terminates is "abandoned", not "resumed".** There is no default that
  could be mistaken for consent to continue; nobody said the session was fit to resume, so it is
  not.
- **Commands are validated before they reach the browser** (`_validate`), because they cross a
  trust boundary — they arrive from a UI, not from this codebase.
- **`_loggable` is imported from `agent/discover.py`, not re-implemented.** A filled value may be a
  password, and a bare password has no shape any redaction pattern can match, so it has to be kept
  out of the log at the point of writing. One copy means the agent path and the operator path
  cannot drift apart; a private-name import is a smaller price than two half-synchronised rules.
- **Request ids are deterministic** (a `blake2s` digest over the stuck context), so the same stuck
  state raised twice overwrites its own evidence instead of littering the directory with
  near-identical files nobody can tell apart.

`operator/console.html` is the mocked surface, and `operator/README.md` is scrupulous about the
line between real and mocked: the request shape, the seven-command vocabulary, the screenshot and
the allowlist band are the real artefacts, while co-browsing, the backend, operator authentication
and the example request are not. One detail the README predates: the console still inlines an
illustrative request (`iv-20260911-2f3a`, a third id belonging to neither the live request nor any
self-check fixture) rather than loading the real file now sitting in `evidence/interventions/`. In the running system that object is loaded from
`evidence/interventions/<request_id>.json`.

The known gap here is `param_names`, which was empty in the live request because it is derived from
the accessible names of the page's input controls and this application's login inputs have none.
Harmless, but it means the field is only as good as the page's accessibility markup; deriving it
from the artifact's declared inputs would be sturdier whenever a capability is in play.

---

## 6. Safety

### Allowlist

`guardrails/allowlist.py` is deny-by-default across four dimensions — URL scheme, host, path glob,
action type — and raises `AllowlistViolation` rather than returning a boolean a caller could
quietly ignore. Three decisions are load-bearing:

- **Subdomain matching is an explicit flag, default off, and matches on a leading dot**
  (`host == d or host.endswith("." + d)`). The naive `endswith` version lets `evil-example.com`
  match an `example.com` entry, and `guardrails/check_allowlist.py` was mutation-tested against
  exactly that mistake.
- **Scheme is checked**, though the brief did not ask. Without it, `file://` and `javascript:` slip
  past a host-only check. It is cheap, and it is a trust boundary.
- **`BrowserSession._guard_route` filters what the *page* initiates**, not only what this code
  initiates — a redirect, a meta refresh, a link the model clicked. Sub-resources are not filtered:
  the allowlist describes where the agent may act, not which CDN a stylesheet comes from.

That last one earned its keep in the live discovery run. At 23:13:18 the model clicked the site's
search button; the resulting navigation to `/search.jsp?query=` was blocked
(`browser.blocked_navigation`, `"path '/search.jsp' not allowed"`), the tab parked on
`chrome-error://`, and the discovery loop recovered deterministically to the last good URL — **and
dropped the step that led there from the transcript** (`"dropping the step that led to the error
page"`, `dropped_step: 7`). The saved artifact therefore contains only the path that actually
worked. The guardrail did not merely block a navigation; it kept a dead end out of a capability
that would otherwise have been replayed forever.

The allowlist constrained the human too, in the same evidence package: `www.chase.com` blocked
during the operator handoff (§5).

### Redaction

`guardrails/redaction.py` covers seven classes of secret and PII, replaced with kind-preserving
markers (`[REDACTED:SSN]`) so logs stay debuggable. Two structural choices:

- **It runs as a logging `Filter`, not a `Formatter`.** A filter sits on the logger and fires once
  per record, so every sink is covered by one pass. A formatter must be attached to each handler
  individually — add a third sink later, forget once, and you leak.
- **Pattern order is specificity-descending**, documented inline with the rationale for each
  position. Card detection is Luhn-gated, which is what separates a real PAN from any other 13–19
  digit run (step ids, timestamps) and keeps the logs from being shredded by false positives.

Two real leaks were found and closed, and both are visible in the evidence:

1. **A GET form put filled values into the query string, and URLs were logged verbatim.** The fix
   is the query-value rule, swept last so earlier and more specific rules keep their precise kind
   tags. In `evidence/discovery-live-01.jsonl` every URL now reads
   `showAccount?listAccounts=[REDACTED:QUERYVAL]` — the parameter name stays visible, the value does
   not. The same rule caught the blocked search URL.
2. **The discovery loop logged whole action dicts, so a password reached an evidence file.** The fix
   is `_loggable`, which replaces a filled value with its length before the dict is logged; the live
   log shows `"value": "<8 chars>"` and `value_len: 8`, never a value.
   `agent/check_discover.py` case (h) asserts that a filled value never reaches the log file.

Redaction also applies to model-authored free text, which matters more than it sounds: the model's
own rationale at step 5 reads `"Select the [REDACTED:ACCOUNT] from the dropdown"` in the log,
because the account-number pattern matched inside a sentence the model wrote.

### What is still open, stated plainly

- **Artifact descriptions are not redacted.** `save_artifact` serialises the model's rationale
  verbatim, and `"800002"` — the account number used during discovery — appears exactly once in
  `artifacts/altoro.account_balance.v1.json`, inside step 5's `description`. Rule 7 protects
  `value`; nothing protects `description`. This is the most concrete safety gap in the build. Per
  `PRD_files/Rules.md`, changing redaction behaviour requires explicit approval, so it is flagged
  rather than quietly patched.
- **Over-redaction of a field named `secret`.** `redact_obj` redacts any value under a
  secret-looking key wholesale, which is right for `"password": "hunter2"` and wrong for
  `"secret": true` — the replay param log reads `"secret": "[REDACTED:SECRET]"` where it should read
  `false`. Harmless, and a reminder that key-name-based redaction has a cost as well as a benefit.
- **Known redaction gaps, accepted and recorded**: bare account numbers with no nearby keyword,
  non-Luhn 13–19 digit runs, bare 10-digit phone numbers, non-US formats, names, dates of birth and
  street addresses. Account and phone patterns are keyword-anchored on purpose — a bare 6–19 digit
  pattern would eat every timestamp, `elapsed_ms` and artifact id in the logs. A deliberate
  false-negative trade, not an oversight.
- **The live capability runs over plain HTTP.** `demo.testfire.net` presents an expired certificate
  under this machine's clock — `agent/check_browser.py` skips its live case with
  `net::ERR_CERT_DATE_INVALID` on the HTTPS URL — so the artifact's target is `http://`. The
  allowlist permits both schemes. For a public, deliberately-vulnerable sandbox with a throwaway
  demo login this is acceptable; against a real back office it would not be, and the allowlist is
  where that would be enforced.

---

## 7. Cuts

Things deliberately not built, and things built but still imperfect. Nothing here is a surprise
discovered while writing the report; all of it is recorded in `Memory.md` or visible in the code.

### Cut on purpose, per the PRD's own scope

| Cut | Why | What it would take |
|---|---|---|
| Multi-tenant plumbing, queues, clusters | PRD: design story only | §4 — the injected seams exist, the infrastructure does not |
| Desktop support | PRD: design story only | a non-Playwright `_build_locator`; the `Observation` boundary already fits |
| Real co-browsing operator console | PRD: mocked handoff is sufficient | a live view attached to the held session; `operator/console.html` composes commands instead |
| Capability catalog/API, code generation from artifacts, confidence scoring, assisted LLM fallback, multi-run stability testing | PRD: stretch goals, only if the core is solid | the core absorbed the time; see the determinism work in §3 |
| Database | Architecture.md: versioned JSON on disk | nothing at this scale justifies one |
| Unit-test framework | Rules.md scopes tests to schema round-trips and error classification | ten runnable module self-checks (`python -m <module>.check_*`); all ten pass |

### Built, and still imperfect — the honest list

- **`BUSINESS_SIGNALS` is still the eight generic phrases** written before the real UI was known.
  The error-case evidence proves the cost: Altoro's `"Login Failed: We're sorry, but this username
  or password was not found in our system."` is a genuine domain answer reported as a hard failure
  because no marker matched. Fixing it is a one-line reviewed data change; it has not been made,
  and pretending the current list is tuned to this UI would be the dishonest option.
- **`cell` is a poor output name.** It comes from a read that landed straight on a value with no
  neighbouring label to borrow a name from, so the compiler fell back to the node's role (§2).
- **`param_names` is empty when a page's inputs have no accessible names**, which is exactly the
  case in the live intervention. It is derived from the page rather than from the artifact's
  declared inputs; deriving it from the artifact would be sturdier whenever a capability is in play.
- **The Phase 2 example artifact describes a guessed flow.**
  `artifacts/example_member_lookup.v1.json` is an eight-step member-search capability written before
  the sandbox was ever visited, under a standing constraint that forbade browsing. It validates, and
  `replay/check_engine.py` replays it cleanly against local markup — but it is a schema fixture, not
  a real capability. The authoritative artifact is `artifacts/altoro.account_balance.v1.json`,
  compiled from the live run.
- **Replay against the public sandbox is intermittent.** `Memory.md` records roughly one run in five
  where the login click does not navigate. The bounded re-navigation recovery exists for it and, as
  §3 shows, has now been observed firing — in the error-case run, where it correctly failed to
  rescue a genuinely wrong state. It has not been observed rescuing the flake itself, because the
  flake did not occur in any of the seven live replays in the evidence. The recovery is otherwise
  proven by `replay/check_engine.py` case (i), against a scripted session whose clicks only land
  when repeated.
- **The artifact records a click on either side of the select it logically follows.** Steps 4 and 6
  are both clicks around the step-5 `select`, because that is the order the model actually took and
  the compiler records the path that worked rather than the path that reads well. It replays
  deterministically, seven times over, so it is left alone — but it is a transcript, not a design.
- **Two module docstrings disagree about ref stability within a single document.**
  `agent/browser.py` says refs measurably survive a re-snapshot on 1.62.0; `replay/engine.py` says
  they were measured renumbering. Both modules nonetheless assume the pessimistic case, which is the
  safe direction, and replay uses no refs at all — so the disagreement is a documentation defect
  rather than a behavioural one.
- **The LLM backend is local Ollama, not Claude** (§1). A documented deviation, not a hidden one;
  the client is injected and `build_client("auto")` prefers Anthropic the moment a key exists.

### What the evidence package contains

Record counts are as of writing; the sinks append, so re-running a demo grows a file rather than
replacing it.

| File | Records | What it shows |
|---|---|---|
| `evidence/discovery-live-01.jsonl` | 45 | the live LLM-driven run: 8 decisions, 17 observations, 8 executed actions |
| `evidence/replay-determinism.jsonl` | 156 | three consecutive replays, step for step identical |
| `evidence/replay-live-01.jsonl` | 52 | one clean replay, no model on the path |
| `evidence/replay-error-case.jsonl` | 33 | bad credentials: a business outcome naming the "login failed" signal |
| `evidence/replay-hard-failure.jsonl` | 38 | unknown account: an honest hard failure with a screenshot |
| `evidence/phase6-handoff.jsonl` | 83 | three live human handoffs on the same session, one operator command blocked in each |
| `evidence/phase1-manual-test.jsonl` | 3 | allowlist decisions logged through the redacting logger |
| `evidence/interventions/iv-20260912-4344.json` | — | the live intervention request, with its screenshot beside it |
| `evidence/replay_altoro.png` | — | the failure screenshot the hard-failure result points at |
| `evidence/selfcheck-*.jsonl` | — | log output of the module self-checks, not part of the demo evidence |

Every log file parses as JSON lines with zero unparseable records, verified by reading each file
and counting.
