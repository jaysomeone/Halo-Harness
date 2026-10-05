"""halo_harness.providers.stream -- the request-orchestration lift (H0,
plan section D2). This module is the library form of what used to live
inline in bridge.py's Handler._handle_messages_post/_handle_upstream_stream/
_reader_thread: given an Anthropic-shaped request body plus a resolved
Route/profile/credentials, it does the one-fixable-overflow-clamp-retry
dance against the openai-chat dialect (OpenRouter or Databricks-chat), then
yields the Anthropic SSE events for the reply as plain dicts.

2.0.1 finding 18: a genuine CONNECT-phase failure (DNS/TCP/TLS/our own
bounded-connect timeout -- `providers.http.is_connect_failure_message`)
is never retried here any more -- a retry cannot help DNS, and the
documented 8s connect budget (`providers.http.DEFAULT_CONNECT_TIMEOUT_S`)
is now really just 8s, not up to 16s. The one silent retry THIS module
still does is for a POST-connect failure (a dropped keep-alive/mid-
response RST -- `format_post_connect_error`, no CONNECT_FAILURE_MARKER):
that one genuinely can recover on an immediate re-dial.

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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from halo_harness.providers.config import dump_debug, estimate_tokens
from halo_harness.providers.databricks import databricks_unreachable_response, dbx_cache_get_max_tokens_limit
from halo_harness.providers.errors import (
    map_upstream_error, parse_context_overflow,
    parse_databricks_rate_limit, upstream_error_text,
)
from halo_harness.providers.http import (
    UpstreamConnectError, call_anthropic_native, call_databricks_chat, call_ollama_chat, call_openai_chat,
    is_connect_failure_message,
)
from halo_harness.providers.oai_stream import MessageCollector, OpenAIStreamToAnthropic
from halo_harness.providers.ollama_stream import OllamaStreamToAnthropic
from halo_harness.providers.routing import Route
from halo_harness.providers.translate import anthropic_to_openai
from halo_harness.providers.anthropic_sse import AnthropicSSEDecoder
from halo_harness.providers.errors import SSE_CHUNK_IDLE_TIMEOUT_S

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
    # H2 finding 1: row-driven tool-call id transform (providers/hooks.py.
    # normalize_tool_id) -- "mint" (oai_stream.py's own permanent default)
    # keeps the proxy always minting fresh unique ids, exactly as before;
    # only agent/loop.py's Session ever passes anything else.
    tool_id_format: str = "mint"
    # H9 critical review finding 1: the "kimi_functions_idx" rename counter
    # must continue across the WHOLE session (Kimi's own idx is
    # conversation-global on the wire), not restart at 0 on every fresh
    # per-call stream -- agent/loop.py computes this from
    # agent/invariants.highest_kimi_functions_idx(self.log) + 1 before each
    # call; 0 (the default) is correct for the proxy, which never sets this
    # and never uses "kimi_functions_idx" either.
    kimi_tool_id_start: int = 0
    # H5 scope C: when set, `stream_anthropic_completion` (never
    # `stream_completion`, which stays openai-chat-dialect only) sends
    # this ALREADY-native-Anthropic-shaped body verbatim instead of
    # calling `providers.request.build_anthropic_request_body` itself --
    # same "prebuilt body, caller already did the profile-driven building"
    # pattern as `prebuilt_oai_body` above.
    prebuilt_anthropic_body: Optional[dict] = None
    # Halo 2.0.3 round 2: `stream_ollama_completion`'s own sibling field --
    # the caller always builds this via `providers.ollama_request.
    # build_ollama_request_body` first (same "prebuilt body" pattern as
    # `prebuilt_anthropic_body` just above; Ollama's native wire shape is
    # neither openai-chat nor Anthropic-passthrough, so it gets its own
    # field rather than overloading either existing one).
    prebuilt_ollama_body: Optional[dict] = None
    # Halo 2.0.3 round 5c FIX PASS: the retry CEILING for `_run_phase1_
    # ollama`'s own "the server said this prompt exceeds num_ctx" 400 --
    # `min(learned_cap, host_max_ctx, fit_estimate, hard_cap)` for
    # whichever of those four are known (the task's own exact candidate
    # list -- NOT `trained_context`/the conservative fallback, since
    # those are already reflected in `prebuilt_ollama_body["options"]
    # ["num_ctx"]` and second-guessing either would risk asking for more
    # context than the model was actually trained for). `agent/loop.py`'s
    # `_build_ollama_body_for_ref` is the only populator; `None` (every
    # non-ollama dialect, and any ollama caller that predates this fix)
    # means "no bigger number ctx is known to be available" -- `_run_
    # phase1_ollama` then raises the SAME plain `ContextOverflow` it
    # always did, straight to the existing compaction path.
    ollama_ctx_retry_ceiling: Optional[int] = None


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
    retry attempts, OR (must-do 5) during one, via the connect-time socket
    watcher below. Caught in `stream_completion`, which then simply
    returns without yielding anything, exactly like an abort during
    phase 2."""


def _phase1_abort_watcher(abort: "threading.Event", done: threading.Event, sock_box: list) -> None:
    """must-do 5: `_call_upstream()` is a single blocking call (connect,
    send, read response HEADERS) with no other hook point to interrupt it
    from -- this thread polls `abort` (0.25s, matching phase 2's own
    poll interval; `threading.Event` has no "wait for either of two
    events" primitive) while phase 1 is in flight, and force-shuts
    whatever socket `on_connect` most recently registered into `sock_box`
    the moment `abort` fires, so a stuck time-to-first-byte wait doesn't
    run the full 300s idle timeout. `done` (set in `_run_phase1`'s
    `finally`) stops the watcher as soon as phase 1 finishes on its own
    (success OR failure), via `Event.wait`'s immediate-return-on-set, so it
    never outlives the call it's watching."""
    while not done.is_set():
        if abort.is_set():
            sock = sock_box[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return
        done.wait(0.25)


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
    connect retry) gets `_Aborted` instead of a completed request.
    must-do 5: a connect/request already blocked INSIDE `_call_upstream()`
    (time-to-first-byte, up to the 300s idle timeout) is now ALSO
    interruptible -- `on_connect` hands the live socket to
    `_phase1_abort_watcher` the moment `open_upstream()` connects, so an
    abort during that wait force-shuts it instead of running the full
    timeout; phase 2's long-lived read loop remains separately abort-aware
    via its own socket capture."""
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
        # 2.0.3 round 4: a huggingface route with no creds gets its OWN
        # message naming BOTH config keys (`HF_TOKEN` for the router,
        # `huggingface.endpoints` for a named dedicated endpoint) -- before
        # this branch existed, the two-way Databricks/"everything else"
        # label below mislabeled it "OpenRouter not configured", which is
        # wrong and unhelpful for either hf: failure shape (brief item 2:
        # "a missing entry gives a plain ProviderNotConfigured message
        # naming the config key").
        if req.route.provider == "huggingface":
            # Round 5: the message now also names `huggingface.local_
            # servers`/auto-detection -- `req.route` (providers.routing.
            # Route) carries no field distinguishing router/endpoint/local
            # at all (it is built from `ModelRef` by dropping `.host`/
            # `.local`, same as every other Route construction site in
            # this codebase), so this stays ONE generic message naming
            # every way to configure Hugging Face, exactly like round 4's
            # original version already did for router-vs-endpoint.
            raise ProviderNotConfigured(
                "Hugging Face not configured -- set HF_TOKEN for the router, add this name to "
                "huggingface.endpoints, or add/run a local server (huggingface.local_servers, "
                "or auto-detection on a default port)"
            )
        provider_label = "Databricks" if req.route.provider == "databricks" else "OpenRouter"
        raise ProviderNotConfigured(f"{provider_label} not configured")

    sock_box: list = [None]

    def _register_sock(conn) -> None:
        sock_box[0] = conn.sock

    if req.route.provider == "databricks":
        def _call_upstream():
            return call_databricks_chat(
                base_url=req.creds.base_url, api_key=req.creds.api_key, body=oai_body,
                extra_headers=req.extra_headers, state_dir=req.state_dir, model=req.route.upstream_model,
                on_connect=_register_sock,
            )
    else:
        base_url = req.openrouter_base_url or req.creds.base_url

        def _call_upstream():
            return call_openai_chat(
                base_url=base_url, api_key=req.creds.api_key, body=oai_body,
                extra_headers=req.extra_headers, state_dir=req.state_dir,
                on_connect=_register_sock,
            )

    max_attempts = 2
    watcher_done = threading.Event()
    watcher = None
    if abort is not None:
        watcher = threading.Thread(target=_phase1_abort_watcher, args=(abort, watcher_done, sock_box), daemon=True)
        watcher.start()
    try:
        return _run_phase1_attempts(req, oai_body, _call_upstream, abort, max_attempts)
    finally:
        watcher_done.set()  # stop the watcher whether phase 1 succeeded, failed, or raised


def _run_phase1_attempts(req, oai_body, _call_upstream, abort, max_attempts):
    overflow_retries = 0
    result = None
    for attempt in range(max_attempts):
        if abort is not None and abort.is_set():
            raise _Aborted()
        try:
            result = _call_upstream()
        except UpstreamConnectError as e:
            if abort is not None and abort.is_set():
                # must-do 5: the abort watcher force-shut a socket WHILE
                # this call was blocked inside getresponse() -- that's an
                # abort, not a genuine connectivity failure; never retry it
                # or map it to a 502.
                raise _Aborted() from e
            # 2.0.1 finding 18: a genuine CONNECT-phase failure (DNS/TCP/
            # TLS/our own bounded-connect timeout -- carries CONNECT_
            # FAILURE_MARKER) is NEVER retried here any more -- a retry
            # cannot help DNS, so the documented 8s connect budget is now
            # really just 8s, not up to 16s. Only a POST-connect failure
            # (a dropped keep-alive/mid-response RST, no marker -- 1.0.1
            # fixpass finding 2) still gets this one immediate re-dial,
            # since THAT can genuinely recover (review finding 2's own
            # "a load balancer drops a keep-alive while Databricks queues
            # the request" scenario -- see test_step_retries_a_post_
            # connect_failure_through_the_normal_ladder).
            if attempt == 0 and not is_connect_failure_message(str(e)):
                continue
            # 1.0.1 hotfix 2: the wire mapping here is DELIBERATELY left
            # byte-for-byte unchanged (`bridge.py`'s legacy proxy path calls
            # this SAME shared phase1 for its own Databricks/OpenRouter
            # openai-chat requests, and its own pinned test_bridge.py
            # asserts this exact "(are you on the VPN? ...)" wording) --
            # `agent/loop.py`'s own `_step` instead recognizes a connect
            # failure by `providers.http.is_connect_failure_message(e.message)`
            # (the canonical "cannot resolve/reach <host> ..." lead-in
            # `format_connect_error` always bakes in, which survives as a
            # substring through EITHER branch below) to skip its backoff
            # ladder, rather than this function changing status/err_type.
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
            if req.route.provider == "databricks" and result.status == 429 and "Retry-After" not in hdrs:
                # finding 7: Databricks' 429 body carries retry_after
                # (parse_databricks_rate_limit was, pre-H2, only ever
                # called from tests) -- read it whenever the response
                # didn't ALSO send a Retry-After header.
                dbx_retry = parse_databricks_rate_limit(err_obj).get("retry_after")
                if dbx_retry is not None:
                    hdrs = {**hdrs, "Retry-After": str(dbx_retry)}
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
                                  capture_reasoning=harness_mode, strict_tool_json=harness_mode,
                                  tool_id_format=getattr(req, "tool_id_format", "mint"),
                                  kimi_tool_id_start=getattr(req, "kimi_tool_id_start", 0))

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
                    if step["kind"] == "done":
                        terminal_reached = True
                        break
                    if step["kind"] == "error":
                        # finding 12: a mid-stream {"error":...} SSE chunk
                        # delivers an error EVENT to the consumer, but the
                        # upstream connection itself is NOT necessarily
                        # done -- verified: the mock kept streaming ~30 more
                        # chunks over ~3s with the reader thread still
                        # alive. Leave terminal_reached False so the
                        # `finally` block below force-shuts the socket.
                        break
                elif kind == "json":
                    dumped_lines.append(value)
                    for ev in MessageCollector.to_events(value, sm):
                        dumped_events.append(ev)
                        yield ev
                    terminal_reached = True
                    break
                elif kind == "eof":
                    for ev in sm.on_eof():
                        dumped_events.append(ev)
                        yield ev
                    terminal_reached = True
                    break
                elif kind == "exc":
                    ev = sm.error_event(str(value))
                    dumped_events.append(ev)
                    yield ev
                    terminal_reached = True  # the reader thread itself already ended (a real connection error)
                    break
            except Exception as e:
                # finding 6 (see wip/SIGNATURES.md): malformed upstream data
                # must never leave the stream with neither an error event
                # nor message_stop. (Unlike the pre-lift Handler, nothing in
                # this try block can raise a socket/BrokenPipe error anymore
                # -- this generator never writes to the client itself -- so
                # there is no connection-error class to let through uncaught.)
                # finding 12: same rule as the mid-stream error chunk above
                # -- this is OUR parser choking on one malformed piece of a
                # stream the upstream may still be actively sending; leave
                # terminal_reached False so `finally` shuts the socket.
                log.warning("malformed upstream stream data: %s", e)
                ev = sm.error_event(f"upstream sent malformed data: {e}")
                dumped_events.append(ev)
                yield ev
                break
        # Reached via one of the `break`s above; `terminal_reached` was set
        # TRUE only for done/eof/exc (finding 12 -- a genuine end of
        # stream), and stays FALSE for a mid-stream error chunk or a
        # malformed-data exception, so `finally` below still force-shuts a
        # socket the upstream may still be actively writing to. An
        # `abort`-triggered `return` or an external `gen.close()`/exception
        # skips this comment entirely, which is exactly the other
        # "no terminal event was consumed" case `finally` must clean up.
    finally:
        if not terminal_reached and sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # H9 static pass (`-X dev -W error::ResourceWarning`): the reader
        # thread closes the HTTPResponse, but nothing ever closed the
        # HTTPConnection itself, so its socket lived on until garbage
        # collection -- one "unclosed <socket.socket ...>" ResourceWarning
        # per model call (74 of them attributed to agent/loop.py's
        # `for ev in gen`, 15 more to the summariser call, in one full
        # suite run). Every call opens its own connection (no keep-alive
        # reuse), so closing it here -- after the terminal event, or after
        # the SHUT_RDWR above on an early exit -- is always correct.
        conn = getattr(result, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        dump_debug(req.state_dir, "upstream-stream", {"lines": dumped_lines})
        dump_debug(req.state_dir, "emitted-events", {"events": dumped_events})


# ---------------------------------------------------------------------------
# H5 scope C: the native-Anthropic-dialect sibling of stream_completion.
# Deliberately NOT a code path inside stream_completion itself (see this
# module's own docstring: the Databricks passthrough dialect stays split
# from the openai-chat one) -- but it DOES share stream_completion's retry/
# overflow/ping/abort DESIGN, just against `call_anthropic_native` and
# `AnthropicSSEDecoder` instead of the openai-chat call + translator. Events
# pass through essentially AS-IS (already Anthropic-shaped on the wire),
# which is what makes this simpler than stream_completion's translation.
# ---------------------------------------------------------------------------

def _run_phase1_anthropic(req: CompletionRequest, abort: "threading.Event | None" = None):
    if abort is not None and abort.is_set():
        raise _Aborted()
    body = req.prebuilt_anthropic_body
    if req.creds is None:
        provider_label = "Databricks" if req.route.provider == "databricks" else "Anthropic"
        raise ProviderNotConfigured(f"{provider_label} not configured")

    sock_box: list = [None]

    def _register_sock(conn) -> None:
        sock_box[0] = conn.sock

    def _call_upstream():
        return call_anthropic_native(
            base_url=req.creds.base_url, api_key=req.creds.api_key, body=body,
            extra_headers=req.extra_headers, state_dir=req.state_dir,
            route_provider=req.route.provider, on_connect=_register_sock,
        )

    watcher_done = threading.Event()
    watcher = None
    if abort is not None:
        watcher = threading.Thread(target=_phase1_abort_watcher, args=(abort, watcher_done, sock_box), daemon=True)
        watcher.start()
    try:
        for attempt in range(2):
            if abort is not None and abort.is_set():
                raise _Aborted()
            try:
                result = _call_upstream()
            except UpstreamConnectError as e:
                if abort is not None and abort.is_set():
                    raise _Aborted() from e
                # 2.0.1 finding 18: see _run_phase1_attempts's matching
                # comment -- a genuine connect-phase failure is terminal
                # on the first attempt; only a post-connect failure (no
                # CONNECT_FAILURE_MARKER) still gets the one immediate
                # re-dial.
                if attempt == 0 and not is_connect_failure_message(str(e)):
                    continue
                # 1.0.1 hotfix 2: see _run_phase1_attempts's matching comment
                # -- wire mapping here stays exactly as it was (bridge.py's
                # Databricks Claude passthrough also calls this).
                status, jbody, hdrs = map_upstream_error(502, {"error": {"message": str(e)}}, req.route.provider)
                raise _upstream_error_from_mapping(status, jbody, hdrs) from e
            if 200 <= result.status < 300:
                return body, result
            raw = result.resp.read() if result.resp else b""
            try:
                err_obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            except (json.JSONDecodeError, ValueError):
                err_obj = {"error": {"message": raw.decode("utf-8", "replace")}}
            err_msg = upstream_error_text(err_obj)
            if result.status == 400:
                overflow = parse_context_overflow(result.status, err_msg, None, requested_max_tokens=body.get("max_tokens"))
                if overflow:
                    raise ContextOverflow(overflow.limit, overflow.prompt_tokens, overflow.total)
            status, jbody, hdrs = map_upstream_error(result.status, err_obj, req.route.provider, result.headers)
            if result.status == 429 and "Retry-After" not in hdrs:
                # H5 scope E: honour `retry-after`/`anthropic-ratelimit-*`
                # even when the response used neither header's canonical
                # casing (http.client lower-cases headers for us already,
                # so this is really just picking whichever of the two the
                # response actually sent).
                ra = result.headers.get("retry-after") or result.headers.get("anthropic-ratelimit-requests-reset")
                if ra:
                    hdrs = {**hdrs, "Retry-After": str(ra)}
            raise _upstream_error_from_mapping(status, jbody, hdrs)
        raise UpstreamError(502, "api_error", "upstream failure after retries", True)
    finally:
        watcher_done.set()


def stream_anthropic_completion(req: CompletionRequest, abort: "threading.Event | None" = None) -> Iterator[dict]:
    """Drive one native-Anthropic-dialect completion end to end (ant:,
    Databricks Claude passthrough). Same two-phase contract as
    `stream_completion`; `req.prebuilt_anthropic_body` MUST be set (the
    caller -- agent/loop.py -- always builds it via
    `providers.request.build_anthropic_request_body` first)."""
    try:
        _body, result = _run_phase1_anthropic(req, abort=abort)
    except _Aborted:
        return

    decoder = AnthropicSSEDecoder()
    dumped_lines: list = []
    dumped_events: list = []
    q: "queue.Queue" = queue.Queue()
    reader = threading.Thread(target=sse_reader_thread, args=(result.resp, q))
    reader.daemon = True
    reader.start()

    sock = result.conn.sock if result.conn is not None else None
    terminal_reached = False
    last_activity = time.monotonic()

    try:
        poll_timeout = min(req.ping_interval, 0.25) if req.ping_interval > 0 else 0.25
        elapsed = 0.0
        while True:
            if abort is not None and abort.is_set():
                return
            try:
                item = q.get(timeout=poll_timeout)
            except queue.Empty:
                elapsed += poll_timeout
                if time.monotonic() - last_activity >= SSE_CHUNK_IDLE_TIMEOUT_S:
                    # H5 scope F item 3: the chunk-idle watchdog -- classified
                    # retryable (providers.errors.is_retryable_message's own
                    # kind="sse_read_timeout" branch); the CALLER (agent/
                    # loop.py's _step) is what actually retries the whole
                    # request, this generator just reports the failure.
                    ev = {"type": "error", "error": {"type": "api_error",
                          "message": f"SSE read timed out after {SSE_CHUNK_IDLE_TIMEOUT_S:.0f}s of upstream silence",
                          "kind": "sse_read_timeout"}}
                    dumped_events.append(ev)
                    yield ev
                    break
                if elapsed >= req.ping_interval:
                    elapsed = 0.0
                    yield {"type": "ping"}
                continue
            last_activity = time.monotonic()
            elapsed = 0.0
            kind, value = item
            if kind == "line":
                line = value.decode("utf-8", "replace").rstrip("\n")
                dumped_lines.append(line)
                for ev in decoder.feed_line(line):
                    dumped_events.append(ev)
                    yield ev
                    if ev.get("type") == "message_stop":
                        terminal_reached = True
                if decoder.done:
                    break
            elif kind == "eof":
                for ev in decoder.on_eof():
                    dumped_events.append(ev)
                    yield ev
                terminal_reached = True
                break
            elif kind == "exc":
                ev = {"type": "error", "error": {"type": "api_error", "message": f"upstream connection error: {value}"}}
                dumped_events.append(ev)
                yield ev
                terminal_reached = True
                break
    finally:
        if not terminal_reached and sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # H9 static pass (`-X dev -W error::ResourceWarning`): the reader
        # thread closes the HTTPResponse, but nothing ever closed the
        # HTTPConnection itself, so its socket lived on until garbage
        # collection -- one "unclosed <socket.socket ...>" ResourceWarning
        # per model call (74 of them attributed to agent/loop.py's
        # `for ev in gen`, 15 more to the summariser call, in one full
        # suite run). Every call opens its own connection (no keep-alive
        # reuse), so closing it here -- after the terminal event, or after
        # the SHUT_RDWR above on an early exit -- is always correct.
        conn = getattr(result, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        dump_debug(req.state_dir, "upstream-stream", {"lines": dumped_lines})
        dump_debug(req.state_dir, "emitted-events", {"events": dumped_events})


# ---------------------------------------------------------------------------
# Halo 2.0.3 round 2: the `ol:` (Ollama native API) sibling of
# stream_completion/stream_anthropic_completion. Ollama's NDJSON stream is
# neither SSE (stream_completion's own dialect) nor Anthropic-native SSE
# (stream_anthropic_completion's) -- one complete JSON object per line,
# over a plain chunked HTTP body -- so it gets its own reader thread
# (_ndjson_reader_thread) instead of reusing sse_reader_thread, which
# assumes either real SSE framing or a single whole-body JSON fallback.
# ---------------------------------------------------------------------------

def _ndjson_reader_thread(resp, q: "queue.Queue") -> None:
    """Background thread reading Ollama's native `/api/chat` NDJSON
    response (one complete JSON object per line, chunked transfer -- never
    SSE framing) and posting items to `q`, the SAME `(kind, value)`
    protocol `sse_reader_thread` uses (`"line"`/`"eof"`/`"exc"`) so
    `stream_ollama_completion`'s drain loop below can share its shape."""
    try:
        while True:
            line = resp.readline()
            if not line:
                if getattr(resp, "chunk_left", None) is None:
                    q.put(("eof", None))
                else:
                    q.put(("exc", ConnectionError("upstream connection closed mid-stream")))
                break
            q.put(("line", line))
    except Exception as e:
        q.put(("exc", e))
    finally:
        resp.close()


def _ollama_overflow_info(status: int, err_obj) -> "Optional[dict]":
    """Halo 2.0.3 round 5c FIX PASS (live run, build 0.34.2): Ollama DOES
    answer a 400 when a prompt exceeds `num_ctx` -- confirmed live:
    ``{"error": {"code": 400, "message": "request (N tokens) exceeds the
    available context size (M tokens), try increasing it", "type":
    "exceed_context_size_error", "n_prompt_tokens": N, "n_ctx": M}}``.
    NOTE `error` is an OBJECT here, unlike Ollama's usual bare-string
    `{"error": "..."}` shape elsewhere in this codebase. `{"n_prompt_
    tokens", "n_ctx"}` when this exact shape matches, else `None` (any
    other 400, or a malformed/incomplete one, falls through to the
    ordinary `map_upstream_error` path unchanged)."""
    if status != 400 or not isinstance(err_obj, dict):
        return None
    inner = err_obj.get("error")
    if not isinstance(inner, dict) or inner.get("type") != "exceed_context_size_error":
        return None
    n_prompt_tokens, n_ctx = inner.get("n_prompt_tokens"), inner.get("n_ctx")
    if not isinstance(n_prompt_tokens, int) or not isinstance(n_ctx, int):
        return None
    return {"n_prompt_tokens": n_prompt_tokens, "n_ctx": n_ctx}


def _ollama_overflow_retry_num_ctx(req: CompletionRequest, body: dict, overflow: dict) -> "Optional[int]":
    """The bigger `num_ctx` to retry with, or `None` when no larger
    number is known to be available. Brief: "when the fit allows a
    larger num_ctx (learned cap, host max_ctx, fit estimate, hard cap)
    retry once with the smallest power of two that holds n_prompt_tokens
    plus the max output budget" -- `req.ollama_ctx_retry_ceiling` is
    exactly that `min(...)` of the four named candidates (see this
    module's own `CompletionRequest.ollama_ctx_retry_ceiling` docstring
    for why `trained_context`/the conservative fallback are deliberately
    excluded from it). The output budget is `options.num_predict` on
    THIS request when set (the tightest known bound), else `req.
    profile["max_output_tokens"]` (the model's own configured ceiling, a
    looser but always-available bound)."""
    from halo_harness.providers.ollama_fit import power_of_two_ceil
    ceiling = req.ollama_ctx_retry_ceiling
    if not isinstance(ceiling, int) or ceiling <= 0:
        return None
    output_budget = (body.get("options") or {}).get("num_predict")
    if not isinstance(output_budget, int) or output_budget <= 0:
        # `req.profile` is a plain dict in every REAL agent/loop.py ollama
        # request (`_build_request`'s own `{"context_tokens":...,
        # "max_output_tokens":...}`) -- `getattr` as a defensive fallback
        # for any other caller/test that passes a dataclass instead.
        profile = req.profile
        output_budget = (profile.get("max_output_tokens") if isinstance(profile, dict)
                          else getattr(profile, "max_output_tokens", None)) or 16384
    needed = power_of_two_ceil(overflow["n_prompt_tokens"] + output_budget)
    current = (body.get("options") or {}).get("num_ctx")
    if needed <= ceiling and (not isinstance(current, int) or needed > current):
        return needed
    return None


def _body_with_num_ctx(body: dict, num_ctx: int) -> dict:
    new_body = dict(body)
    new_body["options"] = dict(body.get("options") or {})
    new_body["options"]["num_ctx"] = num_ctx
    return new_body


def _run_phase1_ollama_attempt(req: CompletionRequest, body: dict, abort: "threading.Event | None" = None):
    """Connect + POST `body` (a PARAMETER, not necessarily `req.
    prebuilt_ollama_body` -- the ctx-overflow retry below calls this a
    second time with a bumped `options.num_ctx`) to Ollama's native
    `/api/chat`. Same one-immediate-redial rule as every other phase1
    for a POST-CONNECT failure (never for a genuine connect-phase
    failure, 2.0.1 finding 18). Returns `(body, result, overflow_info)`
    on EITHER a 2xx (`overflow_info=None`) or the specific "prompt
    exceeds num_ctx" 400 (`result=None`, `overflow_info` set) -- the
    caller decides what to do with the overflow; every OTHER non-2xx (or
    a connect failure after its own retry) still raises directly, exactly
    as this function always has."""
    if abort is not None and abort.is_set():
        raise _Aborted()
    if req.creds is None:
        raise ProviderNotConfigured("Ollama host not configured")

    sock_box: list = [None]

    def _register_sock(conn) -> None:
        sock_box[0] = conn.sock

    def _call_upstream():
        return call_ollama_chat(
            base_url=req.creds.base_url, api_key=(req.creds.api_key or None), body=body,
            extra_headers=req.extra_headers, state_dir=req.state_dir, on_connect=_register_sock,
        )

    watcher_done = threading.Event()
    watcher = None
    if abort is not None:
        watcher = threading.Thread(target=_phase1_abort_watcher, args=(abort, watcher_done, sock_box), daemon=True)
        watcher.start()
    try:
        for attempt in range(2):
            if abort is not None and abort.is_set():
                raise _Aborted()
            try:
                result = _call_upstream()
            except UpstreamConnectError as e:
                if abort is not None and abort.is_set():
                    raise _Aborted() from e
                if attempt == 0 and not is_connect_failure_message(str(e)):
                    continue
                status, jbody, hdrs = map_upstream_error(502, {"error": {"message": str(e)}}, req.route.provider)
                raise _upstream_error_from_mapping(status, jbody, hdrs) from e
            if 200 <= result.status < 300:
                return body, result, None
            raw = result.resp.read() if result.resp else b""
            try:
                err_obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            except (json.JSONDecodeError, ValueError):
                err_obj = {"error": {"message": raw.decode("utf-8", "replace")}}
            overflow = _ollama_overflow_info(result.status, err_obj)
            if overflow is not None:
                return body, None, overflow
            status, jbody, hdrs = map_upstream_error(result.status, err_obj, req.route.provider, result.headers)
            raise _upstream_error_from_mapping(status, jbody, hdrs)
        raise UpstreamError(502, "api_error", "upstream failure after retries", True)
    finally:
        watcher_done.set()


def _run_phase1_ollama(req: CompletionRequest, abort: "threading.Event | None" = None):
    """`req.prebuilt_ollama_body` through `_run_phase1_ollama_attempt`,
    with ONE extra retry for the specific "prompt exceeds num_ctx" 400
    (FIX PASS -- round 2's own docstring assumed Ollama "truncates
    silently"; a live run on build 0.34.2 found it answers this 400
    instead): when `_ollama_overflow_retry_num_ctx` finds a bigger
    number actually available, bump `options.num_ctx` and try exactly
    once more; otherwise (or if that retry ALSO overflows) raise the
    SAME `ContextOverflow` this function always raised, unchanged --
    `agent/loop.py`'s existing compaction-and-retry path picks it up
    from there with no changes of its own needed."""
    body, result, overflow = _run_phase1_ollama_attempt(req, req.prebuilt_ollama_body, abort=abort)
    if overflow is None:
        return body, result
    retry_num_ctx = _ollama_overflow_retry_num_ctx(req, body, overflow)
    if retry_num_ctx is not None:
        retry_body = _body_with_num_ctx(body, retry_num_ctx)
        body, result, overflow = _run_phase1_ollama_attempt(req, retry_body, abort=abort)
        if overflow is None:
            return body, result
    raise ContextOverflow(overflow["n_ctx"], overflow["n_prompt_tokens"], overflow["n_prompt_tokens"])


def stream_ollama_completion(req: CompletionRequest, abort: "threading.Event | None" = None) -> Iterator[dict]:
    """Drive one `ol:` completion end to end (native `/api/chat`, NDJSON).
    Same two-phase contract as `stream_completion`/`stream_anthropic_
    completion`: raises before the first yield on a phase-1 failure;
    `req.prebuilt_ollama_body` MUST be set (the caller builds it via
    `providers.ollama_request.build_ollama_request_body` first).

    Round 2 brief: `done_reason: "load"` is treated as "retry once" (the
    2.0.5 brief's own assumption; UNCONFIRMED against a live server --
    research doc Q1, round 6's own live-check). The retry is silent only
    while NOTHING has been shown to the caller yet for this turn (checked
    via `OllamaStreamToAnthropic.any_output_emitted`, which `message_start`
    alone never sets) -- real content/thinking/tool-call output alongside
    a "load" done_reason is treated as an ordinary completion instead,
    never discarded. Capped at exactly one retry per turn."""
    try:
        _body, result = _run_phase1_ollama(req, abort=abort)
    except _Aborted:
        return

    estimate = estimate_tokens(req.prebuilt_ollama_body)
    sm = OllamaStreamToAnthropic(req.model_label, estimate)
    dumped_lines: list = []
    dumped_events: list = []
    start_ev = sm.message_start_event()
    dumped_events.append(start_ev)
    yield start_ev

    retried_once = False
    while True:
        reader_q: "queue.Queue" = queue.Queue()
        reader = threading.Thread(target=_ndjson_reader_thread, args=(result.resp, reader_q))
        reader.daemon = True
        reader.start()
        sock = result.conn.sock if result.conn is not None else None
        terminal_reached = False
        retry_this_attempt = False

        try:
            poll_timeout = min(req.ping_interval, 0.25) if req.ping_interval > 0 else 0.25
            elapsed = 0.0
            while True:
                if abort is not None and abort.is_set():
                    return
                try:
                    item = reader_q.get(timeout=poll_timeout)
                except queue.Empty:
                    elapsed += poll_timeout
                    if elapsed >= req.ping_interval:
                        elapsed = 0.0
                        yield {"type": "ping"}
                    continue
                elapsed = 0.0
                kind, value = item
                if kind == "line":
                    line = value.decode("utf-8", "replace").rstrip("\n")
                    dumped_lines.append(line)
                    step_events = sm.feed_line(line)
                    if sm.done:
                        terminal_reached = True
                        if sm.done_reason == "load" and not sm.any_output_emitted and not retried_once:
                            retry_this_attempt = True  # discard step_events (just the trivial finalize)
                        else:
                            for ev in step_events:
                                dumped_events.append(ev)
                                yield ev
                        break
                    for ev in step_events:
                        dumped_events.append(ev)
                        yield ev
                elif kind == "eof":
                    for ev in sm.on_eof():
                        dumped_events.append(ev)
                        yield ev
                    terminal_reached = True
                    break
                else:  # "exc"
                    ev = sm.error_event(f"upstream connection error: {value}")
                    dumped_events.append(ev)
                    yield ev
                    terminal_reached = True
                    break
        finally:
            if not terminal_reached and sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn = getattr(result, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if not retry_this_attempt:
            break
        retried_once = True
        sm = OllamaStreamToAnthropic(req.model_label, estimate, msg_id=sm.msg_id)
        try:
            _body, result = _run_phase1_ollama(req, abort=abort)
        except _Aborted:
            break
        except (UpstreamError, ProviderNotConfigured) as e:
            ev = sm.error_event(str(e))
            dumped_events.append(ev)
            yield ev
            break

    dump_debug(req.state_dir, "upstream-stream", {"lines": dumped_lines})
    dump_debug(req.state_dir, "emitted-events", {"events": dumped_events})
