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
from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
from halo_harness.providers.request import ToolCatalogTooLarge, budget_max_tokens, build_request_body, simplify_schema_for_databricks
from halo_harness.providers.routing import Route

test, TESTS = new_registry()

_READ_TOOL = {"name": "Read", "description": "reads a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"],
}}


def _messages_with_tool_turn(reasoning_text=None, reasoning_details=None):
    """`reasoning` node shape is `{"text": str|None, "details": list|None}`
    (H2: see agent/loop.py's `_step` and providers/hooks.reasoning_echo) --
    NOT the pre-H2 `{"format", "value"}` shape, which could only ever carry
    one of the two at a time (finding 2's second bug)."""
    node_reasoning = ({"text": reasoning_text, "details": reasoning_details}
                       if reasoning_text is not None or reasoning_details is not None else None)
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
    messages = _messages_with_tool_turn(reasoning_text="my reasoning trace")

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
    ctx.check("deepseek-v4.1-flash is a dual-field (echo_required_400) row", profile.reasoning_dual_field is True)
    details = [{"type": "reasoning.text", "text": "step 1"}, {"type": "reasoning.summary", "text": "step 2"}]
    messages = _messages_with_tool_turn(reasoning_text="accumulated reasoning text", reasoning_details=details)
    body = build_request_body(system_text="SYS", messages=messages, tools=[_READ_TOOL], route=route, profile=profile)
    assistants = [m for m in body["messages"] if m.get("role") == "assistant"]
    ctx.check("reasoning_details replayed VERBATIM (identity, not a copy that dropped fields)",
              assistants[0].get("reasoning_details") == details)
    ctx.check("order preserved exactly", [d["type"] for d in assistants[0]["reasoning_details"]] == ["reasoning.text", "reasoning.summary"])
    # finding 2's second bug: a DeepSeek V4 (echo_required_400) row on
    # OpenRouter must ALSO carry reasoning_content beside the verbatim
    # details array -- DeepSeek 400s ("reasoning_content ... must be passed
    # back") if it's missing, regardless of whether reasoning_details is
    # also present; the pre-H2 test asserted the OPPOSITE (absence), which
    # is exactly what finding 2 flagged as a bug, not a spec.
    ctx.check("reasoning_content ALSO present (dual-field DeepSeek V4 rule), got "
              f"{assistants[0].get('reasoning_content')!r}", assistants[0].get("reasoning_content") == "accumulated reasoning text")

    empty_turn_messages = _messages_with_tool_turn(reasoning_text=None, reasoning_details=None)
    empty_body = build_request_body(system_text="SYS", messages=empty_turn_messages, tools=[_READ_TOOL], route=route, profile=profile)
    empty_assistants = [m for m in empty_body["messages"] if m.get("role") == "assistant"]
    ctx.check('dual-field reasoning_content is "" (not omitted) when no reasoning was captured that turn',
              empty_assistants[0].get("reasoning_content") == "")


@test
def test_finding_8_empty_assistant_node_does_not_shift_reasoning_alignment(ctx: Ctx):
    """finding 8's exact repro: an assistant node with EMPTY content (spent
    its whole budget thinking) carrying reasoning R1, followed by a
    tool_use assistant node carrying R2. Pre-H2, `_flatten_messages` simply
    DROPPED the empty node's proto, so R2 ended up attached to the position
    R1 should have had, and R1 was lost. Every assistant proto must survive
    (finding 8's other half: content: "" instead of being dropped)."""
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-deepseek-v4-1-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q1"}]},
        {"role": "assistant", "content": [], "reasoning": {"text": "R1 -- spent the whole budget thinking", "details": None}},
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "a.py"}}],
         "reasoning": {"text": "R2", "details": None}},
    ]
    body = build_request_body(system_text="SYS", messages=messages, tools=[_READ_TOOL], route=route, profile=profile)
    assistants = [m for m in body["messages"] if m.get("role") == "assistant"]
    ctx.check(f"BOTH assistant protos survive (the empty one is not dropped), got {len(assistants)}", len(assistants) == 2)
    ctx.check(f"the empty proto's content is \"\" (never dropped, never omitted), got {assistants[0].get('content')!r}",
              assistants[0].get("content") == "")
    ctx.check(f"the FIRST proto gets R1, never R2's reasoning, got {assistants[0].get('reasoning_content')!r}",
              assistants[0].get("reasoning_content") == "R1 -- spent the whole budget thinking")
    ctx.check(f"the SECOND (tool_use) proto gets R2, never R1's leftover, got {assistants[1].get('reasoning_content')!r}",
              assistants[1].get("reasoning_content") == "R2")


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
def test_finding_13_pattern_property_survives_schema_keyword_strip(ctx: Ctx):
    """finding 13's exact repro: a Grep/Glob-shaped schema has a PROPERTY
    literally named `pattern` -- the pre-H2 simplifier stripped every dict
    key named pattern/anyOf/oneOf/allOf/$defs AT ANY DEPTH, including
    property NAMES inside `properties`, so this tool's `pattern` parameter
    vanished while `required: ["pattern"]` stayed, breaking the tool on
    every Databricks request. A genuine `pattern` SCHEMA KEYWORD (a regex
    constraint on a string) must still be stripped."""
    grep_shaped_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "the regex pattern to search for", "pattern": "^.+$"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
    }
    simplified = simplify_schema_for_databricks(grep_shaped_schema)
    ctx.check(f"the 'pattern' PROPERTY survives, got {simplified['properties']}", "pattern" in simplified["properties"])
    ctx.check("required still names it", simplified["required"] == ["pattern"])
    ctx.check("the property's OWN 'pattern' schema KEYWORD (a regex constraint) is still stripped",
              "pattern" not in simplified["properties"]["pattern"])
    ctx.check("the property's other keys survive", simplified["properties"]["pattern"]["type"] == "string")


@test
def test_finding_13_any_of_nullable_collapses_to_the_real_type(ctx: Ctx):
    """finding 13's other half: `anyOf: [T, {"type": "null"}]` (the common
    "optional nullable" shape) must collapse to T, not lose its type
    entirely the way blanket-deleting `anyOf` did."""
    schema = {"type": "object", "properties": {
        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "optional count"},
    }}
    simplified = simplify_schema_for_databricks(schema)
    prop = simplified["properties"]["count"]
    ctx.check(f"anyOf gone, got {prop}", "anyOf" not in prop)
    ctx.check(f"collapsed to the real (non-null) type, got {prop}", prop.get("type") == "integer")
    ctx.check("other keys on the node survive the collapse", prop.get("description") == "optional count")


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


@test
def test_deepseek_v4_flash_0731_sends_top_p_without_temperature(ctx: Ctx):
    """H5 scope F transform table: DeepSeek V4 Flash omits temperature but
    DOES send top_p=0.95 (OpenCode Appendix G's own per-model rule) --
    `use_top_p` gates top_p independently of `use_temperature`."""
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="deepseek/deepseek-v4-flash-0731", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check("use_temperature is False for this row", profile.use_temperature is False)
    ctx.check("use_top_p opts in independently", profile.use_top_p is True)
    body = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    ctx.check("temperature omitted from the wire body", "temperature" not in body)
    ctx.check(f"top_p=0.95 sent despite use_temperature=False, got {body.get('top_p')}", body.get("top_p") == 0.95)


@test
def test_h9_sampling_unsupported_params_wired_from_model_table_json(ctx: Ctx):
    """H9 sampling-table audit finding: `sampling_unsupported_params` was
    present in EVERY model_table.json row (DeepSeek V4/Grok/MiniMax:
    presence_penalty/frequency_penalty; Kimi K2.6+/K3: temperature/top_p/n/
    presence_penalty/frequency_penalty fixed server-side; Grok: also stop/
    logprobs/top_logprobs) but had no matching `ProviderProfile` field at
    all, so it was silently dropped on load -- pure decoration, byte-
    identical whether the row listed anything or not. Now it round-trips."""
    reset_model_table_cache()
    from halo_harness.providers.profiles import load_model_table
    table = load_model_table()
    for host, model_id in (("openrouter", "moonshotai/kimi-k3"), ("openrouter", "deepseek/deepseek-v4.1-flash"),
                            ("databricks", "databricks-kimi-k3")):
        row = table[host][model_id]
        expected = tuple(row.get("sampling_unsupported_params") or ())
        ctx.check(f"{model_id}: row actually lists some unsupported params", len(expected) > 0)
        route = Route(provider=host, upstream_model=model_id, dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check(f"{model_id}: profile.sampling_unsupported_params matches the row exactly, "
                  f"got {profile.sampling_unsupported_params!r} vs row {expected!r}",
                  set(profile.sampling_unsupported_params) == set(expected))


@test
def test_h9_sampling_unsupported_params_stripped_from_the_wire_body(ctx: Ctx):
    """The list isn't just carried on the dataclass -- build_request_body
    actually pops every listed key from the FINAL body, even overriding
    use_temperature/use_top_p (the Kimi K2.6+/K3 rows already have
    use_temperature=False so this never fires for them in practice, but a
    row with use_temperature=True is used here as the sharper test: the
    strip must win even when the normal sampling logic WOULD have set the
    key)."""
    import dataclasses
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="z-ai/glm-5.3", dialect="openai-chat")
    base_profile = resolve_profile(route)
    ctx.check("baseline sanity: glm-5.3 normally sends temperature", base_profile.use_temperature is True)
    profile = dataclasses.replace(base_profile, sampling_unsupported_params=("temperature", "top_p"))
    body = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    ctx.check(f"temperature stripped despite use_temperature=True, got body keys {sorted(body)}",
              "temperature" not in body)
    ctx.check(f"top_p stripped too, got body keys {sorted(body)}", "top_p" not in body)
    # A key NOT on the list is unaffected -- this isn't accidentally
    # stripping everything sampling-related.
    ctx.check("max_tokens (unrelated key) still present", "max_tokens" in body)


@test
def test_openrouter_claude_gets_cache_control_breakpoints(ctx: Ctx):
    """H5 scope C: or:anthropic/claude-* through the OPENAI dialect still
    needs explicit cache_control (OpenRouter has no automatic caching for
    Anthropic-backed models) on the system node and the last tool result."""
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="anthropic/claude-sonnet-4.5", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check("family resolves to claude", profile.family == "claude")
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q1"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result 1"}]},
    ]
    body = build_request_body(system_text="SYS", messages=messages, route=route, profile=profile)
    sys_msg = body["messages"][0]
    ctx.check(f"system content is an array with cache_control, got {sys_msg}",
              isinstance(sys_msg["content"], list) and sys_msg["content"][-1].get("cache_control") == {"type": "ephemeral"})
    tool_msg = next(m for m in body["messages"] if m.get("role") == "tool")
    ctx.check(f"the tool result carries cache_control, got {tool_msg}",
              isinstance(tool_msg["content"], list) and tool_msg["content"][-1].get("cache_control") == {"type": "ephemeral"})
    ctx.check("system text preserved verbatim inside the block", sys_msg["content"][0]["text"] == "SYS")
    ctx.check("tool result text preserved verbatim inside the block", tool_msg["content"][0]["text"] == "result 1")


@test
def test_non_claude_openrouter_model_gets_no_cache_control(ctx: Ctx):
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="deepseek/deepseek-v3.2", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    ctx.check("plain string content, no cache_control for a non-Claude model",
              body["messages"][0]["content"] == "SYS")


@test
def test_databricks_claude_passthrough_family_never_touches_openai_dialect_cache_control(ctx: Ctx):
    """A Databricks Claude route never reaches build_request_body at all in
    the real loop (it's dialect=anthropic-passthrough), but even if it did,
    host_specific_fields is False for Databricks so the gate stays off."""
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-claude-sonnet-4-6", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check("Databricks never sets host_specific_fields", profile.host_specific_fields is False)


@test
def test_use_top_p_none_falls_back_to_use_temperature(ctx: Ctx):
    """Every OTHER row (use_top_p unset -> None) is completely unaffected
    by the new field -- top_p still follows use_temperature exactly as
    before H5."""
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="z-ai/glm-5.3", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check("use_top_p is unset (None) for a row that never opted in", profile.use_top_p is None)
    body = build_request_body(system_text="S", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile, effort="high")
    ctx.check(f"top_p follows use_temperature (True for GLM 5.3), got {body.get('top_p')}", body.get("top_p") == 0.95)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
