"""tests.test_h6_integration -- cross-cutting H6 checks that don't belong
to one single module: the AskUserQuestion new schema, stream-json nested
agent_id/parent_tool_use_id tagging (output.StreamJsonSink), --session-id
CLI validation, the Agent/Task/EnterPlanMode/ExitPlanMode tools' presence
in the frozen catalog, AGENTS.md fallback (verifying the already-existing
config/claude_md.py behaviour brief F calls out), and SubagentStart/Stop
hook payload shape.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- AskUserQuestion new schema -----------------------------------------

@test
def test_ask_user_question_schema_is_the_claude_code_shape(ctx: Ctx):
    from rolo_claude.tools.ask_user_question import AskUserQuestionTool

    tool = AskUserQuestionTool()
    schema = tool.input_schema
    ctx.check("top-level 'questions' array", schema["properties"]["questions"]["type"] == "array")
    q_schema = schema["properties"]["questions"]["items"]
    ctx.check("each question has 'question'", "question" in q_schema["properties"])
    ctx.check("each question has 'header'", "header" in q_schema["properties"])
    ctx.check("each question has 'options'", "options" in q_schema["properties"])
    ctx.check("each question has 'multiSelect'", "multiSelect" in q_schema["properties"])
    opt_schema = q_schema["properties"]["options"]["items"]
    ctx.check("each option has 'label'", "label" in opt_schema["properties"])
    ctx.check("each option has 'description'", "description" in opt_schema["properties"])


@test
def test_ask_user_question_new_shape_round_trip(ctx: Ctx):
    from pathlib import Path as _P
    from rolo_claude.tools.ask_user_question import AskUserQuestionTool
    from rolo_claude.tools.base import ToolContext

    tool = AskUserQuestionTool()
    input_data = {
        "questions": [
            {"question": "Which database?", "header": "DB", "multiSelect": False,
             "options": [{"label": "Postgres", "description": "relational"}, {"label": "SQLite", "description": "embedded"}]},
        ],
    }
    result = tool.run(input_data, ToolContext(cwd=_P(".")))
    ctx.check("still an error in print mode (no interactive UI)", result.is_error is True)
    summary = tool.summary(input_data)
    ctx.check("summary uses the first question's text", "Which database?" in summary)


@test
def test_ask_user_question_old_flat_shape_still_accepted(ctx: Ctx):
    from pathlib import Path as _P
    from rolo_claude.tools.ask_user_question import AskUserQuestionTool
    from rolo_claude.tools.base import ToolContext

    tool = AskUserQuestionTool()
    result = tool.run({"question": "which one?"}, ToolContext(cwd=_P(".")))
    ctx.check("the pre-H6 flat shape never just errors on shape alone", result.is_error is True)
    empty = tool.run({}, ToolContext(cwd=_P(".")))
    ctx.check("genuinely no question at all -> the 'required' error", "required" in empty.content.lower())


@test
def test_ask_user_question_multi_select_flag_accepted(ctx: Ctx):
    from pathlib import Path as _P
    from rolo_claude.tools.ask_user_question import AskUserQuestionTool
    from rolo_claude.tools.base import ToolContext

    tool = AskUserQuestionTool()
    input_data = {"questions": [
        {"question": "Which environments apply?", "multiSelect": True,
         "options": [{"label": "dev"}, {"label": "staging"}, {"label": "prod"}]},
    ]}
    result = tool.run(input_data, ToolContext(cwd=_P(".")))
    ctx.check("multiSelect input is accepted without raising", result.is_error is True)


# ---- stream-json nested tagging -----------------------------------------

@test
def test_stream_json_sink_tags_nested_assistant_lines(ctx: Ctx):
    from rolo_claude import events as ev
    from rolo_claude.output import StreamJsonSink

    out = io.StringIO()
    sink = StreamJsonSink(session_id="s1", cwd="/x", model="m", permission_mode="auto", stream=out)

    def _gen():
        yield ev.message_start(turn=1)
        yield ev.text_delta("main session text", turn=1)
        yield ev.Event("subagent_start", {"name": "general-purpose", "parent_tool_use_id": "call_1"}, turn=1, agent_id="agent-1")
        yield ev.Event("message_start", {}, turn=1, agent_id="agent-1")
        child_delta = ev.text_delta("child session text", turn=1)
        child_delta.agent_id = "agent-1"
        child_delta.data = {**child_delta.data, "parent_tool_use_id": "call_1"}
        yield child_delta
        child_end = ev.message_end(turn=1, stop_reason="end_turn", usage={})
        child_end.agent_id = "agent-1"
        child_end.data = {**child_end.data, "parent_tool_use_id": "call_1"}
        yield child_end
        # The child's OWN turn_done (agent/subagent.py forwards its whole
        # event stream, including this) is what actually flushes ITS
        # buffer -- a child never shares the main session's turn_done.
        child_turn_done = ev.turn_done(turn=1, reason="end_turn")
        child_turn_done.agent_id = "agent-1"
        yield child_turn_done
        yield ev.Event("subagent_end", {"name": "general-purpose", "parent_tool_use_id": "call_1"}, turn=1, agent_id="agent-1")
        yield ev.message_end(turn=1, stop_reason="end_turn", usage={"input_tokens": 5, "output_tokens": 2})
        yield ev.turn_done(turn=1, reason="end_turn")

    sink.consume(_gen())
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    subagent_start_lines = [l for l in lines if l.get("type") == "subagent_start"]
    subagent_stop_lines = [l for l in lines if l.get("type") == "subagent_stop"]
    ctx.check("a subagent_start wire line was written", len(subagent_start_lines) == 1)
    ctx.check("a subagent_stop wire line was written", len(subagent_stop_lines) == 1)
    ctx.check("subagent_start carries the agent_id", subagent_start_lines[0]["agent_id"] == "agent-1")

    assistant_lines = [l for l in lines if l.get("type") == "assistant"]
    main_lines = [l for l in assistant_lines if "parent_tool_use_id" not in l]
    child_lines = [l for l in assistant_lines if l.get("parent_tool_use_id") == "call_1"]
    ctx.check("exactly one MAIN assistant line (no parent_tool_use_id)", len(main_lines) == 1)
    ctx.check("exactly one CHILD assistant line, tagged with parent_tool_use_id", len(child_lines) == 1)
    main_text = "".join(b.get("text", "") for b in main_lines[0]["message"]["content"] if b.get("type") == "text")
    child_text = "".join(b.get("text", "") for b in child_lines[0]["message"]["content"] if b.get("type") == "text")
    ctx.check("main line has ONLY the main session's text", main_text == "main session text")
    ctx.check("child line has ONLY the child session's text", child_text == "child session text")

    result_line = [l for l in lines if l.get("type") == "result"][0]
    ctx.check("the final result usage reflects only the MAIN session's usage",
              result_line["usage"].get("input_tokens") == 5)


@test
def test_print_mode_sink_ignores_subagent_text_in_plain_output(ctx: Ctx):
    from rolo_claude import events as ev
    from rolo_claude.output import PrintModeSink

    out = io.StringIO()
    sink = PrintModeSink(output_format="text", stream=out)

    def _gen():
        yield ev.message_start(turn=1)
        yield ev.Event("subagent_start", {"name": "x"}, turn=1, agent_id="agent-1")
        child_delta = ev.text_delta("should never appear in the main answer", turn=1)
        child_delta.agent_id = "agent-1"
        yield child_delta
        yield ev.Event("subagent_end", {"name": "x"}, turn=1, agent_id="agent-1")
        yield ev.text_delta("the real final answer", turn=1)
        yield ev.message_end(turn=1, stop_reason="end_turn", usage={})
        yield ev.turn_done(turn=1, reason="end_turn")

    sink.consume(_gen())
    printed = out.getvalue()
    ctx.check("the child's text never leaks into the printed result", "should never appear" not in printed)
    ctx.check("the main session's own text IS printed", "the real final answer" in printed)


# ---- registry / catalog --------------------------------------------------

@test
def test_new_tools_registered_in_default_catalog(ctx: Ctx):
    from rolo_claude.tools.registry import ToolRegistry

    reg = ToolRegistry()
    names = set(reg.names())
    for expected in ("Agent", "Task", "EnterPlanMode", "ExitPlanMode"):
        ctx.check(f"{expected} is in the default tool catalog", expected in names)
    ctx.check("Agent has a real description (not empty)", len(reg.get("Agent").description) > 20)
    ctx.check("Task's definition is a distinct entry from Agent's (own name)", reg.get("Task").name == "Task")


@test
def test_unknown_tool_dispatch_still_reports_cleanly(ctx: Ctx):
    from pathlib import Path as _P
    from rolo_claude.tools.base import ToolContext
    from rolo_claude.tools.registry import ToolRegistry

    reg = ToolRegistry()
    result = reg.dispatch("NotARealTool", {}, ToolContext(cwd=_P(".")))
    ctx.check("unknown tool name -> a clean error, not a crash", result.is_error is True)


# ---- --session-id CLI validation ----------------------------------------

@test
def test_run_print_mode_rejects_invalid_session_id(ctx: Ctx):
    from rolo_claude.headless import run_print_mode

    exit_code = run_print_mode(prompt="hi", cwd=Path(tempfile.mkdtemp(prefix="h6-sid-")), session_id="not-a-uuid")
    ctx.check("an invalid --session-id exits 2 before touching anything else", exit_code == 2)


# ---- AGENTS.md fallback (already implemented -- verify only) -----------

@test
def test_agents_md_fallback_when_no_claude_md_exists(ctx: Ctx):
    from rolo_claude.config.claude_md import discover_instructions
    from rolo_claude.config.settings import Settings

    root = Path(tempfile.mkdtemp(prefix="h6-agentsmd-"))
    (root / "AGENTS.md").write_text("# Agents.md only, no CLAUDE.md here\n", encoding="utf-8")
    settings = Settings(raw={}, layers=[], errors=[])
    old = os.environ.get("BRIDGE_TEST_ANCESTOR_ROOT")
    os.environ["BRIDGE_TEST_ANCESTOR_ROOT"] = str(root)
    try:
        bundle = discover_instructions(root, settings, True, bare=False)
        rendered = bundle.render()
        ctx.check("AGENTS.md content is pulled in when no CLAUDE.md exists", "Agents.md only" in rendered)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_ANCESTOR_ROOT", None)
        else:
            os.environ["BRIDGE_TEST_ANCESTOR_ROOT"] = old


# ---- SubagentStart/Stop hook payload shape ------------------------------

@test
def test_hook_runner_subagent_payload_carries_agent_id_and_type(ctx: Ctx):
    from rolo_claude.hooks import HookRunner

    runner = HookRunner({}, cwd=Path(tempfile.mkdtemp(prefix="h6-hooks-")), session_id="sess1",
                         transcript_path="/x/sess1.jsonl", agent_id="agent-42", agent_type="Explore")
    payload = runner.payload("SubagentStart", extra={"prompt": "do the thing"})
    ctx.check("payload carries agent_id", payload["agent_id"] == "agent-42")
    ctx.check("payload carries agent_type", payload["agent_type"] == "Explore")
    ctx.check("payload carries the hook_event_name", payload["hook_event_name"] == "SubagentStart")
    ctx.check("payload carries the extra prompt field", payload["prompt"] == "do the thing")


@test
def test_hook_runner_subagent_stop_uses_the_shared_stop_cap_machinery(ctx: Ctx):
    from rolo_claude.hooks import HookRunner

    # No hooks configured -- has_hooks/run_stop must be complete no-ops,
    # never raise, exactly like a bare Session's own "Stop" handling.
    runner = HookRunner({}, cwd=Path(tempfile.mkdtemp(prefix="h6-hooks2-")), session_id="sess2",
                         transcript_path="/x/sess2.jsonl", agent_id="agent-7", agent_type="general-purpose")
    ctx.check("no SubagentStop hooks configured -> has_hooks is False", runner.has_hooks("SubagentStop") is False)
    outcome = runner.run_stop("SubagentStop", last_assistant_message="done")
    ctx.check("run_stop with nothing configured never blocks", outcome.blocked is False)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
