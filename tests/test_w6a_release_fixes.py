"""tests.test_w6a_release_fixes -- Halo 2.0.1 release fix pass, round A
(W6a): pinning tests for the review's findings 1-18 and the three parity
gaps fixed in this round (see plans/ for the review and brief). Each test
documents which finding it pins in its own docstring/name. New file (one
per round, matching the existing test_w5_*.py / test_w5b_*.py convention)
so the fake-home + mock-upstream scaffolding below is built once and
reused by every finding in this round instead of duplicated per module.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
test, TESTS = new_registry()


def _begin(fh=None):
    """Start a fresh fake home + mock upstream, point BRIDGE_TEST_HOME and
    BRIDGE_OPENROUTER_BASE_URL at them (same pattern as
    tests/test_w5_prompt_suggestions.py's own `_run_stream_json_turn`).
    Caller must `_end(mock)` in a `finally`."""
    fh = fh or build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    return fh, mock


def _end(mock) -> None:
    mock.stop()
    os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
    os.environ.pop("BRIDGE_TEST_HOME", None)


def _begin_home(fh=None):
    """Like `_begin()` but for a build_controller/build_session-only test
    that never drives a real turn (so no mock upstream server needed)."""
    fh = fh or build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    return fh


def _end_home() -> None:
    os.environ.pop("BRIDGE_TEST_HOME", None)


def _run(cwd, **kwargs):
    """Run one `run_print_mode` call with stdout captured (never printed
    into the test's own output), returning (exit_code, stdout_text)."""
    from halo_harness.headless import run_print_mode
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_print_mode(cwd=cwd, **kwargs)
    return code, out.getvalue()


# ---- finding 1 (critical): --no-session-persistence must never delete a --
# ---- log this process did not create -------------------------------------

@test
def test_f1_no_session_persistence_preserves_a_resumed_session(ctx: Ctx):
    from halo_harness.agent import sessions as agent_sessions

    fh, mock = _begin()
    try:
        code1, _ = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="first turn")
        ctx.check(f"run 1 exit 0, got {code1}", code1 == 0)

        sid = agent_sessions.resolve_continue(fh["proj"])
        ctx.check("a session id was created", bool(sid))
        log_path = agent_sessions.sessions_dir(fh["proj"]) / f"{sid}.jsonl"
        bytes_before = log_path.read_bytes()
        index_before = agent_sessions.load_index(fh["proj"])[sid]

        # stand-in for the sub-agent logs / rewind shadow repo the review
        # says the old code rmtree'd along with the log.
        session_dir = agent_sessions.sessions_dir(fh["proj"]) / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        marker = session_dir / "keepme.txt"
        marker.write_text("sub-agent log / shadow repo stand-in", encoding="utf-8")

        code2, _ = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="second turn",
                         continue_=True, cli_flags={"no_session_persistence": True})
        ctx.check(f"run 2 (-c --no-session-persistence) exit 0, got {code2}", code2 == 0)

        ctx.check("the resumed log file still exists", log_path.exists())
        ctx.check("the resumed log is truncated back to its EXACT pre-run-2 bytes",
                  log_path.exists() and log_path.read_bytes() == bytes_before)
        index_after = agent_sessions.load_index(fh["proj"])
        ctx.check("the index entry still exists", sid in index_after)
        ctx.check("the index entry's first_prompt is untouched (run 1's, never forgotten)",
                  index_after.get(sid, {}).get("first_prompt") == index_before.get("first_prompt"))
        ctx.check("the session dir (sub-agent logs / shadow repo) was left alone", marker.exists())
    finally:
        _end(mock)


@test
def test_f1_no_session_persistence_resume_flag_variant_also_preserves(ctx: Ctx):
    """Same as above but via `--resume <id>` (explicit) instead of `-c`,
    since the brief calls out both forms by name."""
    from halo_harness.agent import sessions as agent_sessions

    fh, mock = _begin()
    try:
        code1, _ = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="first turn")
        ctx.check(f"run 1 exit 0, got {code1}", code1 == 0)
        sid = agent_sessions.resolve_continue(fh["proj"])
        log_path = agent_sessions.sessions_dir(fh["proj"]) / f"{sid}.jsonl"
        bytes_before = log_path.read_bytes()

        code2, _ = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="second turn",
                         resume=sid, cli_flags={"no_session_persistence": True})
        ctx.check(f"run 2 (--resume <id> --no-session-persistence) exit 0, got {code2}", code2 == 0)
        ctx.check("the resumed log file still exists", log_path.exists())
        ctx.check("truncated back to its exact pre-run-2 bytes",
                  log_path.exists() and log_path.read_bytes() == bytes_before)
    finally:
        _end(mock)


@test
def test_f1_no_session_persistence_fork_session_variant_also_preserves(ctx: Ctx):
    """Same again via `--resume <id> --fork-session`: the id came from
    neither -c nor a bare --resume, but from fork_session (brief:
    "whether the id came from -c / --resume / --session-id / --fork")
    -- the FORKED copy must be truncated back to its own pre-run-2 bytes,
    never deleted, and the ORIGINAL session must be untouched throughout."""
    from halo_harness.agent import sessions as agent_sessions

    fh, mock = _begin()
    try:
        code1, _ = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="first turn")
        ctx.check(f"run 1 exit 0, got {code1}", code1 == 0)
        original_sid = agent_sessions.resolve_continue(fh["proj"])
        original_path = agent_sessions.sessions_dir(fh["proj"]) / f"{original_sid}.jsonl"
        original_bytes = original_path.read_bytes()

        code2, out2 = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="second turn",
                            resume=original_sid, fork_session_flag=True,
                            cli_flags={"no_session_persistence": True})
        ctx.check(f"run 2 (--resume <id> --fork-session --no-session-persistence) exit 0, got {code2}", code2 == 0)
        ctx.check("the ORIGINAL session is completely untouched", original_path.read_bytes() == original_bytes)

        forked_ids = [p.stem for p in agent_sessions.sessions_dir(fh["proj"]).glob("*.jsonl")
                      if p.stem != original_sid]
        ctx.check(f"exactly one forked session file exists, got {forked_ids}", len(forked_ids) == 1)
        forked_path = agent_sessions.sessions_dir(fh["proj"]) / f"{forked_ids[0]}.jsonl"
        ctx.check("the forked copy still exists (truncated, never deleted)", forked_path.exists())
        ctx.check(f"truncated back to the ORIGINAL's own byte-for-byte content at fork time, got sizes "
                  f"{forked_path.stat().st_size if forked_path.exists() else None} vs {len(original_bytes)}",
                  forked_path.exists() and forked_path.read_bytes() == original_bytes)
    finally:
        _end(mock)


@test
def test_f1_no_session_persistence_still_fully_deletes_a_brand_new_session(ctx: Ctx):
    """Regression guard: a session THIS process created from scratch must
    still be fully erased (the pre-existing behaviour for the actual
    documented use case), not merely truncated."""
    from halo_harness.agent import sessions as agent_sessions

    fh, mock = _begin()
    try:
        code, _ = _run(fh["proj"], model_ref_raw="or:mock/model", prompt="throwaway",
                        cli_flags={"no_session_persistence": True})
        ctx.check(f"exit 0, got {code}", code == 0)
        sdir = agent_sessions.sessions_dir(fh["proj"])
        remaining = list(sdir.glob("*.jsonl")) if sdir.is_dir() else []
        ctx.check(f"a brand-new session's own log is still fully removed, got {remaining}", not remaining)
        ctx.check("index.json has no entry either", agent_sessions.load_index(fh["proj"]) == {})
    finally:
        _end(mock)


# ---- finding 2 (critical) + --restricted parity gap: the TUI path never --
# ---- threaded cli_flags (or -w) into build_session at all -----------------

def _parsed_args(argv):
    from halo_harness.cli import _build_parser
    return _build_parser().parse_args(argv)


@test
def test_f2_restricted_reaches_the_tui_and_strips_write_and_mcp_tools(ctx: Ctx):
    """Pins both finding 2 (cli_flags reached build_session at all) and the
    --restricted parity gap (it is a READ-ONLY tool set, not just
    Bash/PowerShell/WebFetch)."""
    from halo_harness.tui.bootstrap import build_controller

    fh = _begin_home()
    try:
        args = _parsed_args(["--restricted", "--strict-mcp-config", "--cwd", str(fh["proj"]),
                              "--model", "or:mock/model"])
        controller, _registry, _facade = build_controller(args)
        catalog = getattr(controller.session, "session_catalog", None)
        ctx.check("session has a live tool catalog", catalog is not None)
        names = set(catalog.registry.names())
        ctx.check(f"Bash removed, got {sorted(names)}", "Bash" not in names)
        ctx.check("PowerShell removed", "PowerShell" not in names)
        ctx.check("WebFetch removed", "WebFetch" not in names)
        ctx.check("Edit removed (parity gap)", "Edit" not in names)
        ctx.check("Write removed (parity gap)", "Write" not in names)
        ctx.check("NotebookEdit removed (parity gap)", "NotebookEdit" not in names)
        ctx.check(f"no mcp__ tool survives, got {sorted(n for n in names if n.startswith('mcp__'))}",
                  not any(n.startswith("mcp__") for n in names))
        ctx.check("Read (genuinely read-only) is kept", "Read" in names)
    finally:
        _end_home()


@test
def test_f2_fallback_model_flag_reaches_the_tui_session(ctx: Ctx):
    from halo_harness.tui.bootstrap import build_controller

    fh = _begin_home()
    try:
        args = _parsed_args(["--strict-mcp-config", "--cwd", str(fh["proj"]), "--model", "or:mock/model",
                              "--fallback-model", "dbx:databricks-glm-5-3"])
        controller, _registry, _facade = build_controller(args)
        ctx.check(f"fallback_models reached the Session, got {controller.session.fallback_models!r}",
                  controller.session.fallback_models == ["dbx:databricks-glm-5-3"])
    finally:
        _end_home()


@test
def test_f2_plugin_dir_flag_reaches_the_tui_session(ctx: Ctx):
    import tempfile
    from halo_harness.tui.bootstrap import build_controller

    fh = _begin_home()
    plugin_dir = Path(tempfile.mkdtemp(prefix="w6a-plugin-"))
    try:
        args = _parsed_args(["--strict-mcp-config", "--cwd", str(fh["proj"]), "--model", "or:mock/model",
                              "--plugin-dir", str(plugin_dir)])
        controller, _registry, _facade = build_controller(args)
        roots = [Path(p) for p in controller.session.plugin_roots]
        ctx.check(f"the plugin dir resolved into the Session, got {roots!r}", plugin_dir in roots)
    finally:
        _end_home()


@test
def test_f2_worktree_flag_creates_a_real_worktree_in_the_tui(ctx: Ctx):
    import subprocess
    from halo_harness.tui.bootstrap import build_controller

    fh = _begin_home()
    try:
        repo = fh["proj"]
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
        (repo / "README.md").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

        args = _parsed_args(["--strict-mcp-config", "--cwd", str(repo), "--model", "or:mock/model", "-w"])
        controller, _registry, _facade = build_controller(args)
        session_cwd = Path(controller.session.cwd)
        ctx.check(f"-w moved the TUI session into a fresh worktree, got {session_cwd}",
                  session_cwd != repo and "worktrees" in str(session_cwd) and session_cwd.is_dir())
    finally:
        _end_home()


@test
def test_f2_betas_flag_reaches_the_tui_session_extra_headers(ctx: Ctx):
    from halo_harness.tui.bootstrap import build_controller

    fh = _begin_home()
    try:
        args = _parsed_args(["--strict-mcp-config", "--cwd", str(fh["proj"]), "--model", "ant:claude-mock",
                              "--betas", "x-beta-1", "x-beta-2"])
        controller, _registry, _facade = build_controller(args)
        ctx.check(f"anthropic-beta reached the Session's extra_headers, got {controller.session.extra_headers!r}",
                  controller.session.extra_headers.get("anthropic-beta") == "x-beta-1,x-beta-2")
    finally:
        _end_home()


# ---- finding 9 (major), second half: the shadow "before" snapshot is ----
# ---- taken synchronously on the session worker, before Bash dispatches --

def _text_step(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _tool_call_step(name: str, arguments: dict, call_id: str = "call_1") -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


@test
def test_f9_bash_shadow_before_is_captured_synchronously_pre_dispatch(ctx: Ctx):
    """finding 9 (W6a), second half: the TUI used to take the Bash shadow
    "before" snapshot on a SEPARATE Textual worker thread, scheduled only
    once the drain loop reacted to `tool_use_ready` -- the session worker
    dispatches the real command right after yielding that event
    regardless, so a fast command could finish first, making "before"
    identical to "after". Now taken synchronously on the session's own
    worker, right before `tool_registry.dispatch(...)` -- the real
    command's own new file must NEVER appear in the snapshot carried on
    the matching `tool_result` event's `bash_shadow_before`."""
    import subprocess as _sp
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds
    from halo_harness.shadow import git_status_dirty_paths

    repo = Path(tempfile.mkdtemp(prefix="w6a-f9-repo-"))
    _sp.run(["git", "init", "-q"], cwd=str(repo), check=True)
    _sp.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)

    SCENARIOS["w6a-f9-bash-sync"] = ScriptedTurns([
        _tool_call_step("Bash", {"command": "echo hi > newfile.txt"}, call_id="call_1"),
        _text_step("done"),
    ])
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        model = "or:mock/w6a-f9-bash-sync"
        session = Session(
            cwd=repo, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="w6a-f9-state-")), model_label=model,
            session_context=SessionContext(cwd=repo, model_label=model, bare=True),
            openrouter_base_url=mock.base_url, max_turns=6,
            permission_engine=PermissionEngine(mode="auto", cwd=repo), agents={}, routes={},
        )
        events_seen = list(session.turn("make a file"))
        results = [e for e in events_seen if e.kind == "tool_result" and e.data.get("id") == "call_1"]
        ctx.check(f"the Bash tool_result event was yielded, got {[e.kind for e in events_seen]}",
                  len(results) == 1)
        before = results[0].data.get("bash_shadow_before") if results else None
        ctx.check(f"a before-snapshot dict was carried on the event, got {before!r}", isinstance(before, dict))
        new_path = str((repo / "newfile.txt").resolve())
        ctx.check(f"the command's OWN new file is NOT in the pre-dispatch snapshot, got {before}",
                  before is not None and new_path not in before)
        ctx.check("the command actually ran (the file exists now)", (repo / "newfile.txt").is_file())
        after = git_status_dirty_paths(repo)
        ctx.check(f"a snapshot taken AFTER the turn does see it, got {after}",
                  after is not None and new_path in after)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)


# ---- finding 10 (major): context: fork/agent skills must surface asks, ---
# ---- never block forever on a waiter with no live channel to answer it --

@test
def test_f10_fork_skill_child_does_not_hang_on_a_permission_ask_with_no_live_channel(ctx: Ctx):
    """finding 10 (W6a): `tools/skill.py`'s `context: fork`/`agent` path
    calls `run_agent_call(...)` with NO `on_event` -- in an interactive
    (default-mode) session, `_build_child_session` used to set
    `_subagent_live_asks = True` purely from `parent.interactive`, so the
    child's first permission ask took the LIVE/blocking path with no live
    consumer anywhere to ever answer it (the turn hung until Esc). Fixed
    per the brief's own second option: build the child with live asks OFF
    when no live channel (`on_event`) exists for a foreground child."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.agent.subagent import AgentRuntime, run_agent_call
    from halo_harness.config.agents_md import AgentSpec
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    fh, mock = _begin()
    try:
        model = "or:mock/w6a-f10-child"
        SCENARIOS["w6a-f10-child"] = ScriptedTurns([
            [{"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "Bash", "arguments": json.dumps({"command": "mkdir newdir"})}},
            ]}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}],
            # step 1 (one "tool" message already in history, i.e. after the
            # Bash call got its result -- denied or not): a plain reply, so
            # the turn ends cleanly instead of retrying Bash forever.
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"content": "got it"}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}],
        ])

        parent_cwd = fh["proj"]
        parent = Session(
            cwd=parent_cwd, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="w6a-f10-state-")), model_label=model,
            session_context=SessionContext(cwd=parent_cwd, model_label=model, bare=True),
            openrouter_base_url=mock.base_url, max_turns=4,
            # "default" mode, never pre-allowed -- Bash needs a real
            # permission decision ("ask"), not an automatic allow.
            permission_engine=PermissionEngine(mode="default", cwd=parent_cwd, print_mode=False),
            agents={}, routes={},
        )
        parent.interactive = True  # simulates a real TUI session

        runtime = AgentRuntime(parent=parent, agents={
            "general-purpose": AgentSpec(name="general-purpose", description="gp", model=model),
        }, routes={})

        import threading
        result_box = {}

        def _call():
            events_seen, tool_result = run_agent_call(
                runtime=runtime, tool_id="toolu_skillfork", tool_name="Skill",
                tool_input={"description": "Skill: test", "prompt": "run a command",
                            "subagent_type": "general-purpose"},
                # on_event deliberately omitted -- exactly what tools/skill.py's
                # context: fork/agent path does.
            )
            result_box["events"] = events_seen
            result_box["result"] = tool_result

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(timeout=15)
        ctx.check("run_agent_call returned promptly instead of hanging on an unanswerable ask",
                  not t.is_alive())
        result = result_box.get("result")
        ctx.check(f"a result came back at all, got {result_box}", result is not None)
        events_seen = result_box.get("events") or []
        denial_results = [e for e in events_seen if e.kind == "tool_result" and not e.data.get("ok")
                           and "permission" in str(e.data.get("summary", "")).lower()]
        ctx.check(f"the Bash permission ask was resolved as an immediate, explained denial (never a "
                  f"live card nothing could answer), got tool_result events={[e.data for e in events_seen if e.kind == 'tool_result']}",
                  len(denial_results) == 1)
    finally:
        _end(mock)


# ---- finding 11 (major): ConnectorTool.run must poll ctx.abort and kill --
# ---- the whole process group, not block for the full 120s timeout --------

@test
def test_f11_connector_tool_run_returns_quickly_after_abort(ctx: Ctx):
    """finding 11 (W6a): a plain blocking `subprocess.run(timeout=120)`
    never read `ctx.abort` at all -- Esc did nothing for up to 120s on any
    `connector__*` call. Now routed through `tools._proc.run_streamed`
    (the Bash tool's own runner): a fake slow `claude` that just sleeps
    must be killed and this call must return within a few seconds of
    `ctx.abort.set()`, not the full timeout."""
    import threading
    from halo_harness.mcp.connectors import ConnectorInfo
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.connector_tool import ConnectorTool

    fake_dir = Path(tempfile.mkdtemp(prefix="w6a-f11-"))
    fake_script = fake_dir / "fake_slow_claude.py"
    fake_script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    info = ConnectorInfo(name="Slow Connector", slug="slow_connector", account_token="Slow",
                          url="https://example.invalid/mcp", host="example.invalid",
                          status="connected", status_text="connected", tools=["do_thing"])
    tool = ConnectorTool(info)
    saved = os.environ.get("HALO_CLAUDE_EXE")
    os.environ["HALO_CLAUDE_EXE"] = f'"{sys.executable}" "{fake_script}"'
    try:
        ctx_obj = ToolContext(cwd=Path.cwd())
        result_box = {}

        def _run():
            result_box["result"] = tool.run({"request": "do the slow thing"}, ctx_obj)

        t0 = time.monotonic()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.8)  # let the fake subprocess actually start first
        ctx_obj.abort.set()
        t.join(timeout=15)
        elapsed = time.monotonic() - t0
        ctx.check(f"returned within a few seconds of abort (never the full 120s), got {elapsed:.1f}s",
                  elapsed < 10.0)
        ctx.check("the worker thread actually finished (not just gave up waiting)", not t.is_alive())
        result = result_box.get("result")
        ctx.check(f"a result came back, got {result_box}", result is not None)
        ctx.check(f"marked as an error (interrupted), got {(result.content if result else None)!r}",
                  result is not None and result.is_error)
    finally:
        if saved is None:
            os.environ.pop("HALO_CLAUDE_EXE", None)
        else:
            os.environ["HALO_CLAUDE_EXE"] = saved


# ---- finding 14 (major): isolation: worktree children ---------------------

def _git_init_repo(repo: Path) -> None:
    import subprocess as _sp
    _sp.run(["git", "init", "-q"], cwd=str(repo), check=True)
    _sp.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _sp.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    _sp.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)


def _new_parent_session(repo, mock, model="or:mock/f14-parent"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=repo, model_label=model, bare=True)
    return Session(
        cwd=repo, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="w6a-f14-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=6,
        permission_engine=PermissionEngine(mode="auto", cwd=repo), agents={}, routes={},
    )


@test
def test_f14_child_permission_engine_and_hook_runner_root_at_worktree_cwd(ctx: Ctx):
    """finding 14 (W6a): both used to root at `parent.cwd` unconditionally
    -- for an `isolation: worktree` child, every edit landed OUTSIDE the
    engine's own working directory."""
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session
    from halo_harness.config.agents_md import AgentSpec
    from halo_harness.hooks import HookRunner

    fh, mock = _begin()
    try:
        repo = fh["proj"]
        _git_init_repo(repo)
        parent = _new_parent_session(repo, mock)
        parent.hook_runner = HookRunner({}, cwd=repo, session_id="parent-sess", transcript_path="x")
        spec = AgentSpec(name="wt-agent", description="d", isolation="worktree")
        runtime = AgentRuntime(parent=parent, agents={"wt-agent": spec}, routes={})
        child, _meta_path = _build_child_session(
            runtime=runtime, spec=spec, agent_id="agentid1", model_override=None,
            parent_tool_use_id="toolu_1", background=False, role_override=None,
        )
        ctx.check(f"the child actually got its own worktree cwd, got {child.cwd}", child.cwd != repo)
        ctx.check(f"PermissionEngine is rooted at the CHILD's cwd, got {child.permission_engine.cwd}",
                  Path(child.permission_engine.cwd) == Path(child.cwd))
        ctx.check(f"HookRunner is rooted at the CHILD's cwd, got {child.hook_runner.cwd}",
                  Path(child.hook_runner.cwd) == Path(child.cwd))
        ctx.check(f"a worktree path was recorded for later cleanup, got "
                  f"{getattr(child, '_isolation_worktree_path', None)}",
                  getattr(child, "_isolation_worktree_path", None) == child.cwd)
    finally:
        _end(mock)


@test
def test_f14_clean_worktree_removed_and_branch_deleted_when_child_ends(ctx: Ctx):
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session, _finalize_child_isolation_worktree
    from halo_harness.config.agents_md import AgentSpec
    import subprocess as _sp

    fh, mock = _begin()
    try:
        repo = fh["proj"]
        _git_init_repo(repo)
        parent = _new_parent_session(repo, mock)
        spec = AgentSpec(name="wt-agent", description="d", isolation="worktree")
        runtime = AgentRuntime(parent=parent, agents={"wt-agent": spec}, routes={})
        child, _meta_path = _build_child_session(
            runtime=runtime, spec=spec, agent_id="agentid2", model_override=None,
            parent_tool_use_id="toolu_1", background=False, role_override=None,
        )
        wt_path, branch = child.cwd, child._isolation_worktree_branch
        ctx.check("the worktree directory exists before cleanup", wt_path.is_dir())
        ctx.check(f"a branch name was captured, got {branch!r}", bool(branch))

        note = _finalize_child_isolation_worktree(child)
        ctx.check(f"no note (nothing dirty to surface), got {note!r}", note is None)
        ctx.check(f"the worktree directory is gone, got exists={wt_path.exists()}", not wt_path.exists())
        branches = _sp.run(["git", "branch", "--list", branch], cwd=str(repo), capture_output=True, text=True).stdout
        ctx.check(f"its branch was also deleted, got branch listing={branches!r}", branch not in branches)
    finally:
        _end(mock)


@test
def test_f14_dirty_worktree_kept_and_surfaced_when_child_ends(ctx: Ctx):
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session, _finalize_child_isolation_worktree
    from halo_harness.config.agents_md import AgentSpec

    fh, mock = _begin()
    try:
        repo = fh["proj"]
        _git_init_repo(repo)
        parent = _new_parent_session(repo, mock)
        spec = AgentSpec(name="wt-agent", description="d", isolation="worktree")
        runtime = AgentRuntime(parent=parent, agents={"wt-agent": spec}, routes={})
        child, _meta_path = _build_child_session(
            runtime=runtime, spec=spec, agent_id="agentid3", model_override=None,
            parent_tool_use_id="toolu_1", background=False, role_override=None,
        )
        wt_path, branch = child.cwd, child._isolation_worktree_branch
        (wt_path / "new_work.txt").write_text("the sub-agent's own work", encoding="utf-8")

        note = _finalize_child_isolation_worktree(child)
        ctx.check(f"a note naming the kept tree came back, got {note!r}",
                  note is not None and str(wt_path) in note and (not branch or branch in note))
        ctx.check("the worktree directory was NOT removed", wt_path.is_dir())
        ctx.check("the sub-agent's own file is still there", (wt_path / "new_work.txt").is_file())
    finally:
        _end(mock)


# ---- finding 18 (major): --permission-prompt-tool's wire shape ------------

class _FakeMcpResult:
    def __init__(self, *, text, is_error=False):
        self.content = [SimpleNamespace(text=text)]
        self.isError = is_error


class _FakePromptToolManager:
    def __init__(self, reply_json: str):
        self.reply_json = reply_json
        self.calls = []

    def call(self, server, tool, args, timeout=30):
        self.calls.append({"server": server, "tool": tool, "args": args})
        return _FakeMcpResult(text=self.reply_json)


@test
def test_f18_ask_permission_prompt_tool_sends_input_and_tool_use_id(ctx: Ctx):
    """finding 18 (W6a): Claude Code's documented contract sends {tool_
    name, input, tool_use_id} -- a tool written against that schema
    (which REQUIRES `input`) errored on every ask when this harness sent
    {tool_name, tool_input, reason} instead. `tool_input` and `reason`
    are kept too, as harmless aliases/extras."""
    fh, mock = _begin()
    try:
        session = _session_for_f18(fh, mock)
        fake_mcp = _FakePromptToolManager('{"behavior": "allow"}')
        session.mcp_manager = fake_mcp
        session.permission_prompt_tool = "mcp__ppt_srv__ask"
        result = session._ask_permission_prompt_tool("Bash", {"command": "ls"}, "mode asks", "toolu_123")
        ctx.check(f"allow with no extra fields, got {result}", result == {
            "behavior": "allow", "updatedInput": None, "message": None})
        ctx.check(f"exactly one call was made, got {fake_mcp.calls}", len(fake_mcp.calls) == 1)
        sent = fake_mcp.calls[0]
        ctx.check(f"routed to the right server/tool, got {sent}", sent["server"] == "ppt_srv" and sent["tool"] == "ask")
        args = sent["args"]
        ctx.check(f"'input' carries the tool_input (documented field), got {args}",
                  args.get("input") == {"command": "ls"})
        ctx.check(f"'tool_input' is kept as an alias, got {args}", args.get("tool_input") == {"command": "ls"})
        ctx.check(f"'tool_use_id' is present, got {args}", args.get("tool_use_id") == "toolu_123")
        ctx.check(f"'tool_name' is present, got {args}", args.get("tool_name") == "Bash")
    finally:
        _end(mock)


@test
def test_f18_ask_permission_prompt_tool_parses_updated_input_and_message(ctx: Ctx):
    fh, mock = _begin()
    try:
        session = _session_for_f18(fh, mock)
        session.permission_prompt_tool = "mcp__ppt_srv__ask"

        session.mcp_manager = _FakePromptToolManager('{"behavior": "allow", "updatedInput": {"command": "ls -la"}}')
        allow_result = session._ask_permission_prompt_tool("Bash", {"command": "ls"}, "r", "t1")
        ctx.check(f"updatedInput parsed on allow, got {allow_result}",
                  allow_result == {"behavior": "allow", "updatedInput": {"command": "ls -la"}, "message": None})

        session.mcp_manager = _FakePromptToolManager('{"behavior": "deny", "message": "custom denial text"}')
        deny_result = session._ask_permission_prompt_tool("Bash", {"command": "ls"}, "r", "t2")
        ctx.check(f"message parsed on deny, got {deny_result}",
                  deny_result == {"behavior": "deny", "updatedInput": None, "message": "custom denial text"})

        session.mcp_manager = _FakePromptToolManager("allow")
        bare_result = session._ask_permission_prompt_tool("Bash", {"command": "ls"}, "r", "t3")
        ctx.check(f"a bare text reply still works, got {bare_result}",
                  bare_result == {"behavior": "allow", "updatedInput": None, "message": None})
    finally:
        _end(mock)


def _session_for_f18(fh, mock, *, model="or:mock/f18-model"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds
    cwd = fh["proj"]
    return Session(
        cwd=cwd, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="w6a-f18-state-")), model_label=model,
        session_context=SessionContext(cwd=cwd, model_label=model, bare=True),
        openrouter_base_url=mock.base_url, max_turns=4,
        permission_engine=PermissionEngine(mode="default", cwd=cwd),
        agents={}, routes={},
        cli_flags={"permission_prompt_tool": "mcp__ppt_srv__ask", "permission_prompts": "host"},
    )


@test
def test_f18_updated_input_from_permission_prompt_tool_reaches_the_real_tool_call(ctx: Ctx):
    """End to end: a non-readonly Bash call needs a real permission
    decision ("ask") in default mode; non-interactively that routes to
    --permission-prompt-tool, whose `updatedInput` reply must be what
    ACTUALLY runs, not the model's own original command."""
    fh, mock = _begin()
    try:
        SCENARIOS["w6a-f18-bash"] = ScriptedTurns([
            _tool_call_step_f18("Bash", {"command": "mkdir original_dir"}, call_id="call_1"),
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"content": "done"}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}],
        ])
        session = _session_for_f18(fh, mock, model="or:mock/w6a-f18-bash")
        session.mcp_manager = _FakePromptToolManager(
            '{"behavior": "allow", "updatedInput": {"command": "mkdir renamed_dir"}}')
        events_seen = list(session.turn("make a directory"))
        results = [e for e in events_seen if e.kind == "tool_result" and e.data.get("id") == "call_1"]
        ctx.check(f"the Bash call succeeded, got {results}", results and results[0].data.get("ok") is True)
        ctx.check("the ORIGINAL directory was never created", not (fh["proj"] / "original_dir").exists())
        ctx.check("the RENAMED (updatedInput) directory WAS created", (fh["proj"] / "renamed_dir").is_dir())
    finally:
        _end(mock)


def _tool_call_step_f18(name: str, arguments: dict, call_id: str = "call_1") -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


# ---- parity gap: --betas's extra_headers recomputed on /model switch -----

@test
def test_parity_betas_header_dropped_on_switch_away_from_anthropic_family(ctx: Ctx):
    """parity gap (W6a): `Session.extra_headers` used to be fixed at
    construction time -- a `/model` switch away from an Anthropic-family
    route kept sending `anthropic-beta` to the new (OpenRouter/Databricks-
    chat) route, the exact cross-route leak part 10 already fixed for
    session start."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    fh, mock = _begin()
    try:
        # `build_session` (not `Session.__init__` itself) is what computes
        # this ONCE at construction from `cli_flags["betas"]` -- passed
        # explicitly here to simulate that already-correct starting
        # state (finding 2's own TUI test covers THAT computation); this
        # test's own focus is `set_model`'s later recomputation.
        model = "ant:claude-mock"
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url="https://api.anthropic.com", api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="w6a-parity-betas-")), model_label=model,
            session_context=SessionContext(cwd=fh["proj"], model_label=model, bare=True),
            cli_flags={"betas": ["x-beta-1"]}, extra_headers={"anthropic-beta": "x-beta-1"},
        )
        ctx.check(f"starts WITH the beta header (anthropic-family), got {session.extra_headers}",
                  session.extra_headers.get("anthropic-beta") == "x-beta-1")

        session.set_model(parse_model_ref("or:mock/model"), ModelProfile())
        ctx.check(f"dropped after switching to a non-Anthropic-family route, got {session.extra_headers}",
                  "anthropic-beta" not in session.extra_headers)

        session.set_model(parse_model_ref("ant:claude-mock-2"), ModelProfile())
        ctx.check(f"gained back when switching INTO an Anthropic-family route again, got {session.extra_headers}",
                  session.extra_headers.get("anthropic-beta") == "x-beta-1")
    finally:
        _end(mock)


# ---- 2.0.2 review finding 7 (major): a decision-only endpoint can never
# ---- carry Judge's own hard-coded Explore tool set -------------------

@test
def test_2_0_2_finding7_judge_on_decision_only_endpoint_runs_tool_less(ctx: Ctx):
    """2.0.2 review finding 7 (major) pin: the built-in Judge agent
    always carries Explore's six tools -- a decision-only/judge endpoint
    (`roles.judge` is auto-routed there by `Controller.set_model`/
    `headless.build_session`) can never actually take a tool call, so
    every one of its requests raised ToolsNotSupported before the model
    ever answered. The child must build with an EMPTY tool registry
    instead whenever the RESOLVED model itself can't take tools,
    regardless of which agent/role asked for it."""
    from halo_harness.agent.subagent import AgentRuntime, _build_child_session
    from halo_harness.config.agents_md import AgentSpec
    fh, mock = _begin()
    try:
        repo = fh["proj"]
        parent = _new_parent_session(repo, mock)
        spec = AgentSpec(name="Judge", description="d",
                          tools=["Read", "Glob", "Grep", "Bash", "WebFetch", "ToolSearch"],
                          body="You are a judging sub-agent.")
        runtime = AgentRuntime(parent=parent, agents={"Judge": spec}, routes={})
        child, _meta_path = _build_child_session(
            runtime=runtime, spec=spec, agent_id="agentid-judge",
            model_override="dbx:databricks-openjev-qwen35-4b",
            parent_tool_use_id="toolu_1", background=False, role_override=None,
        )
        ctx.check(f"the resolved model really is decision-only (test premise), got {child.model_ref.raw}",
                  child.model_ref.provider == "databricks")
        ctx.check(f"the child's own tool registry is EMPTY, got {child.tool_registry.names()}",
                  child.tool_registry.names() == [])
        ctx.check("the system prompt tells the model it has no tools at all",
                  "no tool call" in (child.session_context.system_prompt or "").lower())
    finally:
        _end(mock)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
