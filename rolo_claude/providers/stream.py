"""rolo_claude.providers.stream -- the request-orchestration lift (H0,
plan section D2). This module is the library form of what used to live
inline in bridge.py's Handler._handle_messages_post/_handle_upstream_stream/
_reader_thread: given an Anthropic-shaped request body plus a resolved
Route/profile/credentials, it does the one-silent-connect-retry and
one-fixable-overflow-clamp-retry dance against the openai-chat dialect
(OpenRouter or Databricks-chat), then yields the Anthropic SSE events for
the reply as plain dicts.

Two phases, exactly like the design doc:
  * phase 1 (nothing yielded yet): translate the body, apply the profile/
    cached-max_tokens clamp, make the upstream call with retries. Raises
    before the first `yield` on failure -- callers drive this with
    ``next(gen)`` so a 4xx/5xx never gets a 200 already written to the wire.
  * phase 2: yields ``message_start`` first, then the translated Anthropic
    events as they arrive, and ``{"type": "ping"}`` after `req.ping_interval`
    seconds of upstream silence. Passing ``abort`` (a ``threading.Event``)
    lets a caller interrupt a live stream -- checked every
    ``min(ping_interval, 0.25)`` seconds so it reacts within ~250ms.

The Databricks Claude PASSTHROUGH dialect (raw Anthropic-to-Anthropic relay)
deliberately does NOT go through this module -- see providers/anthropic_sse.py
and routing.build_passthrough_body/build_passthrough_headers instead; the
proxy's own Handler._handle_passthrough_stream is unchanged.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from rolo_claude.providers.config import dump_debug, estimate_tokens
from rolo_claude.providers.databricks import databricks_unreachable_response, dbx_cache_get_max_tokens_limit
from rolo_claude.providers.errors import build_prompt_too_long_message, map_upstream_error, parse_context_overflow, upstream_error_text
from rolo_claude.providers.http import UpstreamConnectError, call_databricks_chat, call_openai_chat
from rolo_claude.providers.oai_stream import MessageCollector, OpenAIStreamToAnthropic
from rolo_claude.providers.routing import Route
from rolo_claude.providers.translate import anthropic_to_openai

log = logging.getLogger("bridge")


@dataclass
class ProviderCreds:
    """Whatever stream_completion needs to actually reach the upstream: for
    OpenRouter, `base_url` is its /v1 root and `api_key` is OPENROUTER_API_KEY;
    for Databricks (openai-chat dialect), `base_url` is the bare workspace
    root (already run through derive_workspace_root) and `api_key` is the
    Databricks token."""
    base_url: str
    api_key: str


@dataclass
class CompletionRequest:
    body: dict
    route: Route
    profile: dict
    creds: Optional[ProviderCreds]
    state_dir: Path
    extra_headers: dict
    model_label: str
    # H4 will teach OpenAIStreamToAnthropic to open a `thinking` block from
    # OpenAI-dialect reasoning deltas; the field exists now for contract
    # stability but is currently inert (reasoning stays log-only, unchanged
    # from v0.2.1 -- see providers/oai_stream.py).
    emit_reasoning: bool = True
    ping_interval: float = 15.0
    # BRIDGE_OPENROUTER_BASE_URL test seam: when set, overrides creds.base_url
    # for an "openrouter"-provider route ONLY (mirrors the proxy's own
    # SERVER_MOCK_BASE_URL precedence). Databricks has no equivalent field
    # here because resolve_databricks() already folds its own override
    # (BRIDGE_DBX_BASE_URL) into whatever ProviderCreds the caller builds.
    openrouter_base_url: Optional[str] = None
    # H1 scope C: opt-in only, default False so the proxy (which never sets
    # this) is completely unaffected. True only from agent/loop.py's own
    # request construction -- enables reasoning capture + the strict
    # length/malformed-JSON tool-call tagging in oai_stream.py.
    harness_mode: bool = False
    # H1 scope B: when set, `_run_phase1` uses this OpenAI-dialect body
    # VERBATIM instead of calling `translate.anthropic_to_openai` on
    # `req.body` -- lets the harness's profile-driven
    # `providers.request.build_request_body` supply the wire body while
    # still reusing stream.py's retry/overflow-clamp/ping/abort machinery
    # unchanged. None (the proxy's permanent default) preserves the
    # original translate-from-`req.body` behavior exactly.
    prebuilt_oai_body: Optional[dict] = None


class ContextOverflow(Exception):
    """Raised from phase 1 when the upstream reports a context-length
    overflow that a max_tokens clamp-retry could not (or must not) fix --
    the caller is expected to turn this into the same "prompt is too long"
    400 response call_databricks_chat -- er, Handler -- always has:
    ``build_prompt_too_long_message(total or prompt_tokens or limit+1, limit)``
    routed through ``map_upstream_error(400, ...)``."""

    def __init__(self, limit: int, prompt_tokens: int | None, total: int | None):
        self.limit = limit
        self.prompt_tokens = prompt_tokens
        self.total = total
        super().__init__(f"context overflow: limit={limit} prompt_tokens={prompt_tokens} total={total}")


class UpstreamError(Exception):
    """Raised from phase 1 for any non-2xx upstream response (after retries)
    that isn't a context overflow, and for a connect failure on the final
    attempt. Fields mirror exactly what map_upstream_error/
    databricks_unreachable_response already compute, so a caller can
    reconstruct the identical wire response with no extra logic:
    ``{"error": {"type": err_type, "message": message}}`` at `status`, plus
    ``x-should-retry`` from `retryable` and an optional `Retry-After`."""

    def __init__(self, status: int, err_type: str, message: str, retryable: bool, retry_after: str | None = None):
        self.status = status
        self.err_type = err_type
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message)


class ProviderNotConfigured(Exception):
    """Raised from phase 1 when `req.creds` is None -- the caller asked for
    a route whose provider has no usable credentials. Message text matches
    the proxy's long-standing "Databricks not configured" / "OpenRouter not
    configured" 502 wording exactly."""


def _upstream_error_from_mapping(status: int, jbody: dict, hdrs: dict) -> UpstreamError:
    err = jbody.get("error") or {}
    return UpstreamError(
        status=status,
        err_type=err.get("type", "api_error"),
        message=err.get("message", ""),
        retryable=hdrs.get("x-should-retry") == "true",
        retry_after=hdrs.get("Retry-After"),
    )


def sse_reader_thread(resp, q: "queue.Queue") -> None:
    """Background thread reading an upstream openai-chat response (SSE lines,
    or -- when the upstream lied about the content-type -- one fully
    buffered JSON body) and posting items to `q`. This is exactly
    bridge.py's old ``Handler._reader_thread`` moved out to a plain function
    (it never used `self` for anything)."""
    try:
        if resp.getheader("content-type", "").startswith("text/event-stream"):
            while True:
                line = resp.readline()
                if not line:
                    # A clean readline() EOF happens BOTH for a properly
                    # terminated chunked body AND for a connection that died
                    # mid-chunk (empirically, http.client does not raise
                    # IncompleteRead here) -- resp.chunk_left is the only
                    # reliable signal after the fact: None means the
                    # terminating zero-chunk was actually seen; any other
                    # value means the body was cut off mid-stream.
                    if getattr(resp, "chunk_left", None) is None:
                        q.put(("eof", None))
                    else:
                        q.put(("exc", ConnectionError("upstream connection closed mid-stream")))
                    break
                q.put(("line", line))
        else:
            data = resp.read()
            try:
                obj = json.loads(data.decode("utf-8", "replace"))
                q.put(("json", obj))
            except json.JSONDecodeError:
                q.put(("exc", ValueError("Upstream returned non-JSON")))
            q.put(("eof", None))
    except Exception as e:
        q.put(("exc", e))
    finally:
        resp.close()


class _Aborted(Exception):
    """Internal-only: raised from `_run_phase1` when `abort` fires between
    (not during -- see the module docstring's finding-4 note) retry
    attempts. Caught in `stream_completion`, which then simply returns
    without yielding anything, exactly like an abort during phase 2."""


def _run_phase1(req: CompletionRequest, abort: "threading.Event | None" = None):
    """Translate + call upstream with retries. Returns (oai_body, result) on
    a genuine 2xx. Raises ContextOverflow/UpstreamError/ProviderNotConfigured
    (or lets WebSearchUnavailable from anthropic_to_openai propagate
    untouched) otherwise. Order of checks matches the pre-lift Handler code
    exactly: translate (may raise WebSearchUnavailable) before the
    credentials check, so an untested-but-real simultaneous
    "web_search tool present AND provider unconfigured" case still reports
    the same error it always did.

    `abort` (finding 4) is checked before each connect attempt -- a caller
    that sets it while phase 1 is between attempts (e.g. during the single
    connect retry) gets `_Aborted` instead of a completed request. A
    connect/request already blocked INSIDE `_call_upstream()` cannot be
    interrupted from here: `call_openai_chat`/`call_databricks_chat` open,
    send, and read response headers as one blocking call with no hook point
    to hand back an in-flight socket earlier -- documented limitation, not
    silently ignored; phase 2's long-lived read loop (the actual reported
    leak: "the daemon reader pulling billed tokens until the model
    finishes") is fully abort-aware via the socket capture below."""
    if abort is not None and abort.is_set():
        raise _Aborted()
    oai_body = (req.prebuilt_oai_body if req.prebuilt_oai_body is not None
                else anthropic_to_openai(req.body, req.route, profile=req.profile))

    if req.route.provider == "databricks":
        cached_limit = dbx_cache_get_max_tokens_limit(req.route.upstream_model, req.state_dir)
        if isinstance(cached_limit, int) and isinstance(oai_body.get("max_tokens"), int) \
                and oai_body["max_tokens"] > cached_limit:
            oai_body["max_tokens"] = cached_limit

    if req.creds is None:
        provider_label = "Databricks" if req.route.provider == "databricks" else "OpenRouter"
        raise ProviderNotConfigured(f"{provider_label} not configured")

    if req.route.provider == "databricks":
        def _call_upstream():
            return call_databricks_chat(
                base_url=req.creds.base_url, api_key=req.creds.api_key, body=oai_body,
                extra_headers=req.extra_headers, state_dir=req.state_dir, model=req.route.upstream_model,
            )
    else:
        base_url = req.openrouter_base_url or req.creds.base_url

        def _call_upstream():
            return call_openai_chat(
                base_url=base_url, api_key=req.creds.api_key, body=oai_body,
                extra_headers=req.extra_headers, state_dir=req.state_dir,
            )

    max_attempts = 2
    overflow_retries = 0
    result = None
    for attempt in range(max_attempts):
        if abort is not None and abort.is_set():
            raise _Aborted()
        try:
            result = _call_upstream()
        except UpstreamConnectError as e:
            if attempt == 0:
                continue
            if req.route.provider == "databricks":
                status, jbody, hdrs = databricks_unreachable_response(str(e))
            else:
                status, jbody, hdrs = map_upstream_error(502, {"error": {"message": str(e)}}, req.route.provider)
            raise _upstream_error_from_mapping(status, jbody, hdrs) from e

        if 200 <= result.status < 300:
            return oai_body, result

        # Non-2xx: read the body ONCE (HTTPResponse.read() cannot be
        # replayed) -- call_databricks_chat may already have consumed it
        # itself while checking for a max_tokens-limit wording, in which
        # case it hands the bytes back via body_bytes instead.
        if result.body_bytes is not None:
            raw = result.body_bytes
        else:
            raw = result.resp.read() if result.resp else b""
        try:
            err_obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except (json.JSONDecodeError, ValueError):
            err_obj = {"error": {"message": raw.decode("utf-8", "replace")}}

        try:
            err_msg = upstream_error_text(err_obj)
            raw_meta = None
            if isinstance(err_obj, dict):
                err_field = err_obj.get("error")
                if isinstance(err_field, dict):
                    meta = err_field.get("metadata")
                    if isinstance(meta, dict):
                        raw_meta = meta.get("raw")

            if result.status == 400:
                overflow = parse_context_overflow(result.status, err_msg, raw_meta,
                                                   requested_max_tokens=oai_body.get("max_tokens"))
                if overflow:
                    if overflow.fixable and overflow_retries < 1:
                        overflow_retries += 1
                        oai_body["max_tokens"] = max(1, overflow.limit - overflow.prompt_tokens - 256)
                        continue
                    raise ContextOverflow(overflow.limit, overflow.prompt_tokens, overflow.total)

            status, jbody, hdrs = map_upstream_error(result.status, err_obj, req.route.provider, result.headers)
        except ContextOverflow:
            raise
        except Exception as e:
            log.warning("malformed upstream error body, mapping to 502: %s", e)
            status, jbody, hdrs = 502, {"error": {"type": "api_error", "message": f"malformed upstream response: {e}"}}, {"x-should-retry": "true"}
        raise _upstream_error_from_mapping(status, jbody, hdrs)
    else:
        status, jbody, hdrs = map_upstream_error(502, {"error": {"message": "upstream failure after retries"}}, req.route.provider)
        raise _upstream_error_from_mapping(status, jbody, hdrs)


def stream_completion(req: CompletionRequest, abort: "threading.Event | None" = None) -> Iterator[dict]:
    """Drive one openai-chat-dialect completion end to end. See the module
    docstring for the two-phase contract. `abort.set()` at any point makes
    the generator shut down the upstream connection (SHUT_RDWR, to
    unblock a reader thread stuck in a blocking read) and stop; a caller
    that wants to interrupt early should also call `gen.close()` once it
    stops consuming so cleanup runs promptly rather than waiting on GC."""
    try:
        oai_body, result = _run_phase1(req, abort=abort)
    except _Aborted:
        return

    estimate = estimate_tokens(oai_body)
    # `req.emit_reasoning` defaults to True and the PROXY never sets
    # `harness_mode` -- gate the new capture_reasoning/strict_tool_json
    # behavior on `harness_mode` alone (default False) so `bridge.py`'s own
    # CompletionRequest construction is completely unaffected regardless of
    # what emit_reasoning happens to be.
    harness_mode = getattr(req, "harness_mode", False)
    sm = OpenAIStreamToAnthropic(req.model_label, estimate,
                                  capture_reasoning=harness_mode, strict_tool_json=harness_mode)

    dumped_lines: list = []
    dumped_events: list = []
    q: "queue.Queue" = queue.Queue()
    reader = threading.Thread(target=sse_reader_thread, args=(result.resp, q))
    reader.daemon = True
    reader.start()

    # finding 4: captured ONCE, right here -- never re-read from
    # `result.conn.sock` later, which http.client may already have nulled
    # out by the time a `finally` block gets around to reading it.
    sock = result.conn.sock if result.conn is not None else None
    terminal_reached = False

    try:
        start_ev = sm.message_start_event()
        dumped_events.append(start_ev)
        yield start_ev

        poll_timeout = min(req.ping_interval, 0.25) if req.ping_interval > 0 else 0.25
        elapsed = 0.0
        while True:
            if abort is not None and abort.is_set():
                return
            try:
                item = q.get(timeout=poll_timeout)
            except queue.Empty:
                elapsed += poll_timeout
                if elapsed >= req.ping_interval:
                    elapsed = 0.0
                    # Pings are a wire-protocol nicety, not model output --
                    # never added to dumped_events (matches the pre-lift
                    # Handler, which wrote them via a direct write_chunk call
                    # that bypassed its _write_event/dump accumulator too).
                    yield {"type": "ping"}
                continue
            elapsed = 0.0
            kind, value = item
            try:
                if kind == "line":
                    line = value.decode("utf-8", "replace").rstrip("\n")
                    dumped_lines.append(line)
                    step = sm.feed_sse_line(line)
                    for ev in step["events"]:
                        dumped_events.append(ev)
                        yield ev
                    if step["kind"] in ("error", "done"):
                        break
                elif kind == "json":
                    dumped_lines.append(value)
                    for ev in MessageCollector.to_events(value, sm):
                        dumped_events.append(ev)
                        yield ev
                    break
                elif kind == "eof":
                    for ev in sm.on_eof():
                        dumped_events.append(ev)
                        yield ev
                    break
                elif kind == "exc":
                    ev = sm.error_event(str(value))
                    dumped_events.append(ev)
                    yield ev
                    break
            except Exception as e:
                # finding 6 (see wip/SIGNATURES.md): malformed upstream data
                # must never leave the stream with neither an error event
                # nor message_stop. (Unlike the pre-lift Handler, nothing in
                # this try block can raise a socket/BrokenPipe error anymore
                # -- this generator never writes to the client itself -- so
                # there is no connection-error class to let through uncaught.)
                log.warning("malformed upstream stream data: %s", e)
                ev = sm.error_event(f"upstream sent malformed data: {e}")
                dumped_events.append(ev)
                yield ev
                break
        # Reached only via one of the `break`s above -- every one of them
        # already delivered a real terminal wire event (done/error/eof/exc)
        # to the consumer; an `abort`-triggered `return` or an external
        # `gen.close()`/exception skips this line entirely, which is
        # exactly the "no terminal event was consumed" case `finally` below
        # must still clean up after.
        terminal_reached = True
    finally:
        if not terminal_reached and sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        dump_debug(req.state_dir, "upstream-stream", {"lines": dumped_lines})
        dump_debug(req.state_dir, "emitted-events", {"events": dumped_events})
