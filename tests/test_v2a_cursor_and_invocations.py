"""tests.test_v2a_cursor_and_invocations -- V2a: `cursor/v1/chat/completions`
(gpt-5-5-pro's own cursor-only route, and the GPT family's mlflow-then-cursor
fallback) plus `/serving-endpoints/<name>/invocations` (Bedrock EXTERNAL
Claude -- chat body without `model`, tools supported, streaming shape -- and
the universal fallback for a family/endpoint the discovery cache doesn't
know yet) -- against the extended tests/helpers/mock_databricks.py.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks
from halo_harness.providers.databricks import write_dbx_endpoints_json
from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
from halo_harness.providers.request import build_request_body
from halo_harness.providers.routing import Route
from halo_harness.providers.stream import CompletionRequest, ProviderCreds, stream_completion

test, TESTS = new_registry()


def _req_for(mock, model, state_dir, *, tools=None) -> CompletionRequest:
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               tools=tools, route=route, profile=profile)
    return CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=mock.root, api_key="tok"), state_dir=state_dir, extra_headers={},
        model_label=model, harness_mode=True, prebuilt_oai_body=body,
    )


@test
def test_v2a_gpt_pro_cursor_only_no_mlflow_candidate(ctx: Ctx):
    """databricks-gpt-5-5-pro: no mlflow chat at all -- cursor is the FIRST
    and only chat-shaped candidate tried."""
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-cursor-state-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "databricks-gpt-5-5-pro", "foundation_model_name": "gpt-5-5-pro-fm", "task": "llm/v1/chat",
             "api_types": ["openai/v1/responses", "cursor/v1/chat/completions", "mlflow/v1/responses",
                          "codex/v1/responses"]},
        ])
        list(stream_completion(_req_for(mock, "databricks-gpt-5-5-pro", state_dir)))
        req_seen = mock.requests[-1]
        ctx.check(f"hit the cursor path, got {req_seen['path']!r}",
                  "/ai-gateway/cursor/v1/chat/completions" in req_seen["path"])
        ctx.check(f"model id is the catalog's foundation_model.name, got {req_seen['body'].get('model')!r}",
                  req_seen["body"].get("model") == "gpt-5-5-pro-fm")
    finally:
        mock.stop()


@test
def test_v2a_gpt_family_falls_over_mlflow_to_cursor_on_404(ctx: Ctx):
    """A plain `gpt` endpoint (mlflow chat AND cursor chat both listed) --
    mlflow 404s, cursor answers, and the candidate index gets cached."""
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-cursor-fallback-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "databricks-gpt-5", "foundation_model_name": "gpt-5-mlflow-404-then-ok", "task": "llm/v1/chat",
             "api_types": ["mlflow/v1/chat/completions", "cursor/v1/chat/completions"]},
        ])
        events = list(stream_completion(_req_for(mock, "databricks-gpt-5", state_dir)))
        texts = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"the retried request still got a clean reply, got {texts}", "".join(texts) == "ok")
        paths_hit = [r["path"] for r in mock.requests]
        ctx.check(f"mlflow tried first then cursor, got {paths_hit}",
                  any("/mlflow/" in p for p in paths_hit) and any("/cursor/" in p for p in paths_hit))
        from halo_harness.providers.databricks import dbx_cache_get_route
        from halo_harness.providers.dbx_routing import chat_route_candidates
        cands = chat_route_candidates("databricks-gpt-5", state_dir)
        cached_idx = dbx_cache_get_route("databricks-gpt-5", state_dir)
        ctx.check(f"cursor's index cached for next time, got {cands[cached_idx].key}", cands[cached_idx].key == "cursor")
    finally:
        mock.stop()


@test
def test_v2a_bedrock_claude_external_invocations_only_no_model_field(ctx: Ctx):
    """Bedrock EXTERNAL Claude: invocations-only, chat body without `model`
    at all, despite "claude" being in the endpoint name."""
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-bedrock-state-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "us-anthropic-claude-3-5-sonnet-v2", "task": "llm/v1/external/chat", "api_types": []},
        ])
        from halo_harness.providers.dbx_routing import resolve_databricks_dialect
        _clean, dialect = resolve_databricks_dialect("us-anthropic-claude-3-5-sonnet-v2", state_dir)
        ctx.check(f"stays openai-chat (never native passthrough), got {dialect!r}", dialect == "openai-chat")
        list(stream_completion(_req_for(mock, "us-anthropic-claude-3-5-sonnet-v2", state_dir)))
        req_seen = mock.requests[-1]
        ctx.check(f"hit the invocations path, got {req_seen['path']!r}",
                  req_seen["path"] == "/serving-endpoints/us-anthropic-claude-3-5-sonnet-v2/invocations")
        ctx.check(f"NO model field on an invocations body, got {req_seen['body']!r}", "model" not in req_seen["body"])
    finally:
        mock.stop()


@test
def test_v2a_bedrock_claude_external_tools_supported_and_streams(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-bedrock-tools-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "us-anthropic-claude-3-5-sonnet-v2-tool-call-ok", "task": "llm/v1/external/chat", "api_types": []},
        ])
        tool = {"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}
        events = list(stream_completion(_req_for(mock, "us-anthropic-claude-3-5-sonnet-v2-tool-call-ok", state_dir,
                                                   tools=[tool])))
        starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
        ctx.check(f"tool_use decoded over invocations streaming, got {events}",
                  starts and starts[0]["content_block"]["name"] == "Read")
        sent = mock.requests[-1]["body"]
        ctx.check("tools were actually sent (converted to OpenAI function shape)",
                  sent.get("tools") and sent["tools"][0]["function"]["name"] == "Read")
    finally:
        mock.stop()


@test
def test_v2a_unknown_endpoint_universal_fallback_static_order(ctx: Ctx):
    """No discovery-cache entry at all -- `chat_route_candidates` falls back
    to today's static [invocations-first] order rather than refusing or
    guessing a family; the request still completes end to end."""
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-unknown-state-"))
        events = list(stream_completion(_req_for(mock, "databricks-brand-new-family-not-in-cache", state_dir)))
        texts = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"still gets a clean reply via the static fallback order, got {texts}", "".join(texts) == "ok")
        ctx.check(f"invocations tried first for a non-system.ai. name, got {mock.requests[-1]['path']!r}",
                  mock.requests[-1]["path"] == "/serving-endpoints/databricks-brand-new-family-not-in-cache/invocations")
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
