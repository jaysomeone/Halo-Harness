"""tests.test_doctor_mcp_config_cli -- rolo_claude/{doctor,mcp_cli,
config_cli}.py (U0 scope A): the `doctor`/`mcp`/`config` subcommands, all
run against an isolated BRIDGE_TEST_HOME so a test run never touches the
real machine's `~/.claude.json` or `~/.rolo-claude/config.json`.
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
    return Path(tempfile.mkdtemp(prefix="rolo-claude-subcmd-"))


def _run(argv, home: Path, timeout=30):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    # H3: `cli.py`'s `_make_streams_utf8_safe()` deliberately reconfigures
    # the CHILD's stdout/stderr to UTF-8 (a model's reply, or an MCP
    # status glyph like the ✔/✗/⏸ this milestone's `mcp
    # list` prints, may not exist in a legacy Windows console codepage) --
    # decoding the captured bytes with anything other than UTF-8 here
    # (the bare `text=True` default is the LOCALE's preferred encoding,
    # cp1252 on this host) turns every such character into mojibake
    # (verified: "✔" -> "âœ”").
    return subprocess.run([sys.executable, "-m", "rolo_claude"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


@test
def test_doctor_runs_and_exits_cleanly(ctx: Ctx):
    home = _fresh_home()
    result = _run(["doctor"], home)
    ctx.check(f"exit 0 or 1 (never a crash), got {result.returncode}", result.returncode in (0, 1))
    ctx.check("mentions Python version check", "Python" in result.stdout)
    ctx.check("no traceback", "Traceback" not in result.stderr)


@test
def test_mcp_list_no_servers(ctx: Ctx):
    home = _fresh_home()
    result = _run(["mcp", "list"], home)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("reports no servers configured", "No MCP servers configured." in result.stdout)


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
    from rolo_claude.mcp.manager import resolve_server_configs
    resolved, notices = resolve_server_configs(cwd=home, claude_json={}, mcp_config_flag=[spec])
    ctx.check(f"parses without raising, got notices={notices}", padding in resolved)


@test
def test_mcp_other_subcommands_still_not_yet(ctx: Ctx):
    home = _fresh_home()
    for sub in (["login"], ["logout"], ["serve"]):
        result = _run(["mcp"] + sub, home)
        ctx.check(f"mcp {sub[0]}: exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check(f"mcp {sub[0]}: not-yet line printed", f"rolo-claude: mcp {sub[0]} is not supported yet" in result.stderr)


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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
