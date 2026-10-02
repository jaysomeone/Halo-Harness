"""tests.helpers.fake_oauth_server -- a real, local, generic OAuth 2.0
authorization server for `halo mcp login`/`logout` tests
(halo_harness/mcp/oauth.py). Named after no real vendor on purpose (W4b:
"a generic OAuth authorization-code flow... tested only against a fake
OAuth server").

Endpoints:
  GET  /.well-known/oauth-authorization-server -- RFC 8414 metadata JSON.
  GET  /authorize?response_type=code&client_id=...&redirect_uri=...&
       state=...&code_challenge=...&code_challenge_method=S256[&scope=...]
       -- 302s straight to `redirect_uri` with `?code=<issued>&state=...`
       (simulating instant user approval), unless `deny=1` was passed, in
       which case it redirects with `?error=access_denied&state=...`.
  POST /token -- form-encoded `grant_type=authorization_code&code=...&
       redirect_uri=...&client_id=...&code_verifier=...[&client_secret=...]`;
       verifies the REAL PKCE challenge (S256) recorded at `/authorize`
       time and, if `require_client_secret` was set, the client secret
       too -- a mismatch on either is a 400 `invalid_grant`. A real, valid
       exchange answers with a fresh access/refresh token pair.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeOAuthServer:
    def __init__(self, *, require_client_secret: str = None) -> None:
        self.require_client_secret = require_client_secret
        self.codes: dict = {}  # code -> {"code_challenge", "redirect_uri"}
        self.token_calls: list = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if parsed.path == "/.well-known/oauth-authorization-server":
                    base = f"http://127.0.0.1:{server.port}"
                    self._json(200, {"authorization_endpoint": f"{base}/authorize",
                                      "token_endpoint": f"{base}/token"})
                    return
                if parsed.path == "/authorize":
                    redirect_uri = (qs.get("redirect_uri") or [""])[0]
                    state = (qs.get("state") or [""])[0]
                    if (qs.get("deny") or [""])[0] == "1":
                        location = f"{redirect_uri}?{urlencode({'error': 'access_denied', 'state': state})}"
                    else:
                        code = secrets.token_hex(8)
                        server.codes[code] = {"code_challenge": (qs.get("code_challenge") or [""])[0],
                                                "redirect_uri": redirect_uri}
                        location = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.end_headers()
                    return
                self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/token":
                    self._json(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = parse_qs(self.rfile.read(length).decode("utf-8"))
                get = lambda k: (body.get(k) or [""])[0]
                server.token_calls.append({k: get(k) for k in
                                             ("grant_type", "code", "redirect_uri", "client_id", "client_secret")})
                record = server.codes.get(get("code"))
                if record is None:
                    self._json(400, {"error": "invalid_grant", "error_description": "unknown code"})
                    return
                verifier = get("code_verifier")
                computed = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
                if computed != record["code_challenge"]:
                    self._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
                    return
                if server.require_client_secret is not None and get("client_secret") != server.require_client_secret:
                    self._json(400, {"error": "invalid_client", "error_description": "bad client_secret"})
                    return
                self._json(200, {"access_token": "fake-access-token", "refresh_token": "fake-refresh-token",
                                   "token_type": "Bearer", "expires_in": 3600})

            def log_message(self, *args) -> None:
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="fake-oauth-server")
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
