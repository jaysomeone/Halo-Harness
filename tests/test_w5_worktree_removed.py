"""tests.test_w5_worktree_removed -- W5 (carried from W4a): WorktreeRemoved
has a real trigger now, from two call sites: `halo worktree rm <path>`
(`worktree_cli.cmd_worktree`) and session-end auto-removal
(`headless.maybe_remove_worktree_on_exit`, config `worktree.remove_on_exit`,
default False). Removed from `hooks.NOT_EMITTED_V1`.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
test, TESTS = new_registry()


def _real_worktree():
    """A real git repo + a real worktree off it, via the SAME
    `create_worktree` `-w/--worktree` itself uses -- never hand-rolled,
    so a test exercises the exact tree shape `remove_worktree` has to cope
    with."""
    from halo_harness.worktree import create_worktree

    repo = Path(tempfile.mkdtemp(prefix="w5-wt-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    state_dir = Path(tempfile.mkdtemp(prefix="w5-wt-state-"))
    wt_path, err = create_worktree(repo, "testwt", state_dir=state_dir)
    assert wt_path is not None, f"fixture setup failed: {err}"
    return repo, wt_path


# ---- halo worktree rm ------------------------------------------------

@test
def test_cmd_worktree_rm_usage_errors(ctx: Ctx):
    from halo_harness.worktree_cli import cmd_worktree
    ctx.check("no args -> exit 2", cmd_worktree([]) == 2)
    ctx.check("unknown subcommand -> exit 2", cmd_worktree(["bogus"]) == 2)
    ctx.check("rm with no path -> exit 2", cmd_worktree(["rm"]) == 2)
    not_a_dir = Path(tempfile.mkdtemp(prefix="w5-wt-notdir-")) / "does-not-exist"
    ctx.check("rm of a non-existent path -> exit 1", cmd_worktree(["rm", str(not_a_dir)]) == 1)


@test
def test_cmd_worktree_rm_removes_a_real_worktree_and_fires_the_hook(ctx: Ctx):
    import halo_harness.worktree_cli as worktree_cli_mod

    repo, wt_path = _real_worktree()
    ctx.check("the worktree exists before rm", wt_path.is_dir())

    fired = []
    original = worktree_cli_mod._fire_worktree_removed
    worktree_cli_mod._fire_worktree_removed = lambda cwd, path: fired.append((cwd, path))
    try:
        code = worktree_cli_mod.cmd_worktree(["rm", str(wt_path)])
    finally:
        worktree_cli_mod._fire_worktree_removed = original

    ctx.check(f"exit 0 on a successful removal, got {code}", code == 0)
    ctx.check("the worktree directory is actually gone", not wt_path.exists())
    ctx.check(f"WorktreeRemoved firing was invoked with the removed path, got {fired}",
              len(fired) == 1 and fired[0][1] == wt_path)


@test
def test_create_worktree_with_no_git_on_path_returns_a_clean_error_never_raises(ctx: Ctx):
    """Release review finding 27: `FileNotFoundError` used to escape
    `create_worktree` as a bare traceback when `git` isn't on PATH at
    all, instead of this module's own documented `(None, reason)`
    contract. Same repro technique the review itself verified with on
    this host."""
    from halo_harness.worktree import create_worktree
    old_path = os.environ.get("PATH")
    os.environ["PATH"] = ""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="w6b-no-git-"))
        path, err = create_worktree(tmp, "x", state_dir=tmp)
        ctx.check(f"no worktree created, never raises, got path={path}", path is None)
        ctx.check(f"a clean, specific error message naming git, got {err!r}",
                  err is not None and "git" in err.lower())
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path


@test
def test_git_helper_never_raises_on_a_subprocess_timeout(ctx: Ctx):
    """Release review finding 27: `subprocess.TimeoutExpired` (a hung git
    process) must route through the same non-raising contract, for every
    caller of the shared `_git` helper (`repo_root`, `create_worktree`,
    `remove_worktree` alike)."""
    import subprocess
    import halo_harness.worktree as worktree_mod

    def _fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    old_run = worktree_mod.subprocess.run
    worktree_mod.subprocess.run = _fake_run
    try:
        result = worktree_mod._git(["status"], Path.cwd())
        ctx.check(f"never raises, a synthetic failed result instead, got {result}", result.returncode != 0)
        ctx.check(f"root_root() built on top of it degrades to None, never raises",
                  worktree_mod.repo_root(Path.cwd()) is None)
    finally:
        worktree_mod.subprocess.run = old_run


@test
def test_cmd_worktree_rm_refuses_a_dirty_worktree_and_keeps_it(ctx: Ctx):
    """Release review finding 28: `git worktree remove --force` used to
    discard a session's own uncommitted edits on exit -- a dirty worktree
    must now be kept, reported, never force-removed."""
    import io
    import contextlib
    import halo_harness.worktree_cli as worktree_cli_mod

    repo, wt_path = _real_worktree()
    (wt_path / "uncommitted.txt").write_text("real work, not yet committed\n", encoding="utf-8")

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        code = worktree_cli_mod.cmd_worktree(["rm", str(wt_path)])
    ctx.check(f"exit 1 (kept, not removed), got {code}", code == 1)
    ctx.check("the worktree directory still exists", wt_path.is_dir())
    ctx.check("the uncommitted file itself survives", (wt_path / "uncommitted.txt").exists())
    ctx.check(f"stderr explains it was kept for uncommitted changes, got {captured.getvalue()!r}",
              "uncommitted changes" in captured.getvalue())


@test
def test_cmd_worktree_rm_deletes_the_branch_after_a_successful_removal(ctx: Ctx):
    """Release review finding 28: `halo-worktree-*` used to be left behind
    forever after every successful removal."""
    import subprocess as sp
    import halo_harness.worktree_cli as worktree_cli_mod

    repo, wt_path = _real_worktree()
    branch = sp.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(wt_path),
                     capture_output=True, text=True, check=True).stdout.strip()
    ctx.check(f"fixture sanity: it's a real halo-worktree-* branch, got {branch!r}",
              branch.startswith("halo-worktree-"))

    code = worktree_cli_mod.cmd_worktree(["rm", str(wt_path)])
    ctx.check(f"exit 0 on a successful (clean) removal, got {code}", code == 0)

    listed = sp.run(["git", "branch", "--list", branch], cwd=str(repo), capture_output=True, text=True)
    ctx.check(f"the branch is gone too, got {listed.stdout!r}", listed.stdout.strip() == "")


@test
def test_maybe_remove_worktree_on_exit_keeps_a_dirty_worktree_and_reports_false(ctx: Ctx):
    from halo_harness.headless import maybe_remove_worktree_on_exit
    repo, wt_path = _real_worktree()
    (wt_path / "uncommitted.txt").write_text("real work\n", encoding="utf-8")
    session = _FakeSession()

    def _run():
        return maybe_remove_worktree_on_exit({"_worktree_created_path": str(wt_path)}, session)

    did = _with_config(True, _run)
    ctx.check("dirty -> not removed even though opted in", did is False and session.removed_calls == [])
    ctx.check("the worktree (and its uncommitted file) still exists", (wt_path / "uncommitted.txt").exists())


@test
def test_fire_worktree_removed_builds_a_hook_runner_and_fires_when_configured(ctx: Ctx):
    """The actual hook-firing glue (`worktree_cli._fire_worktree_removed`),
    isolated from the git mechanics above: a `Settings` with a real
    WorktreeRemoved hook configured gets it fired with the removed path."""
    from halo_harness.config.settings import Settings
    from halo_harness.worktree_cli import _fire_worktree_removed

    fired_events = []

    class _FakeRunner:
        def __init__(self, *a, **k):
            pass

        def has_hooks(self, event):
            return event == "WorktreeRemoved"

        def payload(self, event, extra=None):
            return {"hook_event_name": event, **(extra or {})}

        def run(self, event, payload, **kwargs):
            fired_events.append((event, payload))

    import halo_harness.headless as headless_mod
    original_build = headless_mod.build_hook_runner
    headless_mod.build_hook_runner = lambda **kwargs: _FakeRunner()
    try:
        cwd = Path(tempfile.mkdtemp(prefix="w5-wt-firetest-"))
        fake_path = cwd / "some-worktree"
        _fire_worktree_removed(cwd, fake_path)
    finally:
        headless_mod.build_hook_runner = original_build

    ctx.check(f"WorktreeRemoved actually fired with the path, got {fired_events}",
              fired_events and fired_events[0][0] == "WorktreeRemoved"
              and fired_events[0][1].get("path") == str(fake_path))


# ---- session-end auto-removal (config worktree.remove_on_exit) -----------

class _FakeSession:
    def __init__(self):
        self.removed_calls = []

    def _fire_worktree_removed(self, path):
        self.removed_calls.append(path)


def _with_config(value, fn):
    """Runs `fn()` with `theme.get_config_value("worktree.remove_on_exit", ...)`
    forced to `value` -- scoped via a real, isolated `~/.halo/config.json`
    (never the developer's own), restored after."""
    import json
    from halo_harness.config.paths import bridge_home

    config_path = bridge_home() / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    existing = {}
    if original is not None:
        try:
            existing = json.loads(original)
        except ValueError:
            existing = {}
    existing["worktree"] = {"remove_on_exit": value}
    config_path.write_text(json.dumps(existing), encoding="utf-8")
    try:
        return fn()
    finally:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(original, encoding="utf-8")


@test
def test_maybe_remove_worktree_on_exit_noop_when_no_path_was_created(ctx: Ctx):
    from halo_harness.headless import maybe_remove_worktree_on_exit
    session = _FakeSession()
    did = maybe_remove_worktree_on_exit({}, session)
    ctx.check("no-op, no cli_flags path at all", did is False and session.removed_calls == [])


@test
def test_maybe_remove_worktree_on_exit_default_false_leaves_the_tree(ctx: Ctx):
    from halo_harness.headless import maybe_remove_worktree_on_exit
    repo, wt_path = _real_worktree()
    session = _FakeSession()

    def _run():
        return maybe_remove_worktree_on_exit({"_worktree_created_path": str(wt_path)}, session)

    did = _with_config(False, _run)
    ctx.check("default (False) config -> not removed", did is False and session.removed_calls == [])
    ctx.check("the worktree still exists", wt_path.is_dir())


@test
def test_maybe_remove_worktree_on_exit_removes_and_fires_when_opted_in(ctx: Ctx):
    from halo_harness.headless import maybe_remove_worktree_on_exit
    repo, wt_path = _real_worktree()
    session = _FakeSession()

    def _run():
        return maybe_remove_worktree_on_exit({"_worktree_created_path": str(wt_path)}, session)

    did = _with_config(True, _run)
    ctx.check("opted in -> actually removed", did is True)
    ctx.check("the worktree is gone from disk", not wt_path.exists())
    ctx.check(f"WorktreeRemoved was fired with this session's own path, got {session.removed_calls}",
              session.removed_calls == [str(wt_path)])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
