"""rolo_claude.providers.http -- low-level upstream HTTP plumbing: proxy
selection, connection opening (TLS/CA bundle/proxy tunnel), the OpenAI-chat
and Databricks-chat POST calls, and the raw Databricks Claude passthrough
relay (proxy_anthropic/count_tokens/reader thread). Moved out of bridge.py
unchanged in the H0 package split; see wip/SIGNATURES.md part4 and the M4/M5
sections.

NOTE on the http.py <-> databricks.py cycle: call_databricks_chat (here)
needs databricks.py's route-candidate/body/cache helpers, and databricks.py's
probe_databricks_endpoints/probe_openrouter_models need this module's
open_upstream/UpstreamConnectError. Both directions are only used INSIDE
function bodies (never at class/module scope), so the cross-imports are
deferred (done locally inside the functions that need them) to avoid a
circular import at module-load time -- see databricks.py's matching note.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rolo_claude.providers.config import dump_debug, jdumps
from rolo_claude.providers.errors import upstream_error_text

log = logging.getLogger("bridge")


class UpstreamConnectError(Exception):
    pass


def pick_proxy(host: str) -> str | None:
    """Return proxy URL from env if host not bypassed, else None."""
    # Check bypass first
    if urllib.request.proxy_bypass_environment(host):
        return None
    
    # Check environment variables in order
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        proxy = os.environ.get(var)
        if proxy:
            return proxy
    return None


def open_upstream(host: str, port: int, tls: bool, connect_timeout: int = 10,
                   on_connect=None) -> http.client.HTTPConnection | http.client.HTTPSConnection:
    """Open HTTP(S) connection to upstream, respecting proxy and TLS
    settings. `on_connect` (must-do 5), when given, is called with the
    live `conn` object right after `connect()` succeeds -- BEFORE the
    caller sends the request or blocks in `getresponse()` -- so a caller
    that wants to interrupt a phase-1 call mid-flight (a genuinely
    blocking operation with no other hook point) can hand the socket to an
    abort watcher immediately, rather than only after the whole call
    returns. Exceptions from `on_connect` are swallowed (never let a
    watcher-registration bug break a real upstream call)."""
    proxy_url = pick_proxy(host)
    
    # Create TLS context if needed
    ssl_context = None
    if tls:
        ssl_context = ssl.create_default_context()
        # Load custom CA bundle if specified
        for env_var in ("NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "BRIDGE_CA_BUNDLE"):
            ca_path = os.environ.get(env_var)
            if ca_path:
                try:
                    ssl_context.load_verify_locations(ca_path)
                    break
                except Exception:
                    log.warning(f"Failed to load CA bundle from {ca_path}", exc_info=True)
    
    # Determine connection parameters
    if proxy_url:
        proxy_parts = urllib.parse.urlparse(proxy_url)
        proxy_host = proxy_parts.hostname
        proxy_port = proxy_parts.port or (443 if proxy_parts.scheme == "https" else 80)
        
        if tls:
            # HTTPS via proxy tunnel
            conn = http.client.HTTPSConnection(
                proxy_host, proxy_port, timeout=connect_timeout, context=ssl_context
            )
            conn.set_tunnel(host, port)
        else:
            # HTTP via proxy
            conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=connect_timeout)
    else:
        # Direct connection
        if tls:
            conn = http.client.HTTPSConnection(
                host, port, timeout=connect_timeout, context=ssl_context
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    
    # Connect with timeout, then set idle timeout
    conn.connect()
    if conn.sock:
        conn.sock.settimeout(300)  # 5 minutes idle timeout
    if on_connect is not None:
        try:
            on_connect(conn)
        except Exception:
            pass

    return conn


@dataclass
class UpstreamResult:
    """Result of an upstream HTTP call."""
    status: int
    headers: dict[str, str]
    resp: http.client.HTTPResponse | None
    conn: http.client.HTTPConnection | http.client.HTTPSConnection | None
    # Already-read raw response bytes when the producer had to consume `resp`
    # itself (e.g. call_databricks_chat peeking at a 400 body to look for a
    # max_tokens-limit wording before deciding whether to retry) -- None
    # means "resp has not been read yet, read it yourself".
    body_bytes: bytes | None = None


def call_openai_chat(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir: Path,
                      on_connect=None) -> UpstreamResult:
    """POST to OpenAI-compatible chat completions endpoint."""
    # Normalize base_url
    if base_url.endswith("/"):
        base_url = base_url.rstrip("/")
    
    # Parse URL to get host/port/tls
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    
    # Prepare request
    path = parsed.path + "/chat/completions"
    if not path.startswith("/"):
        path = "/" + path
    
    body_bytes = jdumps(body)
    dump_debug(state_dir, "upstream-request", body)
    
    # Build headers
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "HTTP-Referer": "https://github.com/rolo/claude-bridge",
        "X-Title": "claude-bridge",
        "Content-Length": str(len(body_bytes)),
    }
    # Merge extra_headers on top (overriding defaults)
    headers.update(extra_headers)
    
    try:
        # Open connection and send request
        conn = open_upstream(host, port, tls, on_connect=on_connect)
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()

        # Collect headers (lowercase keys)
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}

        return UpstreamResult(
            status=resp.status,
            headers=resp_headers,
            resp=resp,
            conn=conn,
        )
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        # Wrap connection-level errors
        raise UpstreamConnectError(f"Upstream connection failed: {e}") from e




def _dbx_post(base_url: str, path: str, api_key: str, req_body: dict, extra_headers: dict, state_dir,
               on_connect=None):
    """POST to a Databricks endpoint, returning (resp, conn) or raising UpstreamConnectError."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + path
    body_bytes = jdumps(req_body)
    dump_debug(state_dir, "upstream-request", req_body)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "Content-Length": str(len(body_bytes)),
    }
    headers.update(extra_headers)
    try:
        conn = open_upstream(host, port, tls, on_connect=on_connect)
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        return resp, conn
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"Databricks connection failed: {e}") from e


def call_databricks_chat(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir, model: str,
                          on_connect=None) -> UpstreamResult:
    """POST an OpenAI-chat body to a Databricks route, trying the cached/
    candidate paths with 404 fallback and a max_tokens-limit clamp-retry.
    H14 scope D: candidates (path, whether the body needs "model", and
    WHICH string to put there) come from `providers.dbx_routing.
    chat_route_candidates` -- the discovered `~/.rolo-claude/dbx-endpoints.
    json` cache's family/api_types RULES-table order when `model` (the
    endpoint/model name) is in it, else today's static order unchanged."""
    # Deferred import: breaks the http.py <-> databricks.py module cycle
    # (see this module's docstring). By the time this function is actually
    # CALLED, both modules have finished initializing, so a plain `from`
    # import here behaves exactly like a top-level one.
    from rolo_claude.providers.databricks import (
        build_databricks_body,
        parse_databricks_max_tokens_limit,
        dbx_cache_get_route,
        dbx_cache_set_route,
        dbx_cache_set_max_tokens_limit,
    )
    from rolo_claude.providers.dbx_routing import RouteCandidate, chat_route_candidates
    candidates = chat_route_candidates(model, state_dir)
    if not candidates:
        # Defensive only -- callers (model.py's own ModelRef construction,
        # work_matrix.py's own probe loop) already refuse/skip a known
        # non-chat endpoint before ever reaching here, so this is normally
        # unreachable; never crash on an unpack of an empty `chosen` if it
        # somehow is (a direct/future call site that skips that check).
        candidates = [RouteCandidate(key="invocations", path=f"/serving-endpoints/{model}/invocations",
                                      include_model=False, model_value=None)]
    cached_idx = dbx_cache_get_route(model, state_dir)
    if cached_idx is not None and 0 <= cached_idx < len(candidates):
        order = [(cached_idx, candidates[cached_idx])]
        order += [(i, cand) for i, cand in enumerate(candidates) if i != cached_idx]
    else:
        order = list(enumerate(candidates))

    chosen = None  # (orig_idx, candidate, resp, conn)
    for pos, (orig_idx, candidate) in enumerate(order):
        req_body = build_databricks_body(body, candidate.include_model, candidate.model_value)
        resp, conn = _dbx_post(base_url, candidate.path, api_key, req_body, extra_headers, state_dir,
                                on_connect=on_connect)
        if resp.status == 404 and pos != len(order) - 1:
            resp.read()
            continue
        chosen = (orig_idx, candidate, resp, conn)
        break

    orig_idx, candidate, resp, conn = chosen
    dbx_cache_set_route(model, orig_idx, state_dir)
    headers = {k.lower(): v for k, v in resp.getheaders()}

    if resp.status == 400:
        raw = resp.read()
        try:
            err_obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except (json.JSONDecodeError, ValueError):
            err_obj = {"error": {"message": raw.decode("utf-8", "replace")}}
        # finding 2/6: Databricks' own shape is {"error_code":...,"message":...}
        # -- no nested "error" object at all -- and a bare {"error":"boom"}
        # used to crash the old ad hoc ".get('error') or {}).get('message')"
        # extraction with AttributeError.
        err_msg = upstream_error_text(err_obj)
        limit = parse_databricks_max_tokens_limit(err_msg)
        if limit is not None and isinstance(body.get("max_tokens"), int) and body["max_tokens"] > limit:
            # Done with the first attempt's connection -- close it before opening a second one.
            try:
                resp.close()
                conn.close()
            except Exception:
                pass
            retry_body = dict(body)
            retry_body["max_tokens"] = limit
            req_body = build_databricks_body(retry_body, candidate.include_model, candidate.model_value)
            resp2, conn2 = _dbx_post(base_url, candidate.path, api_key, req_body, extra_headers, state_dir,
                                      on_connect=on_connect)
            dbx_cache_set_max_tokens_limit(model, limit, state_dir)
            headers2 = {k.lower(): v for k, v in resp2.getheaders()}
            return UpstreamResult(status=resp2.status, headers=headers2, resp=resp2, conn=conn2, body_bytes=None)
        return UpstreamResult(status=400, headers=headers, resp=resp, conn=conn, body_bytes=raw)

    return UpstreamResult(status=resp.status, headers=headers, resp=resp, conn=conn, body_bytes=None)




def proxy_anthropic(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir, path: str = "/v1/messages",
                     query_suffix: str = "?beta=true", on_connect=None) -> UpstreamResult:
    """POST an already-shaped Anthropic-format body straight through to a
    native Claude endpoint; raw relay, no dialect translation. Originally
    Databricks-only (hence the hardcoded `?beta=true`, kept as the default
    so every existing caller is byte-for-byte unaffected); H5 scope C's
    `call_anthropic_native` (below) reuses this SAME function for BOTH
    Databricks' Claude passthrough (`query_suffix` left at its default) and
    a direct `ant:` call to api.anthropic.com (`query_suffix=""` -- a real
    Anthropic endpoint has no use for Databricks' own gateway flag)."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    request_path = parsed.path.rstrip("/") + path + query_suffix
    body_bytes = jdumps(body)
    dump_debug(state_dir, "upstream-request", body)
    headers = {
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "Content-Length": str(len(body_bytes)),
    }
    headers.update(extra_headers)
    try:
        conn = open_upstream(host, port, tls, on_connect=on_connect)
        conn.request("POST", request_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return UpstreamResult(status=resp.status, headers=resp_headers, resp=resp, conn=conn, body_bytes=None)
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"Anthropic-dialect connection failed: {e}") from e


def call_anthropic_native(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir,
                           route_provider: str, on_connect=None) -> UpstreamResult:
    """H5 scope C: the ONE call site `stream.stream_anthropic_completion`
    uses for every native-Anthropic-dialect route. `route_provider` picks
    the host-specific bits `proxy_anthropic` itself stays agnostic of:
      - "anthropic" (`ant:`): `x-api-key`, `anthropic-version`, no
        Databricks gateway query flag.
      - "databricks" (`dbx:databricks-claude-*`/`dbx:system.ai.claude-*`):
        `Authorization: Bearer <token>`, `x-databricks-use-coding-agent-
        mode: true` (both already merged into `extra_headers` by the
        caller, same as the OpenAI-dialect Databricks path), Databricks'
        own `?beta=true` gateway flag and BOTH candidate paths tried on a
        404 (mirrors `call_databricks_chat`'s own route-candidate dance,
        applied to the two Anthropic-dialect paths from Appendix F:
        `/ai-gateway/anthropic/v1/messages`, `/serving-endpoints/anthropic/
        v1/messages`)."""
    if route_provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        headers.update(extra_headers)
        return proxy_anthropic(base_url, api_key, body, headers, state_dir,
                                path="/v1/messages", query_suffix="", on_connect=on_connect)
    # finding 16 (major, h4-h5-h3c review): the databricks branch never
    # added `Authorization: Bearer <token>` -- this docstring (and
    # headless.py's own extra_headers construction) CLAIMED it was
    # "already merged into extra_headers by the caller", but `build_session`
    # only ever adds `x-databricks-use-coding-agent-mode` there; every
    # Databricks Claude passthrough call (`dbx:databricks-claude-*`/
    # `dbx:system.ai.claude-*`) went out with no auth at all and 401/403'd.
    # `proxy_anthropic` itself never uses its own `api_key` parameter for
    # anything (it just relays whatever `headers` it's given), so the
    # header has to be added HERE, same as the "anthropic" branch above
    # does for `x-api-key`.
    headers = {"Authorization": f"Bearer {api_key}"}
    headers.update(extra_headers)
    # databricks: try the ai-gateway path first, fall back to serving-endpoints on 404.
    for path in ("/ai-gateway/anthropic/v1/messages", "/serving-endpoints/anthropic/v1/messages"):
        result = proxy_anthropic(base_url, api_key, body, headers, state_dir,
                                  path=path, query_suffix="?beta=true", on_connect=on_connect)
        if result.status != 404:
            return result
        try:
            if result.resp is not None:
                result.resp.read()
        except Exception:
            pass
        # NEW (H9 post-acceptance): a 404'd attempt's own connection was
        # never closed before looping to try the next candidate path --
        # `result` (and its `.conn`) is about to be discarded/overwritten
        # by the next iteration either way, so this is the last chance.
        try:
            if result.conn is not None:
                result.conn.close()
        except Exception:
            pass
    return result


def call_databricks_count_tokens(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir) -> UpstreamResult:
    """Blocking (non-streaming) relay to Databricks' count_tokens endpoint."""
    return proxy_anthropic(base_url, api_key, body, extra_headers, state_dir, path="/v1/messages/count_tokens")


def passthrough_reader_thread(resp, q) -> None:
    """Background thread: read the upstream Databricks Claude response in raw chunks and post them to q."""
    try:
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                q.put(("eof", None))
                break
            q.put(("raw", chunk))
    except Exception as e:
        q.put(("exc", e))
    finally:
        try:
            resp.close()
        except Exception:
            pass
