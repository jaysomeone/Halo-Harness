"""tests.helpers.mock_ollama -- Halo 2.0.3 round 2: a fake Ollama native-API
upstream, same shape as `tests/helpers/mock_openai.py`
(`BaseHTTPRequestHandler` + `ThreadingMixIn`, `free_port()` reused from
there rather than duplicated) -- serves `GET /api/version`, `GET
/api/tags`, `POST /api/show`, `GET /api/ps`, and scripted NDJSON `POST
/api/chat` (plain text, tool_calls single/parallel, thinking, every
`done_reason` value, timing fields, and a "what did you actually send"
echo scenario for pinning `options.num_ctx`/`keep_alive`/`think` on the
wire). Never a literal LAN address anywhere in this file
(`tests/test_privacy_scan.py`).
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from tests.helpers.mock_openai import ClientDisconnected, free_port

DEFAULT_VERSION = {"version": "0.1.0-mock"}

DEFAULT_TAGS = {"models": [
    {"name": "qwen3:30b", "model": "qwen3:30b", "modified_at": "2026-01-01T00:00:00Z", "size": 123456,
     "digest": "sha256:deadbeef1", "details": {"family": "qwen3", "families": ["qwen3"],
     "parameter_size": "30B", "quantization_level": "Q4_0", "format": "gguf"}},
    {"name": "gpt-oss:20b", "model": "gpt-oss:20b", "modified_at": "2026-01-01T00:00:00Z", "size": 654321,
     "digest": "sha256:deadbeef2", "details": {"family": "gptoss", "families": ["gptoss"],
     "parameter_size": "20B", "quantization_level": "Q4_0", "format": "gguf"}},
]}

DEFAULT_SHOW = {
    "qwen3:30b": {"modelfile": "", "parameters": "", "template": "",
                  "capabilities": ["tools", "thinking"],
                  "details": {"family": "qwen3", "quantization_level": "Q4_0"},
                  "model_info": {"qwen3.context_length": 40960}},
    "gpt-oss:20b": {"modelfile": "", "parameters": "", "template": "",
                    "capabilities": ["tools", "thinking"],
                    "details": {"family": "gptoss", "quantization_level": "Q4_0"},
                    "model_info": {"gptoss.context_length": 131072}},
}

DEFAULT_PS = {"models": []}

# Halo 2.0.3 round 5c: the default scripted `/api/create` progress sequence
# -- a plain "it worked" streamed response; `MockUpstream.create_responses`
# overrides per-model-name for an error/custom-sequence test.
DEFAULT_CREATE_LINES = [{"status": "reading model metadata"}, {"status": "creating model layer"},
                        {"status": "writing manifest"}, {"status": "success"}]


def partial_offload_ps_entry(model: str, *, size: int, size_vram: int, context_length: int,
                              expires_at: str = "2026-01-01T01:00:00Z") -> dict:
    """Halo 2.0.3 round 3: one `/api/ps` entry with `size_vram < size`
    (brief item 7: "a partial-offload /api/ps case") -- the exact shape
    `providers.ollama_panel.offload_sentence`/`analyze_host` read.
    Callers typically wrap this in `{"models": [this]}` for a mock's
    `ps_response`."""
    return {"model": model, "name": model, "size": size, "size_vram": size_vram,
            "context_length": context_length, "expires_at": expires_at}


def multi_host_config(hosts: "dict", *, default: "str | None" = None) -> list:
    """Halo 2.0.3 round 3: an `ollama.hosts`-shaped config list (brief
    item 7: "a multi-host config") for several `MockUpstream` instances
    keyed by the NAME each entry should get -- `{name, url, default}`
    per entry, never an `api_key` (every mock host here is
    unauthenticated, same as any plain local/LAN Ollama daemon).
    `default`, when given, is the one name whose entry gets `"default":
    true`; otherwise the first name in `hosts` does."""
    names = list(hosts)
    default = default if default in names else (names[0] if names else None)
    return [{"name": name, "url": mock.base_url, "default": name == default} for name, mock in hosts.items()]


def send_json(handler: "_Handler", status: int, obj) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def start_ndjson(handler: "_Handler", status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()


def write_ndjson_line(handler: "_Handler", obj: dict) -> None:
    """One NDJSON line, HTTP-chunk-framed (research doc Q1: Ollama's
    stream is one complete JSON object per line, never SSE "data:"
    framing)."""
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    frame = b"%x\r\n" % len(line) + line + b"\r\n"
    try:
        handler.wfile.write(frame)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError) as e:
        raise ClientDisconnected(str(e)) from None


def end_ndjson(handler: "_Handler") -> None:
    try:
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


def abrupt_disconnect(handler: "_Handler") -> None:
    try:
        handler.connection.shutdown(__import__("socket").SHUT_RDWR)
    except OSError:
        pass
    try:
        handler.connection.close()
    except OSError:
        pass
    handler.close_connection = True


def _finish_chat(handler: "_Handler", lines: list) -> None:
    start_ndjson(handler)
    for obj in lines:
        write_ndjson_line(handler, obj)
    end_ndjson(handler)


# ---- scripted /api/chat scenarios, keyed off the request body's "model" ---

def _scn_plain_text(h, body):
    _finish_chat(h, [
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo!"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "total_duration": 1_000_000, "load_duration": 100_000,
         "prompt_eval_count": 10, "prompt_eval_duration": 200_000,
         "eval_count": 4, "eval_duration": 300_000},
    ])


def _scn_thinking(h, body):
    _finish_chat(h, [
        {"message": {"role": "assistant", "content": "", "thinking": "Let me "}, "done": False},
        {"message": {"role": "assistant", "content": "", "thinking": "think it through..."}, "done": False},
        {"message": {"role": "assistant", "content": "the answer"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 8, "eval_count": 5},
    ])


def _scn_tool_call_single(h, body):
    _finish_chat(h, [
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "Read", "arguments": {"file_path": "a.txt"}}}]},
         "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6},
    ])


def _scn_tool_calls_parallel(h, body):
    _finish_chat(h, [
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"index": 0, "function": {"name": "Read", "arguments": {"file_path": "a.txt"}}},
            {"index": 1, "function": {"name": "Read", "arguments": {"file_path": "b.txt"}}},
        ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 9},
    ])


def _scn_done_reason_stop(h, body):
    _finish_chat(h, [{"message": {"role": "assistant", "content": "ok"}, "done": True,
                       "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 1}])


def _scn_done_reason_length(h, body):
    _finish_chat(h, [{"message": {"role": "assistant", "content": "cut off here"}, "done": True,
                       "done_reason": "length", "prompt_eval_count": 5, "eval_count": 100}])


def _scn_done_reason_unload(h, body):
    _finish_chat(h, [{"message": {"role": "assistant", "content": ""}, "done": True,
                       "done_reason": "unload", "prompt_eval_count": 0, "eval_count": 0}])


def _scn_echo_wire(h, body):
    """Echoes back `options.num_ctx`/`keep_alive`/`think` as the reply TEXT
    -- the pinning-test scenario for "these fields are actually on the
    wire", cheaper than inspecting `mock.requests` by hand in every test."""
    opts = (body or {}).get("options") or {}
    payload = {"num_ctx": opts.get("num_ctx"), "keep_alive": (body or {}).get("keep_alive"),
               "think": (body or {}).get("think")}
    _finish_chat(h, [{"message": {"role": "assistant", "content": json.dumps(payload)}, "done": True,
                       "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1}])


def _scn_die_mid_stream(h, body):
    start_ndjson(h)
    write_ndjson_line(h, {"message": {"role": "assistant", "content": "partial then dies"}, "done": False})
    abrupt_disconnect(h)


class ScriptedByCallCount:
    """A scenario callable keyed on HOW MANY TIMES this one instance has
    been invoked (`steps[call_index]`, clamped to the last step once
    exhausted) -- for "first call: X, second call (Halo's own retry): Y"
    scripts, since a `done_reason: "load"` retry resends the IDENTICAL
    body, giving `ScriptedTurns`' request-content keying (mock_openai.py)
    nothing to key off. Construct a FRESH instance per test (never shared
    at module scope) so call counts never leak between tests."""

    def __init__(self, steps: list):
        self.steps = steps
        self.calls = 0

    def __call__(self, handler, body) -> None:
        if not self.steps:
            send_json(handler, 500, {"error": "ScriptedByCallCount has no steps configured"})
            return
        idx = min(self.calls, len(self.steps) - 1)
        self.calls += 1
        step = self.steps[idx]
        if callable(step) and not isinstance(step, list):
            step(handler, body)
        else:
            _finish_chat(handler, step)


SCENARIOS = {
    "plain-text": _scn_plain_text,
    "thinking": _scn_thinking,
    "tool-call-single": _scn_tool_call_single,
    "tool-calls-parallel": _scn_tool_calls_parallel,
    "done-reason-stop": _scn_done_reason_stop,
    "done-reason-length": _scn_done_reason_length,
    "done-reason-unload": _scn_done_reason_unload,
    "echo-wire": _scn_echo_wire,
    "die-mid-stream": _scn_die_mid_stream,
}


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError, OSError)):
            return
        traceback.print_exc()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (ValueError, TypeError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return {}

    def _record(self, method: str, body) -> None:
        mock: MockUpstream = self.server.mock  # type: ignore[attr-defined]
        with mock._lock:
            mock._requests.append({
                "method": method, "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body, "ts": time.monotonic(),
            })

    def do_GET(self):
        mock: MockUpstream = self.server.mock  # type: ignore[attr-defined]
        self._record("GET", None)
        if self.path.rstrip("/") == "/api/version":
            send_json(self, 200, mock.version_response)
        elif self.path.rstrip("/") == "/api/tags":
            send_json(self, 200, mock.tags_response)
        elif self.path.rstrip("/") == "/api/ps":
            send_json(self, 200, mock.ps_response)
        else:
            send_json(self, 404, {"error": f"mock ollama: unknown GET path {self.path!r}"})

    def do_HEAD(self):
        # Halo 2.0.3 round 5c FIX PASS: `providers.ollama.check_blob_exists`'s
        # own `HEAD /api/blobs/sha256:<hex>` -- 200 when `mock.known_blobs`
        # (per-instance, never module state) already has this digest, 404
        # otherwise (the real daemon's own "needs uploading" signal).
        mock: MockUpstream = self.server.mock  # type: ignore[attr-defined]
        self._record("HEAD", None)
        path = self.path.rstrip("/")
        if path.startswith("/api/blobs/sha256:"):
            digest = path[len("/api/blobs/sha256:"):]
            with mock._lock:
                known = digest in mock.known_blobs
            self.send_response(200 if known else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        mock: MockUpstream = self.server.mock  # type: ignore[attr-defined]
        path = self.path.rstrip("/")
        if path.startswith("/api/blobs/sha256:"):
            # `providers.ollama.upload_blob`'s own raw-bytes POST -- NEVER
            # routed through `_read_json_body` (it would silently discard
            # binary content as unparseable JSON and record `{}`).
            digest = path[len("/api/blobs/sha256:"):]
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except (ValueError, TypeError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            import hashlib
            actual = hashlib.sha256(raw).hexdigest()
            with mock._lock:
                mock._requests.append({"method": "POST", "path": self.path,
                                        "headers": {k.lower(): v for k, v in self.headers.items()},
                                        "body": {"_blob_digest_in_url": digest, "_raw_len": len(raw),
                                                  "_digest_matches_bytes": actual == digest},
                                        "ts": time.monotonic()})
                mock.known_blobs.add(digest)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._read_json_body()
        self._record("POST", body)
        if path == "/api/show":
            name = (body or {}).get("model")
            show = mock.show_responses.get(name)
            if show is None:
                send_json(self, 404, {"error": f"model '{name}' not found"})
            else:
                send_json(self, 200, show)
            return
        if path == "/api/create":
            # Halo 2.0.3 round 5c: `providers.ollama.create_model`'s own
            # NDJSON streamed-status endpoint -- `create_responses` scripts
            # an error/custom sequence per model NAME (the request's own
            # "model" field, i.e. the NEW name being created, never the
            # source model); unscripted names get `DEFAULT_CREATE_LINES`.
            name = (body or {}).get("model") or ""
            lines = mock.create_responses.get(name, list(DEFAULT_CREATE_LINES))
            try:
                _finish_chat(self, lines)
            except ClientDisconnected:
                with mock._lock:
                    mock.disconnect_events.append({"scenario": f"create:{name}", "ts": time.monotonic()})
            return
        if path == "/api/chat":
            scenario = (body or {}).get("model") or ""
            fn = mock.scenarios.get(scenario)
            try:
                if fn is not None:
                    fn(self, body)
                else:
                    send_json(self, 404, {"error": f"unknown mock ollama scenario '{scenario}'"})
            except ClientDisconnected:
                with mock._lock:
                    mock.disconnect_events.append({"scenario": scenario, "ts": time.monotonic()})
            except Exception:
                traceback.print_exc()
            return
        send_json(self, 404, {"error": f"mock ollama: unknown POST path {path!r}"})


class MockUpstream:
    """One fake Ollama host. `scenarios`/`version_response`/`tags_response`/
    `show_responses`/`ps_response` are per-INSTANCE (never module-level
    mutable state) so each test gets an independent, never-leaking copy --
    a test that needs a stateful scenario (`ScriptedByCallCount`) passes it
    in `scenarios` at construction time or assigns `mock.scenarios["name"]
    = ScriptedByCallCount([...])` before issuing the request it scripts."""

    def __init__(self, *, scenarios: "dict | None" = None, version_response=None,
                 tags_response=None, show_responses=None, ps_response=None, create_responses: "dict | None" = None,
                 known_blobs: "set | None" = None):
        self._server: "_ThreadingHTTPServer | None" = None
        self._thread: "threading.Thread | None" = None
        self._requests: list = []
        self._lock = threading.Lock()
        self.disconnect_events: list = []
        # Halo 2.0.3 round 5c: model NAME -> scripted list of /api/create
        # NDJSON status-line dicts (an `{"error": ...}` entry scripts the
        # failure path) -- see do_POST's own "/api/create" branch.
        self.create_responses: dict = dict(create_responses) if create_responses else {}
        # FIX PASS: sha256 hex digests this mock already "has" -- HEAD
        # /api/blobs/sha256:<hex> answers 200 for one of these, 404
        # otherwise; a successful POST to the same path adds to this set
        # (mirrors the real daemon's own blob store).
        self.known_blobs: set = set(known_blobs) if known_blobs else set()
        self.scenarios = dict(SCENARIOS)
        if scenarios:
            self.scenarios.update(scenarios)
        self.version_response = version_response if version_response is not None else dict(DEFAULT_VERSION)
        self.tags_response = tags_response if tags_response is not None else json.loads(json.dumps(DEFAULT_TAGS))
        self.show_responses = show_responses if show_responses is not None else json.loads(json.dumps(DEFAULT_SHOW))
        self.ps_response = ps_response if ps_response is not None else json.loads(json.dumps(DEFAULT_PS))

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list:
        return self._requests

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()
            self.disconnect_events.clear()

    def start(self, port: int = 0) -> "MockUpstream":
        self._server = _ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._server.mock = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server.server_close()
        except Exception:
            pass
        self._server = None

    def __enter__(self) -> "MockUpstream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
