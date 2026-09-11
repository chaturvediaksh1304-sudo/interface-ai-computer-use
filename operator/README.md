# Operator console

`console.html` is the human side of the Phase 6 escalation handoff.

When the automation gets stuck inside the target UI, it stops rather than guessing, raises an
intervention request carrying everything a human needs to triage, and holds its browser session
open. This page is what the operator opens. It shows why the run stopped, what state the session
is sitting in, and it composes the command list that gets handed back to the escalation module.

Open it directly in a browser. There is no build step, no server, and no network request of any
kind: the CSS and JavaScript are inline, the type is a system font stack, and the only external
reference is a relative path to a screenshot already in the repo.

```
open operator/console.html
```

## What the page shows, in the order an operator needs it

1. **Why it stopped.** The `reason` string from the intervention request is the largest text on
   the page and the page's `h1`, because that is what an operator triages on.
2. **What it was trying to do,** and where in the plan it got to: the goal, the capability id,
   and the step index it halted on.
3. **The state the session is holding:** current URL, page title, the accessibility-tree
   observation digest, and the last frame captured before the halt.
4. **The inputs the run holds,** by name only.
5. **The command list** the operator composes, rendered live as the JSON the escalation module
   consumes.
6. **The guardrails that still apply** to the operator, with the live allowlist spelled out.

The held-session clock in the top bar counts up from `raised_at`. It is the only animated element
on the page and it is there to make one thing unmissable: a real browser session is parked and
waiting on this person.

## What is real

- **The request shape.** The page renders the exact object the escalation module produces:
  `request_id`, `raised_at`, `goal`, `capability_id`, `step_index`, `reason`, `url`,
  `page_title`, `observation_digest`, `screenshot`, `param_names`.
- **The command vocabulary.** The composed JSON emits only the seven documented command kinds,
  with exactly the documented keys:
  `{"kind":"navigate","url":...}`, `{"kind":"click","ref":...}`,
  `{"kind":"fill","ref":...,"value":...}`, `{"kind":"select","ref":...,"value":...}`,
  `{"kind":"note","text":...}`, `{"kind":"resume"}`, `{"kind":"abort"}`.
- **The screenshot.** `../evidence/replay_altoro.png` is a real capture from a real run. If the
  file is absent the figure degrades to an explanatory block rather than a broken image.
- **The allowlist.** The domains, schemes, paths, and action kinds in the guardrail band are the
  contents of `guardrails/allowlist.json`, not illustrative values.

## What is mocked

- **Co-browsing.** Real-time remote control of the live session is explicitly out of scope per the
  PRD. This page does not attach to a browser, render a live view, or drive anything. It shows the
  captured state and composes instructions.
- **The backend.** There is no fetch and no submit. Nothing is posted anywhere. "Resume" and
  "Abandon" append the corresponding terminal command and seal the list; handing that list to the
  escalation module is a copy-and-save step today.
- **The example request.** No `evidence/interventions/*.json` file existed when this page was
  built, so the example request is inlined in the page as a JavaScript object matching the agreed
  shape. In the running system that object is loaded from
  `evidence/interventions/<request_id>.json`. The session identifier shown alongside the URL is
  likewise illustrative.
- **Operator identity.** The guardrail copy refers to actions being logged against an operator
  identity. Authentication for this surface is not built; the console has no login.

## Assumptions worth flagging

- **The payload is a bare JSON array of commands**, not an object wrapping them. The brief
  specified the per-command shape and called the output "the composed command list", so the array
  is the most literal reading. The `request_id` it belongs to is displayed on the page and is in
  the suggested filename. If the escalation module wants an envelope with `request_id` alongside
  `commands`, that is a one-line change in `render()`.
- **The `ref` format is not validated here.** The console passes the operator's reference string
  through untouched and lets the replay engine's locator resolver interpret it. The field hint
  suggests a `role=... name=...` form to match how the artifact locators are expressed.
- **Resume with an empty list is allowed.** Sometimes the right intervention is performed out of
  band and the correct instruction is simply "try that step again". The empty state says so.

## Secrets

The page never displays a secret value and has no means of obtaining one. `param_names` is a list
of names, and each is rendered with an explicit "value withheld" marker. The `fill` and `select`
value fields carry a visible warning that anything typed there is written to the intervention log
under the same redaction rules as automated steps, and the held-inputs section states that the
session already holds the credentials so the operator never needs to retype one.

## Accessibility and layout

Semantic landmarks and real elements throughout (`header`, `main` content in `section`s, `dl` for
key/value data, `ol` for the ordered command list, a real `form` with a submit handler). Every
input has a `label` above it, never a placeholder standing in for one. Focus is visible on every
interactive element via a 2px accent outline with offset. The layout is a single column below
1080px and never scrolls horizontally: long URLs wrap, and the digest and JSON blocks scroll
inside their own containers. The pulse on the live indicator is suppressed under
`prefers-reduced-motion: reduce`; the clock itself keeps running, because it is information
rather than decoration.

## Design notes

The page is deliberately closer to an incident console than to a product dashboard: one locked
dark theme, hairline rules instead of card containers, a monospace face for every identifier,
number, URL and payload, and a single amber accent reserved for the live-session state and the
primary action. Red appears exactly once, on the destructive control. There is no iconography and
no decoration; density and typography carry the hierarchy.
