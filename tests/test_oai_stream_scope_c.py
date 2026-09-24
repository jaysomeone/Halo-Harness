"""tests.test_oai_stream_scope_c -- providers/oai_stream.py scope C
additions, unit-level (no server needed): finish_reason length with <=1
token -> length_with_minimal_output; length-truncated tool call vs
malformed JSON tagging (strict_tool_json); strip_display_artifacts;
map_usage's additive keys; capture_reasoning is opt-in only (proxy default
False is completely unaffected).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.providers.oai_stream import OpenAIStreamToAnthropic, map_usage, strip_display_artifacts

test, TESTS = new_registry()


@test
def test_length_with_minimal_output_flagged(ctx: Ctx):
    sm = OpenAIStreamToAnthropic("m", 10, capture_reasoning=True, strict_tool_json=True)
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"content": "x"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                   "usage": {"prompt_tokens": 5000, "completion_tokens": 1}})
    sm.on_eof()
    ctx.check("length with 1 output token flagged for re-route", sm.length_with_minimal_output is True)


@test
def test_length_with_normal_output_not_flagged(ctx: Ctx):
    sm = OpenAIStreamToAnthropic("m", 10, capture_reasoning=True, strict_tool_json=True)
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"content": "a normal length reply"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                   "usage": {"prompt_tokens": 5000, "completion_tokens": 500}})
    sm.on_eof()
    ctx.check("a normal-length length-cutoff is NOT flagged as a provider failure", sm.length_with_minimal_output is False)


@test
def test_length_truncated_tool_call_tagged_when_strict(ctx: Ctx):
    sm = OpenAIStreamToAnthropic("m", 10, strict_tool_json=True)
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "some/very/lo'}},
    ]}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    events = sm.on_eof()
    starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
    ctx.check("the truncated call is KEPT (strict mode), not dropped", len(starts) == 1)
    tool_id = starts[0]["content_block"]["id"]
    ctx.check("tagged truncated_by_length", sm.tool_call_flags.get(tool_id, {}).get("truncated_by_length") is True)
    ctx.check("raw partial input preserved for the loop to inspect", "some/very/lo" in sm.tool_call_flags[tool_id].get("raw_input", ""))


@test
def test_length_truncated_tool_call_dropped_when_not_strict(ctx: Ctx):
    """The proxy's OWN default (strict_tool_json=False) must be completely
    unchanged: the truncated call is DROPPED, exactly like before scope C."""
    sm = OpenAIStreamToAnthropic("m", 10)  # strict_tool_json defaults False
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "some/very/lo'}},
    ]}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    events = sm.on_eof()
    starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
    ctx.check("proxy default: the truncated call is dropped, not kept", len(starts) == 0)
    ctx.check("no tool_call_flags populated when strict_tool_json is off", sm.tool_call_flags == {})


@test
def test_malformed_json_tagged_distinctly_from_truncation(ctx: Ctx):
    sm = OpenAIStreamToAnthropic("m", 10, strict_tool_json=True)
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": }}'}},
    ]}}]})
    sm.feed_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})  # NOT "length" -- genuinely malformed
    events = sm.on_eof()
    starts = [e for e in events if e.get("type") == "content_block_start" and e["content_block"].get("type") == "tool_use"]
    tool_id = starts[0]["content_block"]["id"]
    flags = sm.tool_call_flags.get(tool_id, {})
    ctx.check("malformed (non-truncated) JSON tagged malformed_json", flags.get("malformed_json") is True)
    ctx.check("NOT tagged as a length truncation", "truncated_by_length" not in flags)


@test
def test_strip_display_artifacts(ctx: Ctx):
    text = "<think>internal monologue</think>the real answer"
    ctx.check("leading <think> block stripped", strip_display_artifacts(text) == "the real answer")
    sentinel = "answer<｜end▁of▁sentence｜> trailing junk"
    ctx.check("sentinel token stripped", "end▁of▁sentence" not in strip_display_artifacts(sentinel))
    ctx.check("ordinary text untouched", strip_display_artifacts("nothing special here") == "nothing special here")
    ctx.check("empty string safe", strip_display_artifacts("") == "")


@test
def test_map_usage_additive_keys(ctx: Ctx):
    basic = map_usage({"prompt_tokens": 10, "completion_tokens": 5})
    ctx.check("basic mapping unaffected", basic == {"input_tokens": 10, "output_tokens": 5})
    extended = map_usage({
        "prompt_tokens": 10, "completion_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": 3},
        "prompt_tokens_details": {"cached_tokens": 2},
        "cost": 0.0012,
    })
    ctx.check("reasoning_tokens captured", extended.get("reasoning_tokens") == 3)
    ctx.check("cache_read_input_tokens captured", extended.get("cache_read_input_tokens") == 2)
    ctx.check("OpenRouter cost captured", extended.get("cost") == 0.0012)


@test
def test_capture_reasoning_opt_in_only(ctx: Ctx):
    sm_default = OpenAIStreamToAnthropic("m", 10)
    ctx.check("capture_reasoning defaults False (proxy path)", sm_default.capture_reasoning is False)
    ctx.check("strict_tool_json defaults False (proxy path)", sm_default.strict_tool_json is False)
    sm_default.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "should not be captured"}}]})
    ctx.check("reasoning_text stays empty without the opt-in", sm_default.reasoning_text == "")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
