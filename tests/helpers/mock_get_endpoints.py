"""tests.helpers.mock_get_endpoints -- a tiny, generic GET-only HTTP mock
(H15 part 2 addenda 3.2/4): OpenRouter's `/models`/`/credits`/`/key`
and Anthropic's `/v1/models` are all plain `GET` + a JSON body, unlike
`tests.helpers.mock_openai.MockUpstream` (POST-only, chat/completions-
shaped) -- a separate, minimal server rather than widening that
heavily-used, already-large mock.

Usage: `mock = MockGetEndpoints({"/models": (200, {...}), "/credits": (200,
{...})}).start()`; `mock.base_url` is the bare `http://127.0.0.1:<port>`
root (a caller appends its own `/api/v1`/`/v1` path prefix, matching how
`resolve_openrouter`/`resolve_anthropic`'s own `base_url` is used elsewhere).
`mock.requests` records every `(path, headers)` seen, newest last.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence stdout noise

    def do_GET(self):
        mock: "MockGetEndpoints" = self.server.mock  # type: ignore[attr-defined]
        with mock._lock:
            mock._requests.append({"path": self.path, "headers": dict(self.headers)})
        route = mock._routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"mock: no route for {self.path}"}).encode("utf-8"))
            return
        status, body = route
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockGetEndpoints:
    def __init__(self, routes: "dict[str, tuple[int, dict]]"):
        self._routes = dict(routes)
        self._server: "HTTPServer | None" = None
        self._thread: "threading.Thread | None" = None
        self._requests: "list[dict]" = []
        self._lock = threading.Lock()

    def set_route(self, path: str, status: int, body: dict) -> None:
        with self._lock:
            self._routes[path] = (status, body)

    @property
    def requests(self) -> "list[dict]":
        with self._lock:
            return list(self._requests)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "MockGetEndpoints":
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
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
