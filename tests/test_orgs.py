"""tests.test_orgs -- Halo 2.0.2 round 2 (brief B): `halo_harness.orgs`
schema load/validate/save round trip, built-in copy-once/never-overwritten,
`describe()`'s text tree, and running an org through the EXACT sub-agent
path `tests/test_agent_tool.py` already uses (a real `agent.loop.Session`
dispatching a real `Agent` tool_use against a mocked upstream): a root-only
`solo` run, a two-level org where the root delegates once and the result
flows back, and the `reports` restriction refusing a call outside it. The
TUI org editor's own pilot tests live in test_tui.py, next to the roles
editor's.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


# ---- schema: load/validate/save round trip, built-ins ---------------------

@test
def test_builtin_orgs_are_copied_once_and_valid(ctx: Ctx):
    from halo_harness.orgs import list_orgs, load_org, root_position, validate_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-builtin-"))
    names = list_orgs(state_dir=state_dir)
    ctx.check(f"all three built-ins present, got {names}", names == ["company", "release-flow", "solo"])
    for name in names:
        org = load_org(name, state_dir=state_dir)
        ctx.check(f"{name} loads", org is not None)
        ctx.check(f"{name} validates clean, got {validate_org(org)}", validate_org(org) == [])
        ctx.check(f"{name} has exactly one root", root_position(org) is not None)


@test
def test_builtin_orgs_never_overwritten_once_present(ctx: Ctx):
    from halo_harness.orgs import ensure_builtin_orgs, orgs_dir
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-noclobber-"))
    ensure_builtin_orgs(state_dir=state_dir)
    path = orgs_dir(state_dir) / "solo.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["description"] = "edited by the user"
    path.write_text(json.dumps(data), encoding="utf-8")
    ensure_builtin_orgs(state_dir=state_dir)  # a second "first use" must not touch it
    reread = json.loads(path.read_text(encoding="utf-8"))
    ctx.check("the user's edit survived a second ensure_builtin_orgs call",
              reread["description"] == "edited by the user")


@test
def test_reload_builtin_org_overwrites_the_users_copy(ctx: Ctx):
    """`/org load <name>` (TUI-only, brief item 3) -- the explicit,
    opt-in counterpart to the "never overwritten" default above."""
    from halo_harness.orgs import ensure_builtin_orgs, load_org, orgs_dir, reload_builtin_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-reload-"))
    ensure_builtin_orgs(state_dir=state_dir)
    path = orgs_dir(state_dir) / "solo.json"
    path.write_text(json.dumps({"name": "solo", "positions": [{"title": "X", "reports": []}]}), encoding="utf-8")
    ok, problems = reload_builtin_org("solo", state_dir=state_dir)
    ctx.check(f"reload reports success, got {problems}", ok)
    restored = load_org("solo", state_dir=state_dir)
    ctx.check("the shipped position title is back", restored["positions"][0]["title"] == "Orchestrator")
    ok2, problems2 = reload_builtin_org("not-a-builtin", state_dir=state_dir)
    ctx.check("an unknown name is refused", not ok2 and problems2)


@test
def test_save_load_round_trip(ctx: Ctx):
    from halo_harness.orgs import load_org, save_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-roundtrip-"))
    data = {"name": "pair", "description": "d", "positions": [
        {"title": "Lead", "role": "orchestrator", "effort": "high", "instructions": "lead", "reports": ["Aide"]},
        {"title": "Aide", "role": "coder", "instructions": "aide", "reports": []},
    ]}
    ok, problems = save_org("pair", data, state_dir=state_dir)
    ctx.check(f"save succeeds, got {problems}", ok)
    reloaded = load_org("pair", state_dir=state_dir)
    ctx.check("round trip preserves positions", reloaded["positions"] == data["positions"])


@test
def test_validate_rejects_unknown_role_and_dangling_reports(ctx: Ctx):
    from halo_harness.orgs import validate_org
    bad = {"name": "bad", "positions": [
        {"title": "A", "role": "not-a-real-role", "reports": ["B", "Ghost"]},
        {"title": "B", "reports": []},
    ]}
    problems = validate_org(bad)
    ctx.check(f"unknown role flagged, got {problems}",
              any("unknown role" in p and "not-a-real-role" in p for p in problems))
    ctx.check(f"dangling report flagged, got {problems}", any("Ghost" in p for p in problems))


@test
def test_validate_requires_exactly_one_root(ctx: Ctx):
    from halo_harness.orgs import validate_org
    two_roots = {"name": "tr", "positions": [{"title": "A", "reports": []}, {"title": "B", "reports": []}]}
    problems = validate_org(two_roots)
    ctx.check(f"two roots rejected, got {problems}", any("exactly one root" in p for p in problems))
    no_root = {"name": "nr", "positions": [{"title": "A", "reports": ["B"]}, {"title": "B", "reports": ["A"]}]}
    problems = validate_org(no_root)
    ctx.check(f"zero roots rejected, got {problems}", any("no root" in p for p in problems))


@test
def test_save_rejects_invalid_shape(ctx: Ctx):
    from halo_harness.orgs import save_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-badsave-"))
    ok, problems = save_org("x", {"positions": []}, state_dir=state_dir)
    ctx.check(f"empty positions refused, got {problems}", not ok and problems)


@test
def test_describe_renders_a_text_tree(ctx: Ctx):
    from halo_harness.orgs import describe, load_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-describe-"))
    text = describe(load_org("company", state_dir=state_dir))
    ctx.check(f"root title appears first, got {text!r}", text.splitlines()[1].strip().startswith("CEO"))
    ctx.check("a grandchild is indented under its manager", "    Engineer 1" in text)


@test
def test_org_tree_depth_counts_a_cycle_hop_once(ctx: Ctx):
    """release-flow's Fixer delegates back to Tester -- that hop must
    still count once toward the depth cap `run_org_call` sizes, or Fixer
    would be refused the very delegation the template is built around."""
    from halo_harness.orgs import load_org, org_tree_depth
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-depth-"))
    ctx.check("release-flow depth is 5", org_tree_depth(load_org("release-flow", state_dir=state_dir)) == 5)
    ctx.check("solo depth is 0", org_tree_depth(load_org("solo", state_dir=state_dir)) == 0)
    ctx.check("company depth is 3", org_tree_depth(load_org("company", state_dir=state_dir)) == 3)


# ---- running an org: the same mocked-upstream Agent-tool path as
# tests/test_agent_tool.py -------------------------------------------------

def _make_session(fh, *, scenario: str, mock: MockUpstream, state_dir=None, max_turns: int = 50):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.config.agents_md import discover_agents
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    session_ctx = SessionContext(cwd=fh["proj"], model_label=f"mock/{scenario}")
    model_ref = parse_model_ref(f"or:mock/{scenario}")
    agents = discover_agents(fh["proj"], settings=None)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=(state_dir or Path(tempfile.mkdtemp(prefix="org-test-"))), model_label=f"mock/{scenario}",
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=max_turns, agents=agents,
    )


def _text_chunk(text: str, *, finish: str = "stop"):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]


def _agent_tool_call_chunks(tool_id: str, subagent_type: str, prompt: str, *, description: str = "task"):
    input_obj = {"description": description, "prompt": prompt, "subagent_type": subagent_type}
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": tool_id, "type": "function",
             "function": {"name": "Agent", "arguments": json.dumps(input_obj)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _run_org_dispatch(session, tool_id: str, org: str, prompt: str):
    tu = {"type": "tool_use", "id": tool_id, "name": "Agent",
          "input": {"description": "run org", "prompt": prompt, "org": org}}
    return list(session._dispatch_tools(1, [tu]))


@test
def test_run_solo_org_root_only_no_delegation(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        scn = "org-solo-root"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[scn] = lambda h, b: _finish(h, _text_chunk("The solo orchestrator finished the task alone."))
        session = _make_session(fh, scenario=scn, mock=mock)
        events_seen = _run_org_dispatch(session, "call_1", "solo", "do the thing")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check(f"one tool_result, got {len(results)}", len(results) == 1)
        ctx.check(f"not an error, got {results[0].data}", results[0].data.get("ok") is True)
        ctx.check("the root's own answer reached the wrapper",
                  "finished the task alone" in results[0].data.get("content", ""))
        starts = [e for e in events_seen if e.kind == "subagent_start"]
        ctx.check(f"exactly one sub-agent spawned (the root), got {len(starts)}", len(starts) == 1)
        ctx.check(f"brief item 2: the dock label is '<title> (<role>)', got {starts[0].data.get('name')!r}",
                  starts[0].data.get("name") == "Orchestrator (orchestrator)")
    finally:
        mock.stop()


@test
def test_run_two_level_org_root_delegates_once_and_result_flows_back(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        from halo_harness.orgs import save_org
        state_dir = Path(tempfile.mkdtemp(prefix="org-twolevel-"))
        ok, problems = save_org("pair-ok", {
            "name": "pair-ok", "positions": [
                {"title": "Lead", "model": "or:mock/pair-ok-lead", "reports": ["Helper"]},
                {"title": "Helper", "model": "or:mock/pair-ok-helper", "reports": []},
            ],
        }, state_dir=state_dir)
        ctx.check(f"fixture org saved, got {problems}", ok)

        from tests.helpers.mock_openai import SCENARIOS, _finish

        def _lead(h, body):
            msgs = (body or {}).get("messages") or []
            if any(m.get("role") == "tool" for m in msgs):
                _finish(h, _text_chunk("Lead's final report: helper said 99."))
            else:
                _finish(h, _agent_tool_call_chunks("call_helper", "Helper", "please help"))

        SCENARIOS["pair-ok-lead"] = _lead
        SCENARIOS["pair-ok-helper"] = lambda h, b: _finish(h, _text_chunk("Helper says 99."))

        session = _make_session(fh, scenario="org-top-unused-1", mock=mock, state_dir=state_dir)
        events_seen = _run_org_dispatch(session, "call_1", "pair-ok", "go")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check(f"two tool_results (nested Helper call + the outer org-call wrap), got {len(results)}",
                  len(results) == 2)
        ctx.check(f"neither is an error, got {[e.data.get('ok') for e in results]}",
                  all(e.data.get("ok") is True for e in results))
        ctx.check(f"the helper's answer flowed all the way up into Lead's own final report, got {results[-1].data}",
                  "99" in results[-1].data.get("content", ""))
        starts = [e for e in events_seen if e.kind == "subagent_start"]
        ctx.check(f"two sub-agents spawned (Lead, then Helper), got {len(starts)}", len(starts) == 2)
    finally:
        mock.stop()


@test
def test_reports_restriction_refuses_a_call_outside_it(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        from halo_harness.orgs import save_org
        state_dir = Path(tempfile.mkdtemp(prefix="org-restrict-"))
        ok, problems = save_org("pair-bad", {
            "name": "pair-bad", "positions": [
                {"title": "Lead", "model": "or:mock/pair-bad-lead", "reports": ["Helper"]},
                {"title": "Helper", "model": "or:mock/pair-bad-helper", "reports": ["Sidekick"]},
                {"title": "Sidekick", "model": "or:mock/pair-bad-sidekick", "reports": []},
            ],
        }, state_dir=state_dir)
        ctx.check(f"fixture org saved, got {problems}", ok)

        from tests.helpers.mock_openai import SCENARIOS, _finish

        def _lead_tries_sidekick(h, body):
            msgs = (body or {}).get("messages") or []
            if any(m.get("role") == "tool" for m in msgs):
                _finish(h, _text_chunk("Lead gives up after being refused."))
            else:
                _finish(h, _agent_tool_call_chunks("call_x", "Sidekick", "sneak past my own reports"))

        SCENARIOS["pair-bad-lead"] = _lead_tries_sidekick

        session = _make_session(fh, scenario="org-top-unused-2", mock=mock, state_dir=state_dir)
        events_seen = _run_org_dispatch(session, "call_1", "pair-bad", "go")
        results = [e for e in events_seen if e.kind == "tool_result"]
        refusals = [e for e in results if e.data.get("ok") is False and "Sidekick" in e.data.get("content", "")]
        ctx.check(f"exactly one refusal naming Sidekick, got {[e.data for e in results]}", len(refusals) == 1)
        ctx.check(f"clear tool error wording, got {refusals[0].data.get('content', '')!r}",
                  "may not spawn" in refusals[0].data.get("content", ""))
        completed = [e for e in results if e.data.get("ok") is True and "gives up" in e.data.get("content", "")]
        ctx.check(f"the outer org call still completes normally, got {[e.data for e in results]}",
                  len(completed) == 1)
    finally:
        mock.stop()


class _FakeCostMeter:
    def __init__(self, total: float = 0.0) -> None:
        self.total_usd = total


class _FakeLog:
    def __init__(self, base: Path) -> None:
        self.dir = base
        self.session_id = "fake-session"


class _FakeParent:
    """Just enough of a `Session` for `run_org_call` to run its OWN
    depth-check/org-load/budget-tracker setup without a real one --
    `state_dir`/`log.dir`+`log.session_id` (the goal task, best-effort)/
    `cwd`/`cost_meter.total_usd` (the budget baseline) are everything it
    reads off `runtime.parent` before ever calling `run_agent_call`."""
    def __init__(self, base: Path, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.cost_meter = _FakeCostMeter()
        self.log = _FakeLog(base)
        self.cwd = base


@test
def test_run_org_call_max_depth_is_relative_to_callers_own_depth(ctx: Ctx):
    """2.0.2 review finding 9 (major) part b pin: RELATIVE to the
    caller's own depth, never absolute -- the review's own verified
    repro: an AgentRuntime at depth 1 running org="company" (tree depth
    3) used to build {'depth': 1, 'max_depth': 4} (the SAME cap a
    depth-0 caller would get), when the whole tree needs 1 + 3 + 1 = 5."""
    from halo_harness.agent.subagent import AgentRuntime, run_org_call
    import halo_harness.agent.subagent as subagent_mod
    from halo_harness.orgs import ensure_builtin_orgs
    base = Path(tempfile.mkdtemp(prefix="orgs-depth-rel-cwd-"))
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-depth-rel-state-"))
    ensure_builtin_orgs(state_dir=state_dir)  # company: tree depth 3
    captured = {}

    def fake_run_agent_call(*, runtime, tool_id, tool_input, tool_name, on_event=None):
        captured["depth"] = runtime.depth
        captured["max_depth"] = runtime.max_depth
        from halo_harness.tools.base import ToolResult
        return [], ToolResult("ok")
    orig = subagent_mod.run_agent_call
    subagent_mod.run_agent_call = fake_run_agent_call
    old_state_dir_env = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        from halo_harness.theme import set_config_value
        set_config_value("agents.max_depth", 3)  # so depth=1 isn't ALREADY at the caller's own cap
        parent = _FakeParent(base, state_dir)
        runtime = AgentRuntime(parent=parent, depth=1)  # caller already one hop deep
        _events, result = run_org_call(runtime=runtime, tool_id="t1", tool_name="Agent",
                                        tool_input={"org": "company", "prompt": "go"})
        ctx.check(f"not refused, got {result.content}", not result.is_error)
        ctx.check(f"org runtime keeps the caller's own depth, got {captured}", captured.get("depth") == 1)
        ctx.check(f"max_depth is relative: depth(1) + tree(3) + 1 = 5, got {captured}",
                  captured.get("max_depth") == 5)
    finally:
        subagent_mod.run_agent_call = orig
        if old_state_dir_env is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir_env


@test
def test_run_org_call_refuses_when_caller_already_at_its_depth_cap(ctx: Ctx):
    """2.0.2 review finding 9 (major) part a pin: a sub-agent already at
    ITS OWN depth cap used to still be able to START a whole org tree
    (the depth check only ever happened AFTER max_depth was freshly
    OVERWRITTEN for the org run, so the caller's pre-existing cap never
    got a say), bypassing "Sub-agents cannot spawn further sub-agents"
    entirely."""
    from halo_harness.agent.subagent import MAX_DEPTH, AgentRuntime, run_org_call
    base = Path(tempfile.mkdtemp(prefix="orgs-depth-cap-cwd-"))
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-depth-cap-state-"))
    parent = _FakeParent(base, state_dir)
    runtime = AgentRuntime(parent=parent, depth=MAX_DEPTH)  # already at the default cap
    _events, result = run_org_call(runtime=runtime, tool_id="t1", tool_name="Agent",
                                    tool_input={"org": "company", "prompt": "go"})
    ctx.check(f"refused before building any org runtime, got {result.content}", result.is_error)
    ctx.check(f"clear depth-limit wording, got {result.content!r}", "depth limit" in result.content)


@test
def test_outer_org_task_resume_rebuilds_the_org_runtime(ctx: Ctx):
    """Fix-pass notes ("outer org task resume" -- REFUTED as "loses max_
    depth/max_concurrent"; it was worse: resume failed outright with
    "The agent type for task_id ... is no longer available"). The org
    ROOT position's own task_id is never a name in the session's base
    `agents` catalog -- only `run_org_call`'s own ephemeral `org_
    runtime` ever had it. `run_org_call` now tags the task record with
    the org's own name so `_resume_task` can rebuild an equivalent
    runtime and find the position again."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        from tests.helpers.mock_openai import SCENARIOS, _finish
        calls = {"n": 0}

        def _root(h, body):
            calls["n"] += 1
            _finish(h, _text_chunk("first answer" if calls["n"] == 1 else "resumed answer"))
        scn = "org-resume-top"
        SCENARIOS[scn] = _root  # "Orchestrator" has no model: of its own -> inherits the session model
        session = _make_session(fh, scenario=scn, mock=mock)
        events_seen = _run_org_dispatch(session, "call_1", "solo", "do the thing")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check(f"first run ok, got {[e.data for e in results]}",
                  len(results) == 1 and results[0].data.get("ok") is True)

        tasks = session.agent_runtime.tasks
        ctx.check(f"exactly one task minted for the root, got {tasks}", len(tasks) == 1)
        task_id = next(iter(tasks))
        ctx.check(f"tagged with the org's own name, got {tasks[task_id]}", tasks[task_id].get("org_name") == "solo")

        tu = {"type": "tool_use", "id": "call_2", "name": "Agent",
              "input": {"task_id": task_id, "prompt": "continue", "description": "resume"}}
        events_seen2 = list(session._dispatch_tools(2, [tu]))
        results2 = [e for e in events_seen2 if e.kind == "tool_result"]
        ctx.check(f"exactly one tool_result, got {[e.data for e in results2]}", len(results2) == 1)
        ctx.check(f"the resume succeeds (never 'no longer available'), got {results2[0].data}",
                  results2[0].data.get("ok") is True)
        ctx.check(f"the resumed text reached the top, got {results2[0].data}",
                  "resumed answer" in results2[0].data.get("content", ""))
    finally:
        mock.stop()


@test
def test_run_org_worker_never_crashes_the_tui_on_an_unexpected_exception(ctx: Ctx):
    """2.0.2 review finding 4 (major): `_run_org_worker` (`tui/slash.py`)
    runs on a Textual thread worker with the framework's own default
    `exit_on_error=True` -- the one time anything in the call tree
    violated `run_org_call`'s "never raises" contract, that took the
    WHOLE TUI down with it. Belt-and-suspenders: even an exception
    `run_org_call` itself is now guarded against (any OTHER bug, not
    just the named model-resolution one fixed elsewhere) must never
    escape this worker function."""
    from halo_harness.tui import slash
    import halo_harness.agent.subagent as subagent_mod

    class _FakeSession:
        agent_runtime = object()

    class _FakeEvents:
        def put(self, ev):
            pass

    class _FakeController:
        session = _FakeSession()
        events = _FakeEvents()

    class _FakeApp:
        def __init__(self):
            self.controller = _FakeController()
            self.calls = []

        def call_from_thread(self, fn, *args):
            self.calls.append((fn, args))

    def _boom(**kwargs):
        raise RuntimeError("some unrelated bug deep in the call tree")
    orig = subagent_mod.run_org_call
    subagent_mod.run_org_call = _boom
    try:
        app = _FakeApp()
        slash._run_org_worker(app, "some-org", "do the thing")  # must NOT raise
        ctx.check(f"routed to the finish callback instead of crashing, got {app.calls}", len(app.calls) == 1)
        _fn, args = app.calls[0]
        ctx.check(f"finish callback is is_error=True, got {args}", args[-1] is True)
        ctx.check(f"names the real exception, got {args}", "RuntimeError" in args[2])
    finally:
        subagent_mod.run_org_call = orig


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
