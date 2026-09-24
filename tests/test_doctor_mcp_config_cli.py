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
    return subprocess.run([sys.executable, "-m", "rolo_claude"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, timeout=timeout)


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
    home = _fresh_home()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"demo-server": {"type": "stdio", "command": "python", "args": ["-m", "demo"]}},
    }), encoding="utf-8")
    result = _run(["mcp", "list"], home)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("lists the configured server", "demo-server: python -m demo" in result.stdout)
    ctx.check("honest about not health-checking yet", "not checked" in result.stdout)


@test
def test_mcp_add_and_friends_are_not_yet(ctx: Ctx):
    home = _fresh_home()
    for sub in (["add", "x", "y"], ["remove", "x"], ["get", "x"], ["add-json", "x", "{}"]):
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
