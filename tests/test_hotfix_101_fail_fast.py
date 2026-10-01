"""tests.test_hotfix_101_fail_fast -- 1.0.1 hotfix 2: DNS/connection
failures must fail fast (bounded connect, no backoff-ladder retries beyond
phase 1's own single immediate one) and name the host in a clear message.
Every case is time-bounded (< 10s) and never touches a real network beyond
an intentionally-unresolvable RFC 2606 `.invalid` hostname.
"""
from __future__ import annotations

import http.client
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

# RFC 2606 reserves .invalid as a TLD that will NEVER resolve -- a real,
# deterministic "unresolvable host" without depending on any live DNS
# blackhole/network condition.
_UNRESOLVABLE_HOST = "totally-unresolvable-host.invalid"
_UNRESOLVABLE_DBX_URL = f"https://{_UNRESOLVABLE_HOST}"


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY")}
        d = Path(tempfile.mkdtemp(prefix="hotfix101-failfast-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".rolo-claude")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        self.state_dir = d / ".rolo-claude"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# format_connect_error / UpstreamConnectError.host
# ---------------------------------------------------------------------------

@test
def test_format_connect_error_names_host_and_says_the_required_phrase(ctx: Ctx):
    from rolo_claude.providers.http import format_connect_error
    msg = format_connect_error("your-workspace.cloud.databricks.com", "gaierror: Name or service not known")
    ctx.check(f"names the host, got {msg!r}", "your-workspace.cloud.databricks.com" in msg)
    ctx.check(f"says cannot resolve/reach, got {msg!r}", "cannot resolve/reach" in msg)
    ctx.check(f"says check the machine's network, DNS or VPN, got {msg!r}",
              "check the machine's network, DNS or VPN" in msg)


@test
def test_upstream_connect_error_carries_host(ctx: Ctx):
    from rolo_claude.providers.http import UpstreamConnectError
    e = UpstreamConnectError("boom", host="example.invalid")
    ctx.check(f"host attribute set, got {e.host!r}", e.host == "example.invalid")


# ---------------------------------------------------------------------------
# _bounded_connect: the actual wall-clock cap, independent of real DNS
# behavior on the test box -- a hung `conn.connect()` (simulating a
# black-holed/unresponsive resolver, which a per-socket `timeout=` does NOT
# bound since getaddrinfo runs before any socket exists) must still be
# capped at `timeout_s`, not the fake call's own (much longer) sleep.
# ---------------------------------------------------------------------------

class _HangingConn:
    def __init__(self, hang_s: float):
        self.hang_s = hang_s
        self.connected = threading.Event()

    def connect(self):
        time.sleep(self.hang_s)
        self.connected.set()  # only reached if NOT abandoned in time


@test
def test_bounded_connect_caps_a_hung_connect_call(ctx: Ctx):
    from rolo_claude.providers.http import UpstreamConnectError, _bounded_connect
    conn = _HangingConn(hang_s=5.0)
    t0 = time.monotonic()
    try:
        _bounded_connect(conn, 0.3, "example.invalid")
        ctx.check("expected UpstreamConnectError on timeout", False)
    except UpstreamConnectError as e:
        elapsed = time.monotonic() - t0
        ctx.check(f"capped near the 0.3s budget, not the 5s hang, got {elapsed:.2f}s", elapsed < 1.5)
        ctx.check(f"names the host even on a bare timeout, got {e}", "example.invalid" in str(e))
        ctx.check(f"still says the required phrase, got {e}",
                  "cannot resolve/reach" in str(e) and "DNS or VPN" in str(e))
    ctx.check("the abandoned background thread was never force-killed (still running)",
              not conn.connected.is_set())


@test
def test_bounded_connect_propagates_a_fast_exception(ctx: Ctx):
    from rolo_claude.providers.http import UpstreamConnectError, _bounded_connect

    class _RefusingConn:
        def connect(self):
            raise ConnectionRefusedError("[simulated] connection refused")

    t0 = time.monotonic()
    try:
        _bounded_connect(_RefusingConn(), 8.0, "example.invalid")
        ctx.check("expected UpstreamConnectError", False)
    except UpstreamConnectError as e:
        elapsed = time.monotonic() - t0
        ctx.check(f"a fast failure returns immediately, not after the 8s budget, got {elapsed:.2f}s", elapsed < 1.0)
        ctx.check(f"wraps the real cause, got {e}", "refused" in str(e).lower())


# ---------------------------------------------------------------------------
# open_upstream: a real unresolvable hostname fails fast (bounded), never the
# ~64s an unbounded getaddrinfo hang would take.
# ---------------------------------------------------------------------------

@test
def test_open_upstream_unresolvable_host_fails_in_under_10s(ctx: Ctx):
    from rolo_claude.providers.http import UpstreamConnectError, open_upstream
    t0 = time.monotonic()
    try:
        open_upstream(_UNRESOLVABLE_HOST, 443, True, connect_timeout=8)
        ctx.check("expected UpstreamConnectError for an unresolvable host", False)
    except UpstreamConnectError as e:
        elapsed = time.monotonic() - t0
        ctx.check(f"failed in well under 10s, got {elapsed:.1f}s", elapsed < 10.0)
        ctx.check(f"names the host, got {e}", _UNRESOLVABLE_HOST in str(e))
        ctx.check(f"says the required phrase, got {e}",
                  "cannot resolve/reach" in str(e) and "DNS or VPN" in str(e))


@test
def test_probe_databricks_status_unresolvable_host_fails_in_under_10s(ctx: Ctx):
    from rolo_claude.providers.databricks import probe_databricks_status
    from rolo_claude.providers.http import UpstreamConnectError
    t0 = time.monotonic()
    try:
        probe_databricks_status(_UNRESOLVABLE_DBX_URL, "tok")
        ctx.check("expected UpstreamConnectError", False)
    except UpstreamConnectError as e:
        elapsed = time.monotonic() - t0
        ctx.check(f"doctor/init/catalog-refresh probe fails in well under 10s, got {elapsed:.1f}s", elapsed < 10.0)
        ctx.check(f"names the host, got {e}", _UNRESOLVABLE_HOST in str(e))


@test
def test_doctor_work_vpn_reachability_unresolvable_host_fails_fast_and_names_host(ctx: Ctx):
    from rolo_claude.doctor import _work_check_vpn_reachability
    t0 = time.monotonic()
    line = _work_check_vpn_reachability(_UNRESOLVABLE_DBX_URL)
    elapsed = time.monotonic() - t0
    ctx.check(f"doctor --work VPN check returns in well under 10s, got {elapsed:.1f}s", elapsed < 10.0)
    ctx.check(f"MISSING and names the host, got {line!r}", "MISSING" in line and _UNRESOLVABLE_HOST in line)
    ctx.check(f"says the required phrase, got {line!r}",
              "cannot resolve/reach" in line and "DNS or VPN" in line)


# ---------------------------------------------------------------------------
# is_connect_failure_message: the marker `agent/loop.py`'s own `_step`
# checks to skip the backoff ladder WITHOUT changing either wire mapper's
# status/err_type (`databricks_unreachable_response`/`map_upstream_error`
# both stay byte-for-byte unchanged for bridge.py's sake -- see
# test_bridge.py::test_dbx_dns_failure_502_vpn_hint).
# ---------------------------------------------------------------------------

@test
def test_is_connect_failure_message_survives_both_wire_mappers(ctx: Ctx):
    from rolo_claude.providers.databricks import databricks_unreachable_response
    from rolo_claude.providers.errors import map_upstream_error
    from rolo_claude.providers.http import format_connect_error, is_connect_failure_message

    raw = format_connect_error("your-workspace.cloud.databricks.com", "boom")
    ctx.check(f"the raw canonical message is recognized, got {raw!r}", is_connect_failure_message(raw))

    _status, jbody_dbx, _hdrs = databricks_unreachable_response(raw)
    ctx.check(f"still recognized after databricks_unreachable_response's VPN-hint wrap, got "
              f"{jbody_dbx['error']['message']!r}", is_connect_failure_message(jbody_dbx["error"]["message"]))

    _status2, jbody_or, _hdrs2 = map_upstream_error(502, {"error": {"message": raw}}, "openrouter")
    ctx.check(f"still recognized after map_upstream_error's generic 502 wrap, got "
              f"{jbody_or['error']['message']!r}", is_connect_failure_message(jbody_or["error"]["message"]))

    ctx.check("an ordinary upstream 500 message is NOT mistaken for a connect failure",
              not is_connect_failure_message("internal server error, please retry"))


# ---------------------------------------------------------------------------
# Phase 1 (providers/stream.py): exactly ONE immediate retry, then terminal --
# never the 1-2-4-8-16s ladder -- for BOTH the openai-chat and the
# anthropic-passthrough dialect's own phase1.
# ---------------------------------------------------------------------------

@test
def test_openai_chat_dialect_connect_failure_is_terminal_after_one_retry_no_ladder(ctx: Ctx):
    import rolo_claude.providers.http as http_mod
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_completion

    real_open_upstream = http_mod.open_upstream
    call_count = [0]

    def flaky(host, port, tls, connect_timeout=8, on_connect=None):
        call_count[0] += 1
        raise ConnectionRefusedError("[simulated] connection refused")

    http_mod.open_upstream = flaky
    try:
        model = "mock/model"
        req = CompletionRequest(
            body={"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            route=Route(provider="openrouter", upstream_model=model, dialect="openai-chat"),
            profile={"context_tokens": 128000, "max_output_tokens": 16384},
            creds=ProviderCreds(base_url="https://example.invalid", api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-p1-")), extra_headers={}, model_label=model,
            ping_interval=15.0,
        )
        t0 = time.monotonic()
        try:
            list(stream_completion(req))
            ctx.check("expected UpstreamError", False)
        except UpstreamError as e:
            from rolo_claude.providers.http import is_connect_failure_message
            elapsed = time.monotonic() - t0
            ctx.check(f"exactly 2 connect attempts (1 immediate retry, never a ladder), got {call_count[0]}",
                      call_count[0] == 2)
            ctx.check(f"no backoff sleep at all -- near-instant, got {elapsed:.2f}s", elapsed < 1.0)
            ctx.check(f"the resulting UpstreamError's message is recognized as a connect failure "
                      f"(what agent/loop.py's _step keys off to skip its own ladder), got {e.message!r}",
                      is_connect_failure_message(e.message))
    finally:
        http_mod.open_upstream = real_open_upstream


@test
def test_step_never_ladder_retries_a_connect_failure(ctx: Ctx):
    """The outer `_step` retry ladder (agent/loop.py) must see the SAME
    terminal result phase 1 already produced -- never re-invoke `_stream`
    (and therefore never re-attempt the connect) additional times on a
    1-2-4-8-16s timer just because the failure mapped to a 502."""
    import rolo_claude.providers.http as http_mod
    from tests.helpers.mock_openai import MockUpstream

    real_open_upstream = http_mod.open_upstream
    call_count = [0]

    def flaky(host, port, tls, connect_timeout=8, on_connect=None):
        call_count[0] += 1
        raise ConnectionRefusedError("[simulated] connection refused")

    http_mod.open_upstream = flaky
    try:
        from rolo_claude import headless
        with _Env() as env, tempfile.TemporaryDirectory() as cwd:
            os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
            build = headless.build_session(cwd=Path(cwd), model_ref_raw="or:mock/model", bare=True,
                                            print_mode=True, max_turns=3)
            session = build.session
            t0 = time.monotonic()
            events_seen = list(session.turn("hello"))
            elapsed = time.monotonic() - t0
            kinds = [e.kind for e in events_seen]
            ctx.check(f"turn ends in an error event, got kinds={kinds}", "error" in kinds)
            ctx.check(f"exactly 2 connect attempts total for the WHOLE turn (no outer ladder re-invoking "
                      f"_stream), got {call_count[0]}", call_count[0] == 2)
            ctx.check(f"no backoff delay anywhere -- the whole turn resolves in well under 2s, got {elapsed:.2f}s",
                      elapsed < 2.0)
    finally:
        http_mod.open_upstream = real_open_upstream


# ---------------------------------------------------------------------------
# 1.0.1 fixpass finding 2: a failure AFTER the connection was already open
# (conn.request()/getresponse() raised, never open_upstream()/_bounded_
# connect itself) must NOT carry CONNECT_FAILURE_MARKER -- it has to ride
# the ordinary retryable-502 ladder (MAX_RETRIES, backoff), exactly like
# before this hotfix, instead of agent/loop.py's _step treating a dropped
# keep-alive/mid-response RST as a fail-fast DNS/VPN problem.
# ---------------------------------------------------------------------------

class _DropsOnRequest:
    """A fake `conn` `open_upstream()` hands back once connected -- simulates
    a keep-alive the peer already dropped/reset by the time `request()` is
    called, WITHOUT any real socket (never touches the network)."""

    def request(self, *a, **kw):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    def getresponse(self):  # pragma: no cover -- request() always raises first
        raise AssertionError("getresponse() should never be reached")


@test
def test_post_connect_failure_message_has_no_connect_marker(ctx: Ctx):
    """call_openai_chat / _dbx_post / proxy_anthropic: open_upstream()
    succeeds (faked, no real socket) but conn.request() raises -- the
    UpstreamConnectError this raises must be marker-free (format_post_
    connect_error, never format_connect_error) so is_connect_failure_
    message is False and agent/loop.py's _step does not fail-fast it."""
    import rolo_claude.providers.http as http_mod
    from rolo_claude.providers.http import UpstreamConnectError, is_connect_failure_message

    real_open_upstream = http_mod.open_upstream
    http_mod.open_upstream = lambda *a, **kw: _DropsOnRequest()
    try:
        cases = (
            ("call_openai_chat", http_mod.call_openai_chat,
             dict(base_url="https://example.invalid", api_key="k", body={}, extra_headers={},
                  state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-postconnect-")))),
            ("_dbx_post", http_mod._dbx_post,
             dict(base_url="https://example.invalid", path="/x", api_key="k", req_body={}, extra_headers={},
                  state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-postconnect-")))),
            ("proxy_anthropic", http_mod.proxy_anthropic,
             dict(base_url="https://example.invalid", api_key="k", body={}, extra_headers={},
                  state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-postconnect-")))),
        )
        for label, fn, kwargs in cases:
            try:
                fn(**kwargs)
                ctx.check(f"{label}: expected UpstreamConnectError", False)
            except UpstreamConnectError as e:
                ctx.check(f"{label}: message has NO connect-failure marker, got {e}",
                          not is_connect_failure_message(str(e)))
                ctx.check(f"{label}: still describes the interrupted connection, got {e}",
                          "interrupted" in str(e))
    finally:
        http_mod.open_upstream = real_open_upstream


@test
def test_step_retries_a_post_connect_failure_through_the_normal_ladder(ctx: Ctx):
    """The mirror image of test_step_never_ladder_retries_a_connect_failure:
    once open_upstream() has already succeeded, a failure from conn.
    request()/getresponse() must ride agent/loop.py's ordinary retryable-502
    ladder (recover on a later attempt) rather than dying after phase 1's
    own single immediate retry -- the exact scenario review finding 2
    reports ("a load balancer drops a keep-alive connection while Databricks
    queues the request")."""
    import rolo_claude.providers.stream as stream_mod
    from rolo_claude.providers.http import UpstreamConnectError, format_post_connect_error
    from tests.helpers.mock_openai import MockUpstream

    real_call_openai_chat = stream_mod.call_openai_chat
    call_count = [0]

    def flaky_then_ok(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:  # phase 1's own attempt0 + attempt1 (its one immediate retry)
            raise UpstreamConnectError(
                format_post_connect_error("mock-host", "Remote end closed connection without response"),
                host="mock-host")
        return real_call_openai_chat(*args, **kwargs)

    stream_mod.call_openai_chat = flaky_then_ok
    mock = MockUpstream().start()
    try:
        from rolo_claude import headless
        with _Env(), tempfile.TemporaryDirectory() as cwd:
            os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
            build = headless.build_session(cwd=Path(cwd), model_ref_raw="or:mock/model", bare=True,
                                            print_mode=True, max_turns=3)
            session = build.session
            t0 = time.monotonic()
            events_seen = list(session.turn("hello"))
            elapsed = time.monotonic() - t0
            kinds = [e.kind for e in events_seen]
            ctx.check(f"phase 1 exhausted its own 2 attempts, THEN the outer ladder retried once more "
                      f"(a 3rd call succeeding), got call_count={call_count[0]}", call_count[0] == 3)
            ctx.check(f"no error event -- the turn recovered via the retry ladder, got kinds={kinds}",
                      "error" not in kinds)
            ctx.check(f"a real backoff sleep happened (the ladder, not phase 1's instant retry), "
                      f"got elapsed={elapsed:.2f}s", elapsed >= 1.0)
    finally:
        stream_mod.call_openai_chat = real_call_openai_chat
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
