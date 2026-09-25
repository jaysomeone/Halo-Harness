# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

"""
claude-bridge: A local bridge that routes Claude Code requests to Databricks
or OpenRouter models while preserving Claude Code's settings, MCPs, and tools.

This tool intercepts Claude Code's API calls (via ANTHROPIC_BASE_URL override)
and translates between Anthropic's Messages API and OpenAI-compatible or
Databricks-specific endpoints. It maintains Claude Code's full environment
(memories, skills, permissions) while allowing non-Claude models to drive
the interface.

See SIGNATURES.md for the complete cross-section contract.
"""

from __future__ import annotations

import os
import sys
import io
import re
import json
import time
import uuid
import select
import socket
import ssl
import ipaddress
import hashlib
import secrets
import shutil
import signal
import argparse
import threading
import queue
import subprocess
import logging
import logging.handlers
import http.client
import http.server
import urllib.request
import urllib.parse
import socketserver
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

DEFAULT_PORT = 8787
PROG = "claude-bridge"

log = logging.getLogger("bridge")

# Make `rolo_claude` importable when this file is run directly as a
# script from another cwd (plain `python bridge.py ...`, or `uv run --script
# bridge.py ...`) -- Python normally puts a script's own directory on
# sys.path[0] automatically, but this is cheap insurance for any runner that
# doesn't (finding: uv run --script has been observed to under some
# invocations). No-op if it's already there.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from rolo_claude import __version__
from rolo_claude.providers.config import (
    home,
    default_state_dir,
    load_env_file,
    load_settings_env_chain,
    resolve_openrouter,
    resolve_databricks,
    derive_workspace_root,
    extract_custom_headers,
    load_routes,
    resolve_config,
    ensure_token,
    bridge_py_sha1,
    write_server_json,
    setup_logging,
    jdumps,
    estimate_tokens,
    dump_debug,
)
from rolo_claude.providers.routing import (
    InvalidModelError,
    route_model,
    is_passthrough_ref,
    resolve_profile,
    build_passthrough_body,
    build_passthrough_headers,
)
from rolo_claude.providers.translate import (
    WebSearchUnavailable,
    anthropic_to_openai,
)
from rolo_claude.providers.errors import (
    build_prompt_too_long_message,
    map_upstream_error,
)
from rolo_claude.providers.oai_stream import (
    sse_frame,
)
from rolo_claude.providers.http import (
    UpstreamConnectError,
    proxy_anthropic,
    call_databricks_count_tokens,
    passthrough_reader_thread,
)
from rolo_claude.providers.databricks import (
    databricks_unreachable_response,
    probe_databricks_endpoints,
    probe_openrouter_models,
    models_json_path,
    write_models_json,
    load_models_json,
)
# H0 lift (plan D2): the openai-chat-dialect request orchestration that used
# to live inline in _handle_messages_post/_handle_upstream_stream/
# _reader_thread now lives in providers/stream.py as a reusable library
# function; the two Handler methods below are thin wrappers over it. The
# Databricks Claude passthrough dialect is UNCHANGED (still a raw relay via
# _handle_passthrough_stream, never touching stream_completion).
from rolo_claude.providers.stream import (
    CompletionRequest,
    ProviderCreds,
    ContextOverflow,
    UpstreamError,
    ProviderNotConfigured,
    stream_completion,
)

class ClaudeNotFoundError(Exception):
    pass


class BridgeStartError(Exception):
    pass




def cmd_probe(args) -> None:
    """--probe: report Databricks endpoint reachability and cache the OpenRouter model list."""
    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    dbx = resolve_databricks()
    if dbx is None:
        print("Databricks: not configured")
    else:
        root = derive_workspace_root(dbx.host)
        try:
            status, names = probe_databricks_endpoints(root, dbx.token)
            if status == 200:
                print(f"Databricks: {len(names)} serving endpoint(s) at {root}: {', '.join(names) if names else '(none)'}")
            elif status in (401, 403):
                print("Databricks: token can run inference but not list endpoints -- pass names explicitly")
            else:
                print(f"Databricks: unexpected status {status} listing endpoints at {root}")
        except UpstreamConnectError as e:
            print(f"Databricks: unreachable -- {e} (are you on the VPN? Databricks is whitelisted)")

    orc = resolve_openrouter()
    if orc is None:
        print("OpenRouter: not configured (no OPENROUTER_API_KEY)")
    else:
        try:
            models = probe_openrouter_models(orc.base_url, orc.api_key)
            state_dir = default_state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            write_models_json(state_dir, models)
            print(f"OpenRouter: cached {len(models)} model(s) to {models_json_path(state_dir)}")
        except (UpstreamConnectError, RuntimeError) as e:
            print(f"OpenRouter: probe failed -- {e}")

    # finding 9: --probe never reported whether the local bridge server's own
    # port was busy, free, or held by a stale/foreign process.
    port = getattr(args, "port", None) or DEFAULT_PORT
    port_status = check_server_status(port, bridge_py_sha1())
    port_labels = {
        "fresh": f"Port {port}: claude-bridge running (up to date)",
        "stale": f"Port {port}: claude-bridge running but stale (different version) -- launch will restart it",
        "foreign": f"Port {port}: in use by something that is not claude-bridge",
        "absent": f"Port {port}: free",
    }
    print(port_labels[port_status])



class _Server(http.server.ThreadingHTTPServer):
    """Threading HTTP server with exclusive address binding on Windows."""
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        """Bind socket with SO_EXCLUSIVEADDRUSE on Windows."""
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for Claude Bridge."""
    protocol_version = "HTTP/1.1"
    timeout = 120
    # Applied by socketserver.StreamRequestHandler.setup() BEFORE handle()
    # runs -- unlike a manual setsockopt() from __init__, which (for
    # socketserver handlers) only executes AFTER the entire request has
    # already been processed, since BaseRequestHandler.__init__ itself calls
    # setup()/handle()/finish() synchronously.
    disable_nagle_algorithm = True

    def check_auth(self) -> str | None:
        """Extract token from x-api-key or Authorization header."""
        auth = self.headers.get("x-api-key")
        if auth:
            return auth
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def send_json(self, status: int, obj: dict, extra_headers: dict | None = None):
        """Send JSON response with exact Content-Length."""
        body = jdumps(obj)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Connection": "keep-alive",
        }
        if extra_headers:
            headers.update(extra_headers)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _client_gone(self) -> bool:
        """Non-blocking peek at the client socket to detect it having closed
        its side. Writing small SSE frames alone does not reliably fail
        promptly on every platform (a graceful close over loopback can leave
        small writes silently accepted by the local kernel for a while), so
        the streaming loop also polls this directly every iteration."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def write_chunk(self, data: bytes):
        """Write a single chunk in chunked encoding."""
        try:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            log.debug("Client write error: %s", e)
            raise

    def _read_full_body(self) -> bytes:
        """Read entire request body using Content-Length."""
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _handle_upstream_stream(self, gen, first_event: dict, abort: threading.Event):
        """Thin wrapper over providers.stream.stream_completion's phase 2
        (H0 lift, plan D2): send the SSE response headers, write the
        already-yielded `first_event` (message_start), then relay the rest
        of the generator's events as SSE frames verbatim -- all the actual
        upstream-reading/ping/translation logic now lives in
        stream_completion itself (and its own sse_reader_thread), not here.

        A tiny watcher thread sets `abort` the moment the client goes away
        (the same non-blocking peek `_client_gone` always used); the
        generator notices within ~250ms (see providers/stream.py) and shuts
        down the upstream connection itself. `gen.close()` in the finally
        block both handles the "normal" abort path and guarantees prompt
        cleanup if OUR OWN write to the client fails first."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        # Deliberately NOT "Connection: close" here (despite the design
        # review's literal wording): http.client.HTTPConnection.getresponse()
        # treats a "close" response as already-closed and nulls out its own
        # response reference right after parsing headers, so a client's
        # later conn.close() (with no explicit resp.close(), exactly the
        # pattern test_bridge.py's abort tests use) becomes a no-op that
        # never actually releases the socket's file object -- the peer close
        # then never becomes observable to us (verified empirically). We
        # still close from the SERVER's own side via close_connection=True.
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True

        stop_watch = threading.Event()

        def _watch_client():
            while not stop_watch.is_set():
                if self._client_gone():
                    abort.set()
                    return
                stop_watch.wait(0.2)

        watcher = threading.Thread(target=_watch_client)
        watcher.daemon = True
        watcher.start()

        try:
            self.write_chunk(sse_frame(first_event["type"], first_event))
            for ev in gen:
                self.write_chunk(sse_frame(ev["type"], ev))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            log.debug("Stream write error: %s", e)
            abort.set()
        finally:
            stop_watch.set()
            # Resumes the generator with GeneratorExit at whatever yield it
            # last stopped at, running its own finally (upstream socket
            # shutdown when aborted, dump_debug either way) synchronously --
            # a no-op if the generator already finished on its own.
            gen.close()
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def _handle_passthrough_stream(self, result: UpstreamResult):
        """Raw byte relay of a Databricks Claude passthrough response back to the client (status + headers relayed, body bytes forwarded unparsed)."""
        self.send_response(result.status)
        skip_headers = {"content-length", "connection", "transfer-encoding"}
        for k, v in result.headers.items():
            if k.lower() in skip_headers:
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True

        # For passthrough there is no dialect translation -- what we relay IS
        # what we emit, so both dumps hold the same raw chunks.
        dumped_chunks: list = []

        q = queue.Queue()
        reader = threading.Thread(target=passthrough_reader_thread, args=(result.resp, q))
        reader.daemon = True
        reader.start()

        ping_interval = float(os.environ.get("BRIDGE_PING_INTERVAL", "15"))
        try:
            while True:
                if self._client_gone():
                    raise BrokenPipeError("client disconnected (detected via peek)")
                try:
                    item = q.get(timeout=ping_interval)
                except queue.Empty:
                    # No synthetic pings here -- injecting one would corrupt
                    # the real Anthropic byte stream. Upstream Claude already
                    # sends its own native ping events during long silences.
                    continue
                kind, value = item
                if kind == "raw":
                    dumped_chunks.append(value.decode("utf-8", "replace"))
                    self.write_chunk(value)
                elif kind == "eof":
                    break
                elif kind == "exc":
                    err_ev = {"type": "error", "error": {"type": "overloaded_error", "message": f"upstream: {value}"}}
                    dumped_chunks.append(json.dumps(err_ev))
                    # finding 11: the last relayed read1() chunk may end
                    # mid-event (no trailing blank line yet) -- writing the
                    # synthetic frame straight after it lets the SDK join
                    # "event: error" onto that partial "data:" line and throw
                    # a JSON parse error instead of surfacing our error. A
                    # leading blank line is harmless when the previous chunk
                    # WAS already on an event boundary.
                    self.write_chunk(b"\n\n" + sse_frame("error", err_ev))
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            log.debug("Passthrough stream write error: %s", e)
            if result.conn and result.conn.sock:
                try:
                    result.conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        finally:
            dump_debug(SERVER_STATE_DIR, "upstream-stream", {"lines": dumped_chunks})
            dump_debug(SERVER_STATE_DIR, "emitted-events", {"events": dumped_chunks})
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def _handle_messages_post(self, body: dict):
        """Handle POST /v1/messages."""
        try:
            route = route_model(body["model"], self.headers, SERVER_CFG)
        except InvalidModelError as e:
            self.send_json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            return
        except WebSearchUnavailable:
            self.send_json(400, {"error": {"type": "invalid_request_error",
                                           "message": "web search unavailable via claude-bridge"}})
            return

        dump_debug(SERVER_STATE_DIR, "claude-request", body)

        # -- Databricks Claude passthrough: raw relay, no dialect translation,
        # no overflow/error-shape rewriting (Databricks' native Claude
        # endpoint already speaks Anthropic's own error format). --
        if route.provider == "databricks" and route.dialect == "anthropic-passthrough":
            dbx = SERVER_DBX
            if dbx is None:
                self.send_json(502, {"error": {"type": "api_error", "message": "Databricks not configured"}},
                               {"x-should-retry": "false"})
                return
            root = derive_workspace_root(dbx.host)
            pbody = build_passthrough_body(body, route)
            log_mirrored_headers(extract_custom_headers(self.headers), "databricks passthrough")
            phdrs = build_passthrough_headers(self.headers, dbx.token)
            try:
                result = proxy_anthropic(base_url=f"{root}/ai-gateway/anthropic", api_key=dbx.token,
                                          body=pbody, extra_headers=phdrs, state_dir=SERVER_STATE_DIR)
            except UpstreamConnectError as e:
                status, jbody, hdrs = databricks_unreachable_response(str(e))
                self.send_json(status, jbody, hdrs)
                return
            self._handle_passthrough_stream(result)
            return

        # H0 lift (plan D2): everything from here down used to be an inline
        # translate + one-connect-retry + one-overflow-retry loop, now
        # delegated to providers.stream.stream_completion. This wrapper's
        # only remaining job is building the request (profile + credentials)
        # and mapping stream_completion's exceptions back onto the exact
        # wire responses the pre-lift code always sent -- see
        # providers/stream.py's module docstring for the two-phase contract.
        profile = resolve_profile(route.upstream_model, SERVER_STATE_DIR,
                                   SERVER_CFG.routes if SERVER_CFG else {})

        if route.provider == "databricks":
            dbx = SERVER_DBX
            creds = ProviderCreds(base_url=derive_workspace_root(dbx.host), api_key=dbx.token) if dbx else None
            extra_headers = extract_custom_headers(self.headers)
            if dbx:
                log_mirrored_headers(extra_headers, "databricks chat")
            openrouter_base_url = None
        else:
            creds = (ProviderCreds(base_url=SERVER_OPENROUTER.base_url, api_key=SERVER_OPENROUTER.api_key)
                     if SERVER_OPENROUTER else None)
            extra_headers = {}
            openrouter_base_url = SERVER_MOCK_BASE_URL

        req = CompletionRequest(
            body=body, route=route, profile=profile, creds=creds, state_dir=SERVER_STATE_DIR,
            extra_headers=extra_headers, model_label=body.get("model"),
            ping_interval=float(os.environ.get("BRIDGE_PING_INTERVAL", "15")),
            openrouter_base_url=openrouter_base_url,
        )
        abort = threading.Event()
        gen = stream_completion(req, abort=abort)
        try:
            first_event = next(gen)
        except WebSearchUnavailable:
            self.send_json(400, {"error": {"type": "invalid_request_error",
                                           "message": "web search unavailable via claude-bridge"}})
            return
        except ProviderNotConfigured as e:
            self.send_json(502, {"error": {"type": "api_error", "message": str(e)}}, {"x-should-retry": "false"})
            return
        except ContextOverflow as e:
            # Not (or no longer) fixable -- rewrite to the exact message
            # Claude Code's own compaction regex recognizes, with T > L
            # guaranteed by build_prompt_too_long_message.
            total_for_msg = e.total
            if total_for_msg is None:
                total_for_msg = e.prompt_tokens if e.prompt_tokens is not None else e.limit + 1
            msg = build_prompt_too_long_message(total_for_msg, e.limit)
            status, jbody, hdrs = map_upstream_error(400, {"error": {"message": msg}}, route.provider)
            self.send_json(status, jbody, hdrs)
            return
        except UpstreamError as e:
            jbody = {"error": {"type": e.err_type, "message": e.message}}
            hdrs = {"x-should-retry": "true" if e.retryable else "false"}
            if e.retry_after:
                hdrs["Retry-After"] = e.retry_after
            self.send_json(e.status, jbody, hdrs)
            return

        # Only reachable once stream_completion's phase 1 succeeded (a
        # genuine 2xx) and yielded message_start as `first_event`.
        self._handle_upstream_stream(gen, first_event, abort)

    def _handle_count_tokens_post(self, body: dict):
        """Handle POST /v1/messages/count_tokens: relay to Databricks passthrough refs, falling back to the local estimate on any non-2xx/connect failure or for every other route."""
        try:
            route = route_model(body.get("model", ""), self.headers, SERVER_CFG)
        except (InvalidModelError, WebSearchUnavailable):
            route = None

        if route is not None and route.provider == "databricks" and route.dialect == "anthropic-passthrough" and SERVER_DBX is not None:
            root = derive_workspace_root(SERVER_DBX.host)
            pbody = build_passthrough_body(body, route)
            phdrs = build_passthrough_headers(self.headers, SERVER_DBX.token)
            try:
                result = call_databricks_count_tokens(base_url=f"{root}/ai-gateway/anthropic", api_key=SERVER_DBX.token,
                                                       body=pbody, extra_headers=phdrs, state_dir=SERVER_STATE_DIR)
                raw = result.resp.read() if result.resp else b""
                if 200 <= result.status < 300:
                    try:
                        parsed = json.loads(raw.decode("utf-8", "replace"))
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict) and "input_tokens" in parsed:
                        self.send_json(200, parsed)
                        return
            except UpstreamConnectError:
                pass

        self.send_json(200, {"input_tokens": estimate_tokens(body)})

    def do_GET(self):
        """Handle GET requests."""
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self.send_json(200, {
                "service": "claude-bridge",
                "version": __version__,
                "hash": bridge_py_sha1(),
                "pid": os.getpid(),
                "port": SERVER_PORT
            })
            return
        token = self.check_auth()
        if token != SERVER_TOKEN:
            self.send_json(401, {"error": {"type": "authentication_error", "message": "invalid or missing token"}})
            return
        if path == "/v1/models":
            self.send_json(200, {"data": []})
        else:
            self.send_json(404, {"error": {"type": "not_found_error", "message": f"path {path} not found"}})

    def do_POST(self):
        """Handle POST requests. Body is always read first (even for auth
        failures / bodyless routes) so a keep-alive socket stays in sync."""
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/v1/messages", "/v1/messages/count_tokens", "/shutdown"):
            self._read_full_body()
            self.send_json(404, {"error": {"type": "not_found_error", "message": f"path {path} not found"}})
            return

        body_data = self._read_full_body()

        token = self.check_auth()
        if token != SERVER_TOKEN:
            self.send_json(401, {"error": {"type": "authentication_error",
                                           "message": "invalid or missing token"}})
            return

        if path == "/shutdown":
            self.send_json(200, {"ok": True})
            threading.Timer(0.2, self.server.shutdown).start()
            return

        if not body_data:
            self.send_json(400, {"error": {"type": "invalid_request_error", "message": "empty body"}})
            return
        try:
            body = json.loads(body_data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.send_json(400, {"error": {"type": "invalid_request_error", "message": "invalid JSON"}})
            return

        if path == "/v1/messages":
            self._handle_messages_post(body)
        elif path == "/v1/messages/count_tokens":
            self._handle_count_tokens_post(body)

    def log_message(self, format, *args):
        """Log messages via bridge logger."""
        log.info(format, *args)


SERVER_STATE_DIR = None
SERVER_PORT = None
SERVER_TOKEN = None
SERVER_CFG = None
SERVER_OPENROUTER = None
SERVER_MOCK_BASE_URL = None
SERVER_DBX = None


def cmd_serve(args):
    """Start the HTTP server."""
    global SERVER_STATE_DIR, SERVER_PORT, SERVER_TOKEN, SERVER_CFG, SERVER_OPENROUTER, SERVER_MOCK_BASE_URL, SERVER_DBX

    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    SERVER_STATE_DIR = Path(args.state_dir) if getattr(args, "state_dir", None) else default_state_dir()
    SERVER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(SERVER_STATE_DIR)

    port = args.port or DEFAULT_PORT
    SERVER_PORT = port
    SERVER_TOKEN = ensure_token(SERVER_STATE_DIR)
    SERVER_CFG = resolve_config()
    # SERVER_OPENROUTER/SERVER_DBX hold the REAL (unredacted) configs used to
    # actually authenticate upstream calls -- SERVER_CFG.openrouter/.databricks
    # are the redacted dicts meant only for `--config` display, never for auth.
    SERVER_OPENROUTER = resolve_openrouter()
    SERVER_DBX = resolve_databricks()
    SERVER_MOCK_BASE_URL = os.environ.get("BRIDGE_OPENROUTER_BASE_URL")

    write_server_json(SERVER_STATE_DIR, os.getpid(), port)

    try:
        server = _Server(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"claude-bridge: cannot bind port {port} -- already in use? ({e})", file=sys.stderr)
        sys.exit(1)
    log.info("Server listening on http://127.0.0.1:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped by keyboard interrupt")
    finally:
        server.server_close()
def find_claude_exe(*, _which=None, _windows: "bool | None" = None) -> str:
    """Return path to claude executable, raising ClaudeNotFoundError if not found.

    H9 Linux acceptance (Part A, bug 1 -- CRITICAL): this only ever looked
    for `claude.exe` / `claude.cmd` / `~/.local/bin/claude.exe`, i.e. the
    Windows shims, so on Linux -- the primary platform -- `rolo-claude proxy
    launch` never found a real `claude` on PATH or at `~/.local/bin/claude`
    (the standard native install location) and raised ClaudeNotFoundError on
    a clean Kali box, or, on WSL, picked up a Windows npm `claude.cmd` shim
    through the inherited PATH and crashed trying to run its unresolved
    `%dp0%\\...\\claude.exe`. POSIX now resolves the bare `claude` name first
    (PATH, then `~/.local/bin/claude`) and never touches the Windows shim
    logic; Windows keeps its existing order and gains the same bare-name
    fallback last. `_which`/`_windows` are test seams only."""
    which = _which or shutil.which
    windows = (os.name == "nt") if _windows is None else _windows
    env_exe = os.environ.get("BRIDGE_CLAUDE_EXE")
    if env_exe:
        return env_exe

    if not windows:
        posix_exe = which("claude")
        if posix_exe:
            return posix_exe
        local_posix = home() / ".local" / "bin" / "claude"
        if local_posix.exists():
            return str(local_posix)
        raise ClaudeNotFoundError("claude executable not found (looked for `claude` on PATH and ~/.local/bin/claude)")

    exe = which("claude.exe")
    if exe:
        return exe

    cmd = which("claude.cmd")
    if cmd:
        try:
            with open(cmd, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r'"([^"]+claude\.exe)"', content)
            if m:
                raw = m.group(1)
                # npm's generated shims reference a batch-local %dp0% (the
                # shim's own directory, always trailing-backslash) rather
                # than a literal path -- resolve it against the real file.
                dp0 = str(Path(cmd).resolve().parent) + os.sep
                # A callable replacement avoids re.sub() parsing backslashes
                # in the Windows path (dp0) as escape-sequence templates.
                resolved = re.sub(r"%~?dp0%?", lambda _m: dp0, raw, flags=re.IGNORECASE)
                if os.path.exists(resolved):
                    return resolved
                return raw
        except Exception:
            pass

    local_exe = home() / ".local" / "bin" / "claude.exe"
    if local_exe.exists():
        return str(local_exe)

    bare = which("claude")
    if bare:
        return bare

    raise ClaudeNotFoundError("claude executable not found")


def is_server_alive(port: int, expected_hash: str | None) -> bool:
    """Return True if a bridge server is responding on port with matching hash."""
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        if resp.status != 200:
            return False
        data = json.loads(resp.read())
        if expected_hash is not None and data.get("hash") != expected_hash:
            return False
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def check_server_status(port: int, expected_hash: str) -> str:
    """Classify what's on a port: 'absent' (nothing/refused), 'fresh' (claude-bridge, matching hash), 'stale' (claude-bridge, different hash), 'foreign' (something else answered)."""
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException):
        # finding 9: a foreign listener that accepts the TCP connection but
        # never speaks valid HTTP (e.g. answers with garbage, or a status
        # line http.client can't parse) raised http.client.BadStatusLine --
        # a subclass of HTTPException, NOT OSError -- straight through this
        # function and crashed `launch`.
        return "absent"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "foreign"
    if not isinstance(data, dict) or data.get("service") != "claude-bridge":
        return "foreign"
    return "fresh" if data.get("hash") == expected_hash else "stale"


def port_is_free(port: int) -> bool:
    """True if nothing accepts a TCP connection on 127.0.0.1:port right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def spawn_server_detached(port: int, state_dir):
    """Start a bridge server in a detached subprocess."""
    log_path = Path(state_dir) / "server-stdio.log"
    logfile = open(log_path, "ab")
    argv = [sys.executable, __file__, "--serve", "--port", str(port), "--state-dir", str(state_dir)]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": logfile,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        )
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)
    logfile.close()


def _merge_custom_header_lines(process_value: str, chain_value: str) -> list[str]:
    """Merge ANTHROPIC_CUSTOM_HEADERS from the raw process env and the
    settings chain into one ordered list of "Name: Value" lines. The
    settings chain wins on a name collision; a name present only in one side
    is kept as-is; original order is preserved (process-env names first,
    then any new names from the chain) (finding 7)."""
    def parse(value):
        names = []
        values = {}
        for line in (value or "").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, _, val = line.partition(":")
            name = name.strip()
            if not name:
                continue
            if name not in values:
                names.append(name)
            values[name] = val.strip()
        return names, values

    proc_names, proc_values = parse(process_value)
    chain_names, chain_values = parse(chain_value)
    merged = dict(proc_values)
    merged.update(chain_values)
    order = list(proc_names)
    for name in chain_names:
        if name not in order:
            order.append(name)
    return [f"{name}: {merged[name]}" for name in order]


_SECRET_HEADER_NAME_HINTS = ("token", "key", "secret", "auth", "password", "credential")


def log_mirrored_headers(headers: dict, context: str) -> None:
    """Log the NAMES (and, unless the name looks secret, the values) of
    headers about to be mirrored upstream, at INFO -- with nothing logged at
    all when there's nothing to mirror. Without this there was no way to
    confirm from bridge.log that e.g. a work box's
    `x-databricks-use-coding-agent-mode` header actually made it through
    (finding 7)."""
    if not headers:
        return
    parts = []
    for name, value in headers.items():
        if any(hint in name.lower() for hint in _SECRET_HEADER_NAME_HINTS):
            parts.append(f"{name}=<redacted>")
        else:
            parts.append(f"{name}={value}")
    log.info("%s: mirroring %d custom header(s): %s", context, len(headers), ", ".join(parts))


def cmd_launch(args, forwarded_claude_args=None):
    """Launch Claude Code with bridge settings. `forwarded_claude_args` is
    the already-split argv to hand to `claude` untouched -- finding 14 moved
    that split into main()'s _parse_launch_argv (it has to scan the raw
    sys.argv token stream, not this function's already-parsed Namespace)."""
    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    port = args.port or DEFAULT_PORT
    expected_hash = bridge_py_sha1()
    status = check_server_status(port, expected_hash)

    if status == "foreign":
        raise BridgeStartError(
            f"port {port} is already in use by something that is not claude-bridge -- refusing to touch it"
        )

    if status == "stale":
        # A server from an older bridge.py is holding the port -- shut it
        # down with the token it itself wrote to this same state dir, wait
        # for the port to free, then fall through to spawn a fresh one.
        stale_token_path = state_dir / "token"
        stale_token = stale_token_path.read_text(encoding="utf-8").strip() if stale_token_path.exists() else None
        if stale_token:
            conn = None
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/shutdown", headers={"x-api-key": stale_token})
                conn.getresponse().read()
            except OSError:
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        free_deadline = time.time() + 5
        while time.time() < free_deadline and not port_is_free(port):
            time.sleep(0.2)
        status = "absent"

    if status != "fresh":
        # finding 9: don't blindly attempt to spawn a second server -- if
        # /shutdown was refused (wrong/missing token, stale server hung) or
        # an unresponsive-but-listening foreign process still holds the
        # port, the old code spawned anyway and just burned the whole 10s
        # healthz-poll timeout before failing with a generic "did not start"
        # message. Fail fast and name the port instead.
        if not port_is_free(port):
            raise BridgeStartError(
                f"port {port} is still in use (a stale server would not stop, or an unresponsive "
                f"listener is holding it) -- refusing to start a second server"
            )
        spawn_server_detached(port, state_dir)
        deadline = time.time() + 10
        while time.time() < deadline:
            if is_server_alive(port, expected_hash):
                break
            time.sleep(0.2)
        else:
            raise BridgeStartError(f"Server did not start within 10 seconds on port {port}")

    token = ensure_token(state_dir)

    forwarded_claude_args = list(forwarded_claude_args or [])

    user_settings_path = None
    user_settings = {}
    i = 0
    while i < len(forwarded_claude_args):
        arg = forwarded_claude_args[i]
        if arg == "--settings":
            if i + 1 < len(forwarded_claude_args):
                user_settings_path = forwarded_claude_args[i + 1]
                del forwarded_claude_args[i : i + 2]
                break
        elif arg.startswith("--settings="):
            user_settings_path = arg.split("=", 1)[1]
            del forwarded_claude_args[i]
            break
        else:
            i += 1

    if user_settings_path:
        try:
            with open(user_settings_path, "r", encoding="utf-8") as f:
                user_settings = json.load(f)
        except Exception:
            pass

    # finding 15: routes.json's `default`/`small` were accepted (documented
    # even) but no code path ever actually read them -- only `profiles` was
    # wired up. Full fallback chain: --model -> BRIDGE_MODEL -> routes.json
    # default -> built-in; small mirrors it, falling back to the main ref.
    routes_cfg = load_routes(state_dir / "routes.json")
    main_model = (getattr(args, "model", None) or os.environ.get("BRIDGE_MODEL")
                  or routes_cfg.get("default") or "or:deepseek/deepseek-v3.2")
    small_model = (getattr(args, "small_model", None) or os.environ.get("BRIDGE_MODEL_SMALL")
                   or routes_cfg.get("small") or main_model)

    settings = {"model": main_model, "env": {}}
    env = settings["env"]

    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_AUTH_TOKEN"] = token
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        env[key] = main_model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = small_model
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "true"
    env["ANTHROPIC_CUSTOM_MODEL_NAME"] = main_model
    env["ANTHROPIC_CUSTOM_MODEL_DESCRIPTION"] = main_model

    # NOTE: load_settings_env_chain() already returns the flat merged env
    # dict (see its own docstring/SIGNATURES.md) -- do NOT re-index it with
    # .get("env", {}), that would always yield {} and silently drop both the
    # NO_PROXY-preservation and custom-header-mirroring logic below.
    chain_env = load_settings_env_chain(Path.cwd())

    passthrough = is_passthrough_ref(main_model)
    if not passthrough:
        # openai-chat refs need tool search on (128-function cap) and no
        # native "thinking" (nothing implements it on that side yet).
        env["ENABLE_TOOL_SEARCH"] = "true"
        env["MAX_THINKING_TOKENS"] = "0"
    elif chain_env.get("ENABLE_TOOL_SEARCH"):
        # A real Claude model behaves like plain `claude` -- only turn tool
        # search on if the user's own settings chain already wanted it.
        env["ENABLE_TOOL_SEARCH"] = chain_env["ENABLE_TOOL_SEARCH"]

    # finding 10: strip only a LEADING dbx:/or: prefix -- an OpenRouter model
    # id can itself contain a colon (e.g. "qwen/qwen3-coder:free"), and a
    # naive split on the first ':' anywhere mistook that variant suffix for a
    # prefix separator, looking up the wrong models.json key ("free").
    bare_model = main_model
    for _prefix in ("dbx:", "or:"):
        if bare_model.startswith(_prefix):
            bare_model = bare_model[len(_prefix):]
            break
    models_entry = load_models_json(state_dir).get(bare_model) or {}
    if passthrough and not models_entry:
        # A real Claude model with no specific profile on file behaves like
        # plain `claude` -- omit the CLAUDE_CODE_MAX_* overrides entirely
        # instead of silently forcing the openai-chat 128000/16384 defaults
        # onto a model with a much larger real context window, which made a
        # 200k-context Claude session compact at ~100k unlike plain `claude`
        # (finding 10).
        pass
    else:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(models_entry.get("context_length") or 128000)
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(models_entry.get("max_output_tokens") or 16384)
    env["API_TIMEOUT_MS"] = "600000"

    no_proxy_val = chain_env.get("NO_PROXY") or chain_env.get("no_proxy") or ""
    if no_proxy_val and not no_proxy_val.endswith(","):
        no_proxy_val += ","
    no_proxy_val += "127.0.0.1,localhost"
    env["NO_PROXY"] = no_proxy_val
    env["no_proxy"] = no_proxy_val

    # finding 7: merge the settings chain's ANTHROPIC_CUSTOM_HEADERS with
    # whatever the shell itself exported -- a settings `env` value REPLACES
    # (not merges with) the process var once handed to the child, so a
    # shell-exported header (e.g. a work box's
    # `x-databricks-use-coding-agent-mode: true`) used to be silently
    # dropped just because the settings chain didn't also define it. The
    # settings chain wins on a name collision; bridge's own x-bridge-* lines
    # are appended last either way.
    lines = _merge_custom_header_lines(os.environ.get("ANTHROPIC_CUSTOM_HEADERS", ""),
                                        chain_env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
    lines.append(f"x-bridge-main: {main_model}")
    lines.append(f"x-bridge-small: {small_model}")
    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(lines)

    dbx = resolve_databricks()
    if dbx:
        env["WORK_MCP_BASE_URL"] = dbx.host
        env["WORK_MCP_TOKEN"] = dbx.token
        env["DATABRICKS_MCP_BASE_URL"] = dbx.host
        env["DATABRICKS_MCP_TOKEN"] = dbx.token

    if user_settings:
        for k, v in user_settings.items():
            if k == "env" and isinstance(v, dict) and isinstance(settings.get("env"), dict):
                settings["env"].update(v)
            else:
                settings[k] = v

    settings_path = state_dir / f"launch-{os.getpid()}.json"
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f)

    claude_settings_path = home() / ".claude" / "settings.json"
    snapshot_model = None
    snapshot_missing = False
    if claude_settings_path.exists():
        try:
            with open(claude_settings_path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            if "model" in snapshot:
                snapshot_model = snapshot["model"]
        except Exception:
            snapshot_missing = True
    else:
        snapshot_missing = True

    try:
        claude_exe = find_claude_exe()
        child_env = dict(os.environ)
        child_env.pop("CLAUDECODE", None)
        argv = [claude_exe, "--settings", str(settings_path)] + forwarded_claude_args

        if os.name == "nt":
            signal.signal(signal.SIGINT, lambda *_: None)
        p = subprocess.Popen(argv, env=child_env)
        while True:
            try:
                rc = p.wait()
                break
            except KeyboardInterrupt:
                continue
        if rc == -1073741510:
            rc = 130
    finally:
        try:
            settings_path.unlink(missing_ok=True)
        except Exception:
            pass

        if not snapshot_missing:
            try:
                if claude_settings_path.exists():
                    with open(claude_settings_path, "r", encoding="utf-8") as f:
                        current = json.load(f)
                else:
                    current = {}
                if snapshot_model is None:
                    current.pop("model", None)
                else:
                    current["model"] = snapshot_model
                with open(claude_settings_path, "w", encoding="utf-8") as f:
                    json.dump(current, f)
            except Exception:
                pass

    sys.exit(rc)


def cmd_config(args):
    """Print resolved configuration as JSON (no network, secrets redacted)."""
    cfg = resolve_config()
    out = {
        "state_dir": str(cfg.state_dir),
        "openrouter": cfg.openrouter,
        "databricks": cfg.databricks,
        "routes": cfg.routes,
        "env_file_loaded": cfg.env_file_loaded,
    }
    print(json.dumps(out, indent=2))


def cmd_stop(args):
    """Stop a running bridge server. finding 13: POST /shutdown answers 200
    immediately but the real server.shutdown() only fires ~200ms later from a
    background timer -- returning (and printing success) right after the 200
    let a `launch` issued immediately afterward see the dying server still
    answering /healthz and reuse it, handing its child `claude` an
    ECONNREFUSED mid-request. This now polls until the port actually refuses
    connections (up to 5s) before printing anything about success."""
    state_dir = default_state_dir()
    server_json = state_dir / "server.json"
    if not server_json.exists():
        print("No running server found", file=sys.stderr)
        return
    with open(server_json, "r", encoding="utf-8") as f:
        info = json.load(f)
    port = info["port"]
    token_path = state_dir / "token"
    if not token_path.exists():
        print("Token file missing", file=sys.stderr)
        return
    token = token_path.read_text().strip()

    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/shutdown", headers={"x-api-key": token})
        resp = conn.getresponse()
        resp.read()
        status, reason = resp.status, resp.reason
    except ConnectionRefusedError:
        print(f"no bridge server on port {port}")
        return
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if status != 200:
        print(status, reason)
        return

    deadline = time.time() + 5
    while time.time() < deadline and not port_is_free(port):
        time.sleep(0.05)
    print("stopped")


def _parse_launch_argv(argv: list[str]) -> tuple[str | None, str | None, str | None, list[str]]:
    """Parse `launch`'s own argv (everything after the literal "launch"
    token) by scanning from the front: consume --model/--small-model/--port
    (space- or "="-separated) while they lead; stop at the first token that
    doesn't match one of those (an optional literal "--" right there ends
    the scan and is itself consumed). Everything from that point on is
    forwarded to `claude` untouched -- a user `--model` typed after the
    bridge's own flags, or after an explicit "--", still goes to claude.
    Returns (model, small_model, port_str, forwarded_args); each of the
    first three is None when not given.

    A plain argparse.parse_args() cannot do this: it is strict and errors
    out on the first non-bridge-flag token. That mismatch is exactly what
    broke `claude-bridge --model X -p hi` (no "--") from PATH -- the
    wrapper hardcoded `launch -- %*`, so the bridge's own parser only ever
    saw an EMPTY argv (silently using its default model) while
    `--model X -p hi`, including the user's own `--model`, went straight to
    `claude` as ITS argv instead (finding 14)."""
    model = small_model = port_str = None
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--model" and i + 1 < n:
            model = argv[i + 1]
            i += 2
        elif tok.startswith("--model="):
            model = tok.split("=", 1)[1]
            i += 1
        elif tok == "--small-model" and i + 1 < n:
            small_model = argv[i + 1]
            i += 2
        elif tok.startswith("--small-model="):
            small_model = tok.split("=", 1)[1]
            i += 1
        elif tok == "--port" and i + 1 < n:
            port_str = argv[i + 1]
            i += 2
        elif tok.startswith("--port="):
            port_str = tok.split("=", 1)[1]
            i += 1
        else:
            break
    if i < n and argv[i] == "--":
        i += 1
    return model, small_model, port_str, argv[i:]


def main(argv: list[str] | None = None):
    """Main CLI entry point. `argv` defaults to sys.argv[1:] (no script
    name) so rolo_claude.cli's `proxy` subcommand can call this directly
    with its own already-split remainder (`import bridge; bridge.main(rest)`)."""
    if argv is None:
        argv = sys.argv[1:]

    # Bridge's own flags are only ever recognized BEFORE the first literal
    # "--" (everything after that belongs to the forwarded `claude` args and
    # must never be mistaken for e.g. this tool's own --version/--config).
    # NOTE: this "--" heuristic is NOT used for `launch` (see below) -- a
    # "--" there is optional, and even when present may appear after a run
    # of leading bridge flags rather than immediately after "launch".
    sep = argv.index("--") if "--" in argv else len(argv)
    own_argv = argv[:sep]

    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--serve", action="store_true", help="Run the bridge server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--state-dir", help="State directory path")
    parser.add_argument("--config", action="store_true", help="Print configuration and exit")
    parser.add_argument("--stop", action="store_true", help="Stop a running server")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--probe", action="store_true", help="Probe Databricks/OpenRouter reachability and cache model list")

    if "--version" in own_argv:
        print(f"claude-bridge {__version__}")
        return 0

    if own_argv and own_argv[0] == "launch":
        # argv[0] == own_argv[0] == "launch" here, so argv[1:] is everything
        # after it -- scan directly from the raw argv, not `own_argv` (whose
        # "up to the first --" cut doesn't apply to `launch`).
        model, small_model, port_str, forwarded_claude_args = _parse_launch_argv(argv[1:])
        try:
            port = int(port_str) if port_str is not None else None
        except ValueError:
            print(f"{PROG}: --port must be an integer, got {port_str!r}", file=sys.stderr)
            return 2
        launch_args = argparse.Namespace(model=model, small_model=small_model, port=port)
        cmd_launch(launch_args, forwarded_claude_args)
        return 0

    args, _remaining = parser.parse_known_args(own_argv)

    if args.config:
        cmd_config(args)
        return 0

    if args.stop:
        cmd_stop(args)
        return 0

    if args.probe:
        cmd_probe(args)
        return 0

    cmd_serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
