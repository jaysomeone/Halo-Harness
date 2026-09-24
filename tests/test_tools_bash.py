"""tests.test_tools_bash -- the Bash tool (H2 scope A): timeout+kill,
session-persistent cd, the [exit code] marker, merged stdout/stderr, and
streamed tool_progress via ToolContext.progress_cb.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.bash import BashTool

test, TESTS = new_registry()


def _tmpdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


@test
def test_bash_runs_a_simple_command(ctx: Ctx):
    d = _tmpdir("bash-simple-")
    result = BashTool().run({"command": "echo hello-from-bash"}, ToolContext(cwd=d, bash_state={"cwd": d}))
    ctx.check("no error", result.is_error is False)
    ctx.check(f"stdout captured, got {result.content!r}", "hello-from-bash" in result.content)


@test
def test_bash_merges_stdout_and_stderr(ctx: Ctx):
    d = _tmpdir("bash-merge-")
    result = BashTool().run({"command": "echo out-line; echo err-line 1>&2"}, ToolContext(cwd=d, bash_state={"cwd": d}))
    ctx.check("stdout present", "out-line" in result.content)
    ctx.check("stderr also present (merged)", "err-line" in result.content)


@test
def test_bash_nonzero_exit_reports_exit_code_marker(ctx: Ctx):
    d = _tmpdir("bash-exit-")
    result = BashTool().run({"command": "exit 7"}, ToolContext(cwd=d, bash_state={"cwd": d}))
    ctx.check(f"[exit code 7] marker present, got {result.content!r}", "[exit code 7]" in result.content)


@test
def test_bash_zero_exit_has_no_exit_code_marker(ctx: Ctx):
    d = _tmpdir("bash-exit0-")
    result = BashTool().run({"command": "true"}, ToolContext(cwd=d, bash_state={"cwd": d}))
    ctx.check("no [exit code] noise on a clean exit", "[exit code" not in result.content)


@test
def test_bash_cd_persists_across_calls_in_one_session(ctx: Ctx):
    d = _tmpdir("bash-cd-")
    sub = d / "subdir"
    sub.mkdir()
    bash_state = {"cwd": d}
    ctx_obj = ToolContext(cwd=d, bash_state=bash_state)
    r1 = BashTool().run({"command": "cd subdir"}, ctx_obj)
    ctx.check("cd itself succeeds", r1.is_error is False)
    r2 = BashTool().run({"command": "pwd"}, ctx_obj)
    ctx.check(f"second call ran from the subdir, got {r2.content!r}", "subdir" in r2.content.replace("\\", "/"))


@test
def test_bash_timeout_kills_the_process(ctx: Ctx):
    d = _tmpdir("bash-timeout-")
    t0 = time.monotonic()
    result = BashTool().run({"command": "sleep 30", "timeout": 500}, ToolContext(cwd=d, bash_state={"cwd": d}))
    elapsed = time.monotonic() - t0
    ctx.check(f"returns well before the full sleep (killed), got {elapsed:.2f}s", elapsed < 10)
    ctx.check("timeout is reported as an error", result.is_error is True)
    ctx.check("message names the timeout", "timed out" in result.content.lower())


@test
def test_bash_timeout_clamped_to_600000ms_max(ctx: Ctx):
    from rolo_claude.tools.bash import MAX_TIMEOUT_MS
    ctx.check("MAX_TIMEOUT_MS is 600000", MAX_TIMEOUT_MS == 600_000)


@test
def test_bash_aborts_via_ctx_abort_event(ctx: Ctx):
    d = _tmpdir("bash-abort-")
    abort = threading.Event()
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, abort=abort)

    def _fire_abort():
        time.sleep(0.3)
        abort.set()

    threading.Thread(target=_fire_abort, daemon=True).start()
    t0 = time.monotonic()
    result = BashTool().run({"command": "sleep 30", "timeout": 60_000}, ctx_obj)
    elapsed = time.monotonic() - t0
    ctx.check(f"abort event kills the command promptly, got {elapsed:.2f}s", elapsed < 10)
    ctx.check("aborted command is an error", result.is_error is True)
    ctx.check("message names the abort", "abort" in result.content.lower())


@test
def test_bash_streams_progress_via_callback(ctx: Ctx):
    d = _tmpdir("bash-progress-")
    chunks = []
    ctx_obj = ToolContext(cwd=d, bash_state={"cwd": d}, progress_cb=chunks.append)
    BashTool().run({"command": "echo streamed-chunk"}, ctx_obj)
    joined = "".join(chunks)
    ctx.check(f"progress callback received the output, got {chunks!r}", "streamed-chunk" in joined)


@test
def test_bash_missing_command_is_error(ctx: Ctx):
    result = BashTool().run({}, ToolContext(cwd=Path(".")))
    ctx.check("missing command is an error", result.is_error is True)


@test
def test_bash_sets_claudecode_env_var(ctx: Ctx):
    d = _tmpdir("bash-env-")
    result = BashTool().run({"command": "echo CLAUDECODE=$CLAUDECODE"}, ToolContext(cwd=d, bash_state={"cwd": d}))
    ctx.check(f"CLAUDECODE=1 is set in the subprocess env, got {result.content!r}", "CLAUDECODE=1" in result.content)


@test
def test_bash_result_cap_is_30000_chars(ctx: Ctx):
    ctx.check("Bash.result_cap == 30000", BashTool().result_cap == 30_000)


@test
def test_new_6_backgrounded_command_returns_promptly(ctx: Ctx):
    """finding 7: completion must be keyed on the WRAPPER process exiting,
    never on stdout EOF -- a backgrounded child keeps the pipe's write end
    open long after the foreground command finished, which used to block
    for the full timeout."""
    d = _tmpdir("bash-bg-")
    t0 = time.monotonic()
    result = BashTool().run({"command": "sleep 5 & echo started", "timeout": 60_000},
                             ToolContext(cwd=d, bash_state={"cwd": d}))
    elapsed = time.monotonic() - t0
    ctx.check(f"returns promptly despite the backgrounded sleep, got {elapsed:.2f}s", elapsed < 3.0)
    ctx.check("no error", result.is_error is False)
    ctx.check(f"the foreground output is captured, got {result.content!r}", "started" in result.content)


@test
def test_new_7_bash_recovers_after_its_cwd_is_deleted(ctx: Ctx):
    """finding 9: the persisted cwd is reused with no existence check --
    `cd build` then `rm -rf build` used to fail EVERY later call for the
    rest of the session, `cd /` included."""
    import shutil as _shutil
    d = _tmpdir("bash-cwd-deleted-")
    sub = d / "build"
    sub.mkdir()
    bash_state = {"cwd": sub}
    ctx_obj = ToolContext(cwd=d, bash_state=bash_state)
    _shutil.rmtree(sub)
    result = BashTool().run({"command": "echo still-alive"}, ctx_obj)
    ctx.check(f"no error after the cwd vanished, got {result.content!r}", result.is_error is False)
    ctx.check("the command still ran for real", "still-alive" in result.content)
    ctx.check(f"bash_state falls back to the session cwd, got {bash_state['cwd']!r}",
              Path(bash_state["cwd"]).resolve() == d.resolve())
    # and the session keeps working on the next call too
    result2 = BashTool().run({"command": "echo second-call"}, ctx_obj)
    ctx.check(f"a later call also succeeds, got {result2.content!r}", result2.is_error is False
              and "second-call" in result2.content)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
