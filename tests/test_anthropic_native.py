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
from rolo_claude.providers.errors import parse_context_overflow
from rolo_claude.providers.http import call_anthropic_native
from rolo_claude.providers.profiles import ProviderProfile
from rolo_claude.providers.request import (
    apply_anthropic_cache_control, build_anthropic_request_body, map_effort_anthropic, map_tool_choice_anthropic,
)
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import CompletionRequest, ContextOverflow, ProviderCreds, stream_anthropic_completion

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
        result = call_anthropic_native(mock.base_url, "k", body, {"Authorization": "Bearer k",
                                        "x-databricks-use-coding-agent-mode": "true"},
                                        Path(tempfile.mkdtemp(prefix="ant-dbx-")), route_provider="databricks")
        ctx.check(f"200 status, got {result.status}", result.status == 200)
        ctx.check(f"hit the ai-gateway anthropic path, got {mock.requests[-1]['path']}",
                  mock.requests[-1]["path"].startswith("/ai-gateway/anthropic/v1/messages"))
        ctx.check("Databricks gateway ?beta=true flag present", "beta=true" in mock.requests[-1]["path"])
        ctx.check("coding-agent-mode header forwarded", mock.requests[-1]["headers"].get("x-databricks-use-coding-agent-mode") == "true")
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# build_anthropic_request_body / cache_control / effort mapping (unit-level)
# ---------------------------------------------------------------------------

@test
def test_build_body_thinking_from_effort(ctx: Ctx):
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=None, route=_route("claude-sonnet-4.5"), profile=_profile(), effort="high")
    ctx.check(f"thinking.budget_tokens from --effort=high, got {body.get('thinking')}",
              body.get("thinking") == {"type": "enabled", "budget_tokens": 24000})


@test
def test_build_body_output_config_effort_for_opus(ctx: Ctx):
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=None, route=_route("claude-opus-4"), profile=_profile(), effort="high")
    ctx.check(f"output_config.effort for an Opus-class id, got {body.get('output_config')}",
              body.get("output_config") == {"effort": "high"})
    ctx.check("no thinking.budget_tokens on the Opus/output_config path", "thinking" not in body)


@test
def test_build_body_no_effort_omits_both_fields(ctx: Ctx):
    body = build_anthropic_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         tools=None, route=_route("claude-sonnet-4.5"), profile=_profile())
    ctx.check("no --effort -> no thinking field (provider default)", "thinking" not in body)
    ctx.check("no --effort -> no output_config field", "output_config" not in body)


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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
