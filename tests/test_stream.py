"""tests.test_stream -- providers/stream.py's stream_completion against the
mock upstream: text, ping (BRIDGE_PING_INTERVAL-style via req.ping_interval),
abort, overflow raise + fixable retry, JSON-not-SSE, mid-stream error event.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, assemble_message, events_from_stream_completion, text_of, validate_anthropic_stream
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import CompletionRequest, ContextOverflow, ProviderCreds, ProviderNotConfigured, UpstreamError, stream_completion

test, TESTS = new_registry()


def _req(mock: MockUpstream, scenario: str, *, ping_interval: float = 15.0, max_tokens: int = 100) -> CompletionRequest:
    model = f"mock/{scenario}"
    return CompletionRequest(
        body={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": "hi"}]},
        route=Route(provider="openrouter", upstream_model=model, dialect="openai-chat"),
        profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=mock.base_url, api_key="test-key"),
        state_dir=Path(tempfile.mkdtemp(prefix="test-stream-state-")),
        extra_headers={}, model_label=model, ping_interval=ping_interval,
    )


@test
def test_basic_text_reply(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        wrapped = events_from_stream_completion(stream_completion(_req(mock, "model")))
        validate_anthropic_stream(wrapped)
        msg = assemble_message(wrapped)
        ctx.check(f"text == 'pong', got {text_of(msg)!r}", text_of(msg) == "pong")
        ctx.check(f"stop_reason == end_turn, got {msg['stop_reason']!r}", msg["stop_reason"] == "end_turn")
    finally:
        mock.stop()


@test
def test_ping_during_silence(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        events = list(stream_completion(_req(mock, "slow", ping_interval=0.2)))
        pings = [e for e in events if e.get("type") == "ping"]
        ctx.check(f"at least one ping during the 2s silence, got {len(pings)}", len(pings) >= 1)
        non_ping = [e for e in events if e.get("type") != "ping"]
        wrapped = events_from_stream_completion(iter(non_ping))
        validate_anthropic_stream(wrapped)
        msg = assemble_message(wrapped)
        ctx.check(f"text == 'finally', got {text_of(msg)!r}", text_of(msg) == "finally")
    finally:
        mock.stop()


@test
def test_abort_stops_generator_quickly(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        abort = threading.Event()
        gen = stream_completion(_req(mock, "long-abort", ping_interval=15.0), abort=abort)
        first = next(gen)
        ctx.check("first event is message_start", first.get("type") == "message_start")
        next(gen)  # at least one real content event has arrived
        abort.set()
        t0 = time.monotonic()
        remaining = list(gen)
        dt = time.monotonic() - t0
        ctx.check(f"generator finishes within 2s of abort.set(), took {dt:.2f}s", dt < 2.0)
    finally:
        mock.stop()


@test
def test_context_overflow_unfixable_raises(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        gen = stream_completion(_req(mock, "context-overflow-openrouter"))
        try:
            next(gen)
            ctx.check("expected ContextOverflow to be raised", False)
        except ContextOverflow as e:
            ctx.check(f"limit parsed, got {e.limit}", e.limit == 65536)
            ctx.check(f"prompt_tokens parsed (exceeds limit), got {e.prompt_tokens}", e.prompt_tokens == 68000)
    finally:
        mock.stop()


@test
def test_context_overflow_fixable_retries_and_succeeds(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        wrapped = events_from_stream_completion(stream_completion(_req(mock, "context-overflow-openrouter-fixable", max_tokens=20000)))
        validate_anthropic_stream(wrapped)
        msg = assemble_message(wrapped)
        ctx.check(f"fixable overflow retried transparently, got {text_of(msg)!r}",
                  text_of(msg) == "openrouter retry succeeded")
    finally:
        mock.stop()


@test
def test_json_not_sse_fallback(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        wrapped = events_from_stream_completion(stream_completion(_req(mock, "json-not-sse")))
        validate_anthropic_stream(wrapped)
        msg = assemble_message(wrapped)
        ctx.check(f"text from the buffered JSON reply, got {text_of(msg)!r}", text_of(msg) == "non-stream reply")
    finally:
        mock.stop()


@test
def test_mid_stream_error_event_no_message_stop(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        events = list(stream_completion(_req(mock, "error-mid-stream")))
        kinds = [e.get("type") for e in events]
        ctx.check("an error event was emitted", "error" in kinds)
        ctx.check("no message_stop after a mid-stream error", "message_stop" not in kinds)
    finally:
        mock.stop()


@test
def test_finding_12_socket_shut_after_mid_stream_error(ctx: Ctx):
    """finding 12: `terminal_reached` is set true ONLY for done/eof/exc --
    a mid-stream `{"error":...}` chunk must still force-shut the socket in
    `finally` (verified pre-fix: the mock kept streaming 30 more chunks
    over ~3s with the reader thread still alive)."""
    mock = MockUpstream().start()
    try:
        t0 = time.monotonic()
        events = list(stream_completion(_req(mock, "error-then-keeps-streaming", ping_interval=15.0)))
        dt = time.monotonic() - t0
        kinds = [e.get("type") for e in events]
        ctx.check("an error event was emitted", "error" in kinds)
        ctx.check(f"returned promptly, not after the mock's ~3s of post-error chunks, got dt={dt:.2f}s", dt < 1.5)
        deadline = time.monotonic() + 5.0
        while not mock.disconnect_events and time.monotonic() < deadline:
            time.sleep(0.1)
        ctx.check(f"the mock's own write attempt saw the disconnect (the socket was force-shut), "
                   f"got {mock.disconnect_events}", len(mock.disconnect_events) >= 1)
    finally:
        mock.stop()


@test
def test_unknown_scenario_maps_to_upstream_error(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        gen = stream_completion(_req(mock, "this-scenario-does-not-exist"))
        try:
            next(gen)
            ctx.check("expected UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"404 mapped through, got status={e.status}", e.status == 404)
    finally:
        mock.stop()


@test
def test_provider_not_configured(ctx: Ctx):
    req = CompletionRequest(
        body={"model": "mock/model", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        route=Route(provider="openrouter", upstream_model="mock/model", dialect="openai-chat"),
        profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=None, state_dir=Path(tempfile.mkdtemp(prefix="test-stream-notconfigured-")),
        extra_headers={}, model_label="mock/model",
    )
    gen = stream_completion(req)
    try:
        next(gen)
        ctx.check("expected ProviderNotConfigured", False)
    except ProviderNotConfigured:
        ctx.check("ProviderNotConfigured raised when creds is None", True)


@test
def test_tool_call_round_trip(ctx: Ctx):
    """Not exercised by the H0 agent loop (no tools yet), but stream_completion
    itself must still translate a tool-call-shaped upstream reply correctly
    (it's dialect-translation, independent of who's listening)."""
    mock = MockUpstream().start()
    try:
        wrapped = events_from_stream_completion(stream_completion(_req(mock, "two-calls")))
        validate_anthropic_stream(wrapped)
        msg = assemble_message(wrapped)
        calls = [b for b in msg["content"] if b.get("type") == "tool_use"]
        ctx.check(f"two tool_use blocks, got {len(calls)}", len(calls) == 2)
        ctx.check("stop_reason == tool_use", msg["stop_reason"] == "tool_use")
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
