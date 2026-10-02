"""tests.test_doctor_prescriptive_fixes -- H12 brief Part B: every doctor
`[WARN]`/`[MISSING]` line ends with `-> fix: <command>` or `-> see: <ref>`,
`doctor --json`/`run_checks_structured` expose the same checks as records,
and the new checks (`~/.local/bin` on PATH, tmux mouse mode, MCP servers,
the default model in config.json). Isolated BRIDGE_TEST_HOME throughout;
BRIDGE_TEST_CC_AUTH_STATUS defaults to "not logged in" so the Claude-
subscription check (which every one of these runs touches) never spawns a
real `claude auth status`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="halo-doctor-fix-"))


def _run(argv, home: Path, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR),
                "BRIDGE_TEST_CC_AUTH_STATUS": json.dumps({"loggedIn": False})})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _isolated(fn):
    """Run `fn()` with BRIDGE_TEST_HOME pointed at a fresh temp home and a
    definitive (not-logged-in) BRIDGE_TEST_CC_AUTH_STATUS, restoring both
    env vars afterward."""
    home = _fresh_home()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_auth = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
    try:
        return fn(home)
    finally:
        for var, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_TEST_CC_AUTH_STATUS", old_auth)):
            if old is not None:
                os.environ[var] = old
            else:
                os.environ.pop(var, None)


# ---------------------------------------------------------------------------
# Every WARN/MISSING line carries a fix or see
# ---------------------------------------------------------------------------

def _missing_fix_lines(lines: list) -> list:
    from halo_harness.doctor import MISSING, WARN
    return [l for l in lines if (l.startswith(WARN) or l.startswith(MISSING))
            and "-> fix:" not in l and "-> see:" not in l]


@test
def test_every_warn_or_missing_line_in_run_checks_has_a_fix_or_see(ctx: Ctx):
    from halo_harness.doctor import run_checks
    lines, ok = _isolated(lambda home: run_checks(cwd=home))
    warn_or_missing = [l for l in lines if l.startswith("[WARN]") or l.startswith("[MISSING]")]
    ctx.check(f"a fresh/unconfigured home actually triggers several WARN/MISSING lines, got "
              f"{len(warn_or_missing)}", len(warn_or_missing) >= 8)
    bad = _missing_fix_lines(lines)
    ctx.check(f"every WARN/MISSING line ends with -> fix: or -> see:, offenders={bad}", not bad)
    ctx.check("ok is False (something came back MISSING)", ok is False)


@test
def test_every_warn_or_missing_line_in_run_work_checks_has_a_fix_or_see(ctx: Ctx):
    from halo_harness.doctor import run_work_checks
    lines, ok = _isolated(lambda home: run_work_checks())
    bad = _missing_fix_lines(lines)
    ctx.check(f"every --work WARN/MISSING line ends with -> fix: or -> see:, offenders={bad}", not bad)
    ctx.check("not ok (nothing configured)", ok is False)


@test
def test_real_doctor_cli_shows_a_fix_on_every_warn(ctx: Ctx):
    home = _fresh_home()
    result = _run(["doctor"], home)
    lines = result.stdout.splitlines()
    warn_or_missing = [l for l in lines if "[WARN]" in l or "[MISSING]" in l]
    ctx.check(f"the real CLI also triggers several WARN/MISSING lines, got {len(warn_or_missing)}",
              len(warn_or_missing) >= 5)
    bad = [l for l in warn_or_missing if "-> fix:" not in l and "-> see:" not in l]
    ctx.check(f"every one names its own fix, offenders={bad}", not bad)


# ---------------------------------------------------------------------------
# doctor --json / run_checks_structured
# ---------------------------------------------------------------------------

@test
def test_run_checks_structured_shape(ctx: Ctx):
    from halo_harness.doctor import run_checks, run_checks_structured
    checks, ok = _isolated(lambda home: run_checks_structured(cwd=home))
    lines, ok2 = _isolated(lambda home: run_checks(cwd=home))
    ctx.check(f"same count as the plain lines list, got {len(checks)} vs {len(lines)}", len(checks) == len(lines))
    ctx.check("ok matches run_checks' own ok", ok == ok2)
    for c in checks:
        ctx.check(f"every entry has the 5 expected keys, got {sorted(c)}",
                  set(c) == {"id", "status", "message", "fix", "see"})
        ctx.check(f"status is one of ok/warn/missing/info, got {c['status']!r}",
                  c["status"] in ("ok", "warn", "missing", "info"))
        ctx.check(f"id is a non-empty string, got {c['id']!r}", isinstance(c["id"], str) and c["id"])
        if c["status"] in ("warn", "missing"):
            ctx.check(f"a warn/missing entry has fix or see, got {c}", c["fix"] or c["see"])
        else:
            ctx.check(f"an ok/info entry has neither, got {c}", not c["fix"] and not c["see"])
    ids = [c["id"] for c in checks]
    ctx.check(f"ids are unique, got {ids}", len(ids) == len(set(ids)))


@test
def test_doctor_json_cli_round_trips(ctx: Ctx):
    home = _fresh_home()
    result = _run(["doctor", "--json"], home)
    ctx.check(f"exit 0 or 1, got {result.returncode}", result.returncode in (0, 1))
    data = json.loads(result.stdout)
    ctx.check(f"a real JSON list, got type {type(data)}", isinstance(data, list) and data)
    ctx.check("every id is present in the parsed output",
              {"env_file", "openrouter", "databricks", "default_model"} <= {c["id"] for c in data})


@test
def test_doctor_work_json_cli_round_trips(ctx: Ctx):
    home = _fresh_home()
    result = _run(["doctor", "--work", "--json"], home)
    data = json.loads(result.stdout)
    ctx.check(f"a real JSON list, got {data!r}", isinstance(data, list) and data)
    ctx.check("databricks_config id present", any(c["id"] == "databricks_config" for c in data))


# ---------------------------------------------------------------------------
# New checks
# ---------------------------------------------------------------------------

@test
def test_local_bin_on_path_check_is_windows_aware(ctx: Ctx):
    from halo_harness import doctor
    if sys.platform == "win32":
        ctx.check("skipped entirely on win32 (no such PATH concept there)",
                  doctor._check_local_bin_on_path() is None)
        return
    line = doctor._check_local_bin_on_path()
    ctx.check(f"a real line on POSIX, got {line!r}", isinstance(line, str))


@test
def test_ensure_local_bin_on_rc_recognizes_the_legacy_1_0_1_marker(ctx: Ctx):
    """2.0.0 fixpass finding 6: PATH_MARKER changed from `# rolo-claude: put
    ~/.local/bin on PATH ...` to `# halo: put ~/.local/bin on PATH ...` --
    a line 1.0.1's `rolo-claude init` already appended must still be
    recognized as "already done", or a 2.0.0 `halo init` re-run appends a
    SECOND PATH block right below the first one."""
    from halo_harness.linux_fixes import PATH_LINE, PATH_MARKER, PATH_MARKER_LEGACY, ensure_local_bin_on_rc

    tmp = Path(tempfile.mkdtemp(prefix="halo-linux-fixes-legacy-"))
    home = tmp / "home"
    home.mkdir()
    rc = home / ".profile"
    rc.write_text(f"# something the user already had\n{PATH_MARKER_LEGACY}\n{PATH_LINE}\n", encoding="utf-8")

    written, path = ensure_local_bin_on_rc(shell="/bin/bash", home=home)
    ctx.check(f"a no-op -- already done under the OLD marker, got written={written}", written is False)
    ctx.check("the file is completely untouched", rc.read_text(encoding="utf-8").count(PATH_LINE) == 1)
    ctx.check("no SECOND (new-marker) PATH block was appended", PATH_MARKER not in rc.read_text(encoding="utf-8"))


@test
def test_state_dir_migration_check_warns_only_when_both_exist_nonempty(ctx: Ctx):
    """2.0.0 fixpass finding 1: doctor renders a WARN naming both paths
    when ~/.rolo-claude and ~/.halo both hold real data (the rename
    half-succeeded, most commonly a locked file on Windows); silent in
    every other case, sharing the ONE detection bridge_home() itself uses
    for its own once-per-process stderr warning."""
    from halo_harness import doctor

    ctx.check("a fresh home with neither dir -> no line at all",
              _isolated(lambda home: doctor._check_state_dir_migration()) is None)

    def _both_exist(home):
        old_dir, new_dir = home / ".rolo-claude", home / ".halo"
        old_dir.mkdir(parents=True, exist_ok=True)
        new_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "leftover.txt").write_text("x", encoding="utf-8")
        return doctor._check_state_dir_migration()

    line = _isolated(_both_exist)
    ctx.check(f"a WARN naming both paths, got {line!r}", line is not None and line.startswith(doctor.WARN))
    ctx.check("names .rolo-claude", ".rolo-claude" in line)
    ctx.check("names .halo", ".halo" in line)
    ctx.check("carries a fix/see suffix", "-> fix:" in line or "-> see:" in line)


@test
def test_tmux_mouse_check_skipped_outside_tmux_and_fix_when_off(ctx: Ctx):
    from halo_harness import doctor
    old_tmux = os.environ.pop("TMUX", None)
    try:
        ctx.check("no $TMUX -> the check doesn't even appear", doctor._check_tmux_mouse() is None)
        os.environ["TMUX"] = "/tmp/tmux-1000/default,1234,0"
        line = doctor._check_tmux_mouse()
        ctx.check(f"with $TMUX set, a real line comes back, got {line!r}", isinstance(line, str))
        if line.startswith(doctor.WARN):
            ctx.check("a WARN names the fix", "-> fix:" in line)
    finally:
        if old_tmux is not None:
            os.environ["TMUX"] = old_tmux
        else:
            os.environ.pop("TMUX", None)


@test
def test_tmux_mouse_check_reports_ok_when_tmux_says_on(ctx: Ctx):
    """A fake `tmux show -g mouse` (never a real tmux dependency in CI)."""
    from halo_harness import doctor
    old_tmux = os.environ.get("TMUX")
    old_run = doctor.subprocess.run
    os.environ["TMUX"] = "/tmp/tmux-1000/default,1234,0"

    class _FakeProc:
        stdout = "mouse on\n"

    def _fake_run(argv, **kwargs):
        ctx.check(f"calls tmux show -g mouse, got {argv}", argv == ["tmux", "show", "-g", "mouse"])
        return _FakeProc()

    doctor.subprocess.run = _fake_run
    try:
        line = doctor._check_tmux_mouse()
        ctx.check(f"reports OK, got {line!r}", line.startswith(doctor.OK))
    finally:
        doctor.subprocess.run = old_run
        if old_tmux is not None:
            os.environ["TMUX"] = old_tmux
        else:
            os.environ.pop("TMUX", None)


@test
def test_mcp_servers_check_none_configured_and_enumeration_failure(ctx: Ctx):
    from halo_harness import doctor

    def _check(home):
        return doctor._check_mcp_servers(home)

    line = _isolated(_check)
    # W4b "explain the zero": the zero case now also names every scope
    # searched (never a bare 0) -- see test_mcp_explain.py for the
    # scope-breakdown's own pinning tests; this one just checks doctor
    # still reports OK/"none configured" and doesn't lose the explanation.
    ctx.check(f"a fresh home with nothing configured -> OK none, got {line!r}",
              line.startswith(f"{doctor.OK} MCP servers: none configured"))
    ctx.check(f"names what was searched, got {line!r}", "Searched --" in line)

    import halo_harness.mcp.manager as mcp_manager_mod
    old_fn = mcp_manager_mod.resolve_server_configs

    def _boom(**kwargs):
        raise RuntimeError("boom")

    def _check_after_boom(home):
        mcp_manager_mod.resolve_server_configs = _boom
        try:
            return doctor._check_mcp_servers(home)
        finally:
            mcp_manager_mod.resolve_server_configs = old_fn

    bad_line = _isolated(_check_after_boom)
    ctx.check(f"a real enumeration failure is a WARN with a fix, got {bad_line!r}",
              bad_line.startswith(doctor.WARN) and "-> fix:" in bad_line)


@test
def test_mcp_servers_check_reports_eager_vs_lazy(ctx: Ctx):
    # H13 Part A: lazy is now the DEFAULT -- a server needs an EXPLICIT
    # "mcpLazy": false to count as eager; one with no key at all is lazy.
    from halo_harness import doctor
    home = _fresh_home()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "eager-one": {"type": "stdio", "command": "true", "mcpLazy": False},
            "lazy-one": {"type": "stdio", "command": "true"},
        },
    }), encoding="utf-8")
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        line = doctor._check_mcp_servers(home)
        ctx.check(f"reports 2 configured (1 eager, 1 lazy), got {line!r}",
                  "2 configured" in line and "1 eager" in line and "1 lazy" in line)
        ctx.check("names the mcpLazy lever since something is eager", "mcpLazy" in line)
        ctx.check("never a WARN just for having eager servers (no timing data to justify one)",
                  line.startswith(doctor.OK))
    finally:
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_mcp_servers_check_lazy_is_now_the_default_with_no_mcplazy_key(ctx: Ctx):
    """H13 Part A acceptance: a plain server entry with NO "mcpLazy" key at
    all is now lazy by default (the pre-H13 default was eager)."""
    from halo_harness import doctor
    home = _fresh_home()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"plain-one": {"type": "stdio", "command": "true"}},
    }), encoding="utf-8")
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        line = doctor._check_mcp_servers(home)
        ctx.check(f"the one server with no mcpLazy key is lazy by default, got {line!r}",
                  "1 configured" in line and "0 eager" in line and "1 lazy" in line)
    finally:
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_default_model_check_unset_configured_and_misconfigured(ctx: Ctx):
    from halo_harness import doctor
    from halo_harness.theme import set_config_value

    def _unset(home):
        return doctor._check_default_model()

    line = _isolated(_unset)
    ctx.check(f"not set -> OK with the built-in default named, got {line!r}",
              line.startswith(doctor.OK) and "deepseek-v4.1-flash" in line)

    def _configured(home):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        set_config_value("model", "or:deepseek/deepseek-v3.2")
        return doctor._check_default_model()

    line2 = _isolated(_configured)
    os.environ.pop("OPENROUTER_API_KEY", None)
    ctx.check(f"set + provider configured -> OK, got {line2!r}", line2.startswith(doctor.OK))

    def _unconfigured_provider(home):
        set_config_value("model", "dbx:databricks-deepseek-v4-1-flash")
        return doctor._check_default_model()

    line3 = _isolated(_unconfigured_provider)
    # 1.0.1 hotfix 13 (drive-by): this fix suggestion moved from the
    # deprecated `--preset work` to `--provider databricks` along with
    # every other user-facing spot init's own provider-first rewrite
    # touched -- `--preset work` still works (as a deprecated alias), but
    # nothing prescribes it any more.
    ctx.check(f"set but Databricks not configured -> WARN with a --provider fix, got {line3!r}",
              line3.startswith(doctor.WARN) and "halo init --provider databricks" in line3)

    def _garbage(home):
        set_config_value("model", 12345)
        return doctor._check_default_model()

    line4 = _isolated(_garbage)
    ctx.check(f"a non-string model value is treated as unset, never a crash, got {line4!r}",
              line4.startswith(doctor.OK))


# ---------------------------------------------------------------------------
# 2.0.1 W3a: doctor pins the tested Claude Code version range
# (providers/cc_tested.json) and WARNs when the installed claude is newer.
# ---------------------------------------------------------------------------

@test
def test_claude_subscription_warns_when_installed_claude_is_newer_than_tested(ctx: Ctx):
    import halo_harness.doctor as doctor_mod
    from halo_harness.providers.cc_models import load_cc_tested_range

    tested = load_cc_tested_range()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_auth = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_HOME"] = str(_fresh_home())
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
    real_version = doctor_mod._claude_version
    doctor_mod._claude_version = lambda: "99.0.0"  # newer than any real tested max
    try:
        line = doctor_mod._check_claude_subscription()
        ctx.check(f"WARN (never OK) for a too-new install, got {line!r}", line.startswith(doctor_mod.WARN))
        ctx.check(f"names 'newer than the tested range', got {line!r}", "newer than the tested range" in line)
        ctx.check(f"names the tested max, got {line!r}", str(tested.get("max")) in line)
    finally:
        doctor_mod._claude_version = real_version
        for var, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_TEST_CC_AUTH_STATUS", old_auth)):
            if old is not None:
                os.environ[var] = old
            else:
                os.environ.pop(var, None)


@test
def test_claude_subscription_ok_when_installed_claude_is_within_tested_range(ctx: Ctx):
    import halo_harness.doctor as doctor_mod
    from halo_harness.providers.cc_models import load_cc_tested_range

    tested = load_cc_tested_range()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_auth = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_HOME"] = str(_fresh_home())
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
    real_version = doctor_mod._claude_version
    doctor_mod._claude_version = lambda: tested.get("max")
    try:
        line = doctor_mod._check_claude_subscription()
        ctx.check(f"OK for a version exactly at the tested max, got {line!r}", line.startswith(doctor_mod.OK))
    finally:
        doctor_mod._claude_version = real_version
        for var, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_TEST_CC_AUTH_STATUS", old_auth)):
            if old is not None:
                os.environ[var] = old
            else:
                os.environ.pop(var, None)


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
