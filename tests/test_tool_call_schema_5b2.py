"""tests.test_tool_call_schema_5b2 -- Halo 2.0.3 round 5b part 2 (brief
item 2, repair only after the 2026-10-04 fix pass): pure, no-network unit
tests for `providers.tool_call_schema` -- the constrained-output schema
shapes and the gating predicate the repair round uses.

Fix pass: `expected_to_call_tool` (the per-turn "should this ordinary turn
be constrained" rule) is DELETED along with its own tests below -- a live
run proved forcing the tool-call shape onto every post-tool-result turn
left the model unable to ever just answer in prose, looping for 25 minutes
once it ran out of anything useful to call. See `providers/tool_call_
schema.py`'s own module docstring and `docs/MODELS.md`'s "Reliable tool
calls" section for the corrected rule: constrained decoding now only ever
repairs an ALREADY-ATTEMPTED, already-failed call.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- tool_call_output_schema / openai_response_format_for_schema -------

@test
def test_tool_call_output_schema_shape(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import tool_call_output_schema
    schema = tool_call_output_schema(["Read", "Write"])
    ctx.check(f"required name+arguments, got {schema.get('required')!r}",
              schema.get("required") == ["name", "arguments"])
    ctx.check(f"name enum carries the tool names, got {schema['properties']['name'].get('enum')!r}",
              schema["properties"]["name"].get("enum") == ["Read", "Write"])
    ctx.check(f"arguments is a bare object (no per-tool sub-schema), got {schema['properties']['arguments']!r}",
              schema["properties"]["arguments"] == {"type": "object"})


@test
def test_tool_call_output_schema_empty_names_omits_enum(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import tool_call_output_schema
    schema = tool_call_output_schema([])
    ctx.check("no enum key when no names given", "enum" not in schema["properties"]["name"])


@test
def test_openai_response_format_shape(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import openai_response_format_for_schema, tool_call_output_schema
    schema = tool_call_output_schema(["Read"])
    rf = openai_response_format_for_schema(schema)
    ctx.check(f"type json_schema, got {rf.get('type')!r}", rf.get("type") == "json_schema")
    ctx.check(f"strict is False, got {rf['json_schema'].get('strict')!r}", rf["json_schema"].get("strict") is False)
    ctx.check(f"schema carried verbatim, got {rf['json_schema'].get('schema')!r}",
              rf["json_schema"].get("schema") == schema)


# ---- supports_constrained_tool_calls -----------------------------------

@test
def test_supports_constrained_ollama_local_only(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import supports_constrained_tool_calls
    ctx.check("ollama local -> True",
              supports_constrained_tool_calls(provider="ollama", dialect="ollama", local=True) is True)
    ctx.check("ollama cloud (not local) -> False",
              supports_constrained_tool_calls(provider="ollama", dialect="ollama", local=False) is False)


@test
def test_supports_constrained_huggingface_local_only(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import supports_constrained_tool_calls
    ctx.check("hf:local -> True",
              supports_constrained_tool_calls(provider="huggingface", dialect="openai-chat", local=True) is True)
    ctx.check("hf: router/endpoint (not local) -> False",
              supports_constrained_tool_calls(provider="huggingface", dialect="openai-chat", local=False) is False)


@test
def test_supports_constrained_every_other_dialect_false(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import supports_constrained_tool_calls
    ctx.check("databricks openai-chat -> False",
              supports_constrained_tool_calls(provider="databricks", dialect="openai-chat", local=True) is False)
    ctx.check("openrouter openai-chat -> False",
              supports_constrained_tool_calls(provider="openrouter", dialect="openai-chat", local=True) is False)
    ctx.check("anthropic-passthrough -> False",
              supports_constrained_tool_calls(provider="anthropic", dialect="anthropic-passthrough", local=True) is False)


# ---- repair_prompt_for ---------------------------------------------------

@test
def test_repair_prompt_for_includes_schema_error_and_raw_input(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import repair_prompt_for
    schema = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    system_text, user_text = repair_prompt_for(
        tool_name="Read", schema=schema, error_message="missing required parameter 'file_path'",
        raw_input="{bad json")
    ctx.check("system text asks for JSON only", "JSON object" in system_text)
    ctx.check("user text names the tool", "Read" in user_text)
    ctx.check("user text carries the error", "missing required parameter" in user_text)
    ctx.check("user text carries the schema", "file_path" in user_text)
    ctx.check("user text carries the raw (malformed) input", "bad json" in user_text)


@test
def test_repair_prompt_for_handles_no_schema(ctx: Ctx):
    from halo_harness.providers.tool_call_schema import repair_prompt_for
    system_text, user_text = repair_prompt_for(tool_name="Unknown", schema=None, error_message="unknown tool",
                                                 raw_input={})
    ctx.check("never raises with schema=None", "no schema available" in user_text)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
