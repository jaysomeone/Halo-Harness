"""tests.test_providers_openai_responses -- Halo 2.0.3 round 5i part 1:
the `openai-responses` dialect's own body builder (providers/responses_
request.py) and SSE decoder (providers/responses_stream.py), unit-level
(no server needed) -- mirrors tests/test_oai_stream_scope_c.py's own
"construct the decoder, feed it fixed input, assert on fields" style.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _route_and_profile(model_id="gpt-6-astra"):
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    route = Route(provider="openai", upstream_model=model_id, dialect="openai-responses")
    return route, resolve_profile(route)


# ---- body builder -------------------------------------------------------

@test
def test_system_text_becomes_instructions_not_an_input_item(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    body = build_openai_responses_body(system_text="be terse", messages=[], route=route, profile=profile)
    ctx.check(f"instructions carries the system text, got {body.get('instructions')!r}",
              body["instructions"] == "be terse")
    ctx.check("no system-role item in input", all(i.get("role") != "system" for i in body["input"]))


@test
def test_store_false_and_no_previous_response_id_always(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    body = build_openai_responses_body(system_text="", messages=[], route=route, profile=profile)
    ctx.check(f"store is False, got {body.get('store')!r}", body.get("store") is False)
    ctx.check("previous_response_id is never sent", "previous_response_id" not in body)


@test
def test_tool_use_and_tool_result_round_trip_items(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "read it"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "hello"}]}]},
    ]
    body = build_openai_responses_body(system_text="", messages=messages, route=route, profile=profile)
    items = body["input"]
    fc = next(i for i in items if i.get("type") == "function_call")
    ctx.check(f"call_id carries the tool_use id, got {fc.get('call_id')!r}", fc["call_id"] == "toolu_1")
    ctx.check(f"name carried through, got {fc.get('name')!r}", fc["name"] == "Read")
    ctx.check(f"arguments is a JSON STRING, got {fc.get('arguments')!r}",
              fc["arguments"] == json.dumps({"file_path": "a.txt"}))
    out = next(i for i in items if i.get("type") == "function_call_output")
    ctx.check(f"function_call_output keyed by the SAME call_id, got {out.get('call_id')!r}",
              out["call_id"] == "toolu_1")
    ctx.check(f"output carries the tool result text, got {out.get('output')!r}", out["output"] == "hello")


@test
def test_tools_flattened_never_nested_under_function_key(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    tools = [{"name": "Read", "description": "reads a file",
              "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}]
    body = build_openai_responses_body(system_text="", messages=[], tools=tools, route=route, profile=profile)
    tool_item = body["tools"][0]
    ctx.check(f"flat type/name/description/parameters, got {tool_item!r}",
              tool_item["type"] == "function" and tool_item["name"] == "Read"
              and tool_item["description"] == "reads a file" and "function" not in tool_item)


@test
def test_tool_choice_required_passes_through(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    tools = [{"name": "Read", "description": "", "input_schema": {"type": "object", "properties": {}}}]
    body = build_openai_responses_body(system_text="", messages=[], tools=tools, tool_choice="required",
                                        route=route, profile=profile)
    ctx.check(f"tool_choice required carried through, got {body.get('tool_choice')!r}",
              body.get("tool_choice") == "required")


@test
def test_reasoning_effort_sent_only_when_given(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    body_none = build_openai_responses_body(system_text="", messages=[], route=route, profile=profile, effort=None)
    ctx.check("no reasoning key when effort is None", "reasoning" not in body_none)
    body_high = build_openai_responses_body(system_text="", messages=[], route=route, profile=profile, effort="high")
    ctx.check(f"reasoning.effort carried through, got {body_high.get('reasoning')!r}",
              body_high.get("reasoning") == {"effort": "high"})


@test
def test_max_output_tokens_set_from_requested(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    body = build_openai_responses_body(system_text="", messages=[], route=route, profile=profile,
                                        requested_max_tokens=1234)
    ctx.check(f"max_output_tokens set, got {body.get('max_output_tokens')!r}", body.get("max_output_tokens") == 1234)


@test
def test_image_part_becomes_a_visible_placeholder_not_silently_dropped(ctx: Ctx):
    from halo_harness.providers.responses_request import build_openai_responses_body
    route, profile = _route_and_profile()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "xx"}},
    ]}]
    body = build_openai_responses_body(system_text="", messages=messages, route=route, profile=profile)
    user_item = next(i for i in body["input"] if i.get("role") == "user")
    ctx.check(f"placeholder text present, got {user_item.get('content')!r}",
              "image omitted" in user_item["content"])


# ---- profile / effort ----------------------------------------------------

@test
def test_profile_effort_values_is_the_seven_tuple(ctx: Ctx):
    _route, profile = _route_and_profile()
    ctx.check(f"thinking_format is its own value, got {profile.thinking_format!r}",
              profile.thinking_format == "openai_responses")
    ctx.check(f"reasoning_replay is empty (display-only), got {profile.reasoning_replay!r}",
              profile.reasoning_replay == "empty")
    ctx.check(f"all seven confirmed values accepted, got {profile.effort_values_supported!r}",
              set(profile.effort_values_supported) == {"none", "minimal", "low", "medium", "high", "xhigh", "max"})


@test
def test_clamp_effort_never_narrows_a_harness_level_on_this_dialect(ctx: Ctx):
    from halo_harness.providers.profiles import clamp_effort
    _route, profile = _route_and_profile()
    for level in ("low", "medium", "high", "xhigh", "max"):
        ctx.check(f"{level} passes through unclamped", clamp_effort(level, profile) == level)


# ---- dialect selection (unit level) ---------------------------------------

@test
def test_resolve_openai_dialect_table_and_override(ctx: Ctx):
    from halo_harness.providers.responses_request import RESPONSES_REQUIRED_MODEL_IDS, resolve_openai_dialect
    ctx.check("exactly the two confirmed ids", RESPONSES_REQUIRED_MODEL_IDS == frozenset({"gpt-6-astra", "gpt-6.1-sol"}))
    ctx.check("table hit -> responses", resolve_openai_dialect("gpt-6-astra", overrides={}) == "openai-responses")
    ctx.check("no hit -> chat", resolve_openai_dialect("gpt-5", overrides={}) == "openai-chat")
    ctx.check("override wins toward chat", resolve_openai_dialect("gpt-6-astra", overrides={"gpt-6-astra": "chat"}) == "openai-chat")
    ctx.check("override wins toward responses", resolve_openai_dialect("gpt-5", overrides={"gpt-5": "responses"}) == "openai-responses")


# ---- SSE decoder -----------------------------------------------------------

def _sse_lines(events: list) -> list:
    out = []
    for ev in events:
        out.append(f"event: {ev['type']}")
        out.append(f"data: {json.dumps(ev)}")
        out.append("")
    return out


@test
def test_decoder_text_scenario(ctx: Ctx):
    from halo_harness.providers.responses_stream import ResponsesStreamToAnthropic
    events_in = [
        {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": "He"},
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": "llo"},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "message", "id": "m1", "role": "assistant",
                  "content": [{"type": "output_text", "text": "Hello"}]}},
        {"type": "response.completed", "response": {"id": "r1", "status": "completed",
                                                      "usage": {"input_tokens": 9, "output_tokens": 2}}},
    ]
    sm = ResponsesStreamToAnthropic("oai:mock", 5)
    events_out = [sm.message_start_event()]
    kind = None
    for line in _sse_lines(events_in):
        step = sm.feed_sse_line(line)
        events_out.extend(step["events"])
        if step["kind"] != "events":
            kind = step["kind"]
    ctx.check(f"terminal kind is done, got {kind!r}", kind == "done")
    text = "".join(e["delta"]["text"] for e in events_out
                   if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta")
    ctx.check(f"text assembled, got {text!r}", text == "Hello")
    ctx.check(f"usage mapped, got {sm.usage!r}", sm.usage == {"input_tokens": 9, "output_tokens": 2})


@test
def test_decoder_streamed_tool_call_scenario(ctx: Ctx):
    from halo_harness.providers.responses_stream import ResponsesStreamToAnthropic
    events_in = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "Read", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc1", "output_index": 0, "delta": '{"file_'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc1", "output_index": 0, "delta": 'path":"a"}'},
        {"type": "response.function_call_arguments.done", "item_id": "fc1", "output_index": 0,
         "arguments": '{"file_path":"a"}'},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "Read",
                  "arguments": '{"file_path":"a"}'}},
        {"type": "response.completed", "response": {"id": "r2", "status": "completed",
                                                      "usage": {"input_tokens": 4, "output_tokens": 3}}},
    ]
    sm = ResponsesStreamToAnthropic("oai:mock", 5)
    events_out = []
    for line in _sse_lines(events_in):
        events_out.extend(sm.feed_sse_line(line)["events"])
    tool_blocks = [e for e in events_out if e.get("type") == "content_block_start"
                   and e["content_block"].get("type") == "tool_use"]
    ctx.check(f"exactly one tool_use block, got {len(tool_blocks)}", len(tool_blocks) == 1)
    block = tool_blocks[0]["content_block"]
    ctx.check(f"id is the upstream call_id, got {block['id']!r}", block["id"] == "call1")
    ctx.check(f"name carried through, got {block['name']!r}", block["name"] == "Read")
    delta = next(e for e in events_out if e.get("type") == "content_block_delta"
                 and e["delta"].get("type") == "input_json_delta")
    ctx.check(f"arguments parsed from the done event's FULL string, got {delta['delta']['partial_json']!r}",
              json.loads(delta["delta"]["partial_json"]) == {"file_path": "a"})
    stop = next(e for e in events_out if e.get("type") == "message_delta")
    ctx.check(f"stop_reason is tool_use, got {stop['delta']['stop_reason']!r}", stop["delta"]["stop_reason"] == "tool_use")


@test
def test_decoder_reasoning_item_carried_as_display_only(ctx: Ctx):
    from halo_harness.providers.responses_stream import ResponsesStreamToAnthropic
    events_in = [
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs1", "summary": []}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "reasoning", "id": "rs1", "summary": [{"type": "summary_text", "text": "step one. step two."}]}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 1, "delta": "done"},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
        {"type": "response.completed", "response": {"id": "r3", "status": "completed",
                                                      "usage": {"input_tokens": 5, "output_tokens": 9,
                                                                "output_tokens_details": {"reasoning_tokens": 6}}}},
    ]
    sm = ResponsesStreamToAnthropic("oai:mock", 5)
    events_out = []
    saw_marker = False
    for line in _sse_lines(events_in):
        step = sm.feed_sse_line(line)
        events_out.extend(step["events"])
        saw_marker = saw_marker or any(e.get("type") == "reasoning_started" for e in step["events"])
    ctx.check("a one-time reasoning_started marker fired", saw_marker)
    ctx.check(f"reasoning text captured for DISPLAY, got {sm.reasoning_text!r}", sm.reasoning_text == "step one. step two.")
    ctx.check(f"reasoning_tokens split out of output_tokens, got {sm.usage!r}",
              sm.usage == {"input_tokens": 5, "reasoning_tokens": 6, "output_tokens": 3})


@test
def test_decoder_mid_stream_error_event(ctx: Ctx):
    from halo_harness.providers.responses_stream import ResponsesStreamToAnthropic
    sm = ResponsesStreamToAnthropic("oai:mock", 5)
    events_in = [{"type": "response.failed", "response": {"id": "r4", "status": "failed",
                                                            "error": {"message": "the model overloaded"}}}]
    # The decoder checks BOTH possible nestings for response.failed's own
    # error (UNCONFIRMED which one a real server uses) -- this fixture
    # exercises the nested-under-"response" shape; the second check below
    # exercises the plain top-level `error` event's own documented shape.
    kind = None
    events_out = []
    for line in _sse_lines(events_in):
        step = sm.feed_sse_line(line)
        events_out.extend(step["events"])
        if step["kind"] != "events":
            kind = step["kind"]
    ctx.check(f"kind is error, got {kind!r}", kind == "error")
    ctx.check(f"message read from the nested response.error, got {events_out!r}",
              events_out and events_out[0]["error"]["message"] == "the model overloaded")

    sm2 = ResponsesStreamToAnthropic("oai:mock", 5)
    events2 = []
    kind2 = None
    for line in _sse_lines([{"type": "error", "error": {"message": "boom"}}]):
        step2 = sm2.feed_sse_line(line)
        events2.extend(step2["events"])
        if step2["kind"] != "events":
            kind2 = step2["kind"]
    ctx.check(f"top-level error event -> one error event with the message, got {events2!r}",
              kind2 == "error" and events2[0]["error"]["message"] == "boom")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
