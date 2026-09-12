# interface.ai — Computer-Use Automation

Legacy back-office applications in banking often have no API. An AI agent that needs to act
inside one has no choice but to drive the UI, and re-reasoning about that UI with a language
model on every single invocation is slow, expensive, and non-deterministic.

This project does the reasoning **once**. An LLM drives a real browser against a real sandbox —
observing the page through its accessibility tree, deciding an action, executing it — until it
completes a natural-language goal. That run is then compiled into a typed, versioned
**capability artifact**: a JSON file recording the steps, the locators (with fallbacks), the
declared input parameters, the output shape, and a success checkpoint. From that point on the
capability is replayed deterministically with **no model anywhere in the path**, returning
structured, typed outputs. The consumer is an AI agent — or the engineer wiring one up — not a
human end user.

---

## Requirements

- **Python 3.14.** The checked-in virtual environment was built with 3.14.7. Earlier 3.11+
  versions will very likely work, but 3.14 is what this has actually been run on.
- A machine that can download and run a Chromium build (Playwright fetches one during setup).
- For **discovery** only: either an Anthropic API key or a local Ollama server. See
  [LLM backend](#llm-backend) below.
- For **replay**: nothing beyond the Python packages and Chromium. No key, no model, no
  network beyond the target site itself. That is the entire point of the project.

## Setup

From a fresh clone, at the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

`requirements.txt` pins three direct dependencies: `playwright>=1.48` (browser automation via
the accessibility tree), `pydantic>=2.9` (the typed, versioned artifact schema), and
`anthropic>=0.40` (the discovery-run LLM backend).

The third command is not a no-op — **it downloads a Chromium browser build** (a few hundred
megabytes) into Playwright's local cache. It only needs to be run once per machine.

> **Note on the repository directory name.** The folder on disk is named `interface.ai ` with a
> trailing space. Always quote the path in shell commands: `cd "…/interface.ai "`.

## LLM backend

Discovery needs a language model. There are two paths, and the CLI picks between them
automatically (`--backend auto`, the default):

**1. Anthropic (what `PRD_files/Architecture.md` specifies).** If `ANTHROPIC_API_KEY` is set in
the environment, the Anthropic SDK is used directly.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**2. Local Ollama (the fallback).** If no key is present, the run falls back to a local Ollama
server on `http://localhost:11434` with a model pulled locally:

```bash
ollama pull qwen2.5:14b-instruct
```

Be aware of what this means for the evidence in this repository: **no Anthropic key was
available while this was built, so every run under `/evidence/` — including the discovery run
that produced the shipped artifact — was driven by the local `qwen2.5:14b-instruct` model.**
The loop, the prompt, the artifact compiler, and the schema are identical on both paths, so an
artifact discovered via one backend replays exactly like one discovered via the other. But the
artifact currently checked in was authored by a 14B local model, and it shows — see
[Limitations](#limitations).

You can force either path explicitly with `--backend anthropic` or `--backend ollama`, and
override the model with `--model NAME`.

**Replay needs neither.** A reviewer with no API key and no Ollama installation can still run
the full replay demo below.

---

## Demo commands

```
.venv/bin/python cli.py discover --goal TEXT --target URL [--param k=v ...] [--capability-id ID]
                                 [--backend auto|anthropic|ollama] [--model NAME]
                                 [--max-steps N] [--run-id ID] [--headed]

.venv/bin/python cli.py replay ARTIFACT_PATH [--param k=v ...] [--run-id ID] [--headed]

.venv/bin/python cli.py handoff-demo [--run-id ID] [--headed]
```

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | A handled failure — replay hard-failure, or discovery stuck / out of steps |
| `2` | Misuse — bad arguments, missing artifact |

`--headed` runs the browser visibly instead of headless, which is worth doing at least once to
watch the thing actually work. `--run-id` names the structured log written under `evidence/`;
if omitted, one is generated.

### The target sandbox

Everything here runs against **Altoro Mutual** (`http://demo.testfire.net`), IBM's public,
deliberately-vulnerable demo banking application. The credentials used throughout are the
public demo values `jsmith` / `demo1234`, and the account used is `800002`.

The URL is plain `http`, not `https`, because the sandbox's TLS certificate expired in June
2026. The allowlist in `guardrails/allowlist.json` permits both schemes for this host and
nothing else.

### 1. Discovery

Drive the LLM loop against the live site and compile the result into a new artifact:

```bash
.venv/bin/python cli.py discover \
  --goal "Sign in with the given username and password, select the given account number in the account dropdown, click the GO button beside it, then read the available balance from the resulting Balance Detail page." \
  --target http://demo.testfire.net/login.jsp \
  --param username=jsmith \
  --param password=demo1234 \
  --param account_number=800002 \
  --capability-id altoro.account_balance \
  --run-id discovery-demo
```

This writes a versioned artifact under `artifacts/` and a full structured log to
`evidence/discovery-demo.jsonl`. The step budget defaults to 25; raise or lower it with
`--max-steps`.

Note that parameter *values* never enter the saved artifact. Steps carry `{{param}}` templates
instead, and a parameter marked secret may only appear as a bare `{{name}}` — never composed
into a larger string. It is structurally impossible to bake a credential into an artifact.

### 2. Replay

Run the already-discovered capability with no model in the loop:

```bash
.venv/bin/python cli.py replay artifacts/altoro.account_balance.v1.json \
  --param username=jsmith \
  --param password=demo1234 \
  --param account_number=800002 \
  --run-id replay-demo
```

`artifacts/altoro.account_balance.v1.json` is the real, LLM-discovered capability. It declares
three required inputs — `username`, `password` (marked secret), and `account_number` — and two
string outputs, `available_balance` and `cell`. Nine steps, an accessibility-tree checkpoint,
and locators with CSS fallbacks.

When a replay does not succeed — a bad input, or the page not arriving in the expected state —
it does not crash. It classifies the result through the error taxonomy and returns a structured
failure carrying the step index, the expected description, the observed state, and a pointer to
a screenshot, then exits `1`. `evidence/replay-error-case.jsonl` is a recorded example.

### 3. Human handoff

```bash
.venv/bin/python cli.py handoff-demo --run-id handoff-demo
```

Demonstrates the Phase 6 escalation path: the automation detects that it cannot proceed, raises
an `InterventionRequest` carrying the goal, capability id, step index, reason, URL, page title,
an accessibility-tree digest and a screenshot, and then holds the **same live browser session**
open while a human's commands are applied to it. Control returns to automation on the page the
human left it on. The operator's commands are run through the same allowlist the agent is bound
by, and every one of them is logged.

The human-facing surface is `operator/console.html` — a static mock, no build step and no
network requests. Open it directly:

```bash
open operator/console.html
```

---

## Running the checks

Every module ships a runnable self-check. There is no test framework; each check is a plain
module that asserts its way through the properties that module claims and exits non-zero if
any of them fail. Run them all:

```bash
.venv/bin/python -m guardrails.check_allowlist
.venv/bin/python -m guardrails.check_redaction
.venv/bin/python -m guardrails.check_logging
.venv/bin/python -m artifact.check_schema
.venv/bin/python -m agent.check_llm
.venv/bin/python -m agent.check_browser
.venv/bin/python -m agent.check_discover
.venv/bin/python -m replay.check_outcomes
.venv/bin/python -m replay.check_engine
.venv/bin/python -m escalation.check_intervention
.venv/bin/python check_cli.py
```

What they prove:

| Check | What it establishes |
| --- | --- |
| `guardrails.check_allowlist` | The deny-by-default allowlist actually blocks out-of-scope domains, schemes, paths and action types — including the subdomain-suffix trap where `evil-example.com` must not match an `example.com` entry. |
| `guardrails.check_redaction` | Seven classes of secret and PII are stripped from a realistic log line, with kind-preserving markers so logs stay debuggable. Card detection is Luhn-gated. |
| `guardrails.check_logging` | JSON-lines log shape, redaction of both the message and the caller's structured fields, handler idempotency — and, critically, that the raw secret appears nowhere in the bytes written to disk. |
| `artifact.check_schema` | Lossless serialize/deserialize round-trips with byte-identical re-serialization, the hand-written example artifact validating clean, and 24 rejection cases covering all seven schema validation rules. |
| `agent.check_llm` | The backend adapters, against a stub HTTP server. Does not require Ollama to be running. |
| `agent.check_browser` | The observe/act browser layer, against throwaway pages served over loopback. Requires the Chromium download; needs no internet. |
| `agent.check_discover` | The decide layer, the discovery loop and the artifact compiler, with both the LLM client and the browser session replaced by fakes. No API key, no browser. The fake session deliberately reissues different element refs on every snapshot, mirroring real Playwright behaviour. |
| `replay.check_outcomes` | The error taxonomy in isolation — business outcome vs. recoverable condition vs. hard failure, given literal step outcomes. Pure logic; never leaves the process. |
| `replay.check_engine` | A full replay against real markup returning declared outputs; a bad input classified rather than crashed; a dead primary locator falling through to a fallback and the result naming which one matched; every fallback exhausted producing a hard failure with step index, expected vs. observed, and a screenshot. Requires Chromium; served over loopback. |
| `escalation.check_intervention` | A simulated stuck condition producing a well-formed intervention request, and control transfer to a human and back on the same session — with the operator's own actions run through the *shipped* allowlist, not a permissive stub. No browser, no network. |
| `check_cli.py` | The CLI's argument parsing, exit-code contract, and dispatch. |

Only `agent.check_browser` and `replay.check_engine` need the Chromium build; both serve their
own pages over loopback and make no external network request. Everything else is pure Python
and runs fully offline.

---

## Project layout

```
agent/          discovery loop — observe (a11y tree) → decide (LLM) → act (Playwright)
  browser.py      BrowserSession: accessibility-tree snapshots and action execution
  decide.py       the single LLM call, and validation of what comes back
  discover.py     the loop itself, plus the transcript → artifact compiler
  llm.py          backend adapter (Anthropic / local Ollama)

artifact/       typed, versioned capability schema
  schema.py       Pydantic v2 models + save/load; extra="forbid" everywhere

replay/         deterministic execution — no LLM in this path
  engine.py       step walker, locator fallbacks, checkpoint assertion, output collection
  outcomes.py     the error taxonomy and its classifier

guardrails/     safety
  allowlist.py    deny-by-default over domains, schemes, path globs, action types
  allowlist.json  the shipped config
  redaction.py    secret/PII redaction
  logging_setup.py JSON-lines structured logging, redaction applied as a logging Filter

escalation/     human-in-the-loop
  intervention.py raise an intervention, hand the live session over, resume

operator/       the mocked operator surface
  console.html    static, self-contained mock console
  README.md       what the page shows and why, in that order

artifacts/      saved capability artifacts (JSON)
evidence/       structured logs, screenshots, intervention records
PRD_files/      the planning documents this was built against
cli.py          the three entry-point commands
Memory.md       running project state, decisions, and known issues
REPORT.md       the design defence
```

---

## What is in `/evidence/`

| File | What it demonstrates |
| --- | --- |
| `discovery-live-01.jsonl` | The real LLM-driven discovery run that produced the shipped artifact — 66 records covering 24 accessibility-tree observations, 10 model decisions, 12 executed actions, and the compile. It includes recovery events worth reading: a navigation blocked by the allowlist, a recovery off a browser error page, a dropped bad step, and a `read` retargeted from a label to the value beside it. |
| `replay-live-01.jsonl` | One clean deterministic replay of that artifact — 57 records, nine steps, checkpoint asserted, both declared outputs returned. No model call anywhere in the file. |
| `replay-determinism.jsonl` | Six consecutive replays back to back (342 records). Every one returns the same two outputs at the same lengths with a clean checkpoint. This is the determinism claim, shown rather than asserted. |
| `replay-error-case.jsonl` | A replay that fails, and fails honestly: `HARD_FAILURE` at step 4, `locator not found after every declared fallback`, carrying the step index, the expected description, the observed state naming each fallback tried, and a pointer to the screenshot. Not a crash — a structured result. |
| `replay_altoro.png` | The frame captured at the moment of that hard failure. This is the `evidence` pointer the failure result returns. |
| `phase1-manual-test.jsonl` | The guardrail integration test: one allowed action and two blocked ones, logged through the redacting logger. Short, but it is the record that the allowlist and the log pipeline work together. |
| `phase6-handoff.jsonl` | The live escalation run — 29 records. The agent stops for want of credentials, raises the intervention, hands the session to a human, applies six operator actions (one of which is blocked by the allowlist and one ignored as invalid), and resumes on the same session object. |
| `interventions/iv-20260912-4344.json` + `.png` | The intervention request raised during that live run, with its screenshot: goal, capability id, step index, reason, URL, page title, accessibility digest. |

Note on reading the logs: a successful replay is logged with `outcome: "business_outcome"`.
That is deliberate, not a mislabel — in this taxonomy a business outcome is any valid,
structured result handed back to the caller, as opposed to a recoverable condition handled
internally or a hard failure that stops the run.

---

## Limitations

Stated plainly; `Memory.md` and `REPORT.md` carry the detail.

- **Replay against the public sandbox is intermittent.** Roughly one run in five, the site does
  not complete the login click and the browser stays on `login.jsp`. The account page never
  appears, so there is genuinely no GO button, and replay correctly returns a `HARD_FAILURE` at
  step 4 with a screenshot. That is the right classification for the state the page is actually
  in — but it does mean the capability is not reliable end-to-end against this host. A bounded
  re-click recovery exists for exactly this condition and is proven by `replay.check_engine`,
  but it has not yet been observed firing against the live site.

- **The shipped artifact was discovered by a local 14B model and is imperfect.** Two outputs,
  `cell` and `available_balance`, resolve to the same page cell — the model read the value once
  directly and once via a label, and the compiler does not notice two outputs pointing at one
  element. `cell` is a weak name for the same reason. The checkpoint's `expected` value is the
  literal (and absurd) balance string the sandbox was showing at discovery time. None of this
  breaks replay, but a stronger discovery model would produce a tidier artifact.

- **The operator console is a mock.** `operator/console.html` is a static page showing the
  shape of the handoff surface. Real-time co-browsing was explicitly out of scope. The command
  list it composes is genuinely the JSON the escalation module consumes, but the page does not
  talk to a live session.

- **Known redaction gaps, accepted deliberately.** Account and phone numbers are keyword-anchored
  rather than matched bare, because an unanchored 6–19 digit pattern would shred every timestamp
  and step id in the logs. Names, dates of birth and street addresses are not in scope. This is
  a conscious false-negative tradeoff, enumerated in `Memory.md`.

- **`param_names` on an intervention request is only as good as the page's markup.** It is
  derived from the accessible names of the page's input controls; Altoro's login inputs have
  none, so the field came back empty on the live handoff run. Deriving it from the artifact's
  declared inputs instead would be sturdier.

- **The second artifact, `artifacts/example_member_lookup.v1.json`, is hand-written, not
  discovered.** It describes a plausible member-search flow that was never observed against the
  live site. It validates against the schema and replays in the offline checks, which is what it
  exists for — it is not a real capability.
