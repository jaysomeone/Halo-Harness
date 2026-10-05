"""tests.test_qwen_wire_shapes -- Halo 2.0.2 round 5 (Qwen-at-work brief):
two shapes from the TESTS list not covered elsewhere in this round --
"string arguments" (a tool_use block's `input` replays as REAL JSON via
`json.dumps`, never a Python repr) and "decision-only verdict answer"
(the `openjev-not-chat-model` mock scenario's tools-less branch: a plain
yes/no/choice/score reply, no error, exactly what the `judge` role this
model is routed to actually wants).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks

test, TESTS = new_registry()


@test
def test_tool_use_input_replays_as_real_json_never_python_repr(ctx: Ctx):
    """Research doc (agno#10231): a harness that re-serializes a captured
    tool_use's `input` dict with Python's `str()`/repr reproduces the
    single-quoted `{'city': 'Paris'}` shape Qwen rejects on replay.
    `providers.translate._flatten_messages` must use real `json.dumps`."""
    import json
    from halo_harness.providers.translate import _flatten_messages
    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "x1", "name": "Read",
         "input": {"file_path": "a's \"quoted\" file.txt", "recursive": True, "depth": None}},
    ]}]
    flattened = _flatten_messages(messages, "")
    proto = next(p for p in flattened if p.get("role") == "assistant")
    args_str = proto["tool_calls"][0]["function"]["arguments"]
    ctx.check(f"arguments is a STRING (the wire shape), got {type(args_str).__name__}", isinstance(args_str, str))
    parsed = json.loads(args_str)  # must be real, valid JSON -- raises on a Python-repr shape
    ctx.check(f"round-trips through real json.loads, got {parsed}",
              parsed == {"file_path": "a's \"quoted\" file.txt", "recursive": True, "depth": None})
    ctx.check(f"a JSON boolean/null, never Python True/None literals, got {args_str!r}",
              "true" in args_str and "null" in args_str and "True" not in args_str and "None" not in args_str)


@test
def test_openjev_scenario_answers_plainly_when_no_tools_are_offered(ctx: Ctx):
    """The decision-only verdict shape: a tools-LESS request (the shape
    the `judge` role this model is routed to actually sends) gets a plain
    reply, never an error -- only a tools-BEARING request 400s."""
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
    from halo_harness.providers.request import ToolsNotSupported, build_request_body
    from halo_harness.providers.routing import Route
    from halo_harness.providers.stream import CompletionRequest, ProviderCreds, stream_completion
    reset_model_table_cache()
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="qwen-openjev-verdict-"))
        model = "databricks-openjev-qwen35-4b-openjev-not-chat-model"
        write_dbx_endpoints_json(state_dir, [{"name": model, "task": "llm/v1/chat",
                                               "api_types": ["mlflow/v1/chat/completions"]}])
        route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
        profile = resolve_profile(route)

        body = build_request_body(system_text="s", messages=[{"role": "user", "content": [
            {"type": "text", "text": "is this a cat? yes or no"}]}], tools=None, route=route, profile=profile)
        req = CompletionRequest(body={"messages": []}, route=route,
                                 profile={"context_tokens": 128000, "max_output_tokens": 16384},
                                 creds=ProviderCreds(base_url=mock.root, api_key="tok"), state_dir=state_dir,
                                 extra_headers={}, model_label=model, harness_mode=True, prebuilt_oai_body=body)
        events_seen = list(stream_completion(req))
        texts = [e["delta"]["text"] for e in events_seen
                 if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"a plain verdict reply, no error, got {texts}", "".join(texts) == "yes")

        tool = {"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}
        requests_before = len(mock.requests)
        try:
            build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
                                tools=[tool], route=route, profile=profile)
            ctx.check("ToolsNotSupported was raised for a tools-bearing request", False)
        except ToolsNotSupported:
            ctx.check(f"the tools-bearing half never even reached the wire, got {len(mock.requests)} total requests "
                      f"(was {requests_before} before this attempt)", len(mock.requests) == requests_before)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
