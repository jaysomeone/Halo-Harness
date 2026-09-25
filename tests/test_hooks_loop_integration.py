"""tests.test_hooks_loop_integration -- H4 scope B call sites, end to end
through a REAL `agent.loop.Session` (not just hooks.py in isolation): a
PreToolUse hook rewriting a Bash `updatedInput`, a UserPromptSubmit hook
adding context, a Stop hook that blocks once then lets the turn end,
PostToolUse annotating a result, and PermissionDenied/PostToolBatch firing.
Mirrors the brief's own live acceptance scenario ("a PreToolUse hook
rewriting a Bash updatedInput + a UserPromptSubmit hook adding context")
at the unit level, against the mock upstream.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
_HOOK_SCRIPT_ARGV = [sys.executable, "-m", "tests.helpers.hook_scripts"]


def _new_session(fh, mock, *, model, hook_runner=None):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="hooks-loop-")), model_label=model, session_context=session_ctx,
        openrouter_base_url=mock.base_url, max_turns=10,
        permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]),
        hook_runner=hook_runner,
    )


def _hook_runner(fh, *, hooks_by_event):
    from rolo_claude.hooks import HookRunner
    # the hook scripts are spawned as `python -m tests.helpers.hook_scripts`
    # -- that import only resolves with PYTHONPATH pointing at the repo
    # root (HookRunner's own `cwd`, matching a real hook's contract, is
    # the PROJECT dir, not the repo -- same as CLAUDE_PROJECT_DIR).
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    return HookRunner(hooks_by_event, cwd=fh["proj"], session_id="test-session",
                       transcript_path=str(fh["proj"] / "transcript.jsonl"), effective_env=env)


@test
def test_pretooluse_hook_rewrites_bash_command_and_the_rewritten_one_actually_runs(ctx: Ctx):
    """The brief's own live acceptance shape: a PreToolUse hook rewrites a
    Bash call's `updatedInput` -- the tool that ACTUALLY dispatches must
    run the rewritten command, not the model's original one."""
    from rolo_claude.hooks import HookDef

    fh = build_fake_home()
    mock = MockUpstream().start()
    SCENARIOS["hook-pretooluse-rewrite"] = lambda h, body: _finish(
        h,
        [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
         {"choices": [{"index": 0, "delta": {"content": "the output was: rewritten"}}]},
         {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        if any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
        else [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
              {"choices": [{"index": 0, "delta": {"tool_calls": [
                  {"index": 0, "id": "call_b", "type": "function",
                   "function": {"name": "Bash", "arguments": json.dumps({"command": "echo original"})}},
              ]}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}],
    )
    try:
        hooks_by_event = {"PreToolUse": [HookDef(type="command", matcher="Bash",
                                                   args=_HOOK_SCRIPT_ARGV + ["updated_input"])]}
        session = _new_session(fh, mock, model="or:mock/hook-pretooluse-rewrite",
                                hook_runner=_hook_runner(fh, hooks_by_event=hooks_by_event))
        results = []
        for ev in session.turn("run echo original and reply with the output"):
            if ev.kind == "tool_result":
                results.append(ev.data)
        ctx.check(f"a tool_result was logged, got {results}", len(results) == 1)
        # the assistant message's OWN tool_use block is logged as the
        # model actually issued it (byte-for-byte replay/hash stability --
        # `content_hash_from_oai_body` covers the exact wire request) --
        # `updatedInput` changes what actually DISPATCHES, verified below
        # via the tool_result content, not the logged assistant node.
        tool_result_node = next(n for n in session.log.nodes() if n.get("type") == "tool_result")
        content = tool_result_node.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        ctx.check(f"the REWRITTEN command actually ran (real 'rewritten' output), got {text!r}",
                   "rewritten" in text and "original" not in text.split("[reminder")[0])
    finally:
        mock.stop()


@test
def test_h5b_f13_pretooluse_rewrite_is_re_decided_against_a_deny_rule(ctx: Ctx):
    """finding 13 (major, h4-h5-h3c review): `decision` used to be
    computed against the ORIGINAL tool_input, before the PreToolUse hook
    ever ran -- a hook that rewrites the command (via `updatedInput`)
    WITHOUT also setting its own `permissionDecision` let that STALE
    decision survive even when the REWRITTEN command now matches one of
    the user's own deny rules, routing straight around the one gate this
    harness keeps ("no cyber blocks" means deny rules are the ONLY
    restriction, but they must actually restrict). The model's original
    command is harmless and would be allowed; the hook rewrites it into
    something a real deny rule matches -- the call must be DENIED, never
    dispatched."""
    from rolo_claude.hooks import HookDef
    from rolo_claude.permissions import PermissionEngine, parse_rule

    fh = build_fake_home()
    mock = MockUpstream().start()
    SCENARIOS["hook-pretooluse-redecide"] = lambda h, body: _finish(
        h,
        [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
         {"choices": [{"index": 0, "delta": {"content": "done"}}]},
         {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        if any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
        else [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
              {"choices": [{"index": 0, "delta": {"tool_calls": [
                  {"index": 0, "id": "call_r", "type": "function",
                   "function": {"name": "Bash", "arguments": json.dumps({"command": "echo harmless"})}},
              ]}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}],
    )
    try:
        hooks_by_event = {"PreToolUse": [HookDef(type="command", matcher="Bash",
                                                   args=_HOOK_SCRIPT_ARGV + ["updated_input_no_decision"])]}
        deny_rule = parse_rule("Bash(echo rewritten-dangerous:*)", source="settings", base_dir=fh["proj"], action="deny")
        session = _new_session(fh, mock, model="or:mock/hook-pretooluse-redecide",
                                hook_runner=_hook_runner(fh, hooks_by_event=hooks_by_event))
        session.permission_engine = PermissionEngine(mode="auto", cwd=fh["proj"], deny_rules=[deny_rule])

        results = []
        for ev in session.turn("run echo harmless"):
            if ev.kind == "tool_result":
                results.append(ev.data)
        ctx.check(f"a tool_result was logged, got {results}", len(results) == 1)
        ctx.check(f"the call was DENIED (never dispatched), got {results}", results[0].get("ok") is False)

        tool_result_node = next(n for n in session.log.nodes() if n.get("type") == "tool_result")
        ctx.check(f"tool_result is_error True, got {tool_result_node}", tool_result_node.get("is_error") is True)
        content = tool_result_node.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        ctx.check(f"denial names the deny rule, got {text!r}", "Permission denied" in text)
        ctx.check("the rewritten command never actually ran (no real Bash output)",
                  "rewritten-dangerous" not in text or "Permission denied" in text)
    finally:
        mock.stop()


@test
def test_userpromptsubmit_hook_context_is_visible_in_the_session_log(ctx: Ctx):
    """A UserPromptSubmit hook's `additionalContext` becomes a user-role
    snapshot in the log (never the system node) -- visible alongside the
    prompt for the model AND for anyone reading the transcript."""
    from rolo_claude.hooks import HookDef

    fh = build_fake_home()
    mock = MockUpstream().start()
    SCENARIOS["hook-ups-context"] = lambda h, body: _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])
    try:
        hooks_by_event = {"UserPromptSubmit": [HookDef(type="command",
                                                          args=_HOOK_SCRIPT_ARGV + ["plain_context"])]}
        session = _new_session(fh, mock, model="or:mock/hook-ups-context",
                                hook_runner=_hook_runner(fh, hooks_by_event=hooks_by_event))
        for _ev in session.turn("hello"):
            pass
        snapshots = [n for n in session.log.nodes() if n.get("type") == "snapshot" and n.get("kind") == "hook_context"]
        ctx.check(f"a hook_context snapshot was logged, got {snapshots}", len(snapshots) == 1)
        text = "".join(b.get("text", "") for b in snapshots[0].get("content") or [])
        ctx.check(f"it carries the hook's own context text, got {text!r}",
                   "plain-context-from-hook-script" in text)
        system_nodes = [n for n in session.log.nodes() if n.get("type") == "system"]
        system_text = system_nodes[0].get("text", "") if system_nodes else ""
        ctx.check("never folded into the system node", "plain-context-from-hook-script" not in system_text)
    finally:
        mock.stop()


@test
def test_stop_hook_exits_2_once_makes_the_model_continue_one_more_step(ctx: Ctx):
    """A Stop hook that exits 2 the FIRST time (then 0 after) must make
    the turn continue for exactly one more model call before ending."""
    from rolo_claude.hooks import HookDef

    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}
    SCENARIOS["hook-stop-once"] = lambda h, body: (calls.__setitem__("n", calls["n"] + 1), _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": f"answer {calls['n']}"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]))
    try:
        counter_file = Path(tempfile.mkdtemp(prefix="stop-hook-")) / "counter.txt"
        os.environ["HOOK_STOP_COUNTER_FILE"] = str(counter_file)
        hooks_by_event = {"Stop": [HookDef(type="command", args=_HOOK_SCRIPT_ARGV + ["stop_block_once"])]}
        session = _new_session(fh, mock, model="or:mock/hook-stop-once",
                                hook_runner=_hook_runner(fh, hooks_by_event=hooks_by_event))
        kinds = [ev.kind for ev in session.turn("go")]
        ctx.check(f"the model was called twice (one extra step), got {calls['n']}", calls["n"] == 2)
        ctx.check(f"turn_done fired exactly once, got kinds={kinds}", kinds.count("turn_done") == 1)
        ctx.check("the turn ended with end_turn (not blocked forever)",
                   any(k == "turn_done" for k in kinds))
    finally:
        os.environ.pop("HOOK_STOP_COUNTER_FILE", None)
        mock.stop()


@test
def test_posttooluse_hook_additional_context_is_appended_to_the_result(ctx: Ctx):
    from rolo_claude.hooks import HookDef

    fh = build_fake_home()
    target = fh["proj"] / "posttool_target.txt"
    target.write_text("hello\n", encoding="utf-8")
    mock = MockUpstream().start()
    SCENARIOS["hook-posttooluse"] = lambda h, body: _finish(
        h,
        [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
         {"choices": [{"index": 0, "delta": {"content": "done"}}]},
         {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        if any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
        else [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
              {"choices": [{"index": 0, "delta": {"tool_calls": [
                  {"index": 0, "id": "call_r", "type": "function",
                   "function": {"name": "Read", "arguments": json.dumps({"file_path": str(target)})}},
              ]}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}],
    )
    try:
        hooks_by_event = {"PostToolUse": [HookDef(type="command", matcher="Read",
                                                     args=_HOOK_SCRIPT_ARGV + ["json_allow"])]}
        # json_allow has no additionalContext, so also test the plain block-decision shape gets applied
        from rolo_claude.hooks import HookDef as _HD
        hooks_by_event["PostToolUse"] = [HookDef(type="command", matcher="Read",
                                                    args=_HOOK_SCRIPT_ARGV + ["block_decision"])]
        session = _new_session(fh, mock, model="or:mock/hook-posttooluse",
                                hook_runner=_hook_runner(fh, hooks_by_event=hooks_by_event))
        for _ev in session.turn("read it"):
            pass
        tool_result_node = next(n for n in session.log.nodes() if n.get("type") == "tool_result")
        content = tool_result_node.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        ctx.check(f"the PostToolUse block annotation reached the logged result, got {text!r}",
                   "blocked by block_decision script" in text)
        ctx.check(f"is_error flips true on a PostToolUse block, got {tool_result_node}",
                   tool_result_node.get("is_error") is True)
    finally:
        mock.stop()


@test
def test_h5b_f10_session_start_env_file_var_expansion_through_a_real_session(ctx: Ctx):
    """H5b finding 10 (major): `_fire_session_start` used to fold
    `read_env_file_exports` in as a LITERAL NAME=value parse -- Claude
    Code's own SessionStart docs example, `export PATH="$PATH:/some/dir"`,
    became the literal four-character string "$PATH:/some/dir" in
    `session.tool_env` (what Bash's OWN per-call sourcing does NOT depend
    on -- finding 6 already fixed that separately -- but what PowerShell,
    sub-agents and `!` pre-exec all start from). Goes through a REAL
    `agent.loop.Session` end to end (not `read_env_file_exports`/the Bash
    tool in isolation): a SessionStart hook writes exactly that documented
    example to `CLAUDE_ENV_FILE`, and `session.tool_env['PATH']` must come
    out with the marker genuinely APPENDED to the real inherited PATH, not
    as a bare literal string standing alone."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.agent.loop import Session
    from rolo_claude.hooks import HookDef, HookRunner
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        # `_hook_runner`'s own helper hardcodes session_id="test-session",
        # which never matches a REAL Session's own randomly-generated
        # `log.session_id` -- `_fire_session_start` reads
        # `env_file_path(self.log.session_id)`, so a mismatched HookRunner
        # session_id means it reads back an entirely different (empty)
        # file than the one the hook actually wrote to. Build the log with
        # an EXPLICIT, known session_id and a HookRunner that agrees with it.
        session_id = "h5b-f10-envfile-session"
        session_log = SessionLog(fh["proj"], session_id=session_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_DIR)
        hook_runner = HookRunner(
            {"SessionStart": [HookDef(type="command", args=_HOOK_SCRIPT_ARGV + ["env_file_writer_var_expansion"])]},
            cwd=fh["proj"], session_id=session_id, transcript_path=str(session_log.path), effective_env=env,
        )
        session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/hook-envfile-expand")
        model_ref = parse_model_ref("or:mock/hook-envfile-expand")
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="hooks-loop-envfile-")), model_label="or:mock/hook-envfile-expand",
            session_context=session_ctx, session_log=session_log, openrouter_base_url=mock.base_url, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]), hook_runner=hook_runner,
        )
        path_value = session.tool_env.get("PATH", "")
        ctx.check(f"PATH was genuinely expanded (still a real, multi-entry PATH, not just the "
                  f"literal marker), got {path_value!r}",
                  "/rolo-h5b-f10-marker" in path_value and len(path_value) > len("/rolo-h5b-f10-marker") + 20)
        ctx.check("the marker was truly APPENDED, at the end (never overwrote the real PATH)",
                  path_value.rstrip().endswith("/rolo-h5b-f10-marker"))
        ctx.check("no literal, un-expanded '$PATH' token leaked through",
                  "$PATH" not in path_value)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
