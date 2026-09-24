"""tests.test_request_builder -- providers/request.py: reasoning replay
(DeepSeek "" injection, OpenRouter reasoning_details verbatim/ordered),
strict allowlist filtering, max_tokens budgeting table, tool_choice
required->auto fallback, the 32-tool cap error, and the Databricks schema
simplifier (<=16 keys, no $ref).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
from rolo_claude.providers.request import ToolCatalogTooLarge, budget_max_tokens, build_request_body, simplify_schema_for_databricks
from rolo_claude.providers.routing import Route

test, TESTS = new_registry()

_READ_TOOL = {"name": "Read", "description": "reads a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"],
}}


def _messages_with_tool_turn(reasoning_value=None):
    node_reasoning = {"format": "text", "value": reasoning_value} if reasoning_value is not None else None
    msg = {"role": "assistant", "content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "a.py"}},
    ]}
    if node_reasoning is not None:
        msg["reasoning"] = node_reasoning
    return [
        {"role": "user", "content": [{"type": "text", "text": "read a.py"}]},
        msg,
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "contents"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},  # a later turn with NO reasoning
    ]


@test
def test_deepseek_reasoning_content_injected_with_and_without_tools(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-deepseek-v4-1-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    messages = _messages_with_tool_turn(reasoning_value="my reasoning trace")

    with_tools = build_request_body(system_text="SYS", messages=messages, tools=[_READ_TOOL], route=route, profile=profile)
    assistants = [m for m in with_tools["messages"] if m.get("role") == "assistant"]
    ctx.check(f"2 assistant messages, got {len(assistants)}", len(assistants) == 2)
    ctx.check("first assistant carries the real reasoning verbatim", assistants[0].get("reasoning_content") == "my reasoning trace")
    ctx.check('second assistant (no reasoning that turn) gets "" injected, not omitted', assistants[1].get("reasoning_content") == "")

    without_tools = build_request_body(system_text="SYS", messages=messages, route=route, profile=profile)
    assistants_nt = [m for m in without_tools["messages"] if m.get("role") == "assistant"]
    ctx.check("no tools in the request -> reasoning_content key is ABSENT entirely (not even \"\")",
              all("reasoning_content" not in m for m in assistants_nt))


@test
def test_openrouter_reasoning_details_verbatim_and_ordered(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="deepseek/deepseek-v4.1-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    details = [{"type": "reasoning.text", "text": "step 1"}, {"type": "reasoning.summary", "text": "step 2"}]
    messages = _messages_with_tool_turn(reasoning_value=details)
    body = build_request_body(system_text="SYS", messages=messages, tools=[_READ_TOOL], route=route, profile=profile)
    assistants = [m for m in body["messages"] if m.get("role") == "assistant"]
    ctx.check("reasoning_details replayed VERBATIM (identity, not a copy that dropped fields)",
              assistants[0].get("reasoning_details") == details)
    ctx.check("order preserved exactly", [d["type"] for d in assistants[0]["reasoning_details"]] == ["reasoning.text", "reasoning.summary"])
    ctx.check("no reasoning_content key on an OpenRouter (details-replay) profile", "reasoning_content" not in assistants[0])


@test
def test_single_leading_system_message(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="deepseek/deepseek-v4.1-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="THE SYSTEM PROMPT", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    system_msgs = [m for m in body["messages"] if m.get("role") == "system"]
    ctx.check("exactly one system message", len(system_msgs) == 1)
    ctx.check("it is the FIRST message", body["messages"][0]["role"] == "system")
    ctx.check("content matches", system_msgs[0]["content"] == "THE SYSTEM PROMPT")


@test
def test_max_tokens_budgeting_table(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-deepseek-v4-pro-0813", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check(f"cap is the profile's OTPM-derived cap (4000), got {profile.max_tokens_cap}", profile.max_tokens_cap == 4000)

    # Plenty of headroom -> uses the profile default, capped at the OTPM ceiling.
    got = budget_max_tokens(profile=profile, context_tokens=1048576, prompt_estimate=100)
    ctx.check(f"budget == min(default, cap) with headroom, got {got}", got == min(profile.max_tokens_default, profile.max_tokens_cap))

    # Requested value ABOVE the cap is clamped down to the cap.
    got_over = budget_max_tokens(profile=profile, context_tokens=1048576, prompt_estimate=100, requested=999999)
    ctx.check(f"requested above cap clamps to the cap (4000), got {got_over}", got_over == 4000)

    # Headroom collapse (context - estimate - buffer <= 0) still returns >= 1, never <= 0.
    got_tiny = budget_max_tokens(profile=profile, context_tokens=1000, prompt_estimate=999)
    ctx.check(f"collapsed headroom -> still a positive int, got {got_tiny}", isinstance(got_tiny, int) and got_tiny >= 1)


@test
def test_tool_choice_required_falls_back_to_auto_when_unsupported(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-deepseek-v4-1-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               tools=[_READ_TOOL], tool_choice={"type": "any"}, route=route, profile=profile)
    ctx.check(f'tool_choice "required" downgraded to "auto" for a thinking-mode-400 profile, got {body.get("tool_choice")!r}',
              body.get("tool_choice") == "auto")

    k3_route = Route(provider="openrouter", upstream_model="moonshotai/kimi-k3", dialect="openai-chat")
    k3_profile = resolve_profile(k3_route)
    body_k3 = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                  tools=[_READ_TOOL], tool_choice={"type": "any"}, route=k3_route, profile=k3_profile)
    ctx.check('kimi-k3 (required IS supported) keeps "required"', body_k3.get("tool_choice") == "required")


@test
def test_32_tool_cap_raises_clear_error(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-kimi-k3", dialect="openai-chat")
    profile = resolve_profile(route)
    many_tools = [{"name": f"tool_{i}", "input_schema": {"type": "object", "properties": {}}} for i in range(33)]
    try:
        build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                            tools=many_tools, route=route, profile=profile)
        ctx.check("33 tools against a 32-cap profile must raise ToolCatalogTooLarge", False)
    except ToolCatalogTooLarge as e:
        ctx.check(f"clear error names the count and limit, got {e}", e.count == 33 and e.limit == 32)


@test
def test_databricks_allowlist_drops_openrouter_only_fields(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-kimi-k3", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               tools=[_READ_TOOL], route=route, profile=profile)
    ctx.check("no 'provider' field sent to Databricks", "provider" not in body)
    ctx.check("no 'usage' field sent to Databricks", "usage" not in body)
    ctx.check("every key is in the strict allowlist (plus 'model'/'messages'/'stream')",
              all(k in profile.body_allowlist or k in ("model", "messages", "stream") for k in body))


@test
def test_schema_simplifier_caps_properties_and_strips_ref(ctx: Ctx):
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(20)},
        "required": ["p0", "p1"],
        "nested": {"$ref": "#/$defs/thing"},
    }
    simplified = simplify_schema_for_databricks(schema, max_keys=16)
    ctx.check("$schema stripped", "$schema" not in simplified)
    ctx.check(f"properties capped at 16, got {len(simplified['properties'])}", len(simplified["properties"]) == 16)
    ctx.check("required list pruned to only still-present keys", set(simplified["required"]) <= set(simplified["properties"]))
    ctx.check("$ref node replaced with a permissive object", simplified["nested"] == {"type": "object"})


@test
def test_databricks_tool_schemas_are_simplified_in_the_built_body(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-glm-5-3", dialect="openai-chat")
    profile = resolve_profile(route)
    big_tool = {"name": "BigTool", "input_schema": {
        "type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(20)},
    }}
    body = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               tools=[big_tool], route=route, profile=profile)
    params = body["tools"][0]["function"]["parameters"]
    ctx.check(f"Databricks tool schema capped at 16 properties, got {len(params['properties'])}", len(params["properties"]) <= 16)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
