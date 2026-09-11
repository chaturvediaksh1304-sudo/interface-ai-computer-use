"""Browser layer: observe the accessibility tree, act on it, never leave the allowlist.

This is the only module that touches Playwright. Everything above it (the discovery
loop, the replay engine) speaks in terms of an ``Observation`` -- a flattened
accessibility tree with a stable ``ref`` per node -- and an action dict using the same
vocabulary as ``artifact.schema.Step.action``.

Accessibility tree, not DOM
---------------------------
Architecture.md's central bet is that the a11y tree is the addressing mechanism, because
it survives legacy surfaces that have no clean DOM. Playwright's old tree API,
``page.accessibility.snapshot()``, NO LONGER EXISTS in the installed version (1.62) --
it was deprecated in favour of ARIA snapshots and has since been removed from the Python
bindings entirely. The supported successor is ``locator.aria_snapshot(mode="ai")``, which
returns the same accessibility tree serialised as YAML with a stable ``[ref=eN]`` handle
on every addressable node. That is what this module uses. It is still the accessibility
tree -- roles and accessible names computed by the browser -- not DOM scraping.

ref -> element
--------------
A ``ref`` is Playwright's own handle, resolved back through its ``aria-ref=`` selector
engine. We do not maintain a parallel ref table, because Playwright already owns the
element registry; any home-grown index would be a second source of truth that can
disagree with the first.

READ THIS BEFORE USING A REF: **a ref is only valid for the snapshot that produced it.**
Take a fresh ``observe()`` and act on those refs; never stash a ref and use it later.
Two independent reasons:

* Refs restart at ``e1`` in every new document, so a ref held across a navigation can
  resolve to a completely different element on the new page -- the "clicked the wrong
  thing" failure this module must not have.
* Ref numbering is Playwright's business, not ours. On the installed build (1.62.0) refs
  measurably survive re-snapshots of the same document, but that is an implementation
  detail nobody promised, and it has been observed differing elsewhere.

``_resolve_ref`` therefore enforces the strict rule: a ref is accepted only if it came
from the most recent ``observe()`` *and* that observation was taken on the document
currently loaded. Anything else raises ``StaleRefError`` immediately -- no timeout, no
guessing.

Failure policy
--------------
``AllowlistViolation`` always propagates: a guardrail that a caller can catch as a
generic action failure is not a guardrail. Everything else that goes wrong inside
``act()`` -- element gone, timeout, a locator that matched nothing -- comes back as
``ActResult(ok=False, detail=...)``, because the discovery loop's whole job is to read a
failed action and try something else. Timeouts are bounded (10s by default) and are
failures, never retried in a loop.
"""

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from guardrails.allowlist import AllowlistViolation

DEFAULT_TIMEOUT_MS = 10_000

# Per-candidate budget when walking a locator's fallback chain. Bounded on purpose: a
# chain of five fallbacks must not be able to spend five full default timeouts.
CANDIDATE_TIMEOUT_MS = 2_000
# The primary locator is what discovery recorded, so it is given longer than a
# fallback. Still bounded: worst case is this plus one short wait per fallback.
PRIMARY_TIMEOUT_MS = 10_000  # the sandbox is a public demo host and is sometimes slow;
# with two named fallbacks at 2s each the worst case is still bounded well under the
# limit check_browser asserts.

# A ref that passed the staleness check but whose element has since left the page fails
# here. Short, because the answer is already known -- waiting 10s to be told an element
# is gone just makes a stuck run slower to diagnose.
REF_TIMEOUT_MS = 1_500

# Roles that can take keyboard focus. The ARIA snapshot does not carry focusability, and
# asking the page about every node would be one round-trip per node, so this is derived
# from the role.
# ponytail: role-based heuristic; compute per-element focusability only if some target
# surface turns out to put tabindex on roles outside this set.
_FOCUSABLE_ROLES = frozenset(
    {
        "button", "checkbox", "combobox", "link", "listbox", "menuitem",
        "menuitemcheckbox", "menuitemradio", "option", "radio", "searchbox",
        "slider", "spinbutton", "switch", "tab", "textbox",
    }
)

# One line of an ARIA snapshot, e.g.
#   - textbox "Username" [ref=e3]: someone
# Property lines ("- /url: /bank/main.jsp") and wrapped text deliberately fail to match
# and are skipped.
_NODE_LINE = re.compile(
    r'^\s*-\s+(?P<role>[A-Za-z][\w-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?:\s+\[[^\]]*\])*)'
    r'\s*(?::\s*(?P<value>.*))?$'
)
_ATTR = re.compile(r"\[([^\]=]+)(?:=([^\]]*))?\]")

# Roles whose current contents are a value the agent cares about. A filled control
# reports its text inline ("- textbox \"Member ID\": bob") unless it also has property
# children, in which case the text moves to a nested "- text:" child -- see the stack in
# _parse_snapshot. A button's child text is its label, not a value, hence the whitelist.
_VALUE_ROLES = frozenset({"combobox", "searchbox", "slider", "spinbutton", "textbox"})

# How each A11yLocator strategy is expressed as a Playwright locator. `exact` and `nth`
# are honoured by _build_locator.
_STRATEGY_FIELD = {
    "role": "role", "label": "name", "text": "name",
    "placeholder": "name", "testid": "name", "css": "selector",
}


class BrowserError(Exception):
    """Something went wrong in the browser layer that is not an allowlist violation."""


class StaleRefError(BrowserError):
    """A ref was used that does not belong to the document currently loaded."""


class Observation:
    """A snapshot of the current page, in the form the LLM and the replay engine read.

    ``nodes`` is the accessibility tree flattened depth-first. Nodes that Playwright
    gives a ref to are addressable by an action; nodes without one (static text, the
    options inside a closed combobox) carry ``ref=None`` and are there as context.
    """

    __slots__ = ("url", "title", "nodes", "text_digest")

    def __init__(self, url: str, title: str, nodes: list[dict], text_digest: str):
        self.url = url
        self.title = title
        self.nodes = nodes
        self.text_digest = text_digest

    def __repr__(self) -> str:
        return f"Observation(url={self.url!r}, title={self.title!r}, nodes={len(self.nodes)})"


class ActResult:
    """The outcome of one action, plus the page state it left behind."""

    __slots__ = ("ok", "action", "detail", "observation")

    def __init__(self, ok: bool, action: dict, detail: str, observation: Observation | None):
        self.ok = ok
        self.action = action
        self.detail = detail
        self.observation = observation

    def __repr__(self) -> str:
        return f"ActResult(ok={self.ok}, action={self.action!r}, detail={self.detail!r})"


def _unescape(name: str) -> str:
    return name.replace('\\"', '"').replace("\\\\", "\\")


def _parse_snapshot(snapshot: str) -> list[dict]:
    """Flatten an ARIA snapshot (YAML) into the node dicts the contract specifies."""
    nodes = []
    open_parents: list[tuple[int, dict]] = []  # (indent, node) for enclosing nodes
    for line in snapshot.splitlines():
        match = _NODE_LINE.match(line)
        if not match:
            continue  # property line ("- /url: ...") or wrapped text
        attrs = dict(_ATTR.findall(match.group("attrs") or ""))
        value = (match.group("value") or "").strip()
        role = match.group("role")
        node = {
            "ref": attrs.get("ref"),
            "role": role,
            "name": _unescape(match.group("name")) if match.group("name") else None,
            # A YAML block indicator means the text continues on wrapped lines; the full
            # text is in text_digest, so there is nothing useful to put here.
            "value": None if value in ("", "|", "|-") else value,
            "focusable": role in _FOCUSABLE_ROLES and "disabled" not in attrs,
        }

        indent = len(line) - len(line.lstrip())
        while open_parents and open_parents[-1][0] >= indent:
            open_parents.pop()
        if open_parents and role == "text" and node["value"]:
            parent = open_parents[-1][1]
            if parent["role"] in _VALUE_ROLES and parent["value"] is None:
                parent["value"] = node["value"]
        open_parents.append((indent, node))
        nodes.append(node)
    return nodes


# Where the browser parks the tab when a navigation fails or is blocked.
# Deliberately NOT "about:blank": that is a fresh session with nothing loaded,
# and acting on it is a caller mistake that should still raise, not a dead end
# to recover from.
# How long to let a click-triggered navigation settle before judging the result.
_SETTLE_MS = 3000

_ERROR_PAGE_SCHEMES = ("chrome-error://",)

class BrowserSession:
    """One Chromium session, fenced by the allowlist.

    Every navigation and every action is checked before it happens, and the context also
    aborts any document request that fails the same check -- so a link inside the page
    cannot carry the session somewhere the allowlist does not name.
    """

    def __init__(self, allowlist, logger, headless: bool = True):
        self.allowlist = allowlist
        self.logger = logger
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        # Refs from the most recent observe(), and the identity of the document they
        # were taken on. Both are required to accept a ref -- see _resolve_ref.
        self._doc_nonce = None
        self._last_refs: set[str] = set()

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context()
        self._context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        self._context.route("**/*", self._guard_route)
        self._page = self._context.new_page()
        self.logger.info("browser.start", extra={"headless": self.headless})

    def stop(self) -> None:
        """Close everything. Safe to call twice, and safe to call after a failed start."""
        for attr, close in (
            ("_context", "close"), ("_browser", "close"), ("_playwright", "stop"),
        ):
            obj = getattr(self, attr)
            if obj is None:
                continue
            try:
                getattr(obj, close)()
            except PlaywrightError:
                pass  # already gone; stop() must not fail on a half-dead session
            setattr(self, attr, None)
        self._page = None
        self._last_refs.clear()
        self._doc_nonce = None
        self.logger.info("browser.stop")

    @property
    def url(self) -> str:
        return self._require_page().url

    def _require_page(self):
        if self._page is None:
            raise BrowserError("session not started; call start() first")
        return self._page

    def _guard_route(self, route, request) -> None:
        """Abort any top-level document load the allowlist does not permit.

        The explicit checks in navigate/act cover what this layer initiates; this covers
        what the *page* initiates -- a redirect, a meta refresh, a link the LLM clicked.
        Sub-resources are not filtered: the allowlist describes where the agent may act,
        not which CDN a stylesheet comes from.
        """
        if request.resource_type != "document":
            route.continue_()
            return
        try:
            self.allowlist.check_navigation(request.url)
        except AllowlistViolation as exc:
            # A route handler cannot raise usefully -- Playwright swallows it -- so the
            # loud part is the log line plus the aborted load the caller will see.
            self.logger.warning(
                "browser.blocked_navigation", extra={"blocked_url": request.url, "reason": str(exc)}
            )
            route.abort()
            return
        route.continue_()

    # -- observe -----------------------------------------------------------------

    def observe(self) -> Observation:
        page = self._require_page()
        snapshot = page.locator("body").aria_snapshot(mode="ai", timeout=DEFAULT_TIMEOUT_MS)
        nodes = _parse_snapshot(snapshot)

        # This observation's refs replace the previous set outright. The nonce lives on
        # `window`, so a new document resets it -- that is how a ref held across a
        # navigation gets caught even if the number happens to exist on the new page.
        self._doc_nonce = page.evaluate("() => (window.__ia_doc ??= String(Math.random()))")
        self._last_refs = {n["ref"] for n in nodes if n["ref"]}

        try:
            text = page.locator("body").inner_text(timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightError:
            text = ""
        digest = " ".join(text.split())[:1200]

        observation = Observation(page.url, page.title(), nodes, digest)
        self.logger.info(
            "browser.observe",
            extra={
                "url": observation.url,
                "title": observation.title,
                "node_count": len(nodes),
                "ref_count": sum(1 for n in nodes if n["ref"]),
                "digest_chars": len(digest),
            },
        )
        return observation

    def screenshot(self, path: str) -> str:
        self._require_page().screenshot(path=path, full_page=True, timeout=DEFAULT_TIMEOUT_MS)
        self.logger.info("browser.screenshot", extra={"path": path})
        return path

    # -- element resolution ------------------------------------------------------

    def _resolve_ref(self, ref: str):
        """Turn a ref back into a locator, or fail loudly if it is not current.

        Two gates, both cheap and both instant. The nonce gate catches a ref held across
        a navigation -- the dangerous case, because Playwright renumbers from e1 and the
        ref would otherwise resolve to an unrelated element. The membership gate catches
        a ref from an older snapshot of this same document. Only then do we touch the
        page, on a short budget, because an element that has since been removed should
        say so in a second rather than in ten.
        """
        page = self._require_page()
        if self._doc_nonce is None:
            raise StaleRefError(f"ref {ref!r} used before any observe(); observe first")
        if page.evaluate("() => window.__ia_doc ?? null") != self._doc_nonce:
            raise StaleRefError(
                f"ref {ref!r} belongs to a previous document; re-observe before acting"
            )
        if ref not in self._last_refs:
            raise StaleRefError(
                f"ref {ref!r} is not in the most recent observation "
                f"({len(self._last_refs)} refs); re-observe before acting"
            )
        locator = page.locator(f"aria-ref={ref}")
        try:
            locator.wait_for(state="attached", timeout=REF_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise StaleRefError(
                f"ref {ref!r} no longer resolves to an element on {page.url}"
            ) from None
        return locator

    def _build_locator(self, spec: dict):
        """One A11yLocator dict -> a Playwright locator."""
        page = self._require_page()
        strategy = spec["strategy"]
        exact = bool(spec.get("exact", False))
        name = spec.get("name")

        if strategy == "role":
            kwargs = {"exact": exact} if name else {}
            locator = page.get_by_role(spec["role"], name=name, **kwargs) if name \
                else page.get_by_role(spec["role"])
        elif strategy == "label":
            locator = page.get_by_label(name, exact=exact)
        elif strategy == "text":
            locator = page.get_by_text(name, exact=exact)
        elif strategy == "placeholder":
            locator = page.get_by_placeholder(name, exact=exact)
        elif strategy == "testid":
            locator = page.get_by_test_id(name)
        elif strategy == "css":
            locator = page.locator(spec["selector"])
        else:
            raise BrowserError(f"unknown locator strategy {strategy!r}")

        nth = spec.get("nth")
        return locator.nth(nth) if nth is not None else locator

    @staticmethod
    def _describe(spec: dict) -> str:
        """How a locator reads in a log line or an ActResult.detail."""
        described = f"{spec['strategy']}={spec.get(_STRATEGY_FIELD[spec['strategy']])!r}"
        if spec["strategy"] == "role" and spec.get("name"):
            described += f" name={spec['name']!r}"
        return described

    def _resolve_locator(self, locator_spec: dict):
        """Try ``primary``, then each fallback in order; report which one matched.

        Phase 4 replay needs to know a fallback was used -- a capability that only still
        works via its third fallback is a capability whose artifact wants re-recording --
        so the winning strategy is carried back in ActResult.detail.
        """
        candidates = [("primary", locator_spec["primary"])]
        candidates += [
            (f"fallback[{i}]", spec) for i, spec in enumerate(locator_spec.get("fallbacks") or [])
        ]

        tried = []
        for label, spec in candidates:
            locator = self._build_locator(spec)
            # The primary is what discovery actually recorded, so give it the full
            # timeout; fallbacks get the shorter one. Otherwise a slow page makes
            # the primary lose a race to a looser fallback -- and a positional
            # fallback matches SOME element instantly, so the loss is silent and
            # the wrong element gets clicked. That showed up as a replay that
            # passed or failed depending on how fast the page rendered.
            budget = PRIMARY_TIMEOUT_MS if label == "primary" else CANDIDATE_TIMEOUT_MS
            try:
                locator.wait_for(state="attached", timeout=budget)
            except PlaywrightError as exc:
                # Any reason this candidate cannot be used means try the next one.
                # Timeouts are the obvious case, but an ambiguous match matters just
                # as much: a loose name can hit several nodes (in nested tables an
                # ancestor cell contains its descendants' text and matches too), and
                # a strict-mode violation there should fall through to a more
                # specific fallback rather than abandoning the chain that exists
                # precisely to handle it.
                reason = "ambiguous" if "strict mode violation" in str(exc) else "no match"
                tried.append(f"{label} {self._describe(spec)} ({reason})")
                continue
            return locator, f"matched {label} {self._describe(spec)}"

        raise BrowserError("no locator matched; tried " + "; ".join(tried))

    def _target(self, action: dict):
        """Resolve whichever addressing form the action used."""
        if "ref" in action:
            return self._resolve_ref(action["ref"]), f"matched ref {action['ref']!r}"
        if "locator" in action:
            return self._resolve_locator(action["locator"])
        raise BrowserError(f"action {action.get('action')!r} needs a 'ref' or a 'locator'")

    # -- act ---------------------------------------------------------------------

    def act(self, action: dict) -> ActResult:
        """Execute one action dict. AllowlistViolation propagates; nothing else does."""
        page = self._require_page()
        action_type = action["action"]

        # Guardrail first, before any browser work. For a navigate the URL under test is
        # the destination; for everything else it is the page we are about to act on.
        target_url = action["url"] if action_type == "navigate" else page.url
        if action_type != "navigate" and target_url.startswith(_ERROR_PAGE_SCHEMES):
            # The tab is parked on a browser error page after a blocked or failed
            # navigation. Acting here is meaningless, but it is not an allowlist
            # violation either -- nothing out of scope was reached. Report it as a
            # failed action naming the way out, so the caller can navigate back
            # instead of being wedged by a confusing scheme error. A navigate is
            # still checked normally below: that is exactly how you escape.
            return self._result(
                False, action,
                f"the tab is on a browser error page ({target_url}); "
                f"navigate to an allowed URL before acting again",
            )
        self.allowlist.check_action(action_type, target_url)

        self._log_action(action)
        try:
            detail = self._perform(action, action_type)
        except (BrowserError, PlaywrightError) as exc:
            return self._result(False, action, f"{type(exc).__name__}: {exc}")

        # A click can navigate. If it landed somewhere out of scope, that is a violation
        # even though the action itself was allowed when it started.
        landed = page.url
        if landed.startswith(_ERROR_PAGE_SCHEMES):
            # A blocked or failed navigation parks the tab on a browser error page.
            # Nothing out of scope was reached -- the block worked -- but the session
            # is now somewhere useless, so report it as a failed action with a way
            # out rather than raising a confusing scheme violation that would leave
            # the caller wedged here with no recovery.
            return self._result(
                False, action,
                f"navigation did not complete; the tab is on a browser error page "
                f"({landed}). Navigate to an allowed URL to continue.",
            )
        self.allowlist.check_navigation(landed)
        return self._result(True, action, detail)

    def _perform(self, action: dict, action_type: str) -> str:
        page = self._require_page()

        if action_type == "navigate":
            page.goto(action["url"], timeout=DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded")
            return f"navigated to {page.url}"

        locator, how = self._target(action)

        if action_type == "click":
            locator.click(timeout=DEFAULT_TIMEOUT_MS)
            # A click that starts a navigation returns before that navigation
            # resolves, so the URL check in act() would still see the old page
            # and call the action a success. Wait for the load to settle first:
            # otherwise a click that ends on a blocked page is recorded as
            # working, and discovery bakes it into a capability that cannot
            # replay. A click that navigates nowhere just falls straight through.
            try:
                # "networkidle" rather than "domcontentloaded": this app renders
                # into frames, and the outer document reports itself loaded while
                # the frame holding the actual content is still arriving. Settling
                # on the weaker signal makes the NEXT step race the render, which
                # showed up as a replay that succeeded once and failed the next
                # time -- the one thing a deterministic replay must never do.
                page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
            except PlaywrightError:
                pass
            return how
        if action_type == "fill":
            locator.fill(action["value"], timeout=DEFAULT_TIMEOUT_MS)
            return f"{how}; filled {len(action['value'])} chars"
        if action_type == "select":
            chosen = locator.select_option(action["value"], timeout=DEFAULT_TIMEOUT_MS)
            return f"{how}; selected {chosen}"
        if action_type == "read":
            # `.value` for form controls, rendered text for everything else -- one
            # round-trip instead of branching on the element type from Python.
            text = locator.evaluate(
                "e => (e.value !== undefined ? e.value : e.innerText) ?? ''",
                timeout=DEFAULT_TIMEOUT_MS,
            )
            return text.strip()

        raise BrowserError(f"unsupported action {action_type!r}")

    def _log_action(self, action: dict) -> None:
        """Log the shape of an action, never a filled value."""
        fields = {"action": action["action"]}
        if "ref" in action:
            fields["ref"] = action["ref"]
        if "locator" in action:
            fields["locator_strategy"] = action["locator"]["primary"]["strategy"]
        if "url" in action:
            fields["url"] = action["url"]
        if "value" in action:
            # The value may be a member id, a password, anything. Redaction would catch
            # the patterns it knows; not logging it at all catches the rest.
            fields["value_len"] = len(action["value"])
        self.logger.info("browser.act", extra=fields)

    def _result(self, ok: bool, action: dict, detail: str) -> ActResult:
        try:
            observation = self.observe()
        except PlaywrightError as exc:
            observation = None
            detail = f"{detail} (post-action observe failed: {exc})"
        self.logger.info(
            "browser.act_result",
            extra={"action": action["action"], "ok": ok, "detail": detail},
        )
        return ActResult(ok, action, detail, observation)
