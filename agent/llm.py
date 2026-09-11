"""LLM client adapters for the discovery loop.

``agent.decide.decide`` takes an injected client and only ever calls
``client.messages.create(...)``, so any backend presenting that one method can
drive discovery. Two are supported:

* ``anthropic.Anthropic`` used unchanged -- what Architecture.md specifies, and
  what a run should use whenever an API key is available.
* ``OllamaClient`` below, which talks to a local Ollama server over localhost.
  It needs no API key and sends nothing off the machine.

The Ollama path exists because discovery is otherwise blocked with no Anthropic
key. It is a documented deviation from Architecture.md, not a replacement: the
loop, the compiler and the artifact schema are identical either way, so an
artifact discovered through one backend replays exactly like one discovered
through the other.
"""

import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace

__all__ = ["OllamaClient", "OllamaError", "build_client",
           "DEFAULT_OLLAMA_MODEL", "DEFAULT_CLAUDE_MODEL"]

DEFAULT_OLLAMA_MODEL = "qwen2.5:14b-instruct"
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

# Ollama constrains generation to this schema. Without it, local models reliably
# flatten "ref"/"value" to the top level instead of nesting them inside
# "action", which decide() then correctly rejects as malformed.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "click", "fill", "select", "read", "done", "stuck"],
                },
                "url": {"type": "string"},
                "ref": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["action"],
        },
        "rationale": {"type": "string"},
    },
    "required": ["action", "rationale"],
}


class OllamaError(RuntimeError):
    """Raised when the local Ollama server cannot be reached or returns an error."""


def _normalise_refs(payload: dict) -> dict:
    """Strip display brackets a local model may copy out of the prompt.

    The prompt lists nodes as ``[e103] textbox ...``; smaller models sometimes
    answer with ``"[e103]"`` instead of ``"e103"``. The bracketed form would
    fail ref resolution in the browser layer, so it is normalised here rather
    than loosening the resolver, which should stay strict.
    """
    action = payload.get("action")
    if isinstance(action, dict):
        ref = action.get("ref")
        if isinstance(ref, str):
            action["ref"] = ref.strip().lstrip("[").rstrip("]")
    return payload


class OllamaClient:
    """Minimal client shaped like ``anthropic.Anthropic`` for ``decide()``.

    Only ``.messages.create(model=, max_tokens=, system=, messages=)`` is
    implemented, because that is the whole surface decide() touches. The return
    value mimics an Anthropic Messages response closely enough for that
    module's ``_response_text`` to read it.
    """

    def __init__(self, host: str | None = None, timeout: float = 240.0):
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        self.timeout = timeout
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, model, max_tokens, system, messages):
        body = json.dumps({
            "model": model,
            "stream": False,
            "format": DECISION_SCHEMA,
            # Temperature 0: discovery should be as repeatable as a sampled model allows.
            "options": {"temperature": 0, "num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}] + list(messages),
        }).encode("utf-8")
        request = urllib.request.Request(
            self.host + "/api/chat", body, {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                parsed = json.load(response)
        except urllib.error.HTTPError as exc:
            # The server answered, so it is up; the request itself was rejected.
            # A 404 here almost always means the model name is not pulled.
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise OllamaError(
                f"Ollama at {self.host} rejected the request ({exc.code}): {detail} "
                f"(model was {model!r}; `ollama list` shows what is available)"
            ) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.host}: {exc}. Is `ollama serve` running?"
            ) from exc

        text = parsed.get("message", {}).get("content", "")
        # Re-emit through the ref normaliser so decide() sees a clean ref.
        try:
            text = json.dumps(_normalise_refs(json.loads(text)))
        except (TypeError, ValueError):
            # Not valid JSON: hand it to decide() unchanged and let it fail loudly.
            pass
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def build_client(backend: str = "auto"):
    """Return ``(client, model)`` for the requested backend.

    ``"auto"`` prefers Anthropic when ANTHROPIC_API_KEY is set, and otherwise
    falls back to a local Ollama server, so the same command works in both
    situations without the caller choosing.
    """
    if backend == "auto":
        backend = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "ollama"

    if backend == "anthropic":
        import anthropic  # imported lazily so the Ollama path needs no SDK

        return anthropic.Anthropic(), DEFAULT_CLAUDE_MODEL
    if backend == "ollama":
        return OllamaClient(), os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    raise ValueError(f"unknown backend {backend!r}; expected 'anthropic', 'ollama' or 'auto'")
