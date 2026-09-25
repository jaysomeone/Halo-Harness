"""tests.test_mcp_tool -- rolo_claude/tools/mcp_tool.py (H3 scope B):
content conversion (text/image/embedded resource/structuredContent/
isError), the output cap (min(_meta[anthropic/maxResultSizeChars],
MAX_MCP_OUTPUT_TOKENS*4)) + Claude Code's exact truncation string + spill
to tool-results/, image conversion vs the no-vision note, and McpTool
itself (name/is_read_only/always_load/definition/run) through a real fake
MCP server.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools import mcp_tool as T
from rolo_claude.tools.base import ToolContext

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _image(data="QQ==", mime="image/png"):
    return SimpleNamespace(type="image", data=data, mime_type=mime)


def _resource_text(uri, text, mime="text/plain"):
    res = SimpleNamespace(uri=uri, text=text, blob=None, mime_type=mime)
    return SimpleNamespace(type="resource", resource=res)


def _resource_blob(uri, blob="QQ==", mime="image/png"):
    res = SimpleNamespace(uri=uri, text=None, blob=blob, mime_type=mime)
    return SimpleNamespace(type="resource", resource=res)


# ---- content conversion ------------------------------------------------

@test
def test_convert_text_block_passthrough(ctx: Ctx):
    blocks = T.convert_content_blocks([_text("hello")], vision=False)
    ctx.check("text block passes through unchanged", blocks == [{"type": "text", "text": "hello"}])


@test
def test_convert_image_block_with_vision(ctx: Ctx):
    blocks = T.convert_content_blocks([_image(data="QQ==", mime="image/jpeg")], vision=True)
    ctx.check("becomes a real image block", blocks[0]["type"] == "image")
    ctx.check("base64 source shape", blocks[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="})


@test
def test_convert_image_block_without_vision_becomes_note(ctx: Ctx):
    blocks = T.convert_content_blocks([_image(mime="image/png")], vision=False)
    ctx.check("becomes a text note, not an image block", blocks[0]["type"] == "text")
    ctx.check("note mentions no vision support", "no vision support" in blocks[0]["text"])
    ctx.check("mime type mentioned", "image/png" in blocks[0]["text"])


@test
def test_convert_resource_with_text(ctx: Ctx):
    blocks = T.convert_content_blocks([_resource_text("file:///x.txt", "the content")], vision=False)
    ctx.check("resource text embedded", "the content" in blocks[0]["text"])
    ctx.check("uri mentioned", "file:///x.txt" in blocks[0]["text"])


@test
def test_convert_resource_blob_image_with_vision(ctx: Ctx):
    blocks = T.convert_content_blocks([_resource_blob("file:///x.png")], vision=True)
    ctx.check("binary image resource becomes a real image block", blocks[0]["type"] == "image")


@test
def test_h8_convert_image_block_over_byte_limit_is_omitted(ctx: Ctx):
    """H8 scope B: an MCP server's own image result (a screenshot tool, a
    browser snapshot) gets the SAME OpenCode size gate a local file read
    does -- previously convert_content_blocks embedded ANY size image
    as-is with no cap at all."""
    import base64 as _b64
    big = _b64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode("ascii")
    blocks = T.convert_content_blocks([_image(data=big, mime="image/png")], vision=True)
    ctx.check("becomes a text note, not an image block", blocks[0]["type"] == "text")
    ctx.check(f"OpenCode's own omitted wording, got {blocks[0]['text']!r}",
              "could not be resized below the image size limit" in blocks[0]["text"])


@test
def test_h8_convert_resource_blob_image_over_byte_limit_is_omitted(ctx: Ctx):
    import base64 as _b64
    big = _b64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode("ascii")
    blocks = T.convert_content_blocks([_resource_blob("file:///huge.png", blob=big)], vision=True)
    ctx.check("becomes a text note, not an image block", blocks[0]["type"] == "text")


@test
def test_h8_convert_image_block_malformed_base64_falls_back_to_passthrough(ctx: Ctx):
    """A server handing back non-base64 garbage must not crash conversion
    -- falls back to embedding it unchanged (pre-existing behaviour for a
    decode error, unrelated to the SIZE gate this milestone added)."""
    blocks = T.convert_content_blocks([_image(data="not-valid-base64!!!", mime="image/png")], vision=True)
    ctx.check("still becomes an image block (best-effort passthrough)", blocks[0]["type"] == "image")


@test
def test_convert_resource_blob_non_image_becomes_note(ctx: Ctx):
    blob_res = SimpleNamespace(uri="file:///x.bin", text=None, blob="QQ==", mime_type="application/octet-stream")
    blocks = T.convert_content_blocks([SimpleNamespace(type="resource", resource=blob_res)], vision=True)
    ctx.check("non-image binary resource is a text note even with vision", blocks[0]["type"] == "text")


@test
def test_convert_resource_no_text_no_blob(ctx: Ctx):
    res = SimpleNamespace(uri="file:///empty", text=None, blob=None, mime_type=None)
    blocks = T.convert_content_blocks([SimpleNamespace(type="resource", resource=res)], vision=False)
    ctx.check("empty resource becomes a short URI-only note", "file:///empty" in blocks[0]["text"])


@test
def test_convert_unsupported_block_type_is_an_honest_placeholder(ctx: Ctx):
    blocks = T.convert_content_blocks([SimpleNamespace(type="mystery")], vision=False)
    ctx.check("unsupported type becomes a placeholder, never dropped silently", "mystery" in blocks[0]["text"])


@test
def test_convert_empty_content_list(ctx: Ctx):
    ctx.check("empty content -> empty blocks", T.convert_content_blocks([], vision=False) == [])
    ctx.check("None content -> empty blocks", T.convert_content_blocks(None, vision=False) == [])


# ---- structuredContent fallback ---------------------------------------

@test
def test_structured_fallback_used_when_no_text(ctx: Ctx):
    blocks = T.content_with_structured_fallback([], {"a": 1, "b": [1, 2]})
    ctx.check("one text block appended", len(blocks) == 1 and blocks[0]["type"] == "text")
    ctx.check("valid JSON", json.loads(blocks[0]["text"]) == {"a": 1, "b": [1, 2]})


@test
def test_structured_fallback_skipped_when_text_present(ctx: Ctx):
    blocks = T.content_with_structured_fallback([{"type": "text", "text": "real answer"}], {"a": 1})
    ctx.check("structuredContent NOT appended when real text exists", blocks == [{"type": "text", "text": "real answer"}])


@test
def test_structured_fallback_noop_when_neither(ctx: Ctx):
    ctx.check("no text, no structuredContent -> unchanged", T.content_with_structured_fallback([], None) == [])


# ---- OpenCode-H9 MCP compatibility: per-family schema sanitising --------

@test
def test_sanitize_schema_noop_for_claude_and_none(ctx: Ctx):
    schema = {"type": "object", "properties": {"x": {"$ref": "#/defs/X", "description": "should stay"}}}
    ctx.check("family=None is a no-op", T.sanitize_tool_schema(schema, family=None) == schema)
    ctx.check("family='claude' is a no-op", T.sanitize_tool_schema(schema, family="claude") == schema)


@test
def test_sanitize_schema_kimi_strips_ref_siblings(ctx: Ctx):
    """Moonshot/Kimi expands $ref before validation and rejects sibling
    keywords (e.g. description) on the same node."""
    schema = {"type": "object", "properties": {
        "x": {"$ref": "#/defs/X", "description": "extra sibling keyword"},
        "y": {"type": "string"},
    }}
    out = T.sanitize_tool_schema(schema, family="kimi")
    ctx.check(f"$ref node reduced to just $ref, got {out['properties']['x']}",
              out["properties"]["x"] == {"$ref": "#/defs/X"})
    ctx.check("a node without $ref is untouched", out["properties"]["y"] == {"type": "string"})


@test
def test_sanitize_schema_kimi_flattens_tuple_items(ctx: Ctx):
    schema = {"type": "object", "properties": {
        "pair": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]},
    }}
    out = T.sanitize_tool_schema(schema, family="kimi")
    ctx.check(f"tuple items flattened to items[0], got {out['properties']['pair']['items']}",
              out["properties"]["pair"]["items"] == {"type": "string"})


@test
def test_sanitize_schema_kimi_adds_required_to_empty_object(ctx: Ctx):
    """Kimi K2.5 rejects a bare {} parameter schema without an explicit
    "required": [] -- a real, common shape for a no-arg MCP tool."""
    out = T.sanitize_tool_schema({"type": "object", "properties": {}}, family="kimi")
    ctx.check(f"required: [] added, got {out}", out.get("required") == [])


@test
def test_sanitize_schema_gemini_coerces_enum_to_strings(ctx: Ctx):
    schema = {"type": "object", "properties": {"level": {"type": "integer", "enum": [1, 2, 3]}}}
    out = T.sanitize_tool_schema(schema, family="gemini")
    ctx.check(f"enum values coerced to strings, got {out['properties']['level']['enum']}",
              out["properties"]["level"]["enum"] == ["1", "2", "3"])


@test
def test_sanitize_schema_gemini_leaves_kimi_quirks_alone(ctx: Ctx):
    schema = {"type": "object", "properties": {"x": {"$ref": "#/defs/X", "description": "kept for gemini"}}}
    out = T.sanitize_tool_schema(schema, family="gemini")
    ctx.check("gemini never strips $ref siblings (that's a kimi-only rule)",
              out["properties"]["x"] == {"$ref": "#/defs/X", "description": "kept for gemini"})


@test
def test_mcptool_applies_family_sanitising_to_its_own_input_schema(ctx: Ctx):
    sdk_tool = SimpleNamespace(name="t", description="d", input_schema={
        "type": "object", "properties": {"x": {"$ref": "#/defs/X", "description": "sibling"}}})
    tool = T.McpTool("srv", sdk_tool, manager=None, family="kimi")
    ctx.check(f"McpTool.input_schema was sanitised for kimi, got {tool.input_schema}",
              tool.input_schema["properties"]["x"] == {"$ref": "#/defs/X"})


# ---- tool_always_load / maxResultSizeChars validation -------------------

@test
def test_tool_always_load_strict_true_only(ctx: Ctx):
    ctx.check("=== true counts", T.tool_always_load({"anthropic/alwaysLoad": True}) is True)
    ctx.check("the string 'true' does NOT count (must be === true)",
              T.tool_always_load({"anthropic/alwaysLoad": "true"}) is False)
    ctx.check("1 does not count", T.tool_always_load({"anthropic/alwaysLoad": 1}) is False)
    ctx.check("missing key -> False", T.tool_always_load({}) is False)
    ctx.check("None meta -> False", T.tool_always_load(None) is False)


@test
def test_meta_max_result_size_chars_validation(ctx: Ctx):
    ctx.check("a positive int is accepted", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": 500}) == 500)
    ctx.check("zero is rejected", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": 0}) is None)
    ctx.check("negative is rejected", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": -5}) is None)
    ctx.check("infinity is rejected", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": float("inf")}) is None)
    ctx.check("NaN is rejected", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": float("nan")}) is None)
    ctx.check("a bool is rejected (not a real number)", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": True}) is None)
    ctx.check("a string is rejected", T.tool_meta_max_result_size_chars({"anthropic/maxResultSizeChars": "500"}) is None)
    ctx.check("missing key -> None", T.tool_meta_max_result_size_chars({}) is None)


# ---- cap_and_spill: output cap + exact truncation string + spill --------

def _with_env(name, value, fn):
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


@test
def test_cap_and_spill_noop_under_the_cap(ctx: Ctx):
    blocks = [{"type": "text", "text": "short"}]
    ctx.check("under the cap: returned unchanged (same object)", T.cap_and_spill(blocks) is blocks)


@test
def test_cap_and_spill_exact_truncation_string(ctx: Ctx):
    def _run():
        blocks = [{"type": "text", "text": "X" * 5000}]
        result = T.cap_and_spill(blocks)
        joined = " ".join(b.get("text", "") for b in result)
        # binary-facts sec.9, verbatim.
        ctx.check(f"exact Claude Code truncation string present, got tail={joined[-60:]!r}",
                  "[OUTPUT TRUNCATED - exceeded 50 token limit]" in joined)
    _with_env("MAX_MCP_OUTPUT_TOKENS", "50", _run)  # cap = 200 chars


@test
def test_cap_and_spill_respects_meta_max_result_size_chars_when_tighter(ctx: Ctx):
    blocks = [{"type": "text", "text": "Y" * 1000}]
    result = T.cap_and_spill(blocks, meta={"anthropic/maxResultSizeChars": 100})
    text_len = sum(len(b.get("text", "")) for b in result if b.get("type") == "text")
    ctx.check(f"capped to the TIGHTER meta value (~100 chars + marker), got {text_len}", text_len < 250)


@test
def test_cap_and_spill_writes_full_content_to_tool_results(ctx: Ctx):
    def _run():
        with tempfile.TemporaryDirectory() as td:
            blocks = [{"type": "text", "text": "Z" * 5000}]
            T.cap_and_spill(blocks, session_dir=Path(td), tool_use_id="toolu_abc")
            spill_path = Path(td) / "tool-results" / "toolu_abc.txt"
            ctx.check("spill file written", spill_path.exists())
            ctx.check("spill file has the FULL untruncated content",
                      len(spill_path.read_text(encoding="utf-8")) == 5000)
    _with_env("MAX_MCP_OUTPUT_TOKENS", "50", _run)


@test
def test_cap_and_spill_no_spill_without_session_dir_or_tool_use_id(ctx: Ctx):
    def _run():
        # must not raise even though nowhere to spill to
        blocks = [{"type": "text", "text": "W" * 5000}]
        result = T.cap_and_spill(blocks)
        ctx.check("still truncates cleanly with no spill target", any("TRUNCATED" in b.get("text", "") for b in result))
    _with_env("MAX_MCP_OUTPUT_TOKENS", "50", _run)


@test
def test_cap_and_spill_reserves_room_for_images_never_truncating_them(ctx: Ctx):
    def _run():
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                  {"type": "text", "text": "Q" * 5000}]
        result = T.cap_and_spill(blocks)
        images = [b for b in result if b.get("type") == "image"]
        ctx.check("the image block itself is never truncated/dropped", len(images) == 1 and images[0]["source"]["data"] == "AAAA")
    _with_env("MAX_MCP_OUTPUT_TOKENS", "50", _run)


@test
def test_default_output_token_limit_env_and_default(ctx: Ctx):
    old = os.environ.pop("MAX_MCP_OUTPUT_TOKENS", None)
    try:
        ctx.check("default 25000", T.default_output_token_limit() == 25_000)
        os.environ["MAX_MCP_OUTPUT_TOKENS"] = "1000"
        ctx.check("env override honoured", T.default_output_token_limit() == 1000)
    finally:
        if old is None:
            os.environ.pop("MAX_MCP_OUTPUT_TOKENS", None)
        else:
            os.environ["MAX_MCP_OUTPUT_TOKENS"] = old


# ---- McpTool through a real fake server ------------------------------------

def _mcp_tool_for(name, *, vision=False, manager=None):
    from rolo_claude.mcp.manager import McpManager, McpServerConfig
    if manager is not None:
        mgr = manager  # already started -- start_all() is for the FIRST call only (it's idempotent, but skip the extra round trip)
    else:
        cfg = McpServerConfig(name="fake", type="stdio", command=sys.executable,
                               args=["-m", "tests.helpers.fake_mcp_server"], cwd=str(REPO_DIR))
        mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR)
        mgr.start_all()
    triples = {t[2].name: t for t in mgr.all_tools()}
    server, wire_name, sdk_tool = triples[name]
    return T.McpTool(server, sdk_tool, mgr, vision=vision), mgr


@test
def test_mcptool_name_and_definition(ctx: Ctx):
    tool, mgr = _mcp_tool_for("echo")
    try:
        ctx.check("sanitised wire name", tool.name == "mcp__fake__echo")
        defn = tool.definition()
        ctx.check("definition has name/description/input_schema", set(defn) == {"name", "description", "input_schema"})
    finally:
        mgr.close_all()


@test
def test_mcptool_is_read_only_from_annotations(ctx: Ctx):
    ro_tool, mgr = _mcp_tool_for("read_only_tool")
    try:
        ctx.check("readOnlyHint -> is_read_only True", ro_tool.is_read_only is True)
        echo_tool, _ = _mcp_tool_for("echo", manager=mgr)
        ctx.check("no annotation -> is_read_only False", echo_tool.is_read_only is False)
    finally:
        mgr.close_all()


@test
def test_mcptool_always_load(ctx: Ctx):
    al_tool, mgr = _mcp_tool_for("always_load_tool")
    try:
        ctx.check("always_load() True for the marked tool", al_tool.always_load() is True)
        echo_tool, _ = _mcp_tool_for("echo", manager=mgr)
        ctx.check("always_load() False for an ordinary tool", echo_tool.always_load() is False)
    finally:
        mgr.close_all()


@test
def test_mcptool_run_success(ctx: Ctx):
    tool, mgr = _mcp_tool_for("echo")
    try:
        result = tool.run({"text": "ping"}, ToolContext(cwd=REPO_DIR))
        ctx.check("no error", result.is_error is False)
        ctx.check("content is a list of blocks", isinstance(result.content, list))
        ctx.check("echoed text present", result.content[0]["text"] == "ping")
    finally:
        mgr.close_all()


@test
def test_mcptool_run_is_error_true(ctx: Ctx):
    tool, mgr = _mcp_tool_for("error_tool")
    try:
        result = tool.run({}, ToolContext(cwd=REPO_DIR))
        ctx.check("isError -> ToolResult.is_error", result.is_error is True)
    finally:
        mgr.close_all()


@test
def test_mcptool_run_handles_a_dead_server_without_crashing(ctx: Ctx):
    tool, mgr = _mcp_tool_for("echo")
    mgr.close_all()  # kill the connection out from under the tool
    result = tool.run({"text": "x"}, ToolContext(cwd=REPO_DIR))
    ctx.check("a call after close() is a clean error, never a raised exception", result.is_error is True)
    ctx.check("error message is informative", "mcp__fake__echo" in result.content)


@test
def test_mcptool_result_cap_is_none_manages_own_truncation(ctx: Ctx):
    ctx.check("class-level result_cap is None (like Read)", T.McpTool.result_cap is None)


@test
def test_mcptool_run_no_longer_caps_h4_moved_to_finalize(ctx: Ctx):
    """finding 5/H4 must-do: capping moves to agent/loop.py's
    _finalize_tool_result (after the future PostToolUse hook point) --
    McpTool.run itself must return the FULL, uncapped result."""
    def _run():
        tool, mgr = _mcp_tool_for("huge")
        try:
            result = tool.run({"chars": 200_000}, ToolContext(cwd=REPO_DIR))
            ctx.check("no error", result.is_error is False)
            total_len = sum(len(b.get("text", "")) for b in result.content if b.get("type") == "text")
            ctx.check(f"the full 200000-char result comes back uncapped, got {total_len}", total_len == 200_000)
        finally:
            mgr.close_all()
    _with_env("MAX_MCP_OUTPUT_TOKENS", "50", _run)  # a tiny cap that would have truncated it pre-fix


@test
def test_mcptool_run_aborts_promptly(ctx: Ctx):
    import threading
    tool, mgr = _mcp_tool_for("slow_tool")
    try:
        abort = threading.Event()
        threading.Timer(0.3, abort.set).start()
        t0 = __import__("time").monotonic()
        result = tool.run({"seconds": 6.0}, ToolContext(cwd=REPO_DIR, abort=abort))
        elapsed = __import__("time").monotonic() - t0
        ctx.check(f"aborted well under the 6s sleep, took {elapsed:.2f}s", elapsed < 2.0)
        ctx.check("a clean interrupted error, not a raised exception", result.is_error is True
                  and "interrupted" in result.content.lower())
    finally:
        mgr.close_all()


# ---- ListMcpResourcesTool / ReadMcpResourceTool ----------------------------

def _manager_for_resources():
    from rolo_claude.mcp.manager import McpManager, McpServerConfig
    cfg = McpServerConfig(name="fake", type="stdio", command=sys.executable,
                           args=["-m", "tests.helpers.fake_mcp_server"], cwd=str(REPO_DIR))
    mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR)
    mgr.start_all()
    return mgr


@test
def test_list_mcp_resources_tool_no_manager_is_error(ctx: Ctx):
    result = T.ListMcpResourcesTool().run({}, ToolContext(cwd=REPO_DIR))
    ctx.check("no manager -> a clear error, never a crash", result.is_error is True)


@test
def test_list_mcp_resources_tool_lists_the_real_resource(ctx: Ctx):
    mgr = _manager_for_resources()
    try:
        ctx_obj = ToolContext(cwd=REPO_DIR, mcp_manager=mgr)
        result = T.ListMcpResourcesTool().run({}, ctx_obj)
        ctx.check("no error", result.is_error is False)
        items = json.loads(result.content)
        ctx.check(f"the fake resource is listed, got {items}", any(i["uri"] == "fake://note" for i in items))
    finally:
        mgr.close_all()


@test
def test_read_mcp_resource_tool_reads_real_text(ctx: Ctx):
    mgr = _manager_for_resources()
    try:
        ctx_obj = ToolContext(cwd=REPO_DIR, mcp_manager=mgr)
        result = T.ReadMcpResourceTool().run({"server": "fake", "uri": "fake://note"}, ctx_obj)
        ctx.check("no error", result.is_error is False)
        ctx.check("real resource text came back", "fake MCP resource" in result.content)
    finally:
        mgr.close_all()


@test
def test_read_mcp_resource_tool_missing_args(ctx: Ctx):
    mgr = _manager_for_resources()
    try:
        ctx_obj = ToolContext(cwd=REPO_DIR, mcp_manager=mgr)
        result = T.ReadMcpResourceTool().run({"server": "fake"}, ctx_obj)
        ctx.check("missing uri -> clear error", result.is_error is True)
    finally:
        mgr.close_all()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
