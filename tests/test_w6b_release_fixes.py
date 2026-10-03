"""tests.test_w6b_release_fixes -- Halo 2.0.1 release fix pass, round B
(W6b): pinning tests for the review's findings 19-38 that don't already
have a natural home in an existing test_*.py file (see plans/ for the
review and brief). Each test documents which finding it pins in its own
docstring/name. Mirrors test_w6a_release_fixes.py's own per-round-file
convention.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
test, TESTS = new_registry()


# ============================================================================
# finding 29: ConnectorTool.run() -- --no-session-persistence, and the
# SESSION's cwd (not the halo process's own) for the inner claude call.
# ============================================================================

@test
def test_connector_tool_run_passes_no_session_persistence_and_the_session_cwd(ctx: Ctx):
    """Review finding 29: each connector call ran `claude -p` without
    `--no-session-persistence` (so every call added a "Request: ..."
    session to Claude Code's own `/resume` list) and in the HALO
    process's own cwd rather than the session's (so under `--cwd` the
    inner claude read another project's settings/.mcp.json)."""
    from halo_harness.mcp.connectors import ConnectorInfo
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.connector_tool import ConnectorTool

    fake_dir = Path(tempfile.mkdtemp(prefix="w6b-f29-"))
    capture_path = fake_dir / "capture.json"
    fake_script = fake_dir / "fake_claude.py"
    fake_script.write_text(
        "import json, os, sys\n"
        "with open(" + repr(str(capture_path)) + ", 'w', encoding='utf-8') as f:\n"
        "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, f)\n"
        "print(json.dumps({'result': 'ok', 'is_error': False}))\n",
        encoding="utf-8",
    )
    info = ConnectorInfo(name="Test Connector", slug="test_connector", account_token="Test",
                          url="https://example.invalid/mcp", host="example.invalid",
                          status="connected", status_text="connected", tools=["do_thing"])
    tool = ConnectorTool(info)
    session_cwd = Path(tempfile.mkdtemp(prefix="w6b-f29-session-cwd-"))
    saved = os.environ.get("HALO_CLAUDE_EXE")
    os.environ["HALO_CLAUDE_EXE"] = f'"{sys.executable}" "{fake_script}"'
    try:
        ctx_obj = ToolContext(cwd=session_cwd)
        result = tool.run({"request": "do the thing"}, ctx_obj)
        ctx.check(f"the call succeeded, got {result.content!r}", not result.is_error)
        captured = json.loads(capture_path.read_text(encoding="utf-8"))
        ctx.check(f"--no-session-persistence is in the argv, got {captured['argv']}",
                  "--no-session-persistence" in captured["argv"])
        ctx.check(f"the inner claude ran in the SESSION's cwd, got {captured['cwd']!r} vs "
                  f"{str(session_cwd)!r}", Path(captured["cwd"]).resolve() == session_cwd.resolve())
    finally:
        if saved is None:
            os.environ.pop("HALO_CLAUDE_EXE", None)
        else:
            os.environ["HALO_CLAUDE_EXE"] = saved


# ============================================================================
# finding 31: StreamJsonSink.finish() resets per-turn state.
# ============================================================================

@test
def test_stream_json_sink_resets_prompt_suggestion_and_duration_per_turn(ctx: Ctx):
    """Release review finding 31: `finish()` never reset `_prompt_
    suggestion` (a later turn whose own suggestion call failed, or never
    ran at all, re-emitted the PREVIOUS turn's suggestion) or
    `_start_monotonic` (so `duration_ms`, meant to be per-turn, kept
    accumulating across every turn of this multi-turn stream-json loop)."""
    import io
    from halo_harness.output import StreamJsonSink

    stream = io.StringIO()
    sink = StreamJsonSink(session_id="s1", cwd="/tmp", model="m", permission_mode="default", stream=stream)
    sink.emit_init()

    sink.add_prompt_suggestion("do X next")
    time.sleep(0.12)
    sink.consume(iter(()), finish=True)

    time.sleep(0.12)  # turn 2 never calls add_prompt_suggestion at all
    sink.consume(iter(()), finish=True)

    lines = [json.loads(L) for L in stream.getvalue().splitlines() if L.strip()]
    results = [L for L in lines if L.get("type") == "result"]
    ctx.check(f"two result lines, got {[L.get('type') for L in lines]}", len(results) == 2)
    ctx.check(f"turn 1 carries the suggestion, got {results[0].get('prompt_suggestion')!r}",
              results[0].get("prompt_suggestion") == "do X next")
    ctx.check(f"turn 2 does NOT repeat it, got {results[1].get('prompt_suggestion')!r}",
              results[1].get("prompt_suggestion") is None)
    ctx.check(f"turn 2's own duration_ms measures only its own ~120ms wait, not the "
              f"accumulated ~240ms, got {results[1]['duration_ms']}", results[1]["duration_ms"] < 220)


# ============================================================================
# Parity gap: child Sessions inherit plugin_roots.
# ============================================================================

def _parent_with_plugin_roots(cwd: Path, plugin_roots: list):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine

    session_ctx = SessionContext(cwd=cwd, model_label="or:mock/parent", bare=True)
    return Session(
        cwd=cwd, model_ref=parse_model_ref("or:mock/parent"), model_profile=ModelProfile(), creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="w6b-pluginroots-state-")), model_label="or:mock/parent",
        session_context=session_ctx, max_turns=3, permission_engine=PermissionEngine(mode="auto", cwd=cwd),
        agents={}, routes={}, cli_flags={"resolved_plugin_roots": plugin_roots},
    )


@test
def test_child_session_inherits_parent_plugin_roots(ctx: Ctx):
    """Parity gap: `Session.plugin_roots` reads `cli_flags["resolved_
    plugin_roots"]` -- a child's own `cli_flags` dict used to carry only
    the worktree key, so `self.plugin_roots` was always `[]` for every
    sub-agent regardless of the PARENT's own `--plugin-dir`/
    `--plugin-url` roots, and a model-invoked Skill tool call inside a
    sub-agent could never find one of those skills at all."""
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session
    from halo_harness.config.agents_md import AgentSpec

    cwd = Path(tempfile.mkdtemp(prefix="w6b-pluginroots-cwd-"))
    fake_plugin_root = str(Path(tempfile.mkdtemp(prefix="w6b-pluginroots-plugin-")))
    parent = _parent_with_plugin_roots(cwd, [fake_plugin_root])
    ctx.check(f"sanity: the parent itself has the plugin root, got {parent.plugin_roots}",
              parent.plugin_roots == [fake_plugin_root])

    spec = AgentSpec(name="plain-agent", description="d")
    runtime = AgentRuntime(parent=parent, agents={"plain-agent": spec}, routes={})
    child, _meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id="agentid-plugins", model_override=None,
        parent_tool_use_id="toolu_1", background=False, role_override=None,
    )
    ctx.check(f"the child inherits the SAME plugin roots, got {child.plugin_roots}",
              child.plugin_roots == [fake_plugin_root])


@test
def test_child_session_with_no_parent_plugin_roots_gets_a_narrow_cli_flags(ctx: Ctx):
    """When the parent has none, the child's `cli_flags` stays exactly as
    narrow as before finding this gap (never a stray empty key)."""
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session
    from halo_harness.config.agents_md import AgentSpec

    cwd = Path(tempfile.mkdtemp(prefix="w6b-pluginroots-none-cwd-"))
    parent = _parent_with_plugin_roots(cwd, [])
    spec = AgentSpec(name="plain-agent", description="d")
    runtime = AgentRuntime(parent=parent, agents={"plain-agent": spec}, routes={})
    child, _meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id="agentid-noplugins", model_override=None,
        parent_tool_use_id="toolu_1", background=False, role_override=None,
    )
    ctx.check(f"the child's plugin_roots is empty too, got {child.plugin_roots}", child.plugin_roots == [])
    ctx.check(f"cli_flags carries no stray resolved_plugin_roots key, got {child.cli_flags}",
              "resolved_plugin_roots" not in child.cli_flags)


# ============================================================================
# Parity gap: Agent `memory:` scopes user|project|local, directory
# created, and inside the child's own permission working dirs.
# ============================================================================

def _parent_for_memory_test(cwd: Path):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine

    session_ctx = SessionContext(cwd=cwd, model_label="or:mock/parent", bare=True)
    return Session(
        cwd=cwd, model_ref=parse_model_ref("or:mock/parent"), model_profile=ModelProfile(), creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="w6b-memscope-state-")), model_label="or:mock/parent",
        session_context=session_ctx, max_turns=3, permission_engine=PermissionEngine(mode="default", cwd=cwd),
        agents={}, routes={},
    )


def _build_memory_child(parent, memory_value):
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session
    from halo_harness.config.agents_md import AgentSpec

    spec = AgentSpec(name="mem-agent", description="d", memory=memory_value)
    runtime = AgentRuntime(parent=parent, agents={"mem-agent": spec}, routes={})
    child, _meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id="agentid-mem", model_override=None,
        parent_tool_use_id="toolu_1", background=False, role_override=None,
    )
    return child


@test
def test_agent_memory_user_scope_is_global_created_and_a_working_dir(ctx: Ctx):
    """Parity gap: `memory:` used to be a bare truthy gate always
    resolving to the SAME global directory, which was never created and
    sat OUTSIDE the child's own permission working dirs (asked for in
    default mode, flatly denied in -p/background) -- "user" scope keeps
    the original global path, but now creates it and is a working dir."""
    cwd = Path(tempfile.mkdtemp(prefix="w6b-memscope-user-cwd-"))
    parent = _parent_for_memory_test(cwd)
    child = _build_memory_child(parent, "user")
    expected_dir = Path(parent.state_dir) / "agents" / "mem-agent"
    ctx.check(f"the directory actually exists now, got {expected_dir}", expected_dir.is_dir())
    ctx.check(f"it is a recognized permission working dir, got {child.permission_engine.working_dirs()}",
              child.permission_engine._in_working_dirs(expected_dir / "MEMORY.md"))
    ctx.check(f"the system prompt names the real path, got {child.session_context.system_prompt!r}",
              str(expected_dir) in child.session_context.system_prompt)


@test
def test_agent_memory_legacy_bare_true_still_behaves_like_user_scope(ctx: Ctx):
    """Backward compat: a frontmatter `memory: true` (YAML bool, not a
    scope string) must behave exactly like "user" did before this fix,
    never silently stop working."""
    cwd = Path(tempfile.mkdtemp(prefix="w6b-memscope-legacy-cwd-"))
    parent = _parent_for_memory_test(cwd)
    child = _build_memory_child(parent, True)
    expected_dir = Path(parent.state_dir) / "agents" / "mem-agent"
    ctx.check(f"resolves to the same global path as 'user', got {expected_dir}", expected_dir.is_dir())


@test
def test_agent_memory_project_scope_lives_inside_the_project_no_extra_dirs_needed(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="w6b-memscope-project-cwd-"))
    parent = _parent_for_memory_test(cwd)
    child = _build_memory_child(parent, "project")
    expected_dir = cwd / ".halo" / "agents" / "mem-agent"
    ctx.check(f"created inside the project's own cwd, got {expected_dir}", expected_dir.is_dir())
    ctx.check(f"already a working dir (it's under cwd itself), got {child.permission_engine.working_dirs()}",
              child.permission_engine._in_working_dirs(expected_dir / "MEMORY.md"))


@test
def test_agent_memory_local_scope_is_project_keyed_and_a_working_dir(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="w6b-memscope-local-cwd-"))
    parent = _parent_for_memory_test(cwd)
    child = _build_memory_child(parent, "local")
    ctx.check(f"a distinct 'local' directory was created under state_dir, got "
              f"{list((Path(parent.state_dir) / 'agents-local').rglob('MEMORY.md')) if (Path(parent.state_dir) / 'agents-local').is_dir() else None}",
              (Path(parent.state_dir) / "agents-local").is_dir())
    created = next((Path(parent.state_dir) / "agents-local").rglob("mem-agent"), None)
    ctx.check(f"found the agent's own local-scope directory, got {created}", created is not None and created.is_dir())
    ctx.check(f"it is a recognized permission working dir, got {child.permission_engine.working_dirs()}",
              created is not None and child.permission_engine._in_working_dirs(created / "MEMORY.md"))


@test
def test_agent_memory_project_and_local_scopes_are_different_directories(ctx: Ctx):
    """The whole point of separate scopes: they must not collide."""
    cwd = Path(tempfile.mkdtemp(prefix="w6b-memscope-distinct-cwd-"))
    parent_a = _parent_for_memory_test(cwd)
    parent_b = _parent_for_memory_test(cwd)
    project_child = _build_memory_child(parent_a, "project")
    local_child = _build_memory_child(parent_b, "local")
    project_dir = cwd / ".halo" / "agents" / "mem-agent"
    local_dir = next((Path(parent_b.state_dir) / "agents-local").rglob("mem-agent"))
    ctx.check(f"project scope is inside the project tree, got {project_dir}",
              str(project_dir).startswith(str(cwd)))
    ctx.check(f"local scope is NOT inside the project tree, got {local_dir}",
              not str(local_dir).startswith(str(cwd)))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
