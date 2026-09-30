"""tests.test_v2a_mlflow_chat -- V2a: `mlflow/v1/chat/completions` exact per
family (DeepSeek/Qwen/Llama/Gemma/gpt-oss/GPT/Grok/Gemini, GLM/Kimi default)
-- OpenAI-style chat with the family's own reasoning shape, no sampling
params where a row fixes them server-side, `max_tokens` pre-admitted against
OTPM, `model` = the catalog's `foundation_model.name`, `stream: true`
explicit, and the 32-tool cap + schema simplifier -- against the extended
tests/helpers/mock_databricks.py.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks
from rolo_claude.providers.databricks import write_dbx_endpoints_json
from rolo_claude.providers.hooks import max_tokens_budget, record_databricks_output_tokens, reset_databricks_otpm_history
from rolo_claude.providers.profiles import map_effort, reset_model_table_cache, resolve_profile
from rolo_claude.providers.request import build_request_body, convert_tools
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, stream_completion

test, TESTS = new_registry()


def _dbx_req(mock: MockDatabricks, model: str, *, tools=None, effort=None) -> CompletionRequest:
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=tools, route=route, profile=profile, effort=effort, context_tokens=128000, prompt_estimate=10,
    )
    return CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=Path(tempfile.mkdtemp(prefix="v2a-mlflow-state-")), extra_headers={},
        model_label=model, harness_mode=True, prebuilt_oai_body=body,
    )


def _harness_meta_from(events_list: list) -> dict:
    for ev in events_list:
        if ev.get("type") == "message_delta" and isinstance(ev.get("harness_meta"), dict):
            return ev["harness_meta"]
    return {}


@test
def test_v2a_deepseek_mlflow_reasoning_content_decoded(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        events = list(stream_completion(_dbx_req(mock, "databricks-deepseek-v4-1-flash-reasoning-content-shape")))
        meta = _harness_meta_from(events)
        ctx.check(f"reasoning_content decoded, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "thinking step 1 step 2")
    finally:
        mock.stop()


@test
def test_v2a_deepseek_mlflow_reasoning_replayed_when_tools_present(ctx: Ctx):
    """DeepSeek's own `reasoning_replay: "text"` rule: only echoed back once
    a tool is actually offered on the NEXT turn (`hooks.reasoning_echo`'s
    own "empty without tools" gate)."""
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-deepseek-v4-1-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    tool = {"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}
    history = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "reasoning": {"text": "prior thought", "details": None}},
    ]
    with_tools = build_request_body(system_text="", messages=history, tools=[tool], route=route, profile=profile)
    ctx.check(f"reasoning_content replayed with tools present, got {with_tools['messages']}",
              any(m.get("reasoning_content") == "prior thought" for m in with_tools["messages"] if m.get("role") == "assistant"))
    without_tools = build_request_body(system_text="", messages=history, tools=None, route=route, profile=profile)
    ctx.check("no reasoning_content replayed without tools",
              not any("reasoning_content" in m for m in without_tools["messages"] if m.get("role") == "assistant"))
    replayed_msg = next(m for m in with_tools["messages"] if m.get("role") == "assistant")
    ctx.check(f"only allowlisted keys on the replayed proto, got {sorted(replayed_msg)}",
              set(replayed_msg.keys()) <= {"role", "content", "reasoning_content"})


@test
def test_v2a_gpt_mlflow_reasoning_blocks_decoded_and_displayed(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        events = list(stream_completion(_dbx_req(mock, "databricks-gpt-5-reasoning-blocks-shape")))
        meta = _harness_meta_from(events)
        texts = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"GPT reasoning-blocks decoded, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "block reasoning text")
        ctx.check(f"displayed text excludes reasoning, got {texts}", "".join(texts) == "final answer")
    finally:
        mock.stop()


@test
def test_v2a_grok_mlflow_reasoning_blocks_decoded_and_displayed(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        events = list(stream_completion(_dbx_req(mock, "databricks-grok-4-6-reasoning-blocks-shape")))
        meta = _harness_meta_from(events)
        ctx.check(f"Grok reasoning-blocks decoded, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "block reasoning text")
    finally:
        mock.stop()


@test
def test_v2a_gemini_mlflow_reasoning_blocks_decoded_and_displayed(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        events = list(stream_completion(_dbx_req(mock, "databricks-gemini-3-1-pro-reasoning-blocks-shape")))
        meta = _harness_meta_from(events)
        ctx.check(f"Gemini reasoning-blocks decoded, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "block reasoning text")
    finally:
        mock.stop()


@test
def test_v2a_gpt_oss_mlflow_reasoning_content_shape(ctx: Ctx):
    """gpt-oss's own Harmony analysis channel has no dedicated parser (an
    intentional, documented no-op -- providers/errors.py's `run_code_branch`)
    -- on Databricks it rides the plain `reasoning_content` field like
    DeepSeek/Kimi/GLM (model_table.json's own `reasoning_field` note)."""
    mock = MockDatabricks().start()
    try:
        events = list(stream_completion(_dbx_req(mock, "databricks-gpt-oss-120b-reasoning-content-shape")))
        meta = _harness_meta_from(events)
        ctx.check(f"gpt-oss reasoning_content decoded, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "thinking step 1 step 2")
    finally:
        mock.stop()


@test
def test_v2a_kimi_mlflow_verbatim_native_tool_id_preserved(ctx: Ctx):
    """Kimi's own `tool_id_format: "kimi_functions_idx"` row (model_table.
    json) only ever RENAMES an id that doesn't already match its native
    `functions.<name>:<idx>` shape -- an id the upstream already sent in
    that exact shape must round-trip byte-for-byte, never re-minted with a
    fresh counter value. This is the mlflow-dialect twin of Kimi's own
    anthropic-gateway id test -- the actual transform only ever runs on
    this (Kimi's DEFAULT) dialect; the anthropic profile never reads
    tool_id_format from the row at all."""
    mock = MockDatabricks().start()
    try:
        # A discovery-cache entry so the WIRE model (foundation_model_name)
        # carries the scenario suffix while route.upstream_model stays the
        # real "databricks-kimi-k3" endpoint name -- resolve_profile must
        # match the REAL model_table.json row (keyed by endpoint name), not
        # a synthetic one, to get the real kimi_functions_idx format.
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-kimi-mlflow-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "databricks-kimi-k3", "foundation_model_name": "kimi-k3-tool-call-kimi-native-id",
             "task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions"]},
        ])
        reset_model_table_cache()
        route = Route(provider="databricks", upstream_model="databricks-kimi-k3", dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check(f"kimi's row resolves kimi_functions_idx, got {profile.tool_id_format!r}",
                  profile.tool_id_format == "kimi_functions_idx")
        body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                   route=route, profile=profile)
        req = CompletionRequest(
            body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
            creds=ProviderCreds(base_url=mock.root, api_key="tok"), state_dir=state_dir, extra_headers={},
            model_label="databricks-kimi-k3", harness_mode=True,
            tool_id_format=profile.tool_id_format, prebuilt_oai_body=body,
        )
        events = list(stream_completion(req))
        starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
        ctx.check(f"Kimi's native id preserved verbatim, got {starts}",
                  starts and starts[0]["content_block"]["id"] == "functions.Read:0")
    finally:
        mock.stop()


@test
def test_v2a_kimi_mlflow_sampling_params_omitted(ctx: Ctx):
    """Kimi K2.x-K3 fix temperature/top_p server-side -- neither key may
    ever reach the wire (profile.use_temperature is False AND both names are
    also listed in sampling_unsupported_params as a second guard)."""
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-kimi-k3", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    ctx.check(f"no temperature/top_p on Kimi's wire body, got {sorted(body)}",
              "temperature" not in body and "top_p" not in body)


@test
def test_v2a_glm_mlflow_sends_fixed_temperature_and_top_p(ctx: Ctx):
    """GLM's own row is the opposite of Kimi/DeepSeek: use_temperature is
    True with a FIXED 1.0/0.95 pair -- pinning this so "sampling omitted for
    thinking families" is never over-generalized to every family."""
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-glm-5-3", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    ctx.check(f"GLM sends its fixed temperature/top_p, got {body.get('temperature')}/{body.get('top_p')}",
              body.get("temperature") == 1.0 and body.get("top_p") == 0.95)


@test
def test_v2a_wire_model_is_foundation_model_name_end_to_end(ctx: Ctx):
    """Qwen's endpoint name and its `foundation_model.name` differ in shape
    (`system.ai.qwen35-122b-a10b`) -- the wire body sent to the mock must
    carry the DISCOVERED name, never the endpoint name or a guessed prefix."""
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-mlflow-catalog-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "databricks-qwen35-122b-a10b", "foundation_model_name": "system.ai.qwen35-122b-a10b",
             "task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions"]},
        ])
        reset_model_table_cache()
        route = Route(provider="databricks", upstream_model="databricks-qwen35-122b-a10b", dialect="openai-chat")
        profile = resolve_profile(route)
        body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                   route=route, profile=profile)
        req = CompletionRequest(
            body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
            creds=ProviderCreds(base_url=mock.root, api_key="tok"), state_dir=state_dir, extra_headers={},
            model_label="databricks-qwen35-122b-a10b", harness_mode=True, prebuilt_oai_body=body,
        )
        list(stream_completion(req))
        sent_model = mock.requests[-1]["body"].get("model")
        ctx.check(f"wire model is the foundation_model_name, got {sent_model!r}", sent_model == "system.ai.qwen35-122b-a10b")
        ctx.check(f"hit the mlflow path, got {mock.requests[-1]['path']!r}",
                  "/ai-gateway/mlflow/v1/chat/completions" in mock.requests[-1]["path"])
    finally:
        mock.stop()


@test
def test_v2a_stream_true_explicit_on_every_mlflow_request(ctx: Ctx):
    reset_model_table_cache()
    for model in ("databricks-deepseek-v4-1-flash", "databricks-glm-5-3", "databricks-gpt-5"):
        route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
        profile = resolve_profile(route)
        body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                   route=route, profile=profile)
        ctx.check(f"{model}: stream=True explicit, got {body.get('stream')!r}", body.get("stream") is True)


@test
def test_v2a_mlflow_32_tool_cap_and_schema_simplifier(ctx: Ctx):
    from rolo_claude.providers.request import ToolCatalogTooLarge
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-glm-5-3", dialect="openai-chat")
    profile = resolve_profile(route)
    too_many = [{"name": f"t{i}", "description": "d", "input_schema": {"type": "object", "properties": {}}}
                for i in range(33)]
    try:
        convert_tools(too_many, profile)
        ctx.check("33 tools must raise ToolCatalogTooLarge on Databricks (cap 32)", False)
    except ToolCatalogTooLarge as e:
        ctx.check(f"cap reported as 32, got {e.limit}", e.limit == 32)
    fancy = [{"name": "Grep", "description": "d", "input_schema": {
        "type": "object", "properties": {"pattern": {"type": "string"}, "extra": {"$ref": "#/$defs/x"}},
        "$defs": {"x": {"type": "string"}}, "required": ["pattern"]}}]
    out = convert_tools(fancy, profile)
    params = out[0]["function"]["parameters"]
    ctx.check(f"pattern PROPERTY survives (finding 13), got {params}", "pattern" in params.get("properties", {}))
    ctx.check("$ref replaced with a permissive object", params["properties"]["extra"] == {"type": "object"})
    ctx.check("$defs keyword stripped at the schema level", "$defs" not in params)


@test
def test_v2a_otpm_pre_admission_uses_kimis_real_rate_limits(ctx: Ctx):
    """Kimi's own published OTPM (40,000/60s, model_table.json) pre-admits
    `max_tokens` against a rolling window of actually-spent output tokens --
    a near-exhausted window must clamp the NEXT request's budget down."""
    reset_model_table_cache()
    reset_databricks_otpm_history()
    try:
        route = Route(provider="databricks", upstream_model="databricks-kimi-k3", dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check("kimi's real otpm loaded from model_table.json",
                  profile.databricks_rate_limits == {"itpm": 2000000, "otpm": 40000, "qph": 7200})
        record_databricks_output_tokens("databricks-kimi-k3", 39000)
        budget = max_tokens_budget(profile, model_key="databricks-kimi-k3", requested=40000)
        ctx.check(f"budget clamped well under the cap after near-exhausting otpm, got {budget}", budget < 2000)
    finally:
        reset_databricks_otpm_history()


@test
def test_v2a_qwen_llama_gemma_mlflow_never_send_reasoning_effort(ctx: Ctx):
    """None of these three rows support server-side reasoning effort at all
    (`reasoning_effort_supported: false`) -- `--effort` must never add a
    `reasoning_effort` field for them, unlike the thinking families."""
    reset_model_table_cache()
    for model in ("databricks-qwen35-122b-a10b", "databricks-llama-4-maverick", "databricks-gemma-3-12b"):
        route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check(f"{model}: reasoning_effort_supported is False", profile.reasoning_effort_supported is False)
        ctx.check(f"{model}: --effort high adds nothing, got {map_effort('high', profile)}",
                  map_effort("high", profile) == {})


@test
def test_v2a_reasoning_replay_bug_wording_detected_per_type(ctx: Ctx):
    """DeepSeek's exact 400 wording when this harness fails to replay
    reasoning_content -- classify_error_category must name it PROVIDER_
    FAILURE (never silently retried as an ordinary transient error)."""
    from rolo_claude.providers.errors import classify_error_category, is_reasoning_replay_bug, PROVIDER_FAILURE
    message = "The reasoning_content in the thinking mode must be passed back to the API."
    ctx.check("detected as a reasoning-replay bug", is_reasoning_replay_bug(message) is True)
    ctx.check(f"classified PROVIDER_FAILURE, got {classify_error_category(400, message)}",
              classify_error_category(400, message) == PROVIDER_FAILURE)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
