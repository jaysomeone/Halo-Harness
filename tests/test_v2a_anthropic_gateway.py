"""tests.test_v2a_anthropic_gateway -- V2a: `anthropic/v1/messages` exact
per family (Claude foundation default; GLM/Kimi optional via `@anthropic`/
`databricks.gateway.<endpoint>`) -- native Messages in and SSE out, thinking
blocks + signatures replayed, tool_use blocks (including Kimi's own verbatim
`functions.<name>:<idx>` id), cache_control, the
`x-databricks-use-coding-agent-mode` header, and this dialect's own error
shapes -- against the extended tests/helpers/mock_databricks.py (H14's
`_finish_anthropic` is now scenario-dispatched the same way the openai-chat
side already was).

`build_anthropic_request_body`/`stream_anthropic_completion` are dialect-
generic -- neither one branches on family at all (AnthropicSSEDecoder never
inspects `model`) -- so a GLM/Kimi model id exercises the IDENTICAL code path
a Claude foundation id does; these tests prove that generically-correct
mechanism actually round-trips for each family's own id shape, which is what
the "test both with and without thinking" / "Kimi's verbatim ids" items in
the V2a brief ask to be pinned.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks
from halo_harness.providers.config import merge_databricks_headers
from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
from halo_harness.providers.request import build_anthropic_request_body
from halo_harness.providers.routing import Route
from halo_harness.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_anthropic_completion

test, TESTS = new_registry()

_READ_TOOL = {"name": "Read", "description": "Read a local file.",
              "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                                "required": ["file_path"]}}


def _ant_req(mock: MockDatabricks, model: str, *, with_tools: bool = False, effort=None) -> CompletionRequest:
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model=model, dialect="anthropic-passthrough")
    profile = resolve_profile(route)
    body = build_anthropic_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[_READ_TOOL] if with_tools else None, route=route, profile=profile, effort=effort,
    )
    return CompletionRequest(
        body={}, route=route, profile={}, creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=Path(tempfile.mkdtemp(prefix="v2a-ant-state-")), extra_headers=merge_databricks_headers(None),
        model_label=model,
        prebuilt_anthropic_body=body,
    )


@test
def test_v2a_claude_foundation_anthropic_thinking_and_signature_replayed(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-thinking-and-signature")
        events = list(stream_anthropic_completion(req))
        thinking = [e for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "thinking_delta"]
        sigs = [e for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "signature_delta"]
        ctx.check(f"thinking_delta arrived, got {events}", len(thinking) == 1)
        ctx.check("signature_delta arrived and round-trips", sigs and sigs[0]["delta"]["signature"] == "sig_abc123")
        req_seen = mock.requests[-1]
        ctx.check(f"hit the ai-gateway anthropic path, got {req_seen['path']!r}",
                  req_seen["path"].startswith("/ai-gateway/anthropic/v1/messages"))
        ctx.check("Databricks Bearer auth sent", req_seen["headers"].get("Authorization") == "Bearer test-token")
        ctx.check("x-databricks-use-coding-agent-mode header sent",
                  req_seen["headers"].get("x-databricks-use-coding-agent-mode") == "true")
    finally:
        mock.stop()


@test
def test_v2a_claude_foundation_anthropic_tool_use_and_cache_control(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-tool-use", with_tools=True)
        events = list(stream_anthropic_completion(req))
        starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
        ctx.check("a tool_use content_block_start arrived", len(starts) == 1 and starts[0]["content_block"]["name"] == "Read")
        sent_body = mock.requests[-1]["body"]
        system_blocks = sent_body.get("system") or []
        ctx.check(f"cache_control on the system block, got {system_blocks}",
                  system_blocks and system_blocks[0].get("cache_control") == {"type": "ephemeral"})
        ctx.check("tools sent verbatim (input_schema, no conversion)",
                  sent_body["tools"][0]["input_schema"] == _READ_TOOL["input_schema"])
    finally:
        mock.stop()


@test
def test_v2a_glm_anthropic_opt_in_with_thinking(ctx: Ctx):
    """GLM on the anthropic gateway (a `dbx:databricks-glm-5-3@anthropic`-
    style opt-in) decodes a signed thinking block identically to Claude --
    the decoder is family-blind."""
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-glm-5-3-thinking-and-signature")
        events = list(stream_anthropic_completion(req))
        sigs = [e for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "signature_delta"]
        ctx.check(f"GLM thinking+signature decoded, got {events}", sigs and sigs[0]["delta"]["signature"] == "sig_abc123")
    finally:
        mock.stop()


@test
def test_v2a_glm_anthropic_opt_in_without_thinking(ctx: Ctx):
    """The "without thinking" half of the same GLM/Kimi requirement -- a
    plain text-only reply through the identical dialect."""
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-glm-5-3-ok")
        events = list(stream_anthropic_completion(req))
        texts = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"plain pong, no thinking block at all, got {events}",
                  "".join(texts) == "pong" and not any(
                      e.get("type") == "content_block_start" and e["content_block"].get("type") == "thinking"
                      for e in events))
    finally:
        mock.stop()


@test
def test_v2a_kimi_anthropic_verbatim_tool_id_passthrough(ctx: Ctx):
    """Kimi's own native `functions.<name>:<idx>` tool_use id, sent through
    the anthropic gateway -- AnthropicSSEDecoder never touches an id, so it
    must reach the caller byte-for-byte unchanged."""
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-kimi-k3-kimi-tool-id", with_tools=True)
        events = list(stream_anthropic_completion(req))
        starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
        ctx.check(f"Kimi's verbatim id preserved, got {starts}",
                  starts and starts[0]["content_block"]["id"] == "functions.Read:0")
    finally:
        mock.stop()


@test
def test_v2a_kimi_anthropic_effort_uses_budget_tokens_not_adaptive(ctx: Ctx):
    """Documents (pins, doesn't invent) today's actual behaviour: a non-
    Claude-family id never matches `_anthropic_model_supports_adaptive_
    thinking`'s opus/sonnet/haiku/fable/mythos pattern, so `--effort` on a
    Kimi/GLM id through this gateway gets the `budget_tokens` shape, never
    `output_config`/`{"type":"adaptive"}` -- exactly what the live work
    matrix's own reasoning-replay probe exists to confirm end to end against
    the real gateway (V2a open question 1)."""
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-kimi-k3-ok", effort="high")
        list(stream_anthropic_completion(req))
        sent = mock.requests[-1]["body"]
        ctx.check(f"budget_tokens thinking shape, got {sent.get('thinking')}",
                  sent.get("thinking", {}).get("type") == "enabled" and isinstance(sent["thinking"].get("budget_tokens"), int))
        ctx.check("no output_config sent for a non-Claude-family id", "output_config" not in sent)
    finally:
        mock.stop()


@test
def test_v2a_anthropic_gateway_401_is_auth_not_retryable(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-401-error")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("401 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 401, got {e.status}", e.status == 401)
            ctx.check("not retryable", e.retryable is False)
    finally:
        mock.stop()


@test
def test_v2a_anthropic_gateway_403_ip_access_list(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-403-ip-error")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("403 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 403, got {e.status}", e.status == 403)
    finally:
        mock.stop()


@test
def test_v2a_anthropic_gateway_404_error_surfaces(ctx: Ctx):
    """Unlike the openai-chat dialect (which fails over to another
    candidate on a 404), the anthropic gateway's own two-path fallback
    (ai-gateway -> serving-endpoints) is already covered by
    test_anthropic_native.py's `test_databricks_route_tries_ai_gateway_path_
    first`; this pins the case where BOTH paths 404 (a genuinely wrong
    workspace root) -- a real UpstreamError, not a silent hang."""
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-404-error")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("404 on both candidate paths must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 404, got {e.status}", e.status == 404)
    finally:
        mock.stop()


@test
def test_v2a_anthropic_gateway_overflow_400_raises_context_overflow(ctx: Ctx):
    from halo_harness.providers.stream import ContextOverflow
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-overflow-400")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("overflow wording must raise ContextOverflow", False)
        except ContextOverflow as e:
            ctx.check(f"limit parsed, got {e.limit}", e.limit == 200000)
    finally:
        mock.stop()


@test
def test_v2a_anthropic_gateway_429_retry_after_honoured(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-glm-5-3-rate-limit-429")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("429 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 429, got {e.status}", e.status == 429)
            ctx.check(f"retry_after from the header, got {e.retry_after!r}", e.retry_after == "3")
    finally:
        mock.stop()


@test
def test_v2a_anthropic_gateway_5xx_is_retryable(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _ant_req(mock, "databricks-claude-opus-4-6-500-error")
        try:
            list(stream_anthropic_completion(req))
            ctx.check("500 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status mapped, got {e.status}", e.status == 500)
            ctx.check("retryable", e.retryable is True)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
