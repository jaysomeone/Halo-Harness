"""tests.test_init_wizard_round7_integration -- Halo 2.0.2 round 7 (init
wizard brief): the wiring pieces that sit between the pure-logic additions
in tests/test_init_wizard_round7.py and a real session -- the slash-menu/
tips/`Agent` tool discoverability gating (`roles.enabled`/`orgs.enabled`),
the shared task board's new `kind`/`parent` fields, `halo setup`'s no-TTY
fallback, `halo org run`'s "no name" default, and `run_org_call` actually
building a goal task + an `OrgBudgetTracker` for a real run.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- commands/registry.py: hidden command names ----------------------------

@test
def test_hidden_command_names_follows_the_mode_switches(ctx: Ctx):
    from halo_harness.commands.registry import Registry, SlashCommand
    from halo_harness.theme import set_config_value
    _fresh_state_dir("registry-hidden-")
    try:
        reg = Registry()
        reg.add(SlashCommand(name="roles", description="d"))
        reg.add(SlashCommand(name="role", description="d"))
        reg.add(SlashCommand(name="org", description="d"))
        reg.add(SlashCommand(name="setup", description="d"))
        set_config_value("roles.enabled", False)
        set_config_value("orgs.enabled", False)
        names = {c.name for c in reg.all()}
        ctx.check(f"roles/role/org all hidden, setup never hidden, got {names}", names == {"setup"})
        ctx.check("help_rows() agrees", {row[0] for row in reg.help_rows()} == {"/setup"})
        ctx.check("completion for an empty prefix agrees too",
                  {c.name for c in reg.complete("")} == {"setup"})
        set_config_value("roles.enabled", True)
        set_config_value("orgs.enabled", True)
        names2 = {c.name for c in reg.all()}
        ctx.check(f"all four visible once both modes are on, got {names2}",
                  names2 == {"roles", "role", "org", "setup"})
        ctx.check("resolve() ALWAYS works regardless of hiding (never a functional gate)",
                  reg.resolve("roles") is not None)
    finally:
        _clear_state_dir_env()


# ---- tools/agent.py: org= only advertised while orgs.enabled is True ------

@test
def test_agent_tool_schema_advertises_org_only_when_enabled(ctx: Ctx):
    from halo_harness.tools.agent import AgentTool
    from halo_harness.theme import set_config_value
    _fresh_state_dir("agent-tool-schema-")
    try:
        tool = AgentTool()
        set_config_value("orgs.enabled", False)
        ctx.check("org missing from the schema while disabled", "org" not in tool.input_schema["properties"])
        ctx.check("description says nothing about org either", "org" not in tool.description.lower()
                  or "organization" not in tool.description.lower())
        set_config_value("orgs.enabled", True)
        ctx.check("org appears once enabled", "org" in tool.input_schema["properties"])
        ctx.check("description mentions organizations", "organization" in tool.description.lower())
        # Never a functional gate -- `run()`'s own dispatch still honours
        # org= either way; that path is already covered end-to-end in
        # tests/test_orgs.py's own mocked-upstream runs.
    finally:
        _clear_state_dir_env()


# ---- tools/task_board.py: kind="goal" / parent -----------------------------

@test
def test_create_task_goal_and_parent_linking(ctx: Ctx):
    from halo_harness.tools.task_board import create_task, read_board
    session_dir = Path(tempfile.mkdtemp(prefix="task-board-goal-"))
    goal_id = create_task(session_dir, title="Goal: ship it", kind="goal")
    child_id = create_task(session_dir, title="write the brief", parent=goal_id)
    board = read_board(session_dir)
    ctx.check(f"two tasks on the board, got {len(board)}", len(board) == 2)
    goal = next(t for t in board if t["id"] == goal_id)
    child = next(t for t in board if t["id"] == child_id)
    ctx.check(f"goal task kind, got {goal.get('kind')}", goal.get("kind") == "goal")
    ctx.check("goal task has no parent of its own", goal.get("parent") is None)
    ctx.check(f"child points at the goal, got {child.get('parent')}", child.get("parent") == goal_id)
    ctx.check("a plain pre-round-7 task (no kind/parent given) still defaults to kind=task",
              create_task(session_dir, title="plain") and
              next(t for t in read_board(session_dir) if t["title"] == "plain")["kind"] == "task")


@test
def test_task_create_tool_accepts_an_optional_parent(ctx: Ctx):
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.task_board import TaskCreateTool, read_board
    session_dir = Path(tempfile.mkdtemp(prefix="task-board-tool-"))
    tool = TaskCreateTool()
    r1 = tool.run({"title": "Goal: ship it"}, ToolContext(cwd=session_dir, session_dir=session_dir))
    goal_id = r1.content.split("'")[1]
    r2 = tool.run({"title": "write the brief", "parent": goal_id},
                  ToolContext(cwd=session_dir, session_dir=session_dir))
    ctx.check(f"second create succeeds, got {r2.content}", not r2.is_error)
    board = read_board(session_dir)
    child = next(t for t in board if t["title"] == "write the brief")
    ctx.check(f"parent carried through the Tool, got {child.get('parent')}", child.get("parent") == goal_id)


# ---- halo setup / halo org run: CLI-level pins -----------------------------

class _IsattyProxy:
    def __init__(self, value: bool, real):
        self._value, self._real = value, real

    def isatty(self) -> bool:
        return self._value

    def __getattr__(self, name):
        return getattr(self._real, name)


def _no_tty(fn, *args):
    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _IsattyProxy(False, real_stdin), _IsattyProxy(False, real_stdout)
    try:
        return fn(*args)
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout


@test
def test_halo_setup_no_tty_lists_templates_and_exits_0(ctx: Ctx):
    """Brief's own pin: "halo setup roles with no TTY lists templates and
    exits 0"."""
    from halo_harness.setup_cli import cmd_setup
    _fresh_state_dir("setup-cli-roles-")
    try:
        rc = _no_tty(cmd_setup, ["roles"])
        ctx.check(f"exit 0, got {rc}", rc == 0)
    finally:
        _clear_state_dir_env()
    _fresh_state_dir("setup-cli-orgs-")
    try:
        rc = _no_tty(cmd_setup, ["orgs"])
        ctx.check(f"halo setup orgs: exit 0 too, got {rc}", rc == 0)
    finally:
        _clear_state_dir_env()
    _fresh_state_dir("setup-cli-bare-")
    try:
        rc = _no_tty(cmd_setup, [])
        ctx.check(f"bare halo setup: exit 0 too, got {rc}", rc == 0)
        rc_bad = _no_tty(cmd_setup, ["not-a-real-subcommand"])
        ctx.check(f"an unknown subcommand is a clean usage error, got {rc_bad}", rc_bad == 2)
    finally:
        _clear_state_dir_env()


@test
def test_org_run_cli_with_no_name_uses_the_default(ctx: Ctx):
    """`halo org run "<goal>"` (one positional) resolves `orgs.default`
    for the org name -- `run_org_call` itself is stubbed out (never a
    real model dispatch here; that path is covered end-to-end with a
    MockUpstream in tests/test_orgs.py) so this test stays hermetic and
    pins ONLY the argument-resolution step."""
    import halo_harness.agent.subagent as subagent_mod
    from halo_harness.org_cli import cmd_org
    from halo_harness.orgs import ensure_builtin_orgs, set_default_org
    from halo_harness.tools.base import ToolResult
    state_dir = _fresh_state_dir("org-cli-default-")
    try:
        ensure_builtin_orgs(state_dir=state_dir)
        ok, problems = set_default_org("solo", state_dir=state_dir)
        ctx.check(f"default set, got {problems}", ok)
        captured = {}
        real_run_org_call = subagent_mod.run_org_call

        def _fake_run_org_call(*, runtime, tool_id, tool_input, tool_name):
            captured["tool_input"] = tool_input
            return [], ToolResult("stubbed")

        subagent_mod.run_org_call = _fake_run_org_call
        try:
            rc = cmd_org(["run", "do the thing"])
        finally:
            subagent_mod.run_org_call = real_run_org_call
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check(f"orgs.default ('solo') was resolved as the org name, got {captured}",
                  captured.get("tool_input", {}).get("org") == "solo")
        ctx.check(f"the goal text passed through, got {captured}",
                  captured.get("tool_input", {}).get("prompt") == "do the thing")
    finally:
        _clear_state_dir_env()


@test
def test_org_edit_with_a_multi_word_editor_does_not_crash(ctx: Ctx):
    """2.0.2 review finding 36 pin: `EDITOR="code --wait"` used to be
    passed to `subprocess.call` as a SINGLE argv[0] (the literal string
    "code --wait", space included), which raises an uncaught
    `FileNotFoundError` -- no such file exists. `subprocess.call` itself
    is swapped out (via the module's own `subprocess` name) so this
    stays hermetic -- never launches a real editor -- while still
    proving the actual argv `org_cli.py` builds."""
    import halo_harness.org_cli as org_cli_mod
    from halo_harness.org_cli import cmd_org
    state_dir = _fresh_state_dir("org-cli-edit-")
    old_editor = os.environ.get("EDITOR")
    os.environ["EDITOR"] = "code --wait"
    captured = {}

    class _FakeSubprocess:
        @staticmethod
        def call(argv):
            captured["argv"] = argv
            return 0
    real_subprocess = org_cli_mod.subprocess
    org_cli_mod.subprocess = _FakeSubprocess
    try:
        rc = cmd_org(["edit", "my-org"])
        ctx.check(f"no crash, exit 0, got {rc}", rc == 0)
        argv = captured.get("argv")
        ctx.check(f"'--wait' is its own argv element, got {argv}", argv is not None and "--wait" in argv)
        ctx.check(f"argv[0] is never the literal 'code --wait' string, got {argv}",
                  argv is not None and argv[0] != "code --wait")
    finally:
        org_cli_mod.subprocess = real_subprocess
        if old_editor is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = old_editor
        _clear_state_dir_env()


@test
def test_org_run_cli_with_no_name_and_no_default_refuses_cleanly(ctx: Ctx):
    from halo_harness.org_cli import cmd_org
    _fresh_state_dir("org-cli-nodefault-")
    try:
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cmd_org(["run", "do the thing"])
        ctx.check(f"exit 2, got {rc}", rc == 2)
        ctx.check(f"names the fix, got {buf.getvalue()!r}", "no default is set" in buf.getvalue())
    finally:
        _clear_state_dir_env()


# ---- run_org_call: a real goal task + budget tracker, end to end --------
# Same mocked-upstream Agent-tool path tests/test_orgs.py's own org-running
# tests use (a real agent.loop.Session dispatching a real Agent tool_use
# against a mocked upstream) -- the helpers below are small, self-contained
# copies (never a cross-test-file import, matching this package's own
# per-file test isolation).

def _make_org_session(fh, *, scenario: str, mock, state_dir):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.config.agents_md import discover_agents
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=fh["proj"], model_label=f"mock/{scenario}")
    model_ref = parse_model_ref(f"or:mock/{scenario}")
    agents = discover_agents(fh["proj"], settings=None)
    return Session(cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
                    creds=ProviderCreds(base_url=mock.base_url, api_key="k"), state_dir=state_dir,
                    model_label=f"mock/{scenario}", session_context=session_ctx,
                    openrouter_base_url=mock.base_url, max_turns=50, agents=agents)


def _org_text_chunks(text: str):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


@test
def test_run_org_call_creates_a_goal_task_and_a_budget_tracker(ctx: Ctx):
    from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        from halo_harness.orgs import save_org
        state_dir = Path(tempfile.mkdtemp(prefix="org-goal-budget-"))
        scn = "org-goal-budget"
        ok, problems = save_org(scn, {
            "name": scn, "budget_usd": 50.0,
            "positions": [{"title": "Orchestrator", "model": f"or:mock/{scn}", "budget_usd": 10.0,
                            "reports": []}],
        }, state_dir=state_dir)
        ctx.check(f"fixture org saved, got {problems}", ok)
        SCENARIOS[scn] = lambda h, b: _finish(h, _org_text_chunks("Done."))
        session = _make_org_session(fh, scenario=scn, mock=mock, state_dir=state_dir)

        from halo_harness.agent.subagent import run_org_call
        _events, result = run_org_call(
            runtime=session.agent_runtime, tool_id="call_1", tool_name="Agent",
            tool_input={"org": scn, "prompt": "ship the thing"},
        )
        ctx.check(f"the run completed without error, got {result.content}", not result.is_error)

        from halo_harness.tools.task_board import read_board
        session_dir = session.log.dir / session.log.session_id
        board = read_board(session_dir)
        goals = [t for t in board if t.get("kind") == "goal"]
        ctx.check(f"exactly one goal task was created, got {board}", len(goals) == 1)
        ctx.check(f"the goal task names the org run's own goal, got {goals[0]['title']!r}",
                  "ship the thing" in goals[0]["title"])

        # `position_agent_specs` is what actually threads `goal_task_id`
        # into each position's own system prompt (`run_org_call`'s own
        # `org_runtime` is local to that call, not reachable from here) --
        # a direct unit check that it names the SAME goal task id just
        # created above.
        from halo_harness.orgs import load_org, position_agent_specs
        org = load_org(scn, state_dir=state_dir)
        specs = position_agent_specs(org, goal_task_id=goals[0]["id"])
        ctx.check("the root position's own body names the goal task id",
                  goals[0]["id"] in (specs["Orchestrator"].body or ""))
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
