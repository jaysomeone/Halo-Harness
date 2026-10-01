"""tests.test_bash_background_jobs -- H8 scope A: background Bash jobs
(agent.jobs.JobRegistry), the BashOutput/TaskStop tools, Bash's own
`run_in_background` + foreground-timeout-conversion, the `/tasks` command,
"jobs killed on quit" (Controller.quit), and the full "notice on the next
turn" pattern through a real Session (same pattern test_agent_tool.py's own
background-sub-agent test uses).
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
from rolo_claude.agent.jobs import JobRegistry, JobRecord, MAX_CONCURRENT_JOBS
from rolo_claude.config.paths import git_bash
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.bash import BashTool
from rolo_claude.tools.bash_output import BashOutputTool
from rolo_claude.tools.task_stop import TaskStopTool

test, TESTS = new_registry()

_BASH = str(git_bash())


def _tmpdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


def _real_env() -> dict:
    """A real, populated environment for a JobRegistry call made DIRECTLY
    (bypassing tools/bash.py, which always builds one from ctx.env/
    os.environ) -- an actually-empty env can make Git Bash's own `-lc`
    login-shell profile scripts fail unpredictably on Windows (observed:
    "which: no bash in ((null))") since they depend on PATH existing at
    all, which has nothing to do with this module's own behaviour."""
    return dict(os.environ)


class _FakeParent:
    """Minimal stand-in for agent.loop.Session -- just enough state for
    JobRegistry._push_notice to have somewhere to append a completion
    notice to."""

    def __init__(self):
        self._pending_job_notices: list = []
        self._job_notices_lock = threading.Lock()


def _wait_until(pred, *, timeout=5.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ---- agent.jobs.JobRegistry (unit) -----------------------------------------

@test
def test_job_registry_start_background_and_poll(ctx: Ctx):
    parent = _FakeParent()
    reg = JobRegistry(parent)
    record, err = reg.start_background("echo hello-bg", description="say hi", cwd=_tmpdir("job-bg-"),
                                        env=_real_env(), shell_path=_BASH)
    ctx.check("no error starting", err is None)
    ctx.check("job registered", record.job_id in reg.jobs)
    ok = _wait_until(lambda: reg.jobs[record.job_id].status != "running")
    ctx.check("job completed", ok and reg.jobs[record.job_id].status == "completed")
    text, poll_err = reg.poll(record.job_id)
    ctx.check("poll succeeds", poll_err is None)
    ctx.check(f"output present, got {text!r}", "hello-bg" in text)
    ctx.check("status header present", "<status>completed</status>" in text)
    ok2 = _wait_until(lambda: bool(parent._pending_job_notices))
    ctx.check("a completion notice was pushed", ok2 and len(parent._pending_job_notices) == 1)
    ctx.check("notice names the job", record.job_id in parent._pending_job_notices[0])


@test
def test_h9_completion_notice_truncates_long_output_with_a_bashoutput_hint(ctx: Ctx):
    """H9 whole-tree review finding 7: a completion notice used to embed
    the job's ENTIRE captured output verbatim (up to the collector's own
    ~300k-char cap) as a plain user-role log entry -- unlike a real
    tool_result, prune.py never stubs it, so it stayed at full size in the
    log FOREVER, re-sent on every later request for the rest of the
    session (the review's own repro: a 594k-char job produced a
    300,292-char, ~75k-token notice). Now it's a short tail plus a pointer
    to BashOutput for the rest. Constructs the JobRecord/collector directly
    (bypassing a real subprocess) for a fast, deterministic test of
    `_push_notice` itself."""
    import time as time_mod
    from rolo_claude.agent.jobs import JobRecord
    from rolo_claude.tools._proc import _CappedCollector

    parent = _FakeParent()
    reg = JobRegistry(parent)
    collector = _CappedCollector()
    long_output = "".join(f"line {i}\n" for i in range(2000))  # well over 2000 chars, well under the spill cap
    collector.append(long_output)
    record = JobRecord(job_id="bg_test123", command="a long-output command", description="long output",
                        cwd=str(_tmpdir("job-notice-trunc-")), started_at=time_mod.monotonic(),
                        collector=collector, status="completed", exit_code=0)
    reg._push_notice(record)
    ctx.check("exactly one notice pushed", len(parent._pending_job_notices) == 1)
    notice = parent._pending_job_notices[0]
    ctx.check(f"notice is FAR shorter than the full {len(long_output)}-char output, got {len(notice)} chars",
              len(notice) < len(long_output) / 2)
    ctx.check(f"notice points to BashOutput with the real job_id for the rest, got tail={notice[-200:]!r}",
              "BashOutput('bg_test123')" in notice or 'BashOutput("bg_test123")' in notice)
    ctx.check("notice ends with the TRUE tail of the real output (the last line), not truncated mid-content",
              notice.rstrip("\n]").rstrip().endswith("line 1999") or "line 1999" in notice[-100:])
    ctx.check("still names the job id and exit code up front, same as before this fix",
              "bg_test123" in notice and "exit code 0" in notice)


@test
def test_h9_completion_notice_short_output_is_never_truncated(ctx: Ctx):
    """The fix must be a no-op for the common case (a short job) -- never
    add the BashOutput hint, never shorten output that was already under
    the tail cap."""
    import time as time_mod
    from rolo_claude.agent.jobs import JobRecord
    from rolo_claude.tools._proc import _CappedCollector

    parent = _FakeParent()
    reg = JobRegistry(parent)
    collector = _CappedCollector()
    collector.append("short output\n")
    record = JobRecord(job_id="bg_short1", command="echo short output", description="short",
                        cwd=str(_tmpdir("job-notice-short-")), started_at=time_mod.monotonic(),
                        collector=collector, status="completed", exit_code=0)
    reg._push_notice(record)
    notice = parent._pending_job_notices[0]
    ctx.check(f"short output preserved verbatim, no hint added, got {notice!r}",
              "short output" in notice and "BashOutput" not in notice)


@test
def test_job_registry_poll_only_returns_new_output(ctx: Ctx):
    parent = _FakeParent()
    reg = JobRegistry(parent)
    record, err = reg.start_background("echo first; sleep 1; echo second", description="two lines",
                                        cwd=_tmpdir("job-poll-"), env=_real_env(), shell_path=_BASH)
    ctx.check("no error", err is None)

    # Poll repeatedly (bounded) until "first" shows up, instead of assuming
    # a fixed shell-startup delay -- Git Bash's own login-shell startup
    # time on Windows is itself variable (observed: not yet visible after
    # 150ms), and this test cares about ORDERING (new-output-only), not
    # any particular absolute timing.
    collected = ""

    def _poll_until_first() -> bool:
        nonlocal collected
        text, _ = reg.poll(record.job_id)
        collected += text
        return "first" in collected

    ok = _wait_until(_poll_until_first, timeout=10.0, interval=0.1)
    ctx.check(f"eventually saw 'first', got {collected!r}", ok)
    ctx.check("still running the 1s sleep -- 'second' not seen yet",
              "second" not in collected and reg.jobs[record.job_id].status == "running")

    _wait_until(lambda: reg.jobs[record.job_id].status != "running", timeout=10.0)
    text2, _ = reg.poll(record.job_id)
    ctx.check(f"a later poll shows 'second' (only the NEW output), got {text2!r}", "second" in text2)
    ctx.check("that later poll does not repeat the already-delivered 'first'", "first" not in text2)


@test
def test_job_registry_poll_filter_regex(ctx: Ctx):
    parent = _FakeParent()
    reg = JobRegistry(parent)
    record, _ = reg.start_background("printf 'keep-this\\nskip-this\\nkeep-that\\n'", description="filtered",
                                      cwd=_tmpdir("job-filter-"), env=_real_env(), shell_path=_BASH)
    _wait_until(lambda: reg.jobs[record.job_id].status != "running")
    text, err = reg.poll(record.job_id, filter_regex="keep")
    ctx.check("no error", err is None)
    ctx.check(f"only matching lines kept, got {text!r}",
              "keep-this" in text and "keep-that" in text and "skip-this" not in text)


@test
def test_job_registry_poll_unknown_id_is_error(ctx: Ctx):
    text, err = JobRegistry(_FakeParent()).poll("bash_doesnotexist")
    ctx.check("unknown id is an error", text is None and err is not None)
    ctx.check("error names the id", "bash_doesnotexist" in err)


@test
def test_job_registry_kill_stops_a_running_job(ctx: Ctx):
    parent = _FakeParent()
    reg = JobRegistry(parent)
    record, _ = reg.start_background("sleep 30", description="long sleep", cwd=_tmpdir("job-kill-"),
                                      env=_real_env(), shell_path=_BASH)
    ok, msg = reg.kill(record.job_id)
    ctx.check("kill reports ok", ok)
    ctx.check("message names the job", record.job_id in msg)
    ok2 = _wait_until(lambda: reg.jobs[record.job_id].status == "killed")
    ctx.check("status becomes killed", ok2)
    # `_kill_process_group`'s own taskkill/killpg call can itself take a few
    # seconds (it has a 5s internal bound on Windows) before the drain
    # thread even notices the process exited and pushes the notice -- a
    # generous bound here, matching test_tools_bash.py's own kill-related
    # waits, not a tight one.
    ok3 = _wait_until(lambda: bool(parent._pending_job_notices), timeout=20.0)
    ctx.check("a 'stopped' notice was pushed", ok3 and "stopped" in parent._pending_job_notices[0].lower())


@test
def test_job_registry_kill_unknown_id(ctx: Ctx):
    ok, _msg = JobRegistry(_FakeParent()).kill("bash_nope")
    ctx.check("kill of an unknown id fails", ok is False)


@test
def test_job_registry_kill_all_stops_every_running_job(ctx: Ctx):
    reg = JobRegistry(_FakeParent())
    ids = []
    for i in range(3):
        record, _ = reg.start_background("sleep 30", description=f"job{i}", cwd=_tmpdir(f"job-killall-{i}-"),
                                          env=_real_env(), shell_path=_BASH)
        ids.append(record.job_id)
    reg.kill_all()
    ok = _wait_until(lambda: all(reg.jobs[j].status == "killed" for j in ids))
    ctx.check("every job is killed", ok)


@test
def test_job_registry_caps_concurrent_jobs(ctx: Ctx):
    reg = JobRegistry(_FakeParent())
    try:
        for i in range(MAX_CONCURRENT_JOBS):
            _record, err = reg.start_background("sleep 5", description=f"cap{i}", cwd=_tmpdir(f"job-cap-{i}-"),
                                                  env=_real_env(), shell_path=_BASH)
            ctx.check(f"job {i} starts", err is None)
        _record, err = reg.start_background("sleep 5", description="one too many", cwd=_tmpdir("job-cap-extra-"),
                                             env=_real_env(), shell_path=_BASH)
        ctx.check(f"the {MAX_CONCURRENT_JOBS + 1}th is refused, got {err!r}",
                  err is not None and "too many" in err.lower())
    finally:
        reg.kill_all()


@test
def test_job_registry_list_jobs_is_id_sorted(ctx: Ctx):
    reg = JobRegistry(_FakeParent())
    reg.jobs["bash_b"] = JobRecord(job_id="bash_b", command="echo b", description="", cwd=".", started_at=0.0)
    reg.jobs["bash_a"] = JobRecord(job_id="bash_a", command="echo a", description="", cwd=".", started_at=0.0)
    jobs = reg.list_jobs()
    ctx.check("id-sorted", [j["id"] for j in jobs] == ["bash_a", "bash_b"])


# ---- Bash tool: run_in_background + timeout->background handoff -----------

@test
def test_bash_run_in_background_returns_immediately_with_shell_id(ctx: Ctx):
    d = _tmpdir("bash-rib-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    t0 = time.monotonic()
    result = BashTool().run({"command": "sleep 2 && echo done-bg", "run_in_background": True}, ctx_obj)
    elapsed = time.monotonic() - t0
    ctx.check(f"returns promptly, got {elapsed:.2f}s", elapsed < 1.0)
    ctx.check("not an error", result.is_error is False)
    ctx.check(f"mentions a shell_id, got {result.content!r}", "shell_id" in result.content)
    ctx.check("exactly one job registered", len(reg.jobs) == 1)
    reg.kill_all()


@test
def test_bash_run_in_background_without_a_registry_falls_back_to_foreground(ctx: Ctx):
    d = _tmpdir("bash-rib-none-")
    result = BashTool().run({"command": "echo ran-anyway", "run_in_background": True},
                             ToolContext(cwd=d, bash_state={"cwd": d}))
    ctx.check("no error", result.is_error is False)
    ctx.check(f"ran for real, got {result.content!r}", "ran-anyway" in result.content)
    ctx.check("notes it fell back to the foreground", "foreground" in result.content.lower())


@test
def test_bash_timeout_moves_to_background_when_a_registry_is_present(ctx: Ctx):
    d = _tmpdir("bash-timeout-bg-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    t0 = time.monotonic()
    result = BashTool().run({"command": "sleep 10 && echo finally-done", "timeout": 500}, ctx_obj)
    elapsed = time.monotonic() - t0
    ctx.check(f"returns promptly (moved to bg, not waited out), got {elapsed:.2f}s", elapsed < 3.0)
    ctx.check("NOT an error -- it's running fine, just not done yet", result.is_error is False)
    ctx.check(f"names a shell_id, got {result.content!r}", "shell_id" in result.content)
    ctx.check("exactly one job adopted", len(reg.jobs) == 1)
    job_id = next(iter(reg.jobs))
    ok = _wait_until(lambda: reg.jobs[job_id].status != "running", timeout=15.0)
    ctx.check("the adopted job eventually completes on its own", ok and reg.jobs[job_id].status == "completed")
    ctx.check("its real exit code (0) was recovered from the marker, not the printf wrapper's",
              reg.jobs[job_id].exit_code == 0)
    text, _ = reg.poll(job_id)
    ctx.check(f"its output is clean (no marker leakage), got {text!r}",
              "finally-done" in text and "__ROLO_CLAUDE_EXIT__" not in text and "__ROLO_CLAUDE_CWD__" not in text)


@test
def test_h9b_f30_first_bashoutput_poll_after_a_timeout_handoff_never_repeats_already_delivered_text(ctx: Ctx):
    """Verified bug: a timeout-adopted job's `read_offset` defaulted to 0
    (JobRecord's own dataclass default), so the FIRST BashOutput poll
    after the handoff repeated everything the FOREGROUND Bash call's own
    (already delivered to the model) timeout tool_result already showed."""
    d = _tmpdir("bash-timeout-offset-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    marker = "h9b-f30-already-delivered-marker"
    result = BashTool().run({"command": f"echo {marker} && sleep 10 && echo later-output", "timeout": 800}, ctx_obj)
    ctx.check(f"the foreground timeout result already shows the marker, got {result.content!r}", marker in result.content)
    ctx.check("moved to background (not an error)", result.is_error is False)
    job_id = next(iter(reg.jobs))
    first_poll_text, err = reg.poll(job_id)
    ctx.check(f"BashOutput's own poll call succeeded, got err={err!r}", err is None)
    ctx.check(f"the FIRST poll does NOT repeat the marker the foreground result already delivered, "
              f"got {first_poll_text!r}", marker not in first_poll_text)
    ok = _wait_until(lambda: reg.jobs[job_id].status != "running", timeout=15.0)
    ctx.check("the job eventually completes", ok)
    later_text, _ = reg.poll(job_id)
    ctx.check(f"but genuinely NEW output (produced after the handoff) still arrives, got {later_text!r}",
              "later-output" in later_text)
    ctx.check(f"and the marker is never repeated even across BOTH polls combined, got "
              f"{first_poll_text!r} + {later_text!r}", marker not in (first_poll_text + later_text))


@test
def test_h9b_f30_capped_collector_append_and_result_are_thread_safe(ctx: Ctx):
    """A direct stress test of the collector's own internal lock (finding
    30's second half): `append()` from many threads concurrently with
    `result()` being read repeatedly must never raise or corrupt
    `total_len`'s own accounting, matching the real
    run_streamed-hands-off-to-a-new-drain-thread race this fixes."""
    from rolo_claude.tools._proc import _CappedCollector
    collector = _CappedCollector()
    chunk = "x" * 100
    n_threads = 8
    n_appends = 200
    errors: list = []

    def _writer():
        try:
            for _ in range(n_appends):
                collector.append(chunk)
        except Exception as e:
            errors.append(e)

    def _reader():
        try:
            for _ in range(n_appends):
                collector.result()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_writer) for _ in range(n_threads)] + [threading.Thread(target=_reader)
                                                                                for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    ctx.check(f"no exceptions from concurrent append()/result(), got {errors}", errors == [])
    ctx.check(f"total_len accounts for every append exactly once, expected "
              f"{n_threads * n_appends * len(chunk)}, got {collector.total_len}",
              collector.total_len == n_threads * n_appends * len(chunk))


@test
def test_bash_timeout_without_a_registry_still_kills_as_before(ctx: Ctx):
    d = _tmpdir("bash-timeout-kill-")
    t0 = time.monotonic()
    result = BashTool().run({"command": "sleep 30", "timeout": 500}, ToolContext(cwd=d, bash_state={"cwd": d}))
    elapsed = time.monotonic() - t0
    ctx.check(f"returns promptly (killed), got {elapsed:.2f}s", elapsed < 10)
    ctx.check("timeout without a registry is still an error", result.is_error is True)
    ctx.check("message names the timeout", "timed out" in result.content.lower())


# ---- BashOutput / TaskStop tools -------------------------------------------

@test
def test_bash_output_tool_requires_shell_id(ctx: Ctx):
    result = BashOutputTool().run({}, ToolContext(cwd=Path(".")))
    ctx.check("missing shell_id is an error", result.is_error is True)


@test
def test_bash_output_tool_without_a_registry_is_a_clear_error(ctx: Ctx):
    result = BashOutputTool().run({"shell_id": "bash_x"}, ToolContext(cwd=Path(".")))
    ctx.check("no registry is an error", result.is_error is True)
    ctx.check("message says why", "background" in result.content.lower())


@test
def test_bash_output_tool_reads_a_real_background_job(ctx: Ctx):
    d = _tmpdir("bash-output-tool-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    BashTool().run({"command": "echo from-tool", "run_in_background": True}, ctx_obj)
    job_id = next(iter(reg.jobs))
    _wait_until(lambda: reg.jobs[job_id].status != "running")
    result = BashOutputTool().run({"shell_id": job_id}, ctx_obj)
    ctx.check("no error", result.is_error is False)
    ctx.check(f"shows the output, got {result.content!r}", "from-tool" in result.content)


@test
def test_bash_output_tool_filter_param(ctx: Ctx):
    d = _tmpdir("bash-output-filter-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    BashTool().run({"command": "printf 'keep\\nnope\\n'", "run_in_background": True}, ctx_obj)
    job_id = next(iter(reg.jobs))
    _wait_until(lambda: reg.jobs[job_id].status != "running")
    result = BashOutputTool().run({"shell_id": job_id, "filter": "keep"}, ctx_obj)
    ctx.check("no error", result.is_error is False)
    ctx.check(f"filtered, got {result.content!r}", "keep" in result.content and "nope" not in result.content)


@test
def test_task_stop_tool_stops_a_bash_job(ctx: Ctx):
    d = _tmpdir("task-stop-tool-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    BashTool().run({"command": "sleep 30", "run_in_background": True}, ctx_obj)
    job_id = next(iter(reg.jobs))
    result = TaskStopTool().run({"task_id": job_id}, ctx_obj)
    ctx.check("no error", result.is_error is False)
    ok = _wait_until(lambda: reg.jobs[job_id].status == "killed")
    ctx.check("job is killed", ok)


@test
def test_task_stop_tool_accepts_deprecated_shell_id_alias(ctx: Ctx):
    d = _tmpdir("task-stop-alias-")
    reg = JobRegistry(_FakeParent())
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, job_registry=reg)
    BashTool().run({"command": "sleep 30", "run_in_background": True}, ctx_obj)
    job_id = next(iter(reg.jobs))
    result = TaskStopTool().run({"shell_id": job_id}, ctx_obj)
    ctx.check("the deprecated shell_id alias still works", result.is_error is False)


@test
def test_task_stop_tool_unknown_id_is_error(ctx: Ctx):
    d = _tmpdir("task-stop-unknown-")
    reg = JobRegistry(_FakeParent())
    result = TaskStopTool().run({"task_id": "bash_nope"}, ToolContext(cwd=d, job_registry=reg))
    ctx.check("unknown id is an error", result.is_error is True)


@test
def test_task_stop_tool_requires_an_id(ctx: Ctx):
    result = TaskStopTool().run({}, ToolContext(cwd=Path(".")))
    ctx.check("missing id is an error", result.is_error is True)


# ---- full session integration: notice applied on the next turn ------------

@test
def test_background_bash_job_notice_applied_on_next_turn(ctx: Ctx):
    # H11b finding 27: save/restore BRIDGE_TEST_HOME -- this used to set it
    # and never restore it, so it leaked into every test that ran later in
    # the SAME `run_all.py` process, silently shielding other suites'
    # "never touches the real ~/.rolo-claude" guards for the wrong reason.
    fh = build_fake_home()
    saved_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        _run_background_bash_job_notice_test(ctx, fh)
    finally:
        if saved_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = saved_home


def _run_background_bash_job_notice_test(ctx: Ctx, fh: dict) -> None:
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref

    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/x")
    model_ref = parse_model_ref("or:mock/x")
    session = Session(cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                       state_dir=_tmpdir("bash-e2e-state-"), model_label="mock/x", session_context=session_ctx)
    tu = {"type": "tool_use", "id": "call_bg_bash", "name": "Bash",
          "input": {"command": "sleep 0.3 && echo bash-bg-done", "run_in_background": True}}
    t0 = time.monotonic()
    events_seen = list(session._dispatch_tools(1, [tu]))
    elapsed = time.monotonic() - t0
    results = [e for e in events_seen if e.kind == "tool_result"]
    ctx.check(f"returns promptly, got {elapsed:.2f}s", elapsed < 1.0)
    ctx.check("immediate result mentions the background shell",
              "shell_id" in results[0].data.get("content", ""))

    ok = _wait_until(lambda: bool(session._pending_job_notices), timeout=5.0)
    ctx.check("a background job completion notice was queued", ok and len(session._pending_job_notices) == 1)
    turn_events = list(session._apply_pending_job_notices(2))
    user_msgs = [e for e in turn_events if e.kind == "user_message"]
    ctx.check("the notice was applied as a user_message event", len(user_msgs) == 1)
    ctx.check("the notice carries the job's real output", "bash-bg-done" in user_msgs[0].data.get("text", ""))
    ctx.check("notices are cleared after being applied once", session._pending_job_notices == [])
    session.job_registry.kill_all()


@test
def test_controller_quit_kills_background_jobs(ctx: Ctx):
    # finding 27: same save/restore as the test above.
    fh = build_fake_home()
    saved_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        _run_controller_quit_kills_background_jobs(ctx, fh)
    finally:
        if saved_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = saved_home


def _run_controller_quit_kills_background_jobs(ctx: Ctx, fh: dict) -> None:
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.controller import Controller
    from rolo_claude.model import ModelProfile, parse_model_ref

    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/x")
    model_ref = parse_model_ref("or:mock/x")
    session = Session(cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                       state_dir=_tmpdir("controller-quit-state-"), model_label="mock/x", session_context=session_ctx)
    record, err = session.job_registry.start_background("sleep 30", description="controller-quit-job",
                                                          cwd=fh["proj"], env=_real_env(), shell_path=_BASH)
    ctx.check("job started", err is None)
    controller = Controller(session=session, cwd=fh["proj"])
    controller.quit()
    ok = _wait_until(lambda: session.job_registry.jobs[record.job_id].status == "killed", timeout=20.0)
    ctx.check("Controller.quit() killed the background job", ok)


# ---- /tasks slash command ---------------------------------------------------

@test
def test_tasks_command_reports_no_jobs_by_default(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_tasks
    result = _cmd_tasks("", HeadlessFacade(cwd=Path(".")))
    ctx.check(f"no jobs message, got {result!r}", "No background jobs" in result)


@test
def test_tasks_command_lists_jobs_from_a_live_session(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_tasks

    class _FakeSession:
        pass

    fake_session = _FakeSession()
    fake_session.job_registry = JobRegistry(fake_session)
    fake_session.job_registry.jobs["bash_abc123"] = JobRecord(
        job_id="bash_abc123", command="sleep 30", description="a long sleep", cwd=".", started_at=0.0,
        status="running",
    )
    result = _cmd_tasks("", HeadlessFacade(cwd=Path("."), session=fake_session))
    ctx.check(f"lists the job id, got {result!r}", "bash_abc123" in result)
    ctx.check("lists its status", "running" in result)
    ctx.check("lists its description", "a long sleep" in result)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
