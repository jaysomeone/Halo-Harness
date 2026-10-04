"""tests.test_update -- Halo 2.0.2 round 6: halo_harness/update.py's own
installed_build/install_kind (PEP 610 direct_url.json + a live checkout's
own git HEAD) and latest_available/commits_between (a fake git ls-remote/
GitHub API, the 24h cache, `update.check: false`). Every subprocess/HTTP
call is faked via `run_fn=`/`fetch_json=` -- never the real network, never
a real `git ls-remote` against GitHub, never a real install. Deliberately
does NOT call `ensure_default_provider_credentials()` (it sets
`BRIDGE_TEST_NO_BACKGROUND_NET=1` by default, which would silently mask
the very `background_net_disabled()` gate this file tests directly).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()

from halo_harness import update as upd

test, TESTS = new_registry()


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_run(table: dict):
    """`table`: {substring-of-argv-joined: _Proc}. The first matching
    substring wins; a call matching nothing raises -- proof an update.py
    function never shells out more (or differently) than expected."""
    def run(cmd, **kwargs):
        joined = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        for key, proc in table.items():
            if key in joined:
                return proc
        raise AssertionError(f"unexpected subprocess call: {joined!r}")
    return run


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="halo-update-test-"))


@contextlib.contextmanager
def _background_net_enabled():
    """Several tests below need `background_net_disabled()` to read False
    so they can exercise `latest_available`'s real (fake-seamed) git/API
    path -- but `tests/run_all.py` runs every module in ONE process, and
    an earlier-loaded module's own `ensure_default_provider_credentials()`
    (many call it) sets `BRIDGE_TEST_NO_BACKGROUND_NET=1` process-wide via
    `setdefault`, which then outlives that module. Scoped here instead of
    assumed absent, with the same save/restore every other `_Env`-style
    helper in this tree uses."""
    old = os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
    try:
        yield
    finally:
        if old is not None:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old


def _patched(name, value):
    orig = getattr(upd, name)
    setattr(upd, name, value)
    return name, orig


def _restore(*patches) -> None:
    for name, orig in patches:
        setattr(upd, name, orig)


# ---------------------------------------------------------------------------
# installed_build / format_version_line
# ---------------------------------------------------------------------------

@test
def test_installed_build_git_vcs_with_requested_revision(ctx: Ctx):
    d = {"url": "https://github.com/roloVibes/Halo-Harness",
         "vcs_info": {"vcs": "git", "commit_id": "abcdef1234567890", "requested_revision": "master"}}
    b = upd.installed_build(direct_url=d)
    ctx.check(f"commit is the short form, got {b['commit']!r}", b["commit"] == "abcdef1")
    ctx.check(f"requested_revision kept, got {b['requested_revision']!r}", b["requested_revision"] == "master")
    ctx.check("not editable", b["editable"] is False)
    ctx.check(f"format_version_line shows both, got {upd.format_version_line(b)!r}",
              upd.format_version_line(b) == f"halo {b['version']} (abcdef1, master)")


@test
def test_installed_build_git_vcs_no_requested_revision(ctx: Ctx):
    """The real box this was built on: a `git+URL` install with no
    explicit ref asked for has NO `requested_revision` key at all
    (confirmed against this box's own real direct_url.json) -- must
    degrade to a commit-only version line, never crash."""
    d = {"url": "https://github.com/roloVibes/Halo-Harness",
         "vcs_info": {"vcs": "git", "commit_id": "f0d11745b218d04119849d8f5263fe991e165d2"}}
    b = upd.installed_build(direct_url=d)
    ctx.check(f"commit known, got {b['commit']!r}", b["commit"] == "f0d1174")
    ctx.check("requested_revision is None", b["requested_revision"] is None)
    ctx.check(f"version line has no branch, got {upd.format_version_line(b)!r}",
              upd.format_version_line(b) == f"halo {b['version']} (f0d1174)")


@test
def test_installed_build_editable_runs_git_in_checkout(ctx: Ctx):
    checkout = _tmp()
    d = {"url": f"file://{checkout.as_posix()}", "dir_info": {"editable": True}}
    run_fn = _fake_run({"rev-parse": _Proc("c0ffee1\n"), "branch": _Proc("feature-x\n")})
    b = upd.installed_build(direct_url=d, run_fn=run_fn)
    ctx.check("editable is True", b["editable"] is True)
    ctx.check(f"checkout resolved from the file:// url, got {b['checkout']!r}",
              Path(b["checkout"]).resolve() == checkout.resolve())
    ctx.check(f"commit from git rev-parse, got {b['commit']!r}", b["commit"] == "c0ffee1")
    ctx.check(f"branch from git branch --show-current, got {b['branch']!r}", b["branch"] == "feature-x")


@test
def test_installed_build_non_editable_dir_info_still_resolves_commit(ctx: Ctx):
    """Finding 16: both installers' default install (`uv tool install
    --reinstall .`, never --editable) has NO "editable" key in dir_info
    at all -- `installed_build` must still resolve checkout/commit/branch
    for it, or `halo update`/doctor treat it as "commit unknown" forever
    (so an available update, even one the install COULD pull, never
    shows)."""
    checkout = _tmp()
    d = {"url": f"file://{checkout.as_posix()}", "dir_info": {}}
    run_fn = _fake_run({"rev-parse": _Proc("d00dfee\n"), "branch": _Proc("master\n")})
    b = upd.installed_build(direct_url=d, run_fn=run_fn)
    ctx.check("editable is False (no key present)", b["editable"] is False)
    ctx.check(f"checkout resolved from the file:// url, got {b['checkout']!r}",
              Path(b["checkout"]).resolve() == checkout.resolve())
    ctx.check(f"commit from git rev-parse, got {b['commit']!r}", b["commit"] == "d00dfee")


@test
def test_installed_build_no_direct_url_uses_pythonpath_checkout(ctx: Ctx):
    """"nothing for a plain PYTHONPATH run" -- direct_url=None (no real
    distribution at all) still finds a checkout (faked here instead of
    this repo's own real one) and asks git in it."""
    checkout = _tmp()
    patch = _patched("_checkout_root_on_pythonpath", lambda: checkout)
    try:
        run_fn = _fake_run({"rev-parse": _Proc("1234567\n"), "branch": _Proc("master\n")})
        b = upd.installed_build(direct_url=None, run_fn=run_fn)
    finally:
        _restore(patch)
    ctx.check(f"checkout is the fake one, got {b['checkout']!r}", b["checkout"] == str(checkout))
    ctx.check(f"commit from git, got {b['commit']!r}", b["commit"] == "1234567")
    ctx.check(f"branch from git, got {b['branch']!r}", b["branch"] == "master")


@test
def test_format_version_line_no_commit_at_all(ctx: Ctx):
    b = {"version": "2.0.2", "commit": None, "requested_revision": None, "branch": None}
    ctx.check(f"just the bare version, got {upd.format_version_line(b)!r}",
              upd.format_version_line(b) == "halo 2.0.2")


# ---------------------------------------------------------------------------
# install_kind
# ---------------------------------------------------------------------------

@test
def test_install_kind_uv_tool_via_receipt_file(ctx: Ctx):
    prefix = _tmp()
    (prefix / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    d = {"url": "https://github.com/roloVibes/Halo-Harness",
         "vcs_info": {"vcs": "git", "commit_id": "abc1234567"}}
    k = upd.install_kind(direct_url=d, prefix=str(prefix), run_fn=_fake_run({}))
    ctx.check(f"kind is uv_tool, got {k}", k["kind"] == "uv_tool")
    ctx.check(f"spec is the plain git+ URL, got {k['spec']!r}",
              k["spec"] == "git+https://github.com/roloVibes/Halo-Harness")
    ctx.check(f"reinstall_cmd, got {k['reinstall_cmd']!r}",
              k["reinstall_cmd"] == "uv tool install --reinstall git+https://github.com/roloVibes/Halo-Harness")


@test
def test_install_kind_keeps_the_requested_revision_in_the_spec(ctx: Ctx):
    prefix = _tmp()
    (prefix / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    d = {"url": "https://github.com/roloVibes/Halo-Harness",
         "vcs_info": {"vcs": "git", "commit_id": "abc1234567", "requested_revision": "v2.0.1"}}
    k = upd.install_kind(direct_url=d, prefix=str(prefix), run_fn=_fake_run({}))
    ctx.check(f"spec keeps the requested revision, got {k['spec']!r}",
              k["spec"] == "git+https://github.com/roloVibes/Halo-Harness@v2.0.1")


@test
def test_install_kind_pipx_via_metadata_file(ctx: Ctx):
    prefix = _tmp()
    (prefix / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    d = {"url": "https://github.com/roloVibes/Halo-Harness", "vcs_info": {"vcs": "git", "commit_id": "abc1234567"}}
    # No uv-receipt.toml at this fake prefix -- _uv_tool_has is still
    # consulted first (uv really is on PATH on this box), so it needs a
    # fake answer too, naming something other than this distribution.
    run_fn = _fake_run({"uv tool list": _Proc("some-other-tool v1\n")})
    k = upd.install_kind(direct_url=d, prefix=str(prefix), run_fn=run_fn)
    ctx.check(f"kind is pipx, got {k}", k["kind"] == "pipx")
    ctx.check(f"reinstall_cmd uses pipx install --force, got {k['reinstall_cmd']!r}",
              k["reinstall_cmd"].startswith("pipx install --force "))


@test
def test_install_kind_pip_fallback_after_fake_uv_and_pipx_list(ctx: Ctx):
    """No receipt files at all -- must still ask `uv tool list`/`pipx
    list` (faked here, both say "not found") before falling back to
    plain pip -- proven even on a box (like this one) where `uv` really
    IS on PATH, since the fake `run_fn` answer is what decides it."""
    prefix = _tmp()
    d = {"url": "https://github.com/roloVibes/Halo-Harness", "vcs_info": {"vcs": "git", "commit_id": "abc1234567"}}
    run_fn = _fake_run({"uv tool list": _Proc("some-other-tool v1\n"), "pipx list": _Proc("nothing here\n")})
    k = upd.install_kind(direct_url=d, prefix=str(prefix), run_fn=run_fn)
    ctx.check(f"kind is pip, got {k}", k["kind"] == "pip")
    # Finding 16: `sys.executable`, never whichever bare `python` happens
    # to be first on PATH (which is not necessarily THIS install's own
    # interpreter).
    ctx.check(f"reinstall_cmd uses sys.executable, got {k['reinstall_cmd']!r}",
              k["reinstall_cmd"] == f"{sys.executable} -m pip install --upgrade "
                                     f"git+https://github.com/roloVibes/Halo-Harness")


@test
def test_install_kind_editable_checkout(ctx: Ctx):
    prefix = _tmp()
    (prefix / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    checkout = _tmp()
    d = {"url": f"file://{checkout.as_posix()}", "dir_info": {"editable": True}}
    k = upd.install_kind(direct_url=d, prefix=str(prefix), run_fn=_fake_run({}))
    ctx.check(f"kind is editable_checkout, got {k}", k["kind"] == "editable_checkout")
    ctx.check(f"spec is the checkout path, got {k['spec']!r}", Path(k["spec"]).resolve() == checkout.resolve())
    # Finding 3: never a bare "." -- `-C <checkout>` (and the reinstall's
    # own path argument) keeps the whole command tied to the real
    # checkout no matter what directory it ends up running from.
    ctx.check(f"reinstall_cmd is git -C <checkout> pull then the editable reinstall, got {k['reinstall_cmd']!r}",
              k["reinstall_cmd"] == f"git -C {k['spec']} pull --ff-only "
                                     f"&& uv tool install --reinstall --editable {k['spec']}")


@test
def test_install_kind_bare_checkout(ctx: Ctx):
    checkout = _tmp()
    patch = _patched("_checkout_root_on_pythonpath", lambda: checkout)
    try:
        k = upd.install_kind(direct_url=None, run_fn=_fake_run({}))
    finally:
        _restore(patch)
    ctx.check(f"kind is bare_checkout, got {k}", k["kind"] == "bare_checkout")
    ctx.check(f"reinstall_cmd pulls in the checkout, never a bare git pull, got {k['reinstall_cmd']!r}",
              k["reinstall_cmd"] == f"git -C {k['spec']} pull --ff-only")


@test
def test_install_kind_dir_checkout_non_editable_local_install(ctx: Ctx):
    """Finding 16: both installers' default (`uv tool install --reinstall
    .`, never --editable) records exactly this non-editable local
    `dir_info` shape. Must be recognized as a real checkout (so update
    actually pulls) whenever the local path is a real git repo -- never
    silently reinstall the frozen file:// snapshot from install time."""
    checkout = _tmp()
    (checkout / ".git").mkdir()
    d = {"url": f"file://{checkout.as_posix()}", "dir_info": {}}
    run_fn = _fake_run({"uv tool list": _Proc("some-other-tool v1\n"), "pipx list": _Proc("nothing here\n")})
    k = upd.install_kind(direct_url=d, run_fn=run_fn)
    ctx.check(f"kind is dir_checkout, got {k}", k["kind"] == "dir_checkout")
    ctx.check(f"spec is the checkout path, got {k['spec']!r}", Path(k["spec"]).resolve() == checkout.resolve())
    ctx.check(f"reinstall_cmd pulls first, then a NON-editable reinstall, got {k['reinstall_cmd']!r}",
              k["reinstall_cmd"] == f"git -C {k['spec']} pull --ff-only "
                                     f"&& {sys.executable} -m pip install --upgrade {k['spec']}")


@test
def test_install_kind_dir_info_without_a_real_git_repo_is_not_a_checkout(ctx: Ctx):
    """A plain (non-git) local directory install must NOT be claimed as a
    checkout kind -- there is nothing to `git pull` there."""
    checkout = _tmp()  # no .git inside
    d = {"url": f"file://{checkout.as_posix()}", "dir_info": {}}
    run_fn = _fake_run({"uv tool list": _Proc("some-other-tool v1\n"), "pipx list": _Proc("nothing here\n")})
    k = upd.install_kind(direct_url=d, run_fn=run_fn)
    ctx.check(f"kind is NOT dir_checkout, got {k}", k["kind"] != "dir_checkout")


@test
def test_install_kind_unknown_when_nothing_resolves(ctx: Ctx):
    patch = _patched("_checkout_root_on_pythonpath", lambda: None)
    try:
        k = upd.install_kind(direct_url=None, run_fn=_fake_run({}))
    finally:
        _restore(patch)
    ctx.check(f"kind is unknown, got {k}", k["kind"] == "unknown")
    ctx.check("reinstall_cmd is None", k["reinstall_cmd"] is None)


@test
def test_default_channel_tag_vs_branch(ctx: Ctx):
    ctx.check("a v-tag requested_revision -> stable",
              upd.default_channel({"requested_revision": "v2.0.3"}) == "stable")
    ctx.check("a branch name -> main", upd.default_channel({"requested_revision": "master"}) == "main")
    ctx.check("nothing at all -> main", upd.default_channel({}) == "main")


# ---------------------------------------------------------------------------
# latest_available / the 24h cache / commits_between
# ---------------------------------------------------------------------------

def _seed_cache(state_dir: Path, data: dict) -> None:
    (state_dir / "update-check.json").write_text(json.dumps(data), encoding="utf-8")


@test
def test_latest_available_fresh_cache_hit_never_shells_out_or_fetches(ctx: Ctx):
    state_dir = _tmp()
    _seed_cache(state_dir, {"main": {"channel": "main", "commit": "1111111", "ref": "master",
                                      "reason": None, "checked_at": time.time()}})
    def boom(*a, **k): raise AssertionError("must not touch the network on a fresh cache hit")
    r = upd.latest_available("main", state_dir=state_dir, run_fn=boom, fetch_json=boom)
    ctx.check(f"returns the cached commit, got {r}", r["commit"] == "1111111" and r["source"] == "cache")


@test
def test_latest_available_expired_cache_refetches_via_git_ls_remote_and_resaves(ctx: Ctx):
    state_dir = _tmp()
    _seed_cache(state_dir, {"main": {"channel": "main", "commit": "oldoldo", "ref": "master",
                                      "reason": None, "checked_at": time.time() - upd.CACHE_TTL_S - 10}})
    run_fn = _fake_run({"ls-remote": _Proc("2222222222222222222222222222222222222222\trefs/heads/master\n")})
    with _background_net_enabled():
        r = upd.latest_available("main", state_dir=state_dir, run_fn=run_fn, fetch_json=lambda *a, **k: None)
    ctx.check(f"refetched a NEW commit over git, got {r}", r["commit"] == "2222222" and r["source"] == "live")
    saved = json.loads((state_dir / "update-check.json").read_text(encoding="utf-8"))
    ctx.check(f"cache file now has the new commit, got {saved}", saved["main"]["commit"] == "2222222")


@test
def test_latest_available_stable_channel_picks_newest_semver_tag_ignoring_junk(ctx: Ctx):
    state_dir = _tmp()
    ls_remote = (
        "1111111111111111111111111111111111111111\trefs/heads/master\n"
        "2222222222222222222222222222222222222222\trefs/tags/v2.0.1\n"
        "3333333333333333333333333333333333333333\trefs/tags/v2.0.10\n"
        "4444444444444444444444444444444444444444\trefs/tags/v2.0.10^{}\n"
        "5555555555555555555555555555555555555555\trefs/tags/not-a-version\n"
    )
    run_fn = _fake_run({"ls-remote": _Proc(ls_remote)})
    with _background_net_enabled():
        r = upd.latest_available("stable", state_dir=state_dir, run_fn=run_fn, fetch_json=lambda *a, **k: None)
    ctx.check(f"v2.0.10 beats v2.0.1 (numeric, not lexicographic), got {r}", r["ref"] == "v2.0.10")
    # An annotated tag's `^{}` peel line (the real commit it points at, as
    # opposed to the tag OBJECT's own sha) comes second in real git
    # ls-remote output and correctly wins here -- "4444444", not "3333333".
    ctx.check(f"the dereferenced commit wins over the tag object's own sha, got {r}", r["commit"] == "4444444")


@test
def test_latest_available_falls_back_to_api_when_no_git(ctx: Ctx):
    state_dir = _tmp()
    old_which = upd.shutil.which
    upd.shutil.which = lambda name: None if name == "git" else old_which(name)
    try:
        def fetch_json(url, **kw):
            ctx.check(f"hits the commits API for main, got {url}", url.endswith("/commits/master"))
            return {"sha": "6666666666666666666666666666666666666666"}
        with _background_net_enabled():
            r = upd.latest_available("main", state_dir=state_dir, run_fn=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("git is 'missing' -- must not shell out")), fetch_json=fetch_json)
        ctx.check(f"commit from the API, got {r}", r["commit"] == "6666666")
    finally:
        upd.shutil.which = old_which


@test
def test_latest_available_refresh_true_ignores_a_fresh_cache(ctx: Ctx):
    state_dir = _tmp()
    _seed_cache(state_dir, {"main": {"channel": "main", "commit": "1111111", "ref": "master",
                                      "reason": None, "checked_at": time.time()}})
    run_fn = _fake_run({"ls-remote": _Proc("3333333333333333333333333333333333333333\trefs/heads/master\n")})
    with _background_net_enabled():
        r = upd.latest_available("main", state_dir=state_dir, refresh=True, run_fn=run_fn,
                                  fetch_json=lambda *a, **k: None)
    ctx.check(f"--refresh bypasses even a fresh cache, got {r}", r["commit"] == "3333333")


@test
def test_latest_available_update_check_false_prefers_cache_then_says_so(ctx: Ctx):
    state_dir = _tmp()
    old_state_dir_env = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)  # theme.get_config_value reads bridge_home()
    try:
        from halo_harness.theme import set_config_value
        set_config_value("update.check", False)
        def boom(*a, **k): raise AssertionError("update.check: false must never shell out or fetch")
        r = upd.latest_available("main", state_dir=state_dir, run_fn=boom, fetch_json=boom)
        ctx.check(f"commit is None, reason names the flag, got {r}",
                  r["commit"] is None and "update.check" in (r["reason"] or ""))
        _seed_cache(state_dir, {"main": {"channel": "main", "commit": "4444444", "ref": "master",
                                          "reason": None, "checked_at": time.time()}})
        r2 = upd.latest_available("main", state_dir=state_dir, run_fn=boom, fetch_json=boom)
        ctx.check(f"a cached value still wins over the config flag, got {r2}", r2["commit"] == "4444444")
    finally:
        if old_state_dir_env is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir_env


@test
def test_latest_available_background_net_disabled(ctx: Ctx):
    state_dir = _tmp()
    old = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
    try:
        def boom(*a, **k): raise AssertionError("background-net-disabled must never shell out or fetch")
        r = upd.latest_available("main", state_dir=state_dir, run_fn=boom, fetch_json=boom)
        ctx.check(f"reason names it, got {r}", r["commit"] is None and "background network" in (r["reason"] or ""))
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old


@test
def test_commits_between_local_checkout(ctx: Ctx):
    checkout = _tmp()
    run_fn = _fake_run({"git log --oneline": _Proc("bbbbbbb fix: two\naaaaaaa fix: one\n")})
    lines, count = upd.commits_between("ccccccc", "bbbbbbb", checkout=checkout, run_fn=run_fn)
    ctx.check(f"two one-liners, got {lines}", lines == ["bbbbbbb fix: two", "aaaaaaa fix: one"])
    ctx.check(f"count matches, got {count}", count == 2)


@test
def test_commits_between_api_fallback_when_no_checkout(ctx: Ctx):
    patch = _patched("_checkout_root_on_pythonpath", lambda: None)
    try:
        fetch_json = lambda url, **kw: {"ahead_by": 7}
        lines, count = upd.commits_between("ccccccc", "bbbbbbb", checkout=None,
                                            run_fn=_fake_run({}), fetch_json=fetch_json)
    finally:
        _restore(patch)
    ctx.check(f"no lines without a checkout, got {lines}", lines == [])
    ctx.check(f"count from the compare API, got {count}", count == 7)


@test
def test_note_due_today_once_per_calendar_day(ctx: Ctx):
    state_dir = _tmp()
    ctx.check("first call today is True", upd.note_due_today(state_dir) is True)
    ctx.check("a second call the same day is False", upd.note_due_today(state_dir) is False)


# ---------------------------------------------------------------------------
# other_halo_pids / relaunch_halo
# ---------------------------------------------------------------------------

@test
def test_other_halo_pids_excludes_self_and_filters_by_cmdline(ctx: Ctx):
    this_pid = os.getpid()
    other_pid = this_pid + 1
    if os.name == "nt":
        import json as _json
        rows = _json.dumps([
            {"ProcessId": this_pid, "ParentProcessId": 1, "CommandLine": "halo.exe --something (this very process)"},
            {"ProcessId": other_pid, "ParentProcessId": 1, "CommandLine": "C:\\tools\\halo.exe"},
            {"ProcessId": other_pid + 1, "ParentProcessId": 1, "CommandLine": "notepad.exe"},
        ])
        run_fn = _fake_run({"Win32_Process": _Proc(rows)})
    else:
        table = (f"  PID  PPID ARGS\n{this_pid} 1 halo --something\n{other_pid} 1 /usr/bin/halo\n"
                 f"{other_pid + 1} 1 /usr/bin/vim\n")
        run_fn = _fake_run({"ps -eo": _Proc(table)})
    pids = upd.other_halo_pids(run_fn=run_fn)
    ctx.check(f"this process excluded, got {pids}", this_pid not in pids)
    ctx.check(f"the other halo-looking pid is included, got {pids}", other_pid in pids)
    ctx.check(f"the unrelated pid is excluded, got {pids}", (other_pid + 1) not in pids)


@test
def test_other_halo_pids_excludes_this_process_whole_ancestor_chain(ctx: Ctx):
    """Finding 2 (critical): on Windows, a uv-tool/pipx/pip console-script
    install's own launcher stub (`~/.local/bin/halo.exe`) stays alive as
    the PARENT of the real `python.exe` that runs halo -- its command
    line also names `halo.exe` (it IS one), so excluding only
    `os.getpid()` always counted it as "another" halo process and every
    real `halo update`/`/update` on Windows refused forever. Same shape
    proven on POSIX via `ppid`."""
    this_pid = os.getpid()
    launcher_pid = this_pid + 1000  # this process's OWN parent -- never "another" process
    genuinely_other_pid = this_pid + 2000  # a REAL separate halo invocation elsewhere
    if os.name == "nt":
        import json as _json
        rows = _json.dumps([
            {"ProcessId": launcher_pid, "ParentProcessId": 1, "CommandLine": "C:\\Users\\x\\.local\\bin\\halo.exe"},
            {"ProcessId": this_pid, "ParentProcessId": launcher_pid,
             "CommandLine": "C:\\...\\python.exe C:\\...\\halo.exe --version"},
            {"ProcessId": genuinely_other_pid, "ParentProcessId": 1, "CommandLine": "C:\\tools\\halo.exe"},
        ])
        run_fn = _fake_run({"Win32_Process": _Proc(rows)})
    else:
        table = (f"  PID  PPID ARGS\n{launcher_pid} 1 /usr/bin/halo\n"
                 f"{this_pid} {launcher_pid} /usr/bin/python3 /usr/bin/halo --version\n"
                 f"{genuinely_other_pid} 1 /usr/bin/halo\n")
        run_fn = _fake_run({"ps -eo": _Proc(table)})
    pids = upd.other_halo_pids(run_fn=run_fn)
    ctx.check(f"this process's own launcher PARENT is excluded, got {pids}", launcher_pid not in pids)
    ctx.check(f"this process itself is excluded, got {pids}", this_pid not in pids)
    ctx.check(f"a genuinely separate halo process is still caught, got {pids}", genuinely_other_pid in pids)


@test
def test_is_halo_cmdline_rejects_substring_false_positives(ctx: Ctx):
    """Finding 2: the old `"halo" in cmdline.lower()` also matched an
    editor window titled after this repo and a `tail -f ~/.halo/...` --
    neither names a `halo`/`halo.exe` TOKEN."""
    ctx.check("an editor open on the repo folder is NOT halo",
              not upd._is_halo_cmdline("C:\\Users\\x\\AppData\\Local\\Programs\\Code.exe "
                                        "C:\\Users\\x\\Documents\\Halo-Harness"))
    ctx.check("tailing halo's own log is NOT halo", not upd._is_halo_cmdline("tail -f ~/.halo/bridge.log"))
    ctx.check("an unrelated process is NOT halo", not upd._is_halo_cmdline("/usr/bin/vim notes.txt"))
    ctx.check("a bare halo invocation IS halo", upd._is_halo_cmdline("/usr/bin/halo --continue"))
    ctx.check("a -m halo_harness invocation IS halo",
              upd._is_halo_cmdline("/usr/bin/python3 -m halo_harness --continue"))


@test
def test_relaunch_halo_calls_exec_fn_with_continue_and_never_really_execs(ctx: Ctx):
    calls = []
    code = upd.relaunch_halo(["--continue", "extra"], exec_fn=lambda path, argv: calls.append((path, argv)))
    ctx.check(f"exec_fn was called exactly once, got {calls}", len(calls) == 1)
    ctx.check(f"--continue is in the argv, got {calls[0][1]}", "--continue" in calls[0][1])
    ctx.check(f"the fake exec_fn's own return makes relaunch_halo return too, got {code}", code == 0)


@test
def test_relaunch_halo_on_windows_waits_for_a_real_child_instead_of_execv(ctx: Ctx):
    """2.0.2 review finding 30 pin: `os.execv` on Windows exits the uv
    launcher parent along with this process, handing the console back to
    the shell while the relaunched TUI races it for the same console.
    With no `exec_fn` given (the real default path) on this win32 test
    box, `relaunch_halo` must go through `call_fn` (`subprocess.call` by
    default) and `sys.exit` with its return code -- never `os.execv`."""
    if sys.platform != "win32":
        raise SkipTest("this pins the Windows-only default path")
    calls = []

    def fake_call(argv):
        calls.append(argv)
        return 7
    try:
        upd.relaunch_halo(["--continue"], call_fn=fake_call)
        ctx.check("relaunch_halo must not return normally here", False)
    except SystemExit as e:
        ctx.check(f"call_fn was used (never os.execv), got {calls}", len(calls) == 1 and "--continue" in calls[0])
        ctx.check(f"sys.exit carried call_fn's own return code, got {e.code}", e.code == 7)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
