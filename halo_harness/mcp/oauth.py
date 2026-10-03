"""halo_harness.mcp.oauth -- `halo mcp login`/`logout`: a generic OAuth 2.0
authorization-code (+ PKCE) flow for a remote (http/sse) MCP server (Halo
2.0.1 gap-list brief, W4b item 2). Vendor-neutral by design: no particular
provider is named anywhere in this module -- the flow is driven entirely by
the target server's own `oauth` config block (`mcp add --client-id ...
--client-secret ... --callback-port ...`, already parsed into
`McpServerConfig.oauth` by `mcp.manager.parse_server`) or, when that names
no explicit endpoints, OAuth 2.0 Authorization Server Metadata discovery
(RFC 8414, `<origin>/.well-known/oauth-authorization-server`). Tested only
against a fake local OAuth server (tests/helpers/fake_oauth_server.py) --
see docs/COMMANDS.md for exactly what was verified and what was not.

Tokens are stored under `~/.halo/mcp/oauth/<server-name>.json` -- NEVER in
any of Claude Code's own files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from halo_harness.config.paths import bridge_home

DEFAULT_CALLBACK_TIMEOUT_S = 120.0


def oauth_dir() -> Path:
    return bridge_home() / "mcp" / "oauth"


def token_path(server_name: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in server_name) or "server"
    return oauth_dir() / f"{safe}.json"


def load_tokens(server_name: str) -> Optional[dict]:
    try:
        data = json.loads(token_path(server_name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_tokens(server_name: str, tokens: dict) -> Path:
    path = token_path(server_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # review finding 26: an OAuth access/refresh token pair used to land
    # under the default umask (0644 on a typical Linux box) -- readable by
    # every other local user. `os.name != "nt"` guard (and best-effort,
    # like `providers/config.py::ensure_token`'s own identical pattern) --
    # Windows has no POSIX mode bits to set here, and `os.chmod` there can
    # only toggle the read-only attribute, which this would rather not
    # touch at all.
    if os.name != "nt":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    data = dict(tokens)
    data["saved_at"] = time.time()
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)
    return path


def clear_tokens(server_name: str) -> bool:
    try:
        token_path(server_name).unlink()
        return True
    except OSError:
        return False


def _pkce_pair() -> "tuple[str, str]":
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def discover_endpoints(server_url: str, oauth_cfg: dict, *,
                        timeout: float = 10.0) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """`(authorization_endpoint, token_endpoint, error)` -- explicit
    `oauth.authorization_endpoint`/`oauth.token_endpoint` win outright;
    else RFC 8414 metadata discovery, generic (no vendor ever named)."""
    auth_ep, token_ep = oauth_cfg.get("authorization_endpoint"), oauth_cfg.get("token_endpoint")
    if auth_ep and token_ep:
        return auth_ep, token_ep, None
    parsed = urlparse(server_url or "")
    if not parsed.scheme or not parsed.netloc:
        return auth_ep, token_ep, "no server URL to discover OAuth endpoints from"
    metadata_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/.well-known/oauth-authorization-server")
    try:
        with urlopen(Request(metadata_url, headers={"Accept": "application/json"}), timeout=timeout) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, ValueError, TimeoutError) as e:
        return auth_ep, token_ep, f"could not discover OAuth endpoints at {metadata_url}: {type(e).__name__}: {e}"
    return auth_ep or meta.get("authorization_endpoint"), token_ep or meta.get("token_endpoint"), None


class _CallbackResult:
    def __init__(self) -> None:
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self.error: Optional[str] = None
        self.event = threading.Event()


def _make_handler(result: _CallbackResult):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            result.code = (qs.get("code") or [None])[0]
            result.state = (qs.get("state") or [None])[0]
            result.error = (qs.get("error_description") or qs.get("error") or [None])[0]
            body = (b"halo: authorization received, you can close this tab." if result.code
                    else b"halo: authorization failed -- check the halo terminal.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            result.event.set()

        def log_message(self, *args) -> None:  # silence BaseHTTPRequestHandler's default stderr logging
            pass

    return Handler


def run_authorization_flow(*, server_name: str, server_url: str, oauth_cfg: dict,
                            callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT_S,
                            open_browser: bool = True,
                            print_fn: Callable[[str], None] = print) -> "tuple[Optional[dict], Optional[str]]":
    """`(tokens_or_None, error_or_None)` -- the full authorization-code (+
    PKCE) round trip against whatever endpoints `discover_endpoints`
    resolves. Blocks (bounded by `callback_timeout`) waiting for the local
    callback; never raises."""
    auth_ep, token_ep, err = discover_endpoints(server_url, oauth_cfg)
    if err:
        return None, err
    if not auth_ep or not token_ep:
        return None, "no authorization/token endpoint configured or discoverable for this server"

    result = _CallbackResult()
    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    try:
        httpd = HTTPServer(("127.0.0.1", int(oauth_cfg.get("callback_port") or 0)), _make_handler(result))
    except OSError as e:
        return None, f"could not open a local callback port: {e}"
    redirect_uri = f"http://127.0.0.1:{httpd.server_address[1]}/callback"

    client_id = oauth_cfg.get("client_id") or "halo-mcp-client"
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if oauth_cfg.get("scope"):
        params["scope"] = oauth_cfg["scope"]
    authorize_url = f"{auth_ep}{'&' if '?' in auth_ep else '?'}{urlencode(params)}"

    print_fn(f"halo mcp login {server_name}: open this URL to authorize (it may open automatically):")
    print_fn(f"  {authorize_url}")
    if open_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

    threading.Thread(target=httpd.handle_request, daemon=True, name=f"halo-oauth-{server_name}").start()
    got = result.event.wait(timeout=callback_timeout)
    try:
        httpd.server_close()
    except OSError:
        pass
    if not got:
        return None, f"timed out after {callback_timeout:.0f}s waiting for the browser redirect"
    if result.error:
        return None, f"authorization failed: {result.error}"
    if not result.code or result.state != state:
        return None, "no authorization code received, or the state did not match (possible CSRF)"

    token_body = {"grant_type": "authorization_code", "code": result.code, "redirect_uri": redirect_uri,
                  "client_id": client_id, "code_verifier": verifier}
    if oauth_cfg.get("client_secret"):
        token_body["client_secret"] = oauth_cfg["client_secret"]
    try:
        req = Request(token_ep, data=urlencode(token_body).encode("ascii"),
                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        with urlopen(req, timeout=15.0) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, ValueError, TimeoutError) as e:
        return None, f"token exchange failed: {type(e).__name__}: {e}"
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return None, f"token endpoint did not return an access_token: {tokens!r}"
    return tokens, None


def refresh_tokens(server_name: str, stored: dict, *, oauth_cfg: dict,
                    server_url: str = "") -> "tuple[Optional[dict], Optional[str]]":
    """finding 13 (W6a): `grant_type=refresh_token` against the SAME token
    endpoint `run_authorization_flow` uses -- `(new_tokens_or_None,
    error_or_None)`, saving the new tokens on success (same file
    `load_tokens` reads back). `None`, "no refresh_token on file" when
    `stored` (whatever `load_tokens(server_name)` returned) has none --
    a server that never issued one needs a full `halo mcp login` instead,
    never a silent no-op. Never raises."""
    refresh_token = stored.get("refresh_token") if isinstance(stored, dict) else None
    if not refresh_token:
        return None, "no refresh_token on file for this server"
    _auth_ep, token_ep, err = discover_endpoints(server_url, oauth_cfg)
    if err:
        return None, err
    if not token_ep:
        return None, "no token endpoint configured or discoverable for this server"
    client_id = oauth_cfg.get("client_id") or "halo-mcp-client"
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if oauth_cfg.get("client_secret"):
        body["client_secret"] = oauth_cfg["client_secret"]
    try:
        req = Request(token_ep, data=urlencode(body).encode("ascii"),
                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        with urlopen(req, timeout=15.0) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, ValueError, TimeoutError) as e:
        return None, f"token refresh failed: {type(e).__name__}: {e}"
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return None, f"token endpoint did not return an access_token on refresh: {tokens!r}"
    # A refresh response commonly omits `refresh_token` (the old one stays
    # valid) -- carried over so a LATER refresh still has one to use.
    tokens.setdefault("refresh_token", refresh_token)
    save_tokens(server_name, tokens)
    return tokens, None
