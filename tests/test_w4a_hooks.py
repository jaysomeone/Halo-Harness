"""tests.test_w4a_hooks -- W4a item 1: the 14 hook events wired to a real
trigger point this round (Setup, UserPromptExpansion, MessageDisplay,
TaskCreated/TaskCompleted, StopFailure, InstructionsLoaded, ConfigChange,
CwdChanged, DirectoryAdded, FileChanged, WorktreeCreated, PreModelSwitch/
PostModelSwitch), exercised end to end through a REAL agent.loop.Session
against the mock upstream -- never hooks.py's own pure API (that's
test_hooks_lifecycle.py's job). Each hook is a `command` type pointed at
tests/helpers/hook_scripts.py's `dump_payload` mode, which appends the full
stdin JSON to one shared file per test; `_dump_lines`/`_events_named` read
it back. WorktreeRemoved/ElicitationRequest/ElicitationResponse are NOT
tested here -- they don't fire yet (NOT_EMITTED_V1), see the worker report.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
REPO_DIR = Path(__file__).resolve().parent.parent
_HOOK_SCRIPTS_PATH = str(REPO_DIR / "tests" / "helpers" / "hook_scripts.py")

test, TESTS = new_registry()
_ORIGINAL_BRIDGE_TEST_HOME = os.environ.get("BRIDGE_TEST_HOME")


def _text_step(text: str) -> list:
    return [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": text}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


def _tool_call_step(name: str, arguments: dict, call_id: str = "call_1") -> list:
    return [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": call_id, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]


def _dump_hook(dump_file: Path, events: "list[str]"):
    from halo_harness.hooks import HookDef
    return {ev: [HookDef(type="command", args=[sys.executable, _HOOK_SCRIPTS_PATH, "dump_payload"])]
            for ev in events}


def _dump_env(dump_file: Path) -> dict:
    """A LOCAL env copy (never mutates the real `os.environ` -- the same
    hygiene `test_hooks_lifecycle.py`'s own `HOOK_ONCE_COUNTER_FILE` tests
    use) carrying `HOOK_DUMP_FILE` for `hook_scripts.py`'s `dump_payload`
    mode to read."""
    env = dict(os.environ)
    env["HOOK_DUMP_FILE"] = str(dump_file)
    return env


def _dump_lines(dump_file: Path) -> list:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if dump_file.exists() and dump_file.stat().st_size > 0:
            break
        time.sleep(0.05)
    if not dump_file.exists():
        return []
    out = []
    for line in dump_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _events_named(lines: list, name: str) -> list:
    return [L for L in lines if L.get("hook_event_name") == name]


def _new_session(*, mock, model, hooks_by_event, cwd=None, bare=True, extra_dirs=None, agents=None, env=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.hooks import HookRunner
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    cwd = cwd or Path(tempfile.mkdtemp(prefix="w4a-hooks-"))
    state_dir = Path(tempfile.mkdtemp(prefix="w4a-hooks-state-"))
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w4a-hooks-home-")))
    session_ctx = SessionContext(cwd=cwd, model_label=model, bare=bare)
    model_ref = parse_model_ref(model)
    engine = PermissionEngine(mode="auto", cwd=cwd, extra_dirs=extra_dirs or [])
    hook_runner = HookRunner(hooks_by_event, cwd=cwd, session_id="w4a-sess", transcript_path="t.jsonl",
                              effective_env=(env if env is not None else dict(os.environ)))
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"), state_dir=state_dir,
        model_label=model, session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=10,
        permission_engine=engine, agents=(agents or {}), routes={}, hook_runner=hook_runner,
    )
    session.interactive = False
    return session


def _drain(session, prompt) -> list:
    return list(session.turn(prompt))


@test
def test_filechanged_and_messagedisplay_fire_during_a_turn(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
        cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-"))
        target = cwd / "new_file.txt"
        SCENARIOS["w4a-write"] = ScriptedTurns([
            _tool_call_step("Write", {"file_path": str(target), "content": "hello"}),
            _text_step("wrote it"),
        ])
        session = _new_session(mock=mock, model="or:mock/w4a-write", cwd=cwd,
                                hooks_by_event=_dump_hook(dump, ["FileChanged", "MessageDisplay"]), env=_dump_env(dump))
        _drain(session, "write a file")
        lines = _dump_lines(dump)
        fc = _events_named(lines, "FileChanged")
        ctx.check(f"FileChanged fired once, got {len(fc)}", len(fc) == 1)
        ctx.check(f"FileChanged names the right path, got {fc[0].get('file_path') if fc else None}",
                  fc and fc[0].get("file_path") == str(target))
        md = _events_named(lines, "MessageDisplay")
        ctx.check(f"MessageDisplay fired for the final text message, got {md}",
                  any("wrote it" in L.get("message", "") for L in md))
    finally:
        mock.stop()


@test
def test_cwdchanged_fires_after_a_bash_cd(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
        cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-"))
        sub = cwd / "sub"
        sub.mkdir()
        SCENARIOS["w4a-cd"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": f"cd {sub.name}"}),
            _text_step("moved"),
        ])
        session = _new_session(mock=mock, model="or:mock/w4a-cd", cwd=cwd,
                                hooks_by_event=_dump_hook(dump, ["CwdChanged"]), env=_dump_env(dump))
        _drain(session, "cd into sub")
        lines = _dump_lines(dump)
        cc = _events_named(lines, "CwdChanged")
        # path separators can legitimately differ (`build_payload`'s own
        # str(cwd) vs. the raw bash_state path) -- compare case/slash-
        # insensitively, same as any other cross-platform path check here.
        got_cwd = (cc[0].get("cwd", "") if cc else "").replace("\\", "/").lower()
        ctx.check(f"CwdChanged fired once with the new cwd, got {cc}",
                  cc and str(sub.resolve()).replace("\\", "/").lower() in got_cwd)
    finally:
        mock.stop()


@test
def test_pre_and_post_modelswitch_fire_on_set_model(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
        session = _new_session(mock=mock, model="or:mock/w4a-pre",
                                hooks_by_event=_dump_hook(dump, ["PreModelSwitch", "PostModelSwitch"]), env=_dump_env(dump))
        from halo_harness.model import ModelProfile, parse_model_ref
        session.set_model(parse_model_ref("or:mock/w4a-post"), ModelProfile())
        lines = _dump_lines(dump)
        pre = _events_named(lines, "PreModelSwitch")
        post = _events_named(lines, "PostModelSwitch")
        ctx.check(f"PreModelSwitch fired with old/new, got {pre}",
                  pre and pre[0].get("old_model") == "or:mock/w4a-pre" and pre[0].get("new_model") == "or:mock/w4a-post")
        ctx.check(f"PostModelSwitch fired with old/new, got {post}",
                  post and post[0].get("old_model") == "or:mock/w4a-pre" and post[0].get("new_model") == "or:mock/w4a-post")
    finally:
        mock.stop()


@test
def test_stopfailure_fires_when_a_turn_ends_in_error(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
        # No SCENARIOS entry registered for this model -> MockUpstream's own
        # "unknown scenario" response path is a provider-shaped failure,
        # driving the turn to end with reason="error" (StopFailure's own
        # trigger) -- reuses the mock's existing behaviour rather than
        # inventing a new failure mode just for this test.
        session = _new_session(mock=mock, model="or:mock/w4a-unregistered-scenario",
                                hooks_by_event=_dump_hook(dump, ["StopFailure"]), env=_dump_env(dump))
        _drain(session, "this will fail")
        lines = _dump_lines(dump)
        sf = _events_named(lines, "StopFailure")
        ctx.check(f"StopFailure fired with a reason, got {sf}", sf and sf[0].get("reason"))
    finally:
        mock.stop()


@test
def test_taskcreated_and_taskcompleted_fire_for_a_subagent(ctx: Ctx):
    """TaskCreated/TaskCompleted fire on the PARENT's own hook_runner
    (agent/subagent.py's `_fire_task_hook`) -- never the child's, unlike
    SubagentStart/Stop."""
    from halo_harness.config.agents_md import AgentSpec
    mock = MockUpstream().start()
    try:
        dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
        SCENARIOS["w4a-task-child"] = ScriptedTurns([_text_step("child done")])
        SCENARIOS["w4a-task-parent"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "do thing", "prompt": "do the thing",
                                      "subagent_type": "general-purpose", "model": "or:mock/w4a-task-child"}),
            _text_step("parent done"),
        ])
        spec = AgentSpec(name="general-purpose", description="general purpose sub-agent",
                          tools=None, disallowed_tools=["Agent", "Task"], body="You are a helpful sub-agent.")
        session = _new_session(mock=mock, model="or:mock/w4a-task-parent", agents={"general-purpose": spec},
                                hooks_by_event=_dump_hook(dump, ["TaskCreated", "TaskCompleted"]),
                                env=_dump_env(dump))
        _drain(session, "delegate this")
        lines = _dump_lines(dump)
        created = _events_named(lines, "TaskCreated")
        completed = _events_named(lines, "TaskCompleted")
        ctx.check(f"TaskCreated fired once naming the subagent_type, got {created}",
                  created and created[0].get("subagent_type") == "general-purpose")
        ctx.check(f"TaskCompleted fired once with the SAME task_id, got {completed}",
                  completed and completed[0].get("task_id") == created[0].get("task_id"))
    finally:
        mock.stop()


@test
def test_taskcreated_and_taskcompleted_fire_for_a_background_bash_job(ctx: Ctx):
    """W5 (carried from W4a): the OTHER half of "background jobs and
    sub-agents" -- agent/jobs.py's own `_fire_task_hook` fires the SAME two
    events on the PARENT session's hook_runner for a `run_in_background`
    Bash job, naming the job id/command (and, on TaskCompleted, the job's
    own exit status/code) -- never the child's, there is no child here."""
    mock = MockUpstream().start()
    try:
        dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
        SCENARIOS["w4a-bgjob"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "echo hi-from-bg", "run_in_background": True}),
            _text_step("started it"),
        ])
        session = _new_session(mock=mock, model="or:mock/w4a-bgjob",
                                hooks_by_event=_dump_hook(dump, ["TaskCreated", "TaskCompleted"]),
                                env=_dump_env(dump))
        _drain(session, "run it in the background")
        deadline = time.monotonic() + 10.0
        lines = _dump_lines(dump)
        while time.monotonic() < deadline and not _events_named(lines, "TaskCompleted"):
            time.sleep(0.05)
            lines = _dump_lines(dump)
        created = _events_named(lines, "TaskCreated")
        completed = _events_named(lines, "TaskCompleted")
        ctx.check(f"TaskCreated fired once naming the job id and command, got {created}",
                  created and created[0].get("job_id", "").startswith("bash_")
                  and "echo hi-from-bg" in created[0].get("command", ""))
        ctx.check(f"TaskCompleted fired with the SAME job_id and an exit status, got {completed}",
                  completed and completed[0].get("job_id") == created[0].get("job_id")
                  and completed[0].get("status") == "completed" and completed[0].get("exit_code") == 0)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
