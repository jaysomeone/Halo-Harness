"""tests.test_update_cli -- Halo 2.0.2 round 6: `halo update` (cmd_update,
apply_update). Every piece of halo_harness.update that would shell out,
fetch, list processes, or exec is monkeypatched to a canned fake -- this
file never runs a real install, never lists real processes for a real
refusal, and never execs anything.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()

from halo_harness import update as upd
from halo_harness import update_cli

test, TESTS = new_registry()


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _patch_all(*, installed_commit="aaaaaaa", available_commit="bbbbbbb", other_pids=None,
                run_returncode=0, after_version="halo 2.0.3 (bbbbbbb, master)"):
    """One shared fake rig for the whole report-building + apply chain --
    every test below tweaks just the bits it cares about via the kwargs."""
    orig = {name: getattr(upd, name) for name in
            ("installed_build", "latest_available", "install_kind", "commits_between",
             "other_halo_pids", "run")}
    upd.installed_build = lambda **kw: {"version": "2.0.2", "commit": installed_commit,
                                         "requested_revision": None, "checkout": None,
                                         "branch": "master", "editable": False}
    upd.latest_available = lambda *a, **kw: {"channel": "main", "commit": available_commit,
                                              "ref": "master", "reason": None, "source": "cache"}
    upd.install_kind = lambda **kw: {"kind": "uv_tool", "spec": "git+https://github.com/roloVibes/Halo-Harness",
                                      "reinstall_cmd": "uv tool install --reinstall "
                                                        "git+https://github.com/roloVibes/Halo-Harness"}
    upd.commits_between = lambda *a, **kw: (["bbbbbbb fix: a thing"], 1)
    upd.other_halo_pids = lambda **kw: list(other_pids or [])

    def _fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["halo"]:
            return _Proc(stdout=after_version + "\n")
        return _Proc(returncode=run_returncode)
    upd.run = _fake_run
    return orig


def _restore_all(orig: dict) -> None:
    for name, value in orig.items():
        setattr(upd, name, value)


def _capture(fn, *args) -> "tuple[int, str, str]":
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue(), err.getvalue()


@test
def test_cmd_update_check_exit_0_when_up_to_date(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="aaaaaaa")
    try:
        code, out, _err = _capture(update_cli.cmd_update, ["--check"])
        ctx.check(f"exit 0, got {code}", code == 0)
        ctx.check(f"reports installed and available, got {out!r}", "installed:" in out and "available" in out)
    finally:
        _restore_all(orig)


@test
def test_cmd_update_check_exit_10_when_update_available(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb")
    try:
        code, out, _err = _capture(update_cli.cmd_update, ["--check"])
        ctx.check(f"exit 10, got {code}", code == 10)
        ctx.check(f"lists the commit(s) between, got {out!r}", "bbbbbbb fix: a thing" in out)
        ctx.check(f"names the exact reinstall command, got {out!r}",
                  "uv tool install --reinstall git+https://github.com/roloVibes/Halo-Harness" in out)
    finally:
        _restore_all(orig)


@test
def test_cmd_update_check_exit_1_when_unknown(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb")
    upd.latest_available = lambda *a, **kw: {"channel": "main", "commit": None, "ref": None,
                                              "reason": "network unavailable", "source": "disabled"}
    try:
        code, out, _err = _capture(update_cli.cmd_update, ["--check"])
        ctx.check(f"exit 1, got {code}", code == 1)
        ctx.check(f"names the reason, got {out!r}", "network unavailable" in out)
    finally:
        _restore_all(orig)


@test
def test_cmd_update_check_never_applies_anything(ctx: Ctx):
    """--check must never call the reinstall command (upd.run) at all --
    proven by a run_fn that explodes on anything but `halo --version`."""
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb")
    def boom(cmd, **kw):
        raise AssertionError(f"--check must never run anything, got {cmd!r}")
    upd.run = boom
    try:
        code, _out, _err = _capture(update_cli.cmd_update, ["--check"])
        ctx.check(f"still reports exit 10, got {code}", code == 10)
    finally:
        _restore_all(orig)


@test
def test_apply_update_refuses_while_another_halo_process_is_running(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb", other_pids=[4242])
    try:
        code, _out, err = _capture(update_cli.apply_update)
        ctx.check(f"refuses, exit 1, got {code}", code == 1)
        ctx.check(f"names the pid, got {err!r}", "4242" in err)
    finally:
        _restore_all(orig)


@test
def test_apply_update_force_overrides_the_refusal(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb", other_pids=[4242])
    try:
        code, out, _err = _capture(lambda: update_cli.apply_update(force=True))
        ctx.check(f"--force proceeds anyway, got {code}", code == 0)
        ctx.check(f"runs the reinstall command live, got {out!r}",
                  "uv tool install --reinstall" in out)
    finally:
        _restore_all(orig)


@test
def test_apply_update_reports_before_and_after_from_a_fresh_halo_dash_dash_version(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb",
                       after_version="halo 2.0.3 (bbbbbbb, master)")
    try:
        code, out, _err = _capture(update_cli.apply_update)
        ctx.check(f"exit 0, got {code}", code == 0)
        ctx.check(f"before line names the OLD commit, got {out!r}", "before halo 2.0.2 (aaaaaaa, master)" in out)
        ctx.check(f"after line comes from a FRESH `halo --version`, got {out!r}",
                  "after  halo 2.0.3 (bbbbbbb, master)" in out)
    finally:
        _restore_all(orig)


@test
def test_apply_update_nonzero_reinstall_returncode_is_a_failure(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb", run_returncode=1)
    try:
        code, _out, _err = _capture(update_cli.apply_update)
        ctx.check(f"the reinstall command's own failure propagates, got {code}", code == 1)
    finally:
        _restore_all(orig)


@test
def test_cmd_update_to_retargets_the_spec_in_the_printed_command(ctx: Ctx):
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="aaaaaaa")  # up to date -> --check only prints
    try:
        code, out, _err = _capture(update_cli.cmd_update, ["--check", "--to", "v9.9.9"])
        ctx.check(f"exit 0 (up to date, --to is cosmetic here), got {code}", code == 0)
        ctx.check(f"the printed command targets v9.9.9, got {out!r}",
                  "git+https://github.com/roloVibes/Halo-Harness@v9.9.9" in out)
    finally:
        _restore_all(orig)


@test
def test_cmd_update_channel_flag_is_remembered_in_config(ctx: Ctx):
    state_dir = Path(tempfile.mkdtemp(prefix="halo-update-channel-"))
    old_state_dir_env = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="aaaaaaa")
    try:
        ctx.check("no remembered channel yet", upd.default_channel({}) == "main")
        _capture(update_cli.cmd_update, ["--check", "--channel", "stable"])
        ctx.check("--channel stable is now remembered by default_channel()",
                  upd.default_channel({}) == "stable")
    finally:
        _restore_all(orig)
        if old_state_dir_env is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir_env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
