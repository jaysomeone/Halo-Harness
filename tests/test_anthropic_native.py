"""tests.test_anthropic_native -- H5 scope C end to end: providers/request.py's
`build_anthropic_request_body`/`apply_anthropic_cache_control`/
`map_effort_anthropic`, providers/http.py's `call_anthropic_native` (both
`ant:` and Databricks Claude-passthrough paths), and
providers/stream.py's `stream_anthropic_completion` against
tests/helpers/mock_anthropic.py's native SSE shapes (thinking + signature,
tool_use via input_json_delta, ping, mid-stream error, 429 with
retry-after, overflow 400 in Anthropic wording).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_anthropic import MockAnthropic
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
ensure_default_provider_credentials()
from halo_harness.providers.errors import parse_context_overflow
from halo_harness.providers.http import call_anthropic_native
from halo_harness.providers.profiles import ProviderProfile
from halo_harness.providers.request import (
    apply_anthropic_cache_control, build_anthropic_request_body, map_tool_choice_anthropic,
)
from halo_harness.providers.routing import Route
from halo_harness.providers.stream import CompletionRequest, ContextOverflow, ProviderCreds, stream_anthropic_completion

test, TESTS = new_registry()


def _route(model="claude-sonnet-4.5-ok", provider="anthropic"):
    return Route(provider=provider, upstream_model=model, dialect="anthropic-passthrough")


def _profile():
    return ProviderProfile(family="claude", thinking_format="anthropic_thinking", reasoning_replay="thinking",
                            reasoning_effort_supported=True, max_tokens_default=8192)


def _req(mock, model, *, provider="anthropic", body=None):
    route = _route(model, provider)
    b = body if body is not None else build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=None, route=route, profile=_profile(),
    )
    return CompletionRequest(
        body={}, route=route, profile={}, creds=ProviderCreds(base_url=mock.base_url, api_key="test-key"),
        state_dir=Path(tempfile.mkdtemp(prefix="ant-test-")), extra_headers={}, model_label=model,
        prebuilt_anthropic_body=b, ping_interval=5.0,
    )


@test
def test_ok_scenario_pong_text(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        req = _req(mock, "claude-sonnet-4.5-ok")
        events = list(stream_anthropic_completion(req))
        texts = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"pong received, got {texts}", "".join(texts) == "pong")
        ctx.check("message_stop terminal event present", any(e.get("type") == "message_stop" for e in events))
        ctx.check("request reached the mock at /v1/messages", mock.requests[-1]["path"] == "/v1/messages")
        ctx.check("x-api-key header sent", mock.requests[-1]["headers"].get("x-api-key") == "test-key")
        ctx.check("anthropic-version header sent", "anthropic-version" in mock.requests[-1]["headers"])
        ctx.check("no Databricks ?beta=true on a direct ant: call", "beta" not in mock.requests[-1]["path"])
    finally:
        mock.stop()


@test
def test_thinking_and_signature_events_pass_through(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        req = _req(mock, "claude-opus-thinking-and-signature")
        events = list(stream_anthropic_completion(req))
        thinking_deltas = [e for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "thinking_delta"]
        sig_deltas = [e for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "signature_delta"]
        ctx.check(f"a thinking_delta event arrived, got {events}", len(thinking_deltas) == 1)
        ctx.check("its text is the mock's thinking text", thinking_deltas[0]["delta"]["thinking"] == "let me consider this")
        ctx.check("a signature_delta event arrived", len(sig_deltas) == 1)
        ctx.check("signature value round-trips exactly", sig_deltas[0]["delta"]["signature"] == "sig_abc123")
        usage_events = [e for e in events if e.get("type") == "message_delta" and isinstance(e.get("usage"), dict)]
        ctx.check("usage carries cache fields", usage_events and usage_events[0]["usage"].get("cache_read_input_tokens") == 200)
        ctx.check("usage carries cache_creation too", usage_events[0]["usage"].get("cache_creation_input_tokens") == 50)
    finally:
        mock.stop()


@test
def test_tool_use_input_json_delta_pieces(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        req = _req(mock, "claude-sonnet-tool-use")
        events = list(stream_anthropic_completion(req))
        starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
        deltas = [e for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "input_json_delta"]
        ctx.check("a tool_use content_block_start arrived", len(starts) == 1)
        ctx.check("tool name is Read", starts[0]["content_block"]["name"] == "Read")
        ctx.check(f"input_json_delta pieces arrived, got {len(deltas)}", len(deltas) == 2)
        joined = "".join(d["delta"]["partial_json"] for d in deltas)
        ctx.check(f"pieces concatenate to valid JSON, got {joined!r}", joined == '{"file_path":"/x.py"}')
    finally:
        mock.stop()


@test
def test_ping_event_passes_through(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        req = _req(mock, "claude-sonnet-ping")
        events = list(stream_anthropic_completion(req))
        ctx.check("a real ping EVENT from the wire passed through", any(e.get("type") == "ping" for e in events))
    finally:
        mock.stop()


@test
def test_mid_stream_error_event(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        req = _req(mock, "claude-sonnet-mid-stream-error")
        events = list(stream_anthropic_completion(req))
        partial = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        errors = [e for e in events if e.get("type") == "error"]
        ctx.check("partial text before the error survived", "".join(partial) == "partial")
        ctx.check(f"the error event itself passed through, got {events}", len(errors) == 1)
        ctx.check("error type preserved", errors[0]["error"]["type"] == "overloaded_error")
    finally:
        mock.stop()


@test
def test_429_retry_after_and_ratelimit_headers(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                             tools=None, route=_route("claude-sonnet-rate-limit-429"), profile=_profile())
        result = call_anthropic_native(mock.base_url, "k", body, {"x-api-key": "k", "anthropic-version": "2023-06-01"},
                                        Path(tempfile.mkdtemp(prefix="ant-429-")), route_provider="anthropic")
        ctx.check(f"429 status, got {result.status}", result.status == 429)
        ctx.check("retry-after header present", result.headers.get("retry-after") == "3")
        ctx.check("anthropic-ratelimit-requests-remaining header present",
                  result.headers.get("anthropic-ratelimit-requests-remaining") == "0")
    finally:
        mock.stop()


@test
def test_overflow_400_anthropic_wording_raises_context_overflow(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        req = _req(mock, "claude-sonnet-overflow-400")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("overflow must raise ContextOverflow, not silently stream", False)
        except ContextOverflow as e:
            ctx.check(f"limit parsed correctly, got {e.limit}", e.limit == 200000)
            ctx.check(f"prompt_tokens parsed correctly, got {e.prompt_tokens}", e.prompt_tokens == 210000)
    finally:
        mock.stop()


@test
def test_parse_context_overflow_anthropic_wording_directly(ctx: Ctx):
    info = parse_context_overflow(400, "prompt is too long: 210000 tokens > 200000 maximum", None)
    ctx.check("overflow parsed", info is not None)
    ctx.check("limit", info.limit == 200000)
    ctx.check("prompt_tokens", info.prompt_tokens == 210000)
    ctx.check("never silently clamp-retried (fixable=False)", info.fixable is False)


@test
def test_databricks_route_tries_ai_gateway_path_first(ctx: Ctx):
    mock = MockAnthropic().start()
    try:
        body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                             tools=None, route=_route("claude-sonnet-ok", provider="databricks"), profile=_profile())
        # finding 16/17 (h4-h5-h3c review): `extra_headers` here is EXACTLY
        # what `headless.py.build_session` really produces for a databricks
        # route -- `{"x-databricks-use-coding-agent-mode": "true"}`, NO
        # Authorization header (the pre-fix version of this test added one
        # by hand, which meant it kept passing even though
        # `call_anthropic_native` itself never added it and every real
        # Databricks Claude passthrough call 401/403'd).
        result = call_anthropic_native(mock.base_url, "k", body, {"x-databricks-use-coding-agent-mode": "true"},
                                        Path(tempfile.mkdtemp(prefix="ant-dbx-")), route_provider="databricks")
        ctx.check(f"200 status, got {result.status}", result.status == 200)
        ctx.check(f"hit the ai-gateway anthropic path, got {mock.requests[-1]['path']}",
                  mock.requests[-1]["path"].startswith("/ai-gateway/anthropic/v1/messages"))
        ctx.check("Databricks gateway ?beta=true flag present", "beta=true" in mock.requests[-1]["path"])
        ctx.check("coding-agent-mode header forwarded", mock.requests[-1]["headers"].get("x-databricks-use-coding-agent-mode") == "true")
        ctx.check(f"h5b finding 16: Authorization: Bearer <api_key> was added by call_anthropic_native ITSELF, "
                  f"got {mock.requests[-1]['headers'].get('authorization')!r}",
                  mock.requests[-1]["headers"].get("authorization") == "Bearer k")
    finally:
        mock.stop()


@test
def test_h5b_f16_databricks_bearer_header_survives_the_ai_gateway_404_fallback(ctx: Ctx):
    """The Authorization header must be present on BOTH attempts -- the
    ai-gateway path (which may 404 on some workspaces) and the by-name
    invocations fallback -- not just the first one tried."""
    mock = MockAnthropic().start()
    try:
        # Force the ai-gateway path to 404 so the fallback path actually runs.
        mock.force_404_paths = {"/ai-gateway/anthropic/v1/messages"}
        body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                             tools=None, route=_route("claude-sonnet-ok", provider="databricks"), profile=_profile())
        result = call_anthropic_native(mock.base_url, "k", body, {"x-databricks-use-coding-agent-mode": "true"},
                                        Path(tempfile.mkdtemp(prefix="ant-dbx-fallback-")), route_provider="databricks")
        ctx.check(f"eventually 200, got {result.status}", result.status == 200)
        # V2b fix: the fallback is the endpoint's OWN by-name invocations
        # path, never the literal (non-existent) "anthropic" endpoint name.
        ctx.check(f"fell back to the by-name invocations path, got {mock.requests[-1]['path']}",
                  mock.requests[-1]["path"].startswith("/serving-endpoints/claude-sonnet-ok/invocations"))
        ctx.check("Authorization header present on the FALLBACK attempt too",
                  mock.requests[-1]["headers"].get("authorization") == "Bearer k")
        # And also on the FIRST (404'd) attempt.
        ai_gateway_reqs = [r for r in mock.requests if r["path"].startswith("/ai-gateway/anthropic/v1/messages")]
        ctx.check(f"at least one ai-gateway attempt recorded, got {len(ai_gateway_reqs)}", len(ai_gateway_reqs) >= 1)
        ctx.check("Authorization header present on the FIRST (404) attempt too",
                  all(r["headers"].get("authorization") == "Bearer k" for r in ai_gateway_reqs))
    finally:
        mock.stop()


@test
def test_v2b_anthropic_fallback_uses_real_endpoint_name_not_literal_anthropic(ctx: Ctx):
    """V2b fix (flagged during V2a): the 404 fallback must be built from
    THIS request's own real endpoint name (`body["model"]`), not a
    hardcoded literal "anthropic" segment -- proven with a name that looks
    nothing like the old literal, so a regression back to the hardcoded
    string would fail this immediately."""
    mock = MockAnthropic().start()
    try:
        mock.force_404_paths = {"/ai-gateway/anthropic/v1/messages"}
        body = build_anthropic_request_body(
            system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            tools=None, route=_route("databricks-claude-opus-4-6", provider="databricks"), profile=_profile(),
        )
        result = call_anthropic_native(mock.base_url, "k", body, {}, Path(tempfile.mkdtemp(prefix="ant-dbx-name-")),
                                        route_provider="databricks")
        ctx.check(f"200, got {result.status}", result.status == 200)
        ctx.check(f"fallback path names the real endpoint, got {mock.requests[-1]['path']}",
                  mock.requests[-1]["path"].startswith("/serving-endpoints/databricks-claude-opus-4-6/invocations"))
        ctx.check("no beta=true query flag on the plain invocations fallback",
                  "beta=true" not in mock.requests[-1]["path"])
    finally:
        mock.stop()


@test
def test_h5b_f17_thinking_tool_turn_through_real_session_merges_message_start_usage(ctx: Ctx):
    """finding 17 (test-quality): every OTHER item on finding 17's own
    8-case list is already covered by its matching finding's pinning test
    (Edit near-duplicates -> f02, compaction trigger -> f01, steer during
    dispatch -> f03, steer after checkpoint -> f05, stdin held open ->
    f10, WebSearch in the first meta node -> f07, env-file PATH export ->
    f06) -- this is the ONE case with no coverage anywhere else: "a
    two-request ant: thinking+tool turn through Session with message_start
    usage". Drives a REAL `agent.loop.Session` (not `call_anthropic_native`
    called directly, which is all the rest of this file does) through a
    full thinking + tool_use + tool_result + final-reply round trip against
    `MockAnthropic`, proving -- end to end, not just at the
    `prepare_anthropic_messages` unit level the other f15 tests use -- that
    (a) message_start's usage really does reach the logged `usage` node
    (finding 15 point 3), and (b) the SECOND real wire request really does
    replay the first turn's thinking block with the wire's own `thinking`
    key and its signature intact (finding 15 point 1), not just when
    `prepare_anthropic_messages` is called by hand."""
    import os

    from tests.helpers.fake_home import build_fake_home
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref

    fh = build_fake_home()
    # H10b: never set before -- the real in-process Session below fell
    # through to the REAL `~/.halo/sessions`, leaking
    # `ant:claude-sonnet-4.5-thinking-then-tool-then-reply` sessions into
    # rolo's real session history (H10b report).
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockAnthropic().start()
    try:
        model = "ant:claude-sonnet-4.5-thinking-then-tool-then-reply"
        session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="f17-session-")), model_label=model,
            session_context=session_ctx, max_turns=10, effort="high",
        )

        list(session.turn("please read the file and summarize it"))

        ctx.check(f"exactly two requests reached the mock, got {len(mock.requests)}", len(mock.requests) == 2)

        # (a) message_start's usage reached the logged usage node -- not
        # just message_delta's bare output_tokens (finding 15 point 3).
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check(f"at least one usage node logged, got {len(usage_nodes)}", len(usage_nodes) >= 1)
        first_usage = (usage_nodes[0].get("usage") or {}) if usage_nodes else {}
        ctx.check(f"first step's usage carries message_start's input_tokens (321), got {first_usage}",
                  first_usage.get("input_tokens") == 321)

        # (b) the SECOND real wire request replays the thinking block with
        # the wire's own `thinking` key (not the log's internal `text`
        # key) and the original signature -- proved against the actual
        # HTTP body MockAnthropic received, not a hand-called helper.
        second_body = mock.requests[1]["body"]
        assistant_messages = [m for m in second_body.get("messages", []) if m.get("role") == "assistant"]
        ctx.check(f"an assistant message was replayed in request 2, got {len(assistant_messages)}",
                  len(assistant_messages) >= 1)
        thinking_blocks = [b for m in assistant_messages for b in (m.get("content") or [])
                           if isinstance(b, dict) and b.get("type") == "thinking"]
        ctx.check(f"exactly one replayed thinking block, got {thinking_blocks}", len(thinking_blocks) == 1)
        tb = thinking_blocks[0] if thinking_blocks else {}
        ctx.check(f"replayed with the wire's 'thinking' key, got {tb}",
                  tb.get("thinking") == "need to read the file first")
        ctx.check(f"no stray internal 'text' key on the wire, got {tb}", "text" not in tb)
        ctx.check(f"signature preserved byte-for-byte on replay, got {tb.get('signature')!r}",
                  tb.get("signature") == "sig_f17")

        # The turn actually finished (tool_use handled, final reply logged).
        final_texts = [b.get("text") for m in session.log.nodes() if m.get("type") == "assistant"
                       for b in (m.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
        ctx.check(f"the final reply text made it into the log, got {final_texts}",
                  any("done" in (t or "") for t in final_texts))
    finally:
        mock.stop()


@test
def test_h5c_f05_steer_mid_thinking_before_signature_delta_logs_no_empty_node(ctx: Ctx):
    """H5b/H5c finding 5: a steer noticed WHILE a native thinking block is
    still streaming, before its `signature_delta` ever arrives, used to
    log that block anyway (`{"type": "thinking", "text": "...", "signature":
    ""}`), which `prepare_anthropic_messages` then drops -- leaving an
    assistant node with EMPTY content permanently in the log, 400ing every
    later request on this route. Drives a REAL `Session` against
    `MockAnthropic`'s own `thinking-and-signature` scenario (thinking_delta
    BEFORE signature_delta -- the exact ordering this finding needs),
    single-stepping the turn generator so the steer lands right after the
    first `thinking_delta` and before anything else."""
    import os

    from tests.helpers.fake_home import build_fake_home
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref

    fh = build_fake_home()
    # H10b: never set before -- the real in-process Session below fell
    # through to the REAL `~/.halo/sessions`, leaking
    # `ant:claude-h5c-f05-thinking-and-signature` sessions into rolo's real
    # session history (H10b report).
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockAnthropic().start()
    try:
        model = "ant:claude-h5c-f05-thinking-and-signature"
        session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="f05-thinking-steer-")), model_label=model,
            session_context=session_ctx, max_turns=10,
        )
        gen = session.turn("please think it over")
        seen = []
        for _ in range(50):
            ev = next(gen)
            seen.append(ev)
            if ev.kind == "thinking_delta":
                break
        else:
            raise AssertionError(f"never saw a thinking_delta, got {[e.kind for e in seen]}")

        queued = session.steer("stop thinking, redirect now")
        ctx.check("steer() accepted right after the first thinking_delta", queued is True)

        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
        ctx.check(f"no signature_delta-derived content ever streamed after the steer, got {kinds}",
                   "steer_applied" in kinds)
        ctx.check(f"the model was called a second time, got {len(mock.requests)} requests", len(mock.requests) == 2)

        assistant_nodes = [n for n in session.log.nodes() if n.get("type") == "assistant"]
        ctx.check(f"no empty-content assistant node was ever logged, got {[n.get('content') for n in assistant_nodes]}",
                   all(n.get("content") for n in assistant_nodes))
        ctx.check(f"exactly one assistant node logged (the cut call logged nothing), got "
                  f"{[n.get('type') for n in session.log.nodes()]}", len(assistant_nodes) == 1)

        # The SECOND request's own wire body must never carry a broken
        # (empty-content, or unsigned-thinking-only) assistant message --
        # `prepare_anthropic_messages` must have dropped it and merged the
        # two adjacent user turns into one.
        second_body = mock.requests[1]["body"]
        for m in second_body.get("messages", []):
            if m.get("role") == "assistant":
                ctx.check(f"every replayed assistant message has real content, got {m}", bool(m.get("content")))
        roles = [m.get("role") for m in second_body.get("messages", [])]
        ctx.check(f"no two adjacent user messages in the replayed wire body, got roles={roles}",
                   all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)))

        user_texts = [b.get("text") for n in session.log.nodes() if n.get("type") == "user"
                      for b in (n.get("content") or []) if isinstance(b, dict)]
        ctx.check(f"the steer text is logged as a user message, got {user_texts}",
                   "stop thinking, redirect now" in user_texts)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# build_anthropic_request_body / cache_control / effort mapping (unit-level)
# ---------------------------------------------------------------------------

@test
def test_build_body_thinking_from_effort(ctx: Ctx):
    # finding 15 (h4-h5-h3c review): budget_tokens is clamped BELOW
    # max_tokens (here profile.max_tokens_default=8192, no explicit
    # requested_max_tokens) -- 24,000 would 400 on the real API
    # ("budget_tokens must be < max_tokens"), the exact verified repro.
    # 1.0.1 fixpass finding 11: capped at HALF of max_tokens (4096), not
    # max_tokens - 1 (8191) -- the old near-max_tokens budget left thinking
    # free to crowd out the actual answer.
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=None, route=_route("claude-sonnet-4.5"), profile=_profile(), effort="high")
    ctx.check(f"thinking.budget_tokens from --effort=high, capped at max_tokens // 2, got {body.get('thinking')}",
              body.get("thinking") == {"type": "enabled", "budget_tokens": 4096})
    ctx.check(f"budget_tokens < max_tokens (the real Anthropic wire constraint), got "
              f"budget={body['thinking']['budget_tokens']} max_tokens={body['max_tokens']}",
              body["thinking"]["budget_tokens"] < body["max_tokens"])


@test
def test_h5b_f15_thinking_budget_clamped_below_a_small_max_tokens(ctx: Ctx):
    """finding 15's own verified repro: --effort high sending max_tokens
    16,384 with a 24,000 budget 400s on the first call -- and the
    summariser's 4,096 max_tokens is even tighter."""
    body = build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=None, route=_route("claude-sonnet-4.5"), profile=_profile(), effort="high",
        requested_max_tokens=16384,
    )
    ctx.check(f"budget stays below max_tokens=16384, got {body.get('thinking')}",
              body["thinking"]["budget_tokens"] < 16384)


@test
def test_h5b_f15_thinking_omitted_entirely_when_max_tokens_too_small(ctx: Ctx):
    """A max_tokens too small for even the 1,024-token minimum viable
    budget (the summariser's own 4,096 max_tokens against a genuinely
    tiny cap, or any call under ~1,024) omits thinking outright rather
    than sending an invalid budget."""
    body = build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=None, route=_route("claude-sonnet-4.5"), profile=_profile(), effort="high",
        requested_max_tokens=1000,
    )
    ctx.check(f"thinking omitted entirely, got {body.get('thinking')}", "thinking" not in body)


@test
def test_h5b_f15_message_level_reasoning_key_never_reaches_the_anthropic_wire(ctx: Ctx):
    """finding 15: a stray message-level `reasoning` key (OpenAI-dialect
    bookkeeping -- agent/derive.py attaches it to EVERY assistant node
    that logged one, regardless of which model produced it) must never
    reach Anthropic's wire body -- an unrecognized field on a message
    object. Its text is downgraded to a plain, visible text block
    instead of being silently dropped (a mid-session /model switch from
    an OpenAI-dialect model must not lose the model's own prior
    reasoning outright)."""
    from halo_harness.providers.request import prepare_anthropic_messages

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "the answer"}],
         "reasoning": {"text": "I thought about this carefully."}},
    ]
    prepared = prepare_anthropic_messages(messages)
    assistant_msg = prepared[1]
    ctx.check(f"no stray 'reasoning' key survives, got {list(assistant_msg.keys())}", "reasoning" not in assistant_msg)
    all_text = " ".join(b.get("text", "") for b in assistant_msg["content"] if isinstance(b, dict))
    ctx.check(f"the reasoning text is downgraded to a visible text block, not dropped, got {all_text!r}",
              "I thought about this carefully." in all_text)
    ctx.check(f"the original answer text is still there too, got {all_text!r}", "the answer" in all_text)


@test
def test_h5b_f15_thinking_block_text_renamed_to_thinking_field_on_replay(ctx: Ctx):
    """finding 15: the harness logs a thinking block's content under
    `text` (its own internal storage convention) -- Anthropic's wire
    needs the field named `thinking`. A genuine native thinking block
    (with a real signature) must be renamed, never dropped."""
    from halo_harness.providers.request import prepare_anthropic_messages

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "text": "reasoning content here", "signature": "SIG123"},
            {"type": "text", "text": "the answer"},
        ]},
    ]
    prepared = prepare_anthropic_messages(messages)
    thinking_block = prepared[1]["content"][0]
    ctx.check(f"renamed to 'thinking', got {thinking_block}", thinking_block.get("thinking") == "reasoning content here")
    ctx.check("no stray 'text' key survives on the thinking block", "text" not in thinking_block)
    ctx.check(f"signature preserved byte-for-byte, got {thinking_block.get('signature')!r}",
              thinking_block.get("signature") == "SIG123")


@test
def test_h5b_f15_unsigned_thinking_and_empty_text_blocks_dropped(ctx: Ctx):
    """finding 15: a partial block a steer/abort/interrupt cut short
    before it ever finished forming (no signature yet, or a genuinely
    empty text block) must be DROPPED from the replay, not sent as
    malformed. H5b finding 16 (this overrides the OLD, wrong assumption
    that ANY empty-text thinking block should be dropped): a SIGNED block
    with empty text is the normal "display omitted" shape and must be kept
    and echoed back unchanged, never dropped just because `text` is empty."""
    from halo_harness.providers.request import prepare_anthropic_messages

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "text": "cut short mid-thought", "signature": ""},  # no signature yet -- dropped
            {"type": "thinking", "text": "", "signature": "SIG"},  # signed, empty text -- KEPT (finding 16)
            {"type": "text", "text": ""},  # empty text block -- dropped
            {"type": "text", "text": "the real answer"},
        ]},
    ]
    prepared = prepare_anthropic_messages(messages)
    content = prepared[1]["content"]
    ctx.check(f"unsigned thinking and empty text blocks dropped, signed-empty thinking kept, got {content}",
              content == [{"type": "thinking", "thinking": "", "signature": "SIG"},
                          {"type": "text", "text": "the real answer"}])


@test
def test_h5c_f05_empty_assistant_message_dropped_and_adjacent_user_turns_merged(ctx: Ctx):
    """H5c finding 5: an assistant message whose ONLY block was an unsigned
    thinking block (a steer cut it short before signature_delta, or the log
    predates the H5c loop.py fix that stops this from being logged at all)
    ends up with NO content once the unsigned block is dropped -- the whole
    message must be dropped too (Anthropic rejects empty assistant
    content), and the two now-adjacent user messages either side of it
    merged into one (Anthropic requires alternating roles)."""
    from halo_harness.providers.request import prepare_anthropic_messages

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "original prompt"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "text": "cut short mid-thought", "signature": ""},  # unsigned -- only block
        ]},
        {"role": "user", "content": [{"type": "text", "text": "steer text"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "real reply"}]},
    ]
    prepared = prepare_anthropic_messages(messages)
    ctx.check(f"the empty assistant message was dropped entirely, got {prepared}",
              not any(m.get("role") == "assistant" and not m.get("content") for m in prepared))
    roles = [m.get("role") for m in prepared]
    ctx.check(f"roles now strictly alternate (no two adjacent user messages), got {roles}",
              all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)))
    ctx.check(f"exactly 2 messages remain (merged user+user, then the real reply), got {len(prepared)}",
              len(prepared) == 2)
    ctx.check(f"the merged user message carries BOTH original texts, got {prepared[0]}",
              prepared[0] == {"role": "user", "content": [
                  {"type": "text", "text": "original prompt"}, {"type": "text", "text": "steer text"}]})


@test
def test_build_body_output_config_effort_for_opus(ctx: Ctx):
    """H5c finding 16: an adaptive-capable model (Opus 4.6+)
    gets BOTH `thinking: {type: "adaptive"}` AND `output_config.effort`
    together -- never `budget_tokens` (which these models reject with a
    400)."""
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=None, route=_route("claude-opus-4-6"), profile=_profile(), effort="high")
    ctx.check(f"output_config.effort for an Opus-class id, got {body.get('output_config')}",
              body.get("output_config") == {"effort": "high"})
    ctx.check(f"thinking is the adaptive shape, got {body.get('thinking')}",
              body.get("thinking") == {"type": "adaptive"})
    ctx.check("no budget_tokens anywhere on the adaptive path",
              "budget_tokens" not in (body.get("thinking") or {}))


@test
def test_h5c_f16_sonnet_5_gets_adaptive_thinking_not_budget_tokens(ctx: Ctx):
    """H5c finding 16: `map_effort_anthropic` used to send `thinking.
    budget_tokens` to every non-opus, non-fable id -- including Sonnet 5,
    which REJECTS `budget_tokens` with a 400 (it has no budget-based
    thinking mode at all, only adaptive). Sonnet 4.6 gets the same
    treatment (recommended per Anthropic's own docs), while Sonnet 4.5
    (older, budget_tokens-only) must be UNAFFECTED by this fix."""
    for model_id in ("claude-sonnet-5", "claude-sonnet-4.6"):
        body = build_anthropic_request_body(
            system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            tools=None, route=_route(model_id), profile=_profile(), effort="high",
        )
        ctx.check(f"{model_id}: thinking is adaptive, got {body.get('thinking')}",
                  body.get("thinking") == {"type": "adaptive"})
        ctx.check(f"{model_id}: output_config.effort present too, got {body.get('output_config')}",
                  body.get("output_config") == {"effort": "high"})
        ctx.check(f"{model_id}: no budget_tokens anywhere, got {body.get('thinking')}",
                  "budget_tokens" not in (body.get("thinking") or {}))

    # Sonnet 4.5 (older, pre-4.6) is UNCHANGED -- still budget_tokens.
    old_body = build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=None, route=_route("claude-sonnet-4.5"), profile=_profile(), effort="high",
    )
    ctx.check(f"claude-sonnet-4.5: still budget_tokens-based (unaffected), got {old_body.get('thinking')}",
              old_body.get("thinking", {}).get("type") == "enabled"
              and "budget_tokens" in old_body.get("thinking", {}))


@test
def test_build_body_no_effort_omits_both_fields(ctx: Ctx):
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=None, route=_route("claude-sonnet-4.5"), profile=_profile())
    ctx.check("no --effort -> no thinking field (provider default)", "thinking" not in body)
    ctx.check("no --effort -> no output_config field", "output_config" not in body)


@test
def test_build_body_forced_tool_choice_drops_thinking(ctx: Ctx):
    """1.0.1 fixpass finding 11: Anthropic rejects extended thinking
    together with a FORCED tool_choice -- the leak-parser repair retry
    (agent/loop.py, tool_choice="required" -> {"type": "any"}) must never
    also carry a thinking/output_config field, or the repair call itself
    400s. An ordinary turn (tool_choice omitted/"auto") is unaffected."""
    tools = [{"name": "Read", "input_schema": {"type": "object", "properties": {}}}]
    forced = build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=tools, tool_choice="required", route=_route("claude-opus-4-6"), profile=_profile(), effort="high")
    ctx.check(f"tool_choice forced to 'any', got {forced.get('tool_choice')}",
              forced.get("tool_choice") == {"type": "any"})
    ctx.check(f"thinking dropped entirely despite effort='high', got {forced.get('thinking')}",
              "thinking" not in forced)
    ctx.check(f"output_config dropped too, got {forced.get('output_config')}", "output_config" not in forced)

    # The ordinary (non-forced) turn on the SAME adaptive model is unaffected.
    ordinary = build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=tools, tool_choice=None, route=_route("claude-opus-4-6"), profile=_profile(), effort="high")
    ctx.check(f"an ordinary turn still gets thinking, got {ordinary.get('thinking')}",
              ordinary.get("thinking") == {"type": "adaptive"})


@test
def test_build_body_tools_are_input_schema_verbatim_no_conversion(ctx: Ctx):
    tools = [{"name": "Read", "description": "reads a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}]
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=tools, route=_route("claude-sonnet-4.5"), profile=_profile())
    ctx.check(f"tools pass through verbatim (already Anthropic-shaped), got {body.get('tools')}", body["tools"] == tools)


@test
def test_build_body_tools_name_sorted(ctx: Ctx):
    tools = [{"name": "Write", "input_schema": {}}, {"name": "Bash", "input_schema": {}}, {"name": "Edit", "input_schema": {}}]
    body = build_anthropic_request_body(system_text="SYS", messages=[], tools=tools, route=_route("claude-sonnet-4.5"), profile=_profile())
    ctx.check(f"tools name-sorted for cache stability, got {[t['name'] for t in body['tools']]}",
              [t["name"] for t in body["tools"]] == ["Bash", "Edit", "Write"])


@test
def test_map_tool_choice_anthropic(ctx: Ctx):
    ctx.check("None -> omit (Anthropic default)", map_tool_choice_anthropic(None) is None)
    ctx.check("'auto' -> omit", map_tool_choice_anthropic("auto") is None)
    ctx.check("'required' -> {type: any}", map_tool_choice_anthropic("required") == {"type": "any"})


@test
def test_apply_cache_control_system_and_last_tool_result(ctx: Ctx):
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q1"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result 1"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "result 2"}]},
    ]
    system_blocks, out = apply_anthropic_cache_control("SYS PROMPT", messages)
    ctx.check("system block carries cache_control", system_blocks[0].get("cache_control") == {"type": "ephemeral"})
    last_tool_result_msg = out[-1]
    ctx.check(f"the LAST tool_result's content carries cache_control, got {last_tool_result_msg}",
              last_tool_result_msg["content"][-1].get("cache_control") == {"type": "ephemeral"})
    earlier_tool_result_msg = out[2]
    ctx.check("an EARLIER tool_result is untouched (only the last gets the breakpoint)",
              "cache_control" not in earlier_tool_result_msg["content"][-1])
    ctx.check("total breakpoints <= 4 (Appendix F cap)",
              sum(1 for b in system_blocks if "cache_control" in b) +
              sum(1 for m in out for b in (m["content"] if isinstance(m.get("content"), list) else [])
                  if isinstance(b, dict) and "cache_control" in b) <= 4)


@test
def test_apply_cache_control_never_mutates_input(ctx: Ctx):
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]}]
    original = [dict(m) for m in messages]
    apply_anthropic_cache_control("SYS", messages)
    ctx.check("input messages list untouched", messages == original)


# ---- H8 must-do: the count_tokens relay --------------------------------

@test
def test_h8_call_databricks_count_tokens_returns_real_input_tokens(ctx: Ctx):
    """The count_tokens relay (providers/http.py) built in an earlier
    milestone but never called by anything -- proves the wire mechanics
    work: a request with no stream/max_tokens fields gets back Anthropic's
    real `{"input_tokens": N}` shape."""
    from halo_harness.providers.http import call_databricks_count_tokens
    mock = MockAnthropic().start()
    try:
        body = build_anthropic_request_body(
            system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            tools=None, route=_route("claude-count-tokens-42", "databricks"), profile=_profile(),
        )
        body.pop("stream", None)
        body.pop("max_tokens", None)
        result = call_databricks_count_tokens(mock.base_url, "tok", body, {}, Path(tempfile.mkdtemp()))
        ctx.check(f"200 ok, got {result.status}", result.status == 200)
        parsed = __import__("json").loads(result.resp.read())
        ctx.check(f"real input_tokens count, got {parsed}", parsed.get("input_tokens") == 42)
    finally:
        mock.stop()


@test
def test_h8_compaction_gate_uses_real_count_tokens_when_no_usage_yet(ctx: Ctx):
    """The must-do's own acceptance: `_maybe_auto_compact` uses the route's
    real count-tokens endpoint (never the crude len/4 estimator) whenever
    `_last_prompt_tokens` is still unknown (before the session's first real
    reply, or right after a compaction) on a route that has one."""
    import os
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from tests.helpers.fake_home import build_fake_home

    fh = build_fake_home()
    mock = MockAnthropic().start()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        session_ctx = SessionContext(cwd=fh["proj"], model_label="dbx:databricks-claude-count-tokens-42")
        model_ref = parse_model_ref("dbx:databricks-claude-count-tokens-42")
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(context_tokens=200_000, max_output_tokens=8192),
            creds=ProviderCreds(base_url=mock.base_url, api_key="tok"),
            state_dir=Path(tempfile.mkdtemp(prefix="count-tokens-gate-")), model_label=model_ref.raw,
            session_context=session_ctx,
        )
        session.log.append_user([{"type": "text", "text": "hello"}])
        ctx.check("no usage recorded yet", session._last_prompt_tokens is None)
        counted = session._count_tokens_via_api()
        ctx.check(f"the real count_tokens relay was used, got {counted}", counted == 42)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
