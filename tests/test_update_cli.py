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
                run_returncode=0, after_version="halo 2.0.3 (bbbbbbb, master)", ordering=None):
    """One shared fake rig for the whole report-building + apply chain --
    every test below tweaks just the bits it cares about via the kwargs.
    `ordering` (round C): `commit_is_ancestor`'s own three-way answer --
    None (the default, "no checkout to ask") keeps every existing test's
    "available" wording exactly as before this knob existed; a test that
    wants the new "differs from" wording passes `ordering=False`."""
    orig = {name: getattr(upd, name) for name in
            ("installed_build", "latest_available", "install_kind", "commits_between",
             "commit_is_ancestor", "other_halo_pids", "run")}
    upd.installed_build = lambda **kw: {"version": "2.0.2", "commit": installed_commit,
                                         "requested_revision": None, "checkout": None,
                                         "branch": "master", "editable": False}
    upd.latest_available = lambda *a, **kw: {"channel": "main", "commit": available_commit,
                                              "ref": "master", "reason": None, "source": "cache"}
    upd.install_kind = lambda **kw: {"kind": "uv_tool", "spec": "git+https://github.com/roloVibes/Halo-Harness",
                                      "reinstall_cmd": "uv tool install --reinstall "
                                                        "git+https://github.com/roloVibes/Halo-Harness"}
    upd.commits_between = lambda *a, **kw: (["bbbbbbb fix: a thing"], 1)
    upd.commit_is_ancestor = lambda *a, **kw: ordering
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
        ctx.check(f"says 'available' (ordering unknown/no checkout keeps the old wording), got {out!r}",
                  "available (main): bbbbbbb" in out)
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
def test_cmd_update_without_check_always_queries_live_not_just_with_refresh(ctx: Ctx):
    """Halo 2.0.2 round C: "an explicit --check or /update always
    queries ... with the cache only as the fallback" -- a bare `halo
    update` (no --check, no --refresh) must reach `latest_available`
    with `refresh=True` regardless; the old code only did that when
    `--refresh` was ALSO passed, so a within-TTL cache answer could sit
    stale under an explicit check."""
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="aaaaaaa")
    seen = {}
    real_latest_available = upd.latest_available

    def spy(*a, **kw):
        seen["refresh"] = kw.get("refresh")
        return real_latest_available(*a, **kw)
    upd.latest_available = spy
    try:
        _capture(update_cli.cmd_update, ["--check"])
        ctx.check(f"--check passes refresh=True, got {seen}", seen.get("refresh") is True)
        _capture(update_cli.cmd_update, [])
        ctx.check(f"a bare 'halo update' ALSO passes refresh=True (no --refresh given), got {seen}",
                  seen.get("refresh") is True)
    finally:
        _restore_all(orig)


@test
def test_cmd_update_reports_differs_from_when_a_checkout_confirms_not_an_ancestor(ctx: Ctx):
    """Halo 2.0.2 round C: "'differs from <channel>' when the ordering
    is unknown and git merge-base --is-ancestor when a checkout exists"
    -- a real local checkout that confirms the installed commit is NOT
    an ancestor of the available one (diverged, or pinned to a different
    ref) must say "differs from", never the "available" wording that
    implies a clean, ordinary upgrade path."""
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb", ordering=False)
    try:
        code, out, _err = _capture(update_cli.cmd_update, ["--check"])
        ctx.check(f"exit code is unchanged (still 10 -- commits differ either way), got {code}", code == 10)
        ctx.check(f"says 'differs from', not 'available', got {out!r}", "differs from (main): bbbbbbb" in out)
        ctx.check("the word 'available' never appears as the label itself",
                  "available (main):" not in out)
    finally:
        _restore_all(orig)


@test
def test_apply_update_source_dir_names_the_real_fix_instead_of_could_not_determine(ctx: Ctx):
    """Halo 2.0.2 round 6/C: a plain source directory (no .git, no dist
    metadata -- the tar copy the test suite itself runs from is the
    known real case) used to fall into the generic "could not determine
    how halo was installed -- nothing to run"; now says plainly that
    replacing the directory is the fix, naming its real path."""
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb")
    upd.install_kind = lambda **kw: {"kind": "source_dir", "spec": "/opt/halo-source-copy", "reinstall_cmd": None}
    try:
        code, _out, err = _capture(update_cli.apply_update)
        ctx.check(f"exit 1 (nothing to run), got {code}", code == 1)
        ctx.check(f"names the real path and 'replace the directory', got {err!r}",
                  "/opt/halo-source-copy" in err and "replace the directory" in err)
        ctx.check("never the old generic 'could not determine' wording", "could not determine" not in err)
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
def test_apply_update_runs_in_the_checkout_cwd_never_the_process_cwd(ctx: Ctx):
    """Finding 3 (critical): for an editable/bare/local-dir checkout kind,
    the reinstall command must run WITH `cwd` set to the real checkout --
    never in whatever directory `halo update`/`/update` happened to be
    invoked from (usually an unrelated project repo) -- and must leave
    THIS process's own cwd completely untouched either way."""
    checkout = Path(tempfile.mkdtemp(prefix="halo-update-checkout-"))
    other_project = Path(tempfile.mkdtemp(prefix="halo-update-unrelated-project-"))
    orig = _patch_all(installed_commit="aaaaaaa", available_commit="bbbbbbb")
    upd.install_kind = lambda **kw: {"kind": "editable_checkout", "spec": str(checkout),
                                      "reinstall_cmd": f"git -C {checkout} pull --ff-only && echo reinstalled"}
    seen_cwd = []

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["halo"]:
            return _Proc(stdout="halo 2.0.3 (bbbbbbb, master)\n")
        seen_cwd.append(kwargs.get("cwd"))
        return _Proc(returncode=0)
    upd.run = fake_run
    old_cwd = os.getcwd()
    os.chdir(str(other_project))
    try:
        code, _out, _err = _capture(update_cli.apply_update)
        ctx.check(f"exit 0, got {code}", code == 0)
        ctx.check(f"the reinstall ran with cwd == the checkout, got {seen_cwd}", seen_cwd == [str(checkout)])
        ctx.check(f"this PROCESS's own cwd is untouched, got {os.getcwd()!r}", os.getcwd() == str(other_project))
    finally:
        os.chdir(old_cwd)
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
def test_apply_update_and_relaunch_passes_force_true(ctx: Ctx):
    """Finding 2: `cli._apply_update_and_relaunch`'s own docstring says
    the TUI's Textual app has ALREADY exited by the time this runs -- the
    review found it still called `apply_update()` with no `force`, so it
    silently refused (on account of its own now-irrelevant uv/pipx
    console-script launcher parent -- see the `other_halo_pids` fix) and
    relaunched the OLD build instead of the one it just "applied"."""
    from halo_harness import cli as cli_mod
    calls = []
    orig_apply, orig_relaunch = update_cli.apply_update, upd.relaunch_halo
    update_cli.apply_update = lambda **kw: calls.append(kw) or 0
    upd.relaunch_halo = lambda args, **kw: 0
    try:
        cli_mod._apply_update_and_relaunch(["--continue"])
        ctx.check(f"apply_update called exactly once, got {calls}", len(calls) == 1)
        ctx.check(f"force=True was passed, got {calls}", calls[0].get("force") is True)
    finally:
        update_cli.apply_update = orig_apply
        upd.relaunch_halo = orig_relaunch


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
