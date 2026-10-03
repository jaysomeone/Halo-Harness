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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
