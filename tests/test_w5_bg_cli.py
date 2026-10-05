"""tests.test_w5_bg_cli -- W5 (carried from W4a): `halo bg list|logs|stop|
rm` over `--bg`'s own detached-run files (`<state>/bg/<id>/{output.log,
meta.json}`). `bg_run.py` stayed argparse-free by design (its own module
docstring) so `list_runs`/`pid_alive`/`kill_pid` are pinned directly here;
`bg_cli.cmd_bg` is pinned end to end against REAL files/processes (a real
short-lived `halo --version -p` child for the happy path, a real `sleep`-
shaped child for the stop/kill path) -- no existing test covered `bg_run.py`
at all before this round, not even `start_background_run`/`run_in_tmux`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
        home = Path(tempfile.mkdtemp(prefix="w5-bg-home-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ.pop("BRIDGE_STATE_DIR", None)
        self.home = home
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _capture(fn, *a, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fn(*a, **kw)
    return code, out.getvalue()


# ---- bg_run.py: list_runs / load_run / pid_alive / kill_pid (pure/real) ---

@test
def test_list_runs_and_load_run_read_real_meta_files(ctx: Ctx):
    from halo_harness.bg_run import list_runs, load_run, start_background_run

    class _FakeProc:
        pid = 999999

    with _Env():
        info = start_background_run(["--version"], popen=lambda *a, **k: _FakeProc())
        runs = list_runs()
        ctx.check(f"exactly one run listed, got {runs}", len(runs) == 1 and runs[0]["id"] == info["id"])
        ctx.check("run_dir stashed onto the dict", Path(runs[0]["run_dir"]).is_dir())
        loaded = load_run(info["id"])
        ctx.check(f"load_run finds the same one, got {loaded}", loaded is not None and loaded["pid"] == 999999)
        ctx.check("load_run(unknown) is None", load_run("does-not-exist") is None)


@test
def test_start_background_run_windows_sets_detached_process_flag(ctx: Ctx):
    """Release review finding 37: `CREATE_NEW_PROCESS_GROUP` alone still
    leaves the detached child attached to the launching console --
    Windows sends CTRL_CLOSE_EVENT to every process attached to a
    closing console, killing this child the moment that terminal window
    closes (the POSIX path's `start_new_session` is a real new session,
    survives the launching terminal closing by construction).
    `DETACHED_PROCESS` is what actually detaches it on Windows."""
    if os.name != "nt":
        raise SkipTest("Windows-only creationflags")
    from halo_harness.bg_run import start_background_run

    class _FakeProc:
        pid = 999998

    captured = {}

    def _fake_popen(*a, **kw):
        captured.update(kw)
        return _FakeProc()

    with _Env():
        start_background_run(["--version"], popen=_fake_popen)
        flags = captured.get("creationflags", 0)
        ctx.check(f"CREATE_NEW_PROCESS_GROUP is set, got {flags!r}",
                  bool(flags & subprocess.CREATE_NEW_PROCESS_GROUP))
        ctx.check(f"DETACHED_PROCESS is ALSO set, got {flags!r}",
                  bool(flags & subprocess.DETACHED_PROCESS))
        ctx.check("start_new_session is False on Windows (creationflags is the win32 mechanism instead)",
                  captured.get("start_new_session") is False)


@test
def test_pid_alive_true_for_self_and_false_once_a_real_process_exits(ctx: Ctx):
    from halo_harness.bg_run import pid_alive

    ctx.check("this test process's own pid is alive", pid_alive(os.getpid()) is True)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    deadline = time.monotonic() + 5.0
    dead = False
    while time.monotonic() < deadline:
        if not pid_alive(proc.pid):
            dead = True
            break
        time.sleep(0.1)
    ctx.check(f"an exited, reaped process reports not-alive, got pid_alive={not dead}", dead)


@test
def test_kill_pid_never_kills_the_callers_own_process_group(ctx: Ctx):
    """Regression: a child that shares THIS process's group (a plain Popen,
    unlike a real --bg run) must be killed alone -- the first version
    ran killpg on the caller's own group, which on Linux ended the whole
    test runner, its wrapper shell and the SSH session every time the
    suite reached this module."""
    from halo_harness import bg_run
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    killpg_calls = []
    real_killpg = getattr(os, "killpg", None)
    if real_killpg is not None:
        os.killpg = lambda pgid, sig: killpg_calls.append(pgid)  # record, never signal
    try:
        own_pgid = os.getpgid(0) if hasattr(os, "getpgid") else None
        ok = bg_run.kill_pid(proc.pid)
        ctx.check(f"the same-group child is gone, got {ok}", ok is True)
        if own_pgid is not None:
            ctx.check(f"killpg was never aimed at our own group, calls={killpg_calls}", own_pgid not in killpg_calls)
        ctx.check("kill_pid refuses to kill the calling process", bg_run.kill_pid(os.getpid()) is False)
    finally:
        if real_killpg is not None:
            os.killpg = real_killpg
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


@test
def test_kill_pid_stops_a_real_running_process(ctx: Ctx):
    from halo_harness.bg_run import kill_pid, pid_alive

    # A real --bg run lives in its own session (bg_run.start_background_run
    # uses start_new_session), so the sleeper does too.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=(os.name != "nt"))
    try:
        deadline = time.monotonic() + 5.0
        alive = False
        while time.monotonic() < deadline:
            if pid_alive(proc.pid):
                alive = True
                break
            time.sleep(0.05)
        ctx.check("the sleeper is confirmed alive first", alive)
        ok = kill_pid(proc.pid)
        ctx.check(f"kill_pid reports success, got {ok}", ok is True)
        ctx.check(f"it is actually gone, got pid_alive={pid_alive(proc.pid)}", pid_alive(proc.pid) is False)
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ---- bg_cli.cmd_bg: list / logs / stop / rm, end to end --------------------

@test
def test_cmd_bg_list_and_logs_against_a_real_short_lived_halo_child(ctx: Ctx):
    from halo_harness.bg_cli import cmd_bg
    from halo_harness.bg_run import start_background_run

    with _Env():
        # A real, FAST child: `--version` exits almost instantly, well
        # before `-p` (appended by start_background_run itself, since
        # neither -p nor --print is already present) ever tries to build a
        # session -- main()'s own `if args.version:` branch returns first.
        info = start_background_run(["--version"])
        deadline = time.monotonic() + 20.0
        log_path = Path(info["log_path"])
        while time.monotonic() < deadline:
            if log_path.exists() and log_path.stat().st_size > 0:
                break
            time.sleep(0.1)

        code, out = _capture(cmd_bg, ["list"])
        ctx.check(f"exit 0, got {code}", code == 0)
        ctx.check(f"the run id shows up, got {out!r}", info["id"] in out)
        ctx.check(f"a status column is present, got {out!r}", "running" in out or "exited" in out)

        code, out = _capture(cmd_bg, ["logs", info["id"]])
        ctx.check(f"exit 0, got {code}", code == 0)
        ctx.check(f"the real halo version string is in the captured log, got {out!r}", "halo" in out.lower())

        code, _out = _capture(cmd_bg, ["logs", "not-a-real-id"])
        ctx.check(f"an unknown id is a clean exit-1 error, got {code}", code == 1)


@test
def test_cmd_bg_stop_and_rm_on_a_real_long_running_child(ctx: Ctx):
    from halo_harness.bg_cli import cmd_bg
    from halo_harness.bg_run import list_runs, pid_alive, start_background_run

    with _Env():
        # A long-lived, harmless real command via Bash (not `halo` itself,
        # so there's no race with it exiting on its own before `stop` runs).
        long_cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        info = {"id": "fake-long-run"}
        from halo_harness.bg_run import _bg_root
        run_dir = _bg_root() / info["id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(long_cmd, stdout=open(run_dir / "output.log", "ab"), stderr=subprocess.STDOUT)
        try:
            meta = {"id": info["id"], "pid": proc.pid, "command": long_cmd, "cwd": str(Path.cwd()),
                     "started": time.time(), "log_path": str(run_dir / "output.log")}
            (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not pid_alive(proc.pid):
                time.sleep(0.05)
            ctx.check("the long-running child is confirmed alive", pid_alive(proc.pid))

            code, _out = _capture(cmd_bg, ["rm", info["id"]])
            ctx.check(f"rm WITHOUT --force refuses a still-running job, got exit {code}", code == 1)
            ctx.check("the run directory is still there", run_dir.is_dir())

            code, out = _capture(cmd_bg, ["stop", info["id"]])
            ctx.check(f"stop succeeds, got exit {code} out={out!r}", code == 0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and pid_alive(proc.pid):
                time.sleep(0.05)
            ctx.check("the process is actually dead after stop", not pid_alive(proc.pid))

            code, out = _capture(cmd_bg, ["rm", info["id"]])
            ctx.check(f"rm now succeeds (nothing running any more), got exit {code} out={out!r}", code == 0)
            ctx.check("the run directory is gone", not run_dir.exists())
            ctx.check("list_runs no longer sees it", info["id"] not in [r.get("id") for r in list_runs()])
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


@test
def test_f15_w6a_process_start_time_is_stable_and_present_for_a_real_pid(ctx: Ctx):
    from halo_harness.bg_run import process_start_time

    mine = process_start_time(os.getpid())
    ctx.check(f"a value came back for this process's own real pid, got {mine!r}", mine)
    again = process_start_time(os.getpid())
    ctx.check(f"re-reading it gives the SAME value, got {mine!r} vs {again!r}", mine == again)
    ctx.check("an invalid/unused pid returns None, not a crash", process_start_time(2**30) in (None, ""))


@test
def test_f15_w6a_is_our_process_refuses_a_pid_whose_start_time_differs(ctx: Ctx):
    """finding 15 (W6a): the core of the fix -- a PID that is genuinely
    alive right now (this test's own process) but whose recorded
    `start_time` does NOT match a fresh read is treated as "not our
    run" (the OS reused the pid), never silently reported as still
    running."""
    from halo_harness.bg_run import is_our_process, process_start_time

    real_start = process_start_time(os.getpid())
    matching_meta = {"pid": os.getpid(), "start_time": real_start}
    ctx.check(f"matching start_time -> our process, got {matching_meta}", is_our_process(matching_meta) is True)

    mismatched_meta = {"pid": os.getpid(), "start_time": "definitely-not-the-real-value-12345"}
    ctx.check(f"mismatched start_time -> refused even though the pid IS alive, got {mismatched_meta}",
              is_our_process(mismatched_meta) is False)

    no_field_meta = {"pid": os.getpid()}
    ctx.check(f"no start_time at all -> falls back to pid-only (pre-upgrade meta.json), got {no_field_meta}",
              is_our_process(no_field_meta) is True)

    dead_meta = {"pid": 2**30, "start_time": "whatever"}
    ctx.check("an unused pid is never 'our process' regardless of start_time",
              is_our_process(dead_meta) is False)


@test
def test_f15_w6a_start_background_run_records_start_time_and_writes_status_json(ctx: Ctx):
    """finding 15 (W6a): `meta.json` now carries `start_time`, and the
    detached child (routed through `_bg_wrapper`) writes `status.json`
    once the real command actually exits -- the "Tests to add" item this
    pins end to end against a real short-lived `halo --version -p` run."""
    import json as _json
    from halo_harness.bg_run import load_run, start_background_run

    with _Env():
        info = start_background_run(["--version"])
        meta = load_run(info["id"])
        ctx.check(f"start_time was recorded, got {meta}", meta is not None and meta.get("start_time"))
        status_path = Path(meta["status_path"])

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not status_path.exists():
            time.sleep(0.1)
        ctx.check(f"status.json was written once the real command exited, got exists={status_path.exists()}",
                  status_path.exists())
        if status_path.exists():
            status = _json.loads(status_path.read_text(encoding="utf-8"))
            ctx.check(f"it records a clean exit code, got {status}", status.get("exit_code") == 0)


@test
def test_cmd_bg_unknown_subcommand_and_no_args(ctx: Ctx):
    from halo_harness.bg_cli import cmd_bg
    code, _out = _capture(cmd_bg, [])
    ctx.check(f"no args -> exit 2, got {code}", code == 2)
    code, _out = _capture(cmd_bg, ["bogus"])
    ctx.check(f"unknown subcommand -> exit 2, got {code}", code == 2)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
