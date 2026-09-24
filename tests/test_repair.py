"""tests.test_repair -- agent/repair.py (H2 scope B): unknown-name
normalization + difflib rename, schema validate/coerce, duplicate-call
detection, and text-embedded-call promotion (via the SAME
providers.hooks.leak_parser this module wraps, not a duplicate) for every
leak format the brief names: DSML, Hermes, Qwen3-Coder XML, Kimi, GLM,
MiniMax, and fenced JSON.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent import repair
from rolo_claude.tools.registry import ToolRegistry

test, TESTS = new_registry()


class _FakeProfile:
    def __init__(self, patterns):
        self.tool_leak_patterns = patterns


# ---- name normalization / difflib rename -----------------------------

@test
def test_resolve_exact_name(ctx: Ctx):
    reg = ToolRegistry()
    resolved, close = repair.resolve_tool_name("Read", reg.names())
    ctx.check("exact match resolves directly", resolved == "Read")
    ctx.check("no suggestions needed", close == [])


@test
def test_resolve_alias_table(ctx: Ctx):
    reg = ToolRegistry()
    resolved, _ = repair.resolve_tool_name("read_file", reg.names())
    ctx.check(f"read_file -> Read via alias table, got {resolved!r}", resolved == "Read")


@test
def test_resolve_case_insensitive(ctx: Ctx):
    reg = ToolRegistry()
    resolved, _ = repair.resolve_tool_name("read", reg.names())
    ctx.check(f"case-insensitive exact match, got {resolved!r}", resolved == "Read")


@test
def test_resolve_difflib_close_match(ctx: Ctx):
    reg = ToolRegistry()
    # "Reaad" vs "Read": SequenceMatcher ratio 0.888 -- clears the brief's
    # own ">= 0.85" bar; a 0.75-ratio typo ("Reed") deliberately must NOT
    # auto-rename (see test_resolve_below_threshold_does_not_rename).
    resolved, _ = repair.resolve_tool_name("Reaad", reg.names())
    ctx.check(f"difflib auto-renames a close-enough typo, got {resolved!r}", resolved == "Read")


@test
def test_resolve_below_threshold_does_not_rename(ctx: Ctx):
    reg = ToolRegistry()
    # "Reed" vs "Read": ratio 0.75, BELOW the >= 0.85 bar -- must NOT
    # silently auto-rename to a tool the model didn't clearly mean.
    resolved, close = repair.resolve_tool_name("Reed", reg.names())
    ctx.check(f"below-threshold typo is not auto-renamed, got {resolved!r}", resolved is None)
    ctx.check("candidates still offered for the error message", "Read" in close)


@test
def test_resolve_unknown_name_lists_5_closest(ctx: Ctx):
    reg = ToolRegistry()
    resolved, close = repair.resolve_tool_name("CompletelyUnrelatedXyz123", reg.names())
    ctx.check("no resolution for a wildly unrelated name", resolved is None)
    ctx.check(f"up to 5 candidates offered, got {close}", 0 < len(close) <= 5)


# ---- finding 2: mcp__ names are never fuzzy-renamed ------------------

class _FakeCatalog:
    """Minimal SessionCatalog double: `.deferred` + a `.load()` that
    behaves like the real one enough for repair.py's own logic (pop on
    success, report only genuinely-loaded names)."""
    def __init__(self, deferred: dict):
        self.deferred = dict(deferred)

    def load(self, names):
        loaded = []
        for n in names:
            if n in self.deferred:
                del self.deferred[n]
                loaded.append(n)
        return loaded


@test
def test_mcp_name_never_fuzzy_renamed_even_one_edit_away(ctx: Ctx):
    """finding 2's exact repro: mcp__hardware__hw_clock_stop must NEVER
    resolve to mcp__hardware__hw_clock_start just because they're one
    difflib edit apart -- that dispatches the WRONG tool with the
    model's original arguments."""
    known = ["Bash", "mcp__hardware__hw_clock_start"]
    resolved, close = repair.resolve_tool_name("mcp__hardware__hw_clock_stop", known)
    ctx.check(f"never silently renamed to a sibling mcp__ tool, got {resolved!r}", resolved is None)
    ctx.check("no fuzzy suggestions offered for an mcp__ name either", close == [])


@test
def test_finding_13_mangled_mcp_prefix_never_fuzzy_renamed_either(ctx: Ctx):
    """finding 13: the guard used to check only a RAW `mcp__` prefix, so
    `mcp_hardware__hw_clock_stop` (single underscore) fell through to
    difflib and WAS auto-renamed to the sibling `mcp__hardware__
    hw_clock_start` -- verified in the review. Every mangled spelling
    below (single underscore, wrong case, hyphen) must be normalised to
    "mcp__" and then follow the EXACT same never-fuzzy-rename rule as an
    already-correct mcp__ name."""
    known = ["Bash", "mcp__hardware__hw_clock_start"]
    for mangled in ("mcp_hardware__hw_clock_stop", "MCP__hardware__hw_clock_stop",
                     "mcp-hardware__hw_clock_stop", "Mcp_hardware__hw_clock_stop"):
        resolved, close = repair.resolve_tool_name(mangled, known)
        ctx.check(f"{mangled!r} never silently renamed to a sibling mcp__ tool, got {resolved!r}", resolved is None)
        ctx.check(f"{mangled!r}: no fuzzy suggestions either", close == [])


@test
def test_finding_13_mangled_mcp_prefix_resolves_an_exact_match(ctx: Ctx):
    """The flip side: a mangled prefix must still resolve when the
    CANONICAL name is actually known/loaded -- normalising the prefix
    must not turn a real, resolvable call into a false negative."""
    known = ["Bash", "mcp__hardware__hw_ports"]
    resolved, close = repair.resolve_tool_name("mcp_hardware__hw_ports", known)
    ctx.check(f"resolves via the canonical mcp__ spelling, got {resolved!r}", resolved == "mcp__hardware__hw_ports")
    ctx.check("no fuzzy suggestions needed for a real match", close == [])


@test
def test_finding_13_mangled_prefix_toolsearch_hint_uses_canonical_name(ctx: Ctx):
    """The ToolSearch hint text must name the CANONICAL "select:" query
    (mcp__...), never the model's own mangled spelling -- a retry with the
    mangled name would just fail the same way again."""
    reg = ToolRegistry()
    outcome = repair.repair_tool_use_block({"id": "c1", "name": "mcp_ghost__nope", "input": {}}, reg)
    ctx.check("not ok", outcome.ok is False)
    ctx.check(f"hint uses the canonical mcp__ name, got {outcome.error_text!r}",
              "select:mcp__ghost__nope" in outcome.error_text)


@test
def test_mcp_name_auto_loads_from_catalog_deferred(ctx: Ctx):
    reg = ToolRegistry()
    cat = _FakeCatalog({"mcp__srv__get_x": ("srv", object())})
    resolved, close = repair.resolve_tool_name("mcp__srv__get_x", reg.names(), catalog=cat, registry=reg)
    ctx.check(f"resolves by loading from the deferred pool, got {resolved!r}", resolved == "mcp__srv__get_x")
    ctx.check("actually removed from the deferred pool (loaded, not just peeked)",
              "mcp__srv__get_x" not in cat.deferred)


@test
def test_mcp_name_already_loaded_by_an_earlier_block_this_turn(ctx: Ctx):
    """`known_names` is a snapshot taken once per turn -- a name another
    block already auto-loaded into `registry` (mutated in place) earlier
    in the SAME turn must still resolve, even though the stale snapshot
    doesn't contain it and it's no longer in `catalog.deferred` either."""
    from rolo_claude.tools.read import ReadTool
    reg = ToolRegistry(tools=[ReadTool()])
    stale_names = reg.names()  # snapshot BEFORE the tool gets added
    reg.add_tool(ReadTool())  # stand-in for an mcp__ tool another block just loaded
    reg._tools["mcp__srv__get_x"] = reg._tools.pop("Read")  # rename the stand-in for this test's purposes
    resolved, close = repair.resolve_tool_name("mcp__srv__get_x", stale_names, catalog=None, registry=reg)
    ctx.check(f"resolves via the LIVE registry, not the stale snapshot, got {resolved!r}",
              resolved == "mcp__srv__get_x")


@test
def test_mcp_name_unresolvable_gets_a_toolsearch_hint_not_a_guess(ctx: Ctx):
    reg = ToolRegistry()
    outcome = repair.repair_tool_use_block({"id": "c1", "name": "mcp__ghost__nope", "input": {}}, reg)
    ctx.check("not ok", outcome.ok is False)
    ctx.check(f"error points at ToolSearch, never a guessed rename, got {outcome.error_text!r}",
              "ToolSearch" in outcome.error_text and "select:mcp__ghost__nope" in outcome.error_text)


@test
def test_repaired_block_records_original_name_for_hook_matchers(ctx: Ctx):
    """H4 must-do: a hook matcher needs the model's ORIGINAL spelling, not
    the repaired one -- a plain rename (non-mcp__) sets it; an unrenamed
    exact match does not."""
    reg = ToolRegistry()
    renamed = repair.repair_tool_use_block({"id": "c1", "name": "read_file", "input": {"file_path": "/x"}}, reg)
    ctx.check(f"original_name captures the model's own spelling, got {renamed.original_name!r}",
              renamed.original_name == "read_file")
    exact = repair.repair_tool_use_block({"id": "c2", "name": "Read", "input": {"file_path": "/x"}}, reg)
    ctx.check("no rename -> original_name stays None", exact.original_name is None)


# ---- schema validate/coerce --------------------------------------------

@test
def test_schema_missing_required_param(ctx: Ctx):
    schema = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    coerced, errors = repair.validate_and_coerce({}, schema)
    ctx.check(f"missing required flagged, got {errors}", any("file_path" in e for e in errors))


@test
def test_schema_coerces_numeric_string_to_integer(ctx: Ctx):
    schema = {"type": "object", "properties": {"offset": {"type": "integer"}}, "required": []}
    coerced, errors = repair.validate_and_coerce({"offset": "5"}, schema)
    ctx.check("no errors after coercion", errors == [])
    ctx.check(f"offset coerced to int, got {coerced}", coerced["offset"] == 5)


@test
def test_schema_coerces_bool_string(ctx: Ctx):
    schema = {"type": "object", "properties": {"replace_all": {"type": "boolean"}}, "required": []}
    coerced, errors = repair.validate_and_coerce({"replace_all": "true"}, schema)
    ctx.check("no errors", errors == [])
    ctx.check(f"coerced to real bool True, got {coerced}", coerced["replace_all"] is True)


@test
def test_schema_rejects_bool_where_number_expected(ctx: Ctx):
    schema = {"type": "object", "properties": {"offset": {"type": "integer"}}, "required": []}
    coerced, errors = repair.validate_and_coerce({"offset": True}, schema)
    ctx.check(f"a real bool never satisfies an integer field, got {errors}", len(errors) == 1)


@test
def test_schema_enum_violation(ctx: Ctx):
    schema = {"type": "object", "properties": {"output_mode": {"type": "string", "enum": ["content", "count"]}}, "required": []}
    coerced, errors = repair.validate_and_coerce({"output_mode": "bogus"}, schema)
    ctx.check(f"enum violation flagged, got {errors}", any("one of" in e for e in errors))


@test
def test_schema_valid_input_passes_clean(ctx: Ctx):
    schema = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    coerced, errors = repair.validate_and_coerce({"file_path": "/x.txt"}, schema)
    ctx.check("no errors", errors == [])
    ctx.check("value unchanged", coerced["file_path"] == "/x.txt")


# ---- duplicate detection ---------------------------------------------

@test
def test_duplicate_calls_detected_property_order_ignored(ctx: Ctx):
    blocks = [
        {"id": "c1", "name": "Read", "input": {"file_path": "/a", "offset": 0}},
        {"id": "c2", "name": "Read", "input": {"offset": 0, "file_path": "/a"}},  # same args, different key order
        {"id": "c3", "name": "Read", "input": {"file_path": "/b"}},
    ]
    dupes = repair.find_duplicate_calls(blocks)
    ctx.check(f"c2 flagged as a duplicate of c1, got {dupes}", dupes.get("c2") == "c1")
    ctx.check("c3 (different args) is not a duplicate", "c3" not in dupes)


@test
def test_repair_assistant_turn_marks_duplicate_without_touching_registry(ctx: Ctx):
    reg = ToolRegistry()
    blocks = [
        {"id": "c1", "name": "Bash", "input": {"command": "echo hi"}},
        {"id": "c2", "name": "Bash", "input": {"command": "echo hi"}},
    ]
    outcomes = repair.repair_assistant_turn(blocks, reg)
    ctx.check("first call ok", outcomes[0].ok is True)
    ctx.check("second call flagged as a duplicate", outcomes[1].duplicate_of == "c1")
    ctx.check("duplicate error text references the id", "c1" in outcomes[1].error_text)


# ---- text-embedded call promotion (wraps providers.hooks.leak_parser) ----

@test
def test_extract_leaked_call_hermes(ctx: Ctx):
    text = 'Sure.\n<tool_call>{"name": "Read", "arguments": {"file_path": "/x.txt"}}</tool_call>'
    result = repair.extract_leaked_call(text, _FakeProfile(("hermes_tool_call",)))
    ctx.check(f"hermes call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/x.txt"}})


@test
def test_extract_leaked_call_dsml(ctx: Ctx):
    text = ('<｜DSML｜tool_calls>\n<｜DSML｜invoke name="Read">\n'
            '<｜DSML｜parameter name="file_path" string="true">/y.txt</｜DSML｜parameter>\n'
            '</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
    result = repair.extract_leaked_call(text, _FakeProfile(("dsml",)))
    ctx.check(f"DSML call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/y.txt"}})


@test
def test_extract_leaked_call_qwen3_coder_xml(ctx: Ctx):
    text = "<tool_call><function=Read><parameter=file_path>/a.txt</parameter></function></tool_call>"
    result = repair.extract_leaked_call(text, _FakeProfile(("qwen3_coder_xml",)))
    ctx.check(f"Qwen3-Coder XML call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/a.txt"}})


@test
def test_extract_leaked_call_kimi(ctx: Ctx):
    text = '<|tool_call_begin|>Read:0<|tool_call_argument_begin|>{"file_path": "/k.txt"}<|tool_call_end|>'
    result = repair.extract_leaked_call(text, _FakeProfile(("kimi_section_tokens",)))
    ctx.check(f"Kimi call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/k.txt"}})


@test
def test_extract_leaked_call_glm(ctx: Ctx):
    text = "<tool_call>Read<arg_key>file_path</arg_key><arg_value>/g.txt</arg_value></tool_call>"
    result = repair.extract_leaked_call(text, _FakeProfile(("glm_arg_key",)))
    ctx.check(f"GLM call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/g.txt"}})


@test
def test_extract_leaked_call_minimax(ctx: Ctx):
    text = '<minimax:tool_call><invoke name="Read"><parameter name="file_path">/m.txt</parameter></invoke></minimax:tool_call>'
    result = repair.extract_leaked_call(text, _FakeProfile(("minimax_invoke_xml",)))
    ctx.check(f"MiniMax call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/m.txt"}})


@test
def test_extract_leaked_call_fenced_json(ctx: Ctx):
    text = 'Calling it:\n```json\n{"name": "Read", "arguments": {"file_path": "/f.txt"}}\n```'
    result = repair.extract_leaked_call(text, _FakeProfile(("json_text_call",)))
    ctx.check(f"fenced JSON call extracted, got {result}", result == {"name": "Read", "arguments": {"file_path": "/f.txt"}})


@test
def test_extract_leaked_call_fenced_json_input_key(ctx: Ctx):
    text = '```json\n{"name": "Read", "input": {"file_path": "/i.txt"}}\n```'
    result = repair.extract_leaked_call(text, _FakeProfile(("name_json_text",)))
    ctx.check(f"fenced JSON with input= key also works, got {result}", result == {"name": "Read", "arguments": {"file_path": "/i.txt"}})


@test
def test_extract_leaked_call_returns_none_without_a_match(ctx: Ctx):
    result = repair.extract_leaked_call("just plain prose, no tool call here", _FakeProfile(("hermes_tool_call",)))
    ctx.check("no match -> None", result is None)


@test
def test_extract_leaked_call_no_patterns_configured_returns_none(ctx: Ctx):
    result = repair.extract_leaked_call('<tool_call>{"name": "Read", "arguments": {}}</tool_call>', _FakeProfile(()))
    ctx.check("empty tool_leak_patterns -> never tries anything", result is None)


# ---- repair_tool_use_block end to end ----------------------------------

@test
def test_repair_tool_use_block_renames_and_coerces(ctx: Ctx):
    reg = ToolRegistry()
    block = {"id": "c1", "name": "read_file", "input": {"file_path": "/x.txt", "offset": "3"}}
    outcome = repair.repair_tool_use_block(block, reg)
    ctx.check(f"resolved ok, got {outcome.error_text}", outcome.ok is True)
    ctx.check("name renamed to Read", outcome.block["name"] == "Read")
    ctx.check("offset coerced to int", outcome.block["input"]["offset"] == 3)
    ctx.check("outcome flagged as repaired", outcome.repaired is True)


@test
def test_repair_tool_use_block_schema_error_names_missing_param(ctx: Ctx):
    reg = ToolRegistry()
    block = {"id": "c1", "name": "Read", "input": {}}
    outcome = repair.repair_tool_use_block(block, reg)
    ctx.check("missing required file_path -> not ok", outcome.ok is False)
    ctx.check(f"error names the missing param, got {outcome.error_text!r}", "file_path" in outcome.error_text)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
