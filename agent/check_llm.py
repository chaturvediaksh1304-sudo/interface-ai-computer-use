"""Self-check for the LLM client adapters.

Runs offline against a stub HTTP server; does not require Ollama to be up.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent.llm import DECISION_SCHEMA, OllamaClient, OllamaError, build_client, _normalise_refs

CAPTURED = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CAPTURED.update(body)
        payload = {"message": {"content": json.dumps(
            {"action": {"action": "click", "ref": "[e7]"}, "rationale": "bracketed on purpose"}
        )}}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
host = f"http://127.0.0.1:{server.server_port}"

try:
    client = OllamaClient(host=host)
    resp = client.messages.create(model="m", max_tokens=64, system="SYS", messages=[{"role": "user", "content": "hi"}])
    text = resp.content[0].text
    payload = json.loads(text)

    # The bracketed ref a local model copies from the prompt must be normalised,
    # because the browser layer's resolver is strict and should stay strict.
    assert payload["action"]["ref"] == "e7", payload
    assert resp.content[0].type == "text"

    # Generation must be schema-constrained and deterministic, or local models
    # flatten the action fields and decide() rejects every response.
    assert CAPTURED["format"] == DECISION_SCHEMA, "request did not constrain output to the schema"
    assert CAPTURED["options"]["temperature"] == 0, CAPTURED["options"]
    assert CAPTURED["stream"] is False
    assert CAPTURED["messages"][0] == {"role": "system", "content": "SYS"}

    # Ref normalisation is pure and must leave a clean ref alone.
    assert _normalise_refs({"action": {"ref": "e7"}})["action"]["ref"] == "e7"
    assert _normalise_refs({"action": {"action": "done"}})["action"] == {"action": "done"}

    # An unreachable server is a loud, named failure, not a silent empty decision.
    dead = OllamaClient(host="http://127.0.0.1:1")
    try:
        dead.messages.create(model="m", max_tokens=8, system="s", messages=[])
        raise AssertionError("unreachable Ollama should raise OllamaError")
    except OllamaError as exc:
        assert "cannot reach Ollama" in str(exc), exc

    # Backend selection keys off the API key so one command works either way.
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert type(build_client("auto")[0]).__name__ == "OllamaClient"
        assert build_client("ollama")[1] == "qwen2.5:14b-instruct"
        try:
            build_client("nope")
            raise AssertionError("unknown backend should raise")
        except ValueError:
            pass
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
finally:
    server.shutdown()

print("PASS: llm adapter self-check - schema-constrained deterministic request, "
      "bracketed refs normalised, unreachable server fails loudly, backend selection honours ANTHROPIC_API_KEY")
