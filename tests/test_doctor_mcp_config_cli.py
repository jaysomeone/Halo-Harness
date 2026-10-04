"""tests.test_doctor_mcp_config_cli -- halo_harness/{doctor,mcp_cli,
config_cli}.py (U0 scope A): the `doctor`/`mcp`/`config` subcommands, all
run against an isolated BRIDGE_TEST_HOME so a test run never touches the
real machine's `~/.claude.json` or `~/.halo/config.json`.
"""
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
    return Path(tempfile.mkdtemp(prefix="halo-subcmd-"))


def _run(argv, home: Path, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    # H3: `cli.py`'s `_make_streams_utf8_safe()` deliberately reconfigures
    # the CHILD's stdout/stderr to UTF-8 (a model's reply, or an MCP
    # status glyph like the ✔/✗/⏸ this milestone's `mcp
    # list` prints, may not exist in a legacy Windows console codepage) --
    # decoding the captured bytes with anything other than UTF-8 here
    # (the bare `text=True` default is the LOCALE's preferred encoding,
    # cp1252 on this host) turns every such character into mojibake
    # (verified: "✔" -> "âœ”").
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


@test
def test_doctor_runs_and_exits_cleanly(ctx: Ctx):
    home = _fresh_home()
    result = _run(["doctor"], home)
    ctx.check(f"exit 0 or 1 (never a crash), got {result.returncode}", result.returncode in (0, 1))
    ctx.check("mentions Python version check", "Python" in result.stdout)
    ctx.check("no traceback", "Traceback" not in result.stderr)


@test
def test_h8_doctor_reports_catalog_ages(ctx: Ctx):
    """H8 scope C: doctor shows the age of every cached catalog file -- a
    fresh, never-refreshed BRIDGE_TEST_HOME reports each one as never
    cached (never a crash, never silently omitted)."""
    from halo_harness.doctor import run_checks
    home = _fresh_home()
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        lines, _ok = run_checks()
        catalog_lines = [l for l in lines if "models.json" in l or "dbx-endpoints.json" in l or "models-dev" in l
                          or "models.dev" in l]
        ctx.check(f"at least the 3 catalog files are mentioned, got {catalog_lines}", len(catalog_lines) >= 3)
        ctx.check("a fresh home reports them as never cached (not a crash/omission)",
                  all("never cached" in l or "never refreshed" in l for l in catalog_lines))
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_h8_doctor_work_offline_reports_unreachable_with_vpn_hint(ctx: Ctx):
    """H8 scope F acceptance: `halo doctor --work` with no Databricks
    configured (this build/test box) reports it plainly, never crashes, and
    the VPN hint is present somewhere in the output for when it IS
    configured but genuinely unreachable."""
    home = _fresh_home()
    result = _run(["doctor", "--work"], home)
    ctx.check(f"exit 0 or 1 (never a crash), got {result.returncode}, stderr={result.stderr!r}",
              result.returncode in (0, 1))
    ctx.check("no traceback", "Traceback" not in result.stderr)
    ctx.check(f"reports Databricks as not configured, got {result.stdout!r}",
              "not configured" in result.stdout.lower())
    ctx.check("mentions the open questions from the plan", "open question" in result.stdout.lower())

    from halo_harness.doctor import run_work_checks
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        lines, ok = run_work_checks()
        ctx.check("run_work_checks() also never raises and reports not-configured", not ok)
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_h8_doctor_work_vpn_hint_when_configured_but_unreachable(ctx: Ctx):
    """The VPN hint specifically: a Databricks host that's syntactically
    configured but not actually reachable (a bogus hostname) must mention
    the VPN in its own reachability line."""
    from halo_harness.doctor import _work_check_vpn_reachability
    line = _work_check_vpn_reachability("https://this-host-does-not-exist.invalid.example")
    ctx.check(f"reports unreachable, got {line!r}", "MISSING" in line or "[MISSING]" in line)
    ctx.check("names the VPN as the likely reason", "VPN" in line)


@test
def test_h5b_u5_doctor_reports_the_clipboard_backend(ctx: Ctx):
    """U5's own leftover / H8 cheap must-do: tui/clipboard.py's
    `clipboard_doctor_line()` was written ready-to-call but never actually
    wired into a real `doctor` run -- `run_checks()` must now include it."""
    from halo_harness.doctor import run_checks
    lines, _ok = run_checks()
    ctx.check(f"a clipboard backend line is present, got {lines}",
              any("Clipboard backend" in line for line in lines))
    clipboard_lines = [l for l in lines if "Clipboard backend" in l]
    ctx.check(f"names at least one real backend (OSC 52/xclip/wl-copy/xsel/pbcopy/win32), got {clipboard_lines}",
              any(any(name in l for name in ("OSC 52", "xclip", "wl-copy", "xsel", "pbcopy", "win32"))
                  for l in clipboard_lines))

    # Also exercised through the real `doctor` CLI subprocess.
    home = _fresh_home()
    result = _run(["doctor"], home)
    ctx.check(f"the real doctor CLI also mentions the clipboard backend, got exit={result.returncode}",
              "Clipboard backend" in result.stdout)


@test
def test_doctor_lists_probable_test_leftovers_in_mcp_log_dir(ctx: Ctx):
    """Test hygiene (round B fix pass, notes file): the REAL `~/.halo/
    mcp/` on the build host held `a.log`/`b.log`/`c.log`/`big.log`/
    `crash.log`/`eager1.log` (older standalone-test leftovers) and
    `fake.log`/`fake.log.1`/a `plugin_*fakeserver.log` -- `halo doctor`
    now lists any of these as probable leftovers, safe to delete, never
    as a MISSING (nothing is actually broken)."""
    from halo_harness.doctor import WARN, _check_test_leftovers
    state_dir = Path(tempfile.mkdtemp(prefix="doctor-leftovers-"))
    ctx.check("no mcp dir at all -> no entry", _check_test_leftovers(state_dir) is None)

    mcp_dir = state_dir / "mcp"
    mcp_dir.mkdir()
    (mcp_dir / "my-real-server.log").write_text("real", encoding="utf-8")
    ctx.check("only a real-looking log -> still no entry", _check_test_leftovers(state_dir) is None)

    for name in ("a.log", "crash.log", "fake.log", "fake.log.1", "plugin_demo_fakeserver.log"):
        (mcp_dir / name).write_text("x", encoding="utf-8")
    line = _check_test_leftovers(state_dir)
    ctx.check(f"a WARN naming the matched files, got {line!r}", line is not None and line.startswith(WARN))
    for name in ("a.log", "crash.log", "fake.log", "fake.log.1", "plugin_demo_fakeserver.log"):
        ctx.check(f"{name} is named, got {line!r}", name in line)
    ctx.check(f"the real-looking log is NOT flagged, got {line!r}", "my-real-server.log" not in line)
    ctx.check(f"informational wording, safe-to-delete, got {line!r}", "safe to delete" in line)


@test
def test_h9_doctor_reports_ripgrep_editor_and_shell(ctx: Ctx):
    """H9 OpenCode item 23: doctor must ALSO check for `rg`, `$VISUAL`/
    `$EDITOR` and a usable Bash shell (Git Bash on win32, `/bin/bash` on
    POSIX) -- xclip/wl-copy (clipboard), `claude` (--chrome) and npx
    (--playwright) were already covered before this milestone."""
    from halo_harness.doctor import run_checks, _check_ripgrep, _check_editor, _check_shell
    lines, _ok = run_checks()
    ctx.check(f"an rg/ripgrep line is present, got {lines}", any("rg" in l and "ripgrep" in l for l in lines))
    ctx.check(f"a $VISUAL/$EDITOR line is present, got {lines}", any("$VISUAL/$EDITOR" in l for l in lines))
    ctx.check(f"a shell (bash) line is present, got {lines}",
              any(("Git Bash" in l or " bash " in l or l.strip().endswith("bash") or "bash not found" in l)
                  for l in lines))

    # Unit-level: rg/EDITOR degrade to WARN (never MISSING -- both are
    # optional conveniences, not requirements), never crash either way.
    old_path, old_editor, old_visual = os.environ.get("PATH"), os.environ.pop("EDITOR", None), os.environ.pop("VISUAL", None)
    try:
        os.environ["PATH"] = str(_fresh_home())  # a directory with nothing on it -- rg guaranteed absent
        rg_line = _check_ripgrep()
        ctx.check(f"rg absent -> WARN not MISSING, got {rg_line!r}", rg_line.startswith("[WARN]"))
        editor_line = _check_editor()
        ctx.check(f"$EDITOR unset -> WARN not MISSING, got {editor_line!r}", editor_line.startswith("[WARN]"))
    finally:
        if old_path is not None:
            os.environ["PATH"] = old_path
        if old_editor is not None:
            os.environ["EDITOR"] = old_editor
        if old_visual is not None:
            os.environ["VISUAL"] = old_visual

    # A real, findable shell IS present on every box this suite runs on
    # (Windows has Git Bash per the plan's binary facts; Linux/WSL/Kali all
    # have /bin/bash) -- this must be OK here, never MISSING/a crash.
    shell_line = _check_shell()
    ctx.check(f"a real shell is found on this test box, got {shell_line!r}", shell_line.startswith("[OK]"))

    # The real doctor CLI (subprocess) also surfaces all three.
    home = _fresh_home()
    result = _run(["doctor"], home)
    ctx.check("real doctor CLI mentions ripgrep", "ripgrep" in result.stdout.lower())
    ctx.check("real doctor CLI mentions $VISUAL/$EDITOR", "$VISUAL/$EDITOR" in result.stdout)


@test
def test_doctor_reports_term_program_and_the_vscode_settings(ctx: Ctx):
    """Halo 2.0.2 round C (the owner's own macOS report, "ctrl+e or
    command+e does not work on the mac using halo"): `doctor` always
    prints TERM_PROGRAM/TERM, and names BOTH real VS Code remedies
    verbatim specifically when TERM_PROGRAM is vscode -- so a user can
    paste either straight into settings.json without hunting for the
    exact wording."""
    from halo_harness.doctor import _check_terminal_program

    old_term_program, old_term = os.environ.get("TERM_PROGRAM"), os.environ.get("TERM")
    try:
        os.environ.pop("TERM_PROGRAM", None)
        line = _check_terminal_program()
        ctx.check(f"unset TERM_PROGRAM is still reported plainly, got {line!r}",
                  line.startswith("[OK]") and "TERM_PROGRAM=(not set)" in line)

        os.environ["TERM_PROGRAM"] = "iTerm.app"
        line = _check_terminal_program()
        ctx.check(f"a non-vscode terminal is reported plainly, no settings.json hint, got {line!r}",
                  "TERM_PROGRAM=iTerm.app" in line and "settings.json" not in line)

        os.environ["TERM_PROGRAM"] = "vscode"
        line = _check_terminal_program()
        ctx.check(f"vscode is detected, got {line!r}", "TERM_PROGRAM=vscode" in line)
        ctx.check('the sendKeybindingsToShell remedy is named verbatim, got {line!r}'.format(line=line),
                  '"terminal.integrated.sendKeybindingsToShell": true' in line)
        ctx.check('the commandsToSkipShell remedy is named verbatim, got {line!r}'.format(line=line),
                  '"terminal.integrated.commandsToSkipShell": ["-<command owning ctrl+e>"]' in line)

        # The real doctor CLI (subprocess) surfaces it too -- `_run`'s own
        # `_hermetic_child_env()` forwards the parent's current os.environ
        # (minus BRIDGE_STATE_DIR/HALO_*), so TERM_PROGRAM=vscode (set
        # just above, still in effect here) reaches the child as-is.
        home = _fresh_home()
        result = _run(["doctor"], home)
        ctx.check("real doctor CLI shows the vscode hint", "sendKeybindingsToShell" in result.stdout)
    finally:
        if old_term_program is not None:
            os.environ["TERM_PROGRAM"] = old_term_program
        else:
            os.environ.pop("TERM_PROGRAM", None)
        if old_term is not None:
            os.environ["TERM"] = old_term


@test
def test_mcp_list_no_servers(ctx: Ctx):
    """W4b "explain the zero": a truly empty result now also names every
    scope searched (never a bare 0) -- see test_mcp_explain.py for the
    scope-breakdown's own pinning tests."""
    home = _fresh_home()
    result = _run(["mcp", "list"], home)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("reports no servers configured in this directory",
              "No MCP servers configured in this directory" in result.stdout)
    ctx.check("explains what was searched", "Searched --" in result.stdout)


@test
def test_mcp_list_reads_real_config(ctx: Ctx):
    """H3: `mcp list` now does a REAL health check (not the H2-era 'not
    checked' stub) -- a genuinely-working fake stdio server connects, a
    broken one honestly fails, and either way the line format matches
    binary-facts sec.9 (`${name}: ${command} ${args} - ${status}`)."""
    home = _fresh_home()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "demo-server": {"type": "stdio", "command": sys.executable,
                             "args": ["-m", "tests.helpers.fake_mcp_server"]},
            "broken-server": {"type": "stdio", "command": sys.executable,
                               "args": ["-m", "tests.helpers.fake_mcp_server", "crash"]},
        },
    }), encoding="utf-8")
    result = _run(["mcp", "list"], home, timeout=60)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("prints the health-check header", "Checking MCP server health" in result.stdout)
    ctx.check(f"working server line, got {result.stdout!r}",
              f"demo-server: {sys.executable} -m tests.helpers.fake_mcp_server - ✔ Connected" in result.stdout)
    ctx.check("broken server honestly reports a failure, never a crash",
              "broken-server:" in result.stdout and "✗ Failed to connect" in result.stdout)
    ctx.check("no traceback", "Traceback" not in result.stderr)


@test
def test_mcp_add_get_list_remove_round_trip(ctx: Ctx):
    """H3: `add`/`get`/`remove` are real now -- a read-modify-write of
    `~/.claude.json` that preserves unrelated keys, plus `mcp list`/`mcp
    get` seeing the newly-added (real, connectable) server afterward."""
    home = _fresh_home()
    (home / ".claude.json").write_text(json.dumps({"unrelatedKey": {"nested": [1, 2, 3]}}), encoding="utf-8")

    add_result = _run(["mcp", "add", "-s", "user", "fake", sys.executable,
                        "-m", "tests.helpers.fake_mcp_server"], home)
    ctx.check(f"mcp add: exit 0, got {add_result.returncode}, stderr={add_result.stderr!r}", add_result.returncode == 0)

    claude_json = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    ctx.check("unrelated key preserved", claude_json.get("unrelatedKey") == {"nested": [1, 2, 3]})
    ctx.check("new server present under mcpServers (user scope)",
              claude_json.get("mcpServers", {}).get("fake", {}).get("command") == sys.executable)

    get_result = _run(["mcp", "get", "fake"], home, timeout=60)
    ctx.check(f"mcp get: exit 0, got {get_result.returncode}", get_result.returncode == 0)
    ctx.check("mcp get shows it connected", "✔ Connected" in get_result.stdout)
    ctx.check("mcp get shows real tool count > 0", "Tools: 0" not in get_result.stdout)

    remove_result = _run(["mcp", "remove", "-s", "user", "fake"], home)
    ctx.check(f"mcp remove: exit 0, got {remove_result.returncode}", remove_result.returncode == 0)
    after = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    ctx.check("removed from mcpServers", "fake" not in after.get("mcpServers", {}))
    ctx.check("unrelated key still preserved after remove", after.get("unrelatedKey") == {"nested": [1, 2, 3]})


@test
def test_mcp_list_unapproved_dot_mcp_json_shows_pending_approval_and_spawns_nothing(ctx: Ctx):
    """finding 16 test 8: an unapproved `.mcp.json` server must show
    "⏸ Pending approval" and NEVER actually be spawned by `mcp list` --
    proven by pointing it at a command that doesn't exist at all: if it
    were spawned, the manager would report "✗ Failed to connect", not
    "Pending approval"."""
    home = _fresh_home()
    proj = home / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"unapproved": {"type": "stdio", "command": "definitely-not-a-real-command-xyz"}},
    }), encoding="utf-8")
    result = _run(["mcp", "list", "--cwd", str(proj)], home, timeout=30)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check(f"shows Pending approval, got {result.stdout!r}",
              "unapproved:" in result.stdout and "⏸ Pending approval" in result.stdout)
    ctx.check("never spawned (would show Failed to connect if it had tried)",
              "Failed to connect" not in result.stdout)


@test
def test_mcp_list_prints_checking_header_before_server_lines(ctx: Ctx):
    home = _fresh_home()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"demo-server": {"type": "stdio", "command": sys.executable,
                                         "args": ["-m", "tests.helpers.fake_mcp_server"]}},
    }), encoding="utf-8")
    result = _run(["mcp", "list"], home, timeout=60)
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    ctx.check(f"the health-check header is the FIRST line, got {lines[:2]}",
              lines and "Checking MCP server health" in lines[0])


@test
def test_mcp_add_dash_dash_separator_and_inline_env(ctx: Ctx):
    """finding 9: `mcp add name -- cmd args` (Claude Code's own documented
    form) and `--env=K=V` inline."""
    home = _fresh_home()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    result = _run(["mcp", "add", "-s", "user", "--env=SOME_KEY=1", "airtable", "--",
                    sys.executable, "-m", "tests.helpers.fake_mcp_server"], home)
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    entry = data.get("mcpServers", {}).get("airtable", {})
    ctx.check(f"command is the REAL command, never the literal '--', got {entry.get('command')!r}",
              entry.get("command") == sys.executable)
    ctx.check(f"args carry the rest, got {entry.get('args')!r}",
              entry.get("args") == ["-m", "tests.helpers.fake_mcp_server"])
    ctx.check(f"--env=K=V inline form captured, got {entry.get('env')!r}", entry.get("env") == {"SOME_KEY": "1"})


@test
def test_mcp_config_flag_long_inline_json_works_end_to_end(ctx: Ctx):
    """finding 10: a long inline --mcp-config JSON value with no '/' must
    not crash a real session (verified failure on Linux: OSError errno 36
    'File name too long' out of build_manager)."""
    home = _fresh_home()
    padding = "x" * 400
    spec = '{"mcpServers": {"' + padding + '": {"command": "definitely-not-real"}}}'
    result = _run(["mcp", "list", "--cwd", str(home)], home, timeout=30)
    ctx.check("baseline sanity: plain mcp list still exits 0", result.returncode == 0)
    # mcp list has no --mcp-config flag of its own -- exercise the parser
    # (manager.py's own job) directly instead, in-process, for the exact
    # OSError repro; the subprocess round trip above just confirms the
    # harness as a whole tolerates a long-running real session.
    import sys as _sys
    _sys.path.insert(0, str(REPO_DIR))
    from halo_harness.mcp.manager import resolve_server_configs
    resolved, notices = resolve_server_configs(cwd=home, claude_json={}, mcp_config_flag=[spec])
    ctx.check(f"parses without raising, got notices={notices}", padding in resolved)


@test
def test_mcp_subcommands_are_real_not_stubs(ctx: Ctx):
    """W4b: add-from-claude-desktop/reset-project-choices/serve/login/
    logout are all implemented now (see tests/test_mcp_subcommands.py,
    tests/test_mcp_serve.py and tests/test_mcp_oauth.py for their own real
    behaviour) -- `--help` is the fast, hang-free way to confirm each is a
    real argparse subcommand and not the old "not supported yet" stub."""
    home = _fresh_home()
    for sub in ("add-from-claude-desktop", "reset-project-choices", "serve", "login", "logout"):
        result = _run(["mcp", sub, "--help"], home)
        ctx.check(f"mcp {sub} --help: exit 0, got {result.returncode} stderr={result.stderr!r}",
                  result.returncode == 0)
        ctx.check(f"mcp {sub}: no longer a not-yet stub", "is not supported yet" not in result.stderr)
        ctx.check(f"mcp {sub} --help: prints real usage, got {result.stdout!r}",
                  f"halo mcp {sub}" in result.stdout)


@test
def test_mcp_unknown_subcommand_errors(ctx: Ctx):
    home = _fresh_home()
    result = _run(["mcp", "not-a-real-subcommand"], home)
    ctx.check(f"exit 2, got {result.returncode}", result.returncode == 2)


@test
def test_config_empty_then_set_then_get(ctx: Ctx):
    home = _fresh_home()
    empty = _run(["config"], home)
    ctx.check(f"empty config: exit 0, got {empty.returncode}", empty.returncode == 0)
    ctx.check("reports nothing set", "no config set" in empty.stdout)

    set_result = _run(["config", "set", "theme", "claude-dark-ansi"], home)
    ctx.check(f"config set: exit 0, got {set_result.returncode}", set_result.returncode == 0)

    get_result = _run(["config", "get", "theme"], home)
    ctx.check(f"config get: exit 0, got {get_result.returncode}", get_result.returncode == 0)
    ctx.check(f"round-trips the value, got {get_result.stdout!r}", json.loads(get_result.stdout.strip()) == "claude-dark-ansi")

    list_result = _run(["config", "list"], home)
    ctx.check("theme now shows up in the full listing", "theme=" in list_result.stdout)


@test
def test_config_shorthand_key_equals_value(ctx: Ctx):
    home = _fresh_home()
    result = _run(["config", "mcpPreload=[]"], home)
    ctx.check(f"key=value shorthand accepted, got exit {result.returncode}", result.returncode == 0)


@test
def test_config_set_invalid_theme_rejected(ctx: Ctx):
    home = _fresh_home()
    result = _run(["config", "set", "theme", "not-a-real-theme"], home)
    ctx.check(f"exit 2 (bad value), got {result.returncode}", result.returncode == 2)
    ctx.check("friendly error, not a traceback", "Traceback" not in result.stderr)


@test
def test_config_get_missing_key(ctx: Ctx):
    home = _fresh_home()
    result = _run(["config", "get", "does-not-exist"], home)
    ctx.check(f"exit 1, got {result.returncode}", result.returncode == 1)
    # A missing key (or a missing dotted segment such as `improve.nope`) is a
    # one-line "is not set" message, never a KeyError traceback (found live
    # after H12: `config get model` on a box where init had not run).
    ctx.check("stderr says 'is not set'", "is not set" in (result.stderr or ""))
    ctx.check("no traceback on stderr", "Traceback" not in (result.stderr or ""))
    dotted = _run(["config", "get", "improve.nope"], home)
    ctx.check(f"dotted missing key exits 1, got {dotted.returncode}", dotted.returncode == 1)
    ctx.check("dotted missing key: no traceback", "Traceback" not in (dotted.stderr or ""))


@test
def test_h9b_f33_doctor_python_check_matches_pyprojects_real_minimum(ctx: Ctx):
    """H9 whole-tree review finding 33: `_check_python` hardcoded (3, 9) as
    its OK/WARN threshold, one full minor version below pyproject.toml's
    real `requires-python = ">=3.10"` -- a 3.9 interpreter reported [OK]
    despite not meeting the package's own declared minimum."""
    import collections
    import re
    import sys
    from halo_harness.doctor import _check_python, OK, WARN

    # A plain regex, not tomllib (stdlib only since 3.11 -- pyproject.toml's
    # OWN declared minimum is 3.10, so this test must not itself need 3.11).
    text = (REPO_DIR / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    ctx.check("fixture drift guard: pyproject.toml has a requires-python line", m is not None)
    requires = m.group(1) if m else ""
    ctx.check(f"fixture drift guard: pyproject.toml still requires >=3.10, got {requires!r}",
              requires.strip() == ">=3.10")

    FakeVersion = collections.namedtuple("FakeVersion", "major minor micro")
    old = sys.version_info
    try:
        sys.version_info = FakeVersion(3, 9, 0)
        ctx.check(f"3.9 is WARN (below the real minimum), got {_check_python()!r}",
                  _check_python().startswith(WARN))
        sys.version_info = FakeVersion(3, 10, 0)
        ctx.check(f"3.10 is OK (the real minimum), got {_check_python()!r}", _check_python().startswith(OK))
    finally:
        sys.version_info = old


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
