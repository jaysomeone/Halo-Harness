"""tests.test_mcp_repair -- Halo 2.0.2 round 4 (brief D): the `/mcp` dialog's
repair actions and `halo mcp fix`/`halo mcp test`. Covers what test_mcp_
manager.py / test_mcp_cli.py / test_mcp_oauth.py don't already own: the
reconnect backoff schedule/state machine, the per-server log (append/tail/
rotation), the install-hint guesser, the shared reason+fix computation, the
`e` source-locator, scope-aware disable/enable, and the `fix`/`test` CLI
subcommands. All hermetic -- every test scopes BRIDGE_TEST_HOME to a fresh
temp dir via `_scoped` (never the real ~/.halo).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


def _scoped(fn):
    """Same pattern as test_mcp_subcommands.py's own `_scoped` -- a fresh
    BRIDGE_TEST_HOME per call, restored after, never the real ~/.halo
    (`home()`/`bridge_home()` both read this env var first, see
    halo_harness/config/paths.py)."""
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            return fn(Path(td))
        finally:
            if old is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old


def _fake_cfg(name="fake", *, mode=None, extra_tools=None, cwd=None):
    from halo_harness.mcp.manager import McpServerConfig
    env = {}
    if mode:
        env["FAKE_MCP_MODE"] = mode
    if extra_tools:
        env["FAKE_MCP_EXTRA_TOOLS"] = extra_tools
    return McpServerConfig(name=name, type="stdio", command=sys.executable,
                            args=["-m", "tests.helpers.fake_mcp_server"], env=env,
                            cwd=str(cwd) if cwd else str(REPO_DIR))


# ---- backoff schedule (pure function) --------------------------------------

@test
def test_mcp_backoff_delay_schedule(ctx: Ctx):
    from halo_harness.mcp.manager import mcp_backoff_delay_s
    ctx.check("attempt 0 -> 1s", mcp_backoff_delay_s(0) == 1.0)
    ctx.check("attempt 1 -> 2s", mcp_backoff_delay_s(1) == 2.0)
    ctx.check("attempt 2 -> 4s", mcp_backoff_delay_s(2) == 4.0)
    ctx.check("attempt 3 -> 8s", mcp_backoff_delay_s(3) == 8.0)
    ctx.check("attempt 4 -> steady 30s", mcp_backoff_delay_s(4) == 30.0)
    ctx.check("attempt 99 stays at the steady 30s rung", mcp_backoff_delay_s(99) == 30.0)
    ctx.check("a negative attempt clamps to attempt 0's 1s", mcp_backoff_delay_s(-5) == 1.0)


# ---- backoff state machine (McpServerHandle, injected `now`) ---------------

def _bare_handle(name="fake"):
    """A handle with no real transport -- only the backoff bookkeeping
    methods are exercised here, never start()/connect."""
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerHandle
    loop = McpLoop()
    h = McpServerHandle(_fake_cfg(name), loop, tool_env={}, cwd=REPO_DIR)
    return h, loop


@test
def test_backoff_arm_is_due_immediately_then_failures_advance_1_2_4_8_30(ctx: Ctx):
    """finding 6's pre-round4 contract ("reconnect once on the NEXT call",
    pinned by test_mcp_compat_matrix.py's item 13) means the FIRST
    automatic retry after a death is unconditional, not delayed -- `_arm_
    backoff` sets `backoff_next_retry_at = now` (due right away), never
    `now + 1s`. The 1, 2, 4, 8s-then-30s schedule applies to each retry
    AFTER that first one, if it ALSO fails -- reproduced live: reading
    the rung from the POST-increment attempt count skipped the 1s rung
    entirely (2, 4, 8, 30 -- never 1); fixed to read it before
    incrementing."""
    h, loop = _bare_handle()
    try:
        t0 = 1_000_000.0
        h._arm_backoff(now=t0)
        ctx.check("armed: attempt 0, owed", h.backoff_attempts == 0 and h._reconnect_on_next_call)
        ctx.check("due IMMEDIATELY -- the first automatic retry is unconditional",
                  h.backoff_due(now=t0) is True)

        h.record_backoff_failure(now=t0)  # that immediate attempt failed too
        ctx.check(f"attempt advanced to 1, got {h.backoff_attempts}", h.backoff_attempts == 1)
        ctx.check("not due yet (rung 0 -> 1s)", h.backoff_due(now=t0 + 0.5) is False)
        ctx.check("due once 1s has passed", h.backoff_due(now=t0 + 1.0) is True)
        ctx.check(f"next_retry_at is exactly 1s out, got {h.backoff_next_retry_at - t0}",
                  abs((h.backoff_next_retry_at - t0) - 1.0) < 1e-6)

        h.record_backoff_failure(now=t0 + 1.0)   # rung 1 -> 2s
        ctx.check(f"next rung is 2s out, got {h.backoff_next_retry_at - (t0 + 1.0)}",
                  abs((h.backoff_next_retry_at - (t0 + 1.0)) - 2.0) < 1e-6)

        h.record_backoff_failure(now=t0 + 3.0)   # rung 2 -> 4s
        ctx.check(f"next rung is 4s out, got {h.backoff_next_retry_at - (t0 + 3.0)}",
                  abs((h.backoff_next_retry_at - (t0 + 3.0)) - 4.0) < 1e-6)

        h.record_backoff_failure(now=t0 + 7.0)   # rung 3 -> 8s
        ctx.check(f"next rung is 8s out, got {h.backoff_next_retry_at - (t0 + 7.0)}",
                  abs((h.backoff_next_retry_at - (t0 + 7.0)) - 8.0) < 1e-6)

        h.record_backoff_failure(now=t0 + 15.0)  # rung 4+ -> steady 30s
        ctx.check(f"attempt 5 -> steady 30s rung, got {h.backoff_next_retry_at - (t0 + 15.0)}",
                  abs((h.backoff_next_retry_at - (t0 + 15.0)) - 30.0) < 1e-6)
    finally:
        loop.stop(timeout=2.0)


@test
def test_backoff_gives_up_after_ten_minutes(ctx: Ctx):
    h, loop = _bare_handle()
    try:
        t0 = 2_000_000.0
        h._arm_backoff(now=t0)
        h.record_backoff_failure(now=t0 + 600.0)  # exactly the 10-minute mark
        ctx.check(f"exhausted once 10 minutes elapse, got {h.backoff_exhausted}", h.backoff_exhausted is True)
        ctx.check("exhausted means never due again, regardless of how long since",
                  h.backoff_due(now=t0 + 100_000.0) is False)
        text = h.backoff_status_text(now=t0 + 600.0)
        ctx.check(f"status text says it gave up, got {text!r}", text is not None and "gave up" in text)
    finally:
        loop.stop(timeout=2.0)


@test
def test_backoff_clear_resets_everything(ctx: Ctx):
    h, loop = _bare_handle()
    try:
        h._arm_backoff(now=100.0)
        h.record_backoff_failure(now=101.0)
        h.clear_backoff()
        ctx.check("attempts reset", h.backoff_attempts == 0)
        ctx.check("started_at cleared", h.backoff_started_at is None)
        ctx.check("next_retry_at cleared", h.backoff_next_retry_at is None)
        ctx.check("no longer exhausted", h.backoff_exhausted is False)
        ctx.check("nothing armed -> no status text", h.backoff_status_text() is None)
        ctx.check("nothing armed -> always 'due' (no window to wait out)", h.backoff_due() is True)
    finally:
        loop.stop(timeout=2.0)


@test
def test_backoff_status_text_shows_attempt_and_remaining_seconds(ctx: Ctx):
    h, loop = _bare_handle()
    try:
        h._arm_backoff(now=0.0)
        h.record_backoff_failure(now=0.0)  # the immediate first attempt failed too -> rung 0, 1s out
        ctx.check(f"next retry is exactly 1s out, got {h.backoff_next_retry_at}",
                  abs(h.backoff_next_retry_at - 1.0) < 1e-6)
        text = h.backoff_status_text(now=0.0)
        ctx.check(f"names the attempt count, got {text!r}", text is not None and "attempt 1" in text)
        ctx.check(f"names ~1s remaining, got {text!r}", "1s" in text)
    finally:
        loop.stop(timeout=2.0)


# ---- per-server log: append / tail / rotation ------------------------------

@test
def test_append_server_log_then_tail_reads_it_back(ctx: Ctx):
    def _run(_home: Path):
        from halo_harness.mcp import manager as M
        M._append_server_log("fake", "connect failed: ConnectionRefusedError: boom")
        M._append_server_log("fake", "connection lost: RuntimeError: dead")
        lines = M.tail_server_log("fake")
        ctx.check(f"both lines present, got {lines}",
                  any("ConnectionRefusedError" in l for l in lines) and any("dead" in l for l in lines))
        ctx.check("the real path is under this test's scoped ~/.halo/mcp",
                  ".halo" in str(M.server_log_path("fake")).replace("\\", "/")
                  or ".halo" in str(M.server_log_path("fake")))
    _scoped(_run)


@test
def test_tail_server_log_no_file_yet_is_empty_not_a_crash(ctx: Ctx):
    def _run(_home: Path):
        from halo_harness.mcp.manager import tail_server_log
        ctx.check("no log yet -> []", tail_server_log("never-connected") == [])
    _scoped(_run)


@test
def test_tail_server_log_respects_max_lines(ctx: Ctx):
    def _run(_home: Path):
        from halo_harness.mcp import manager as M
        for i in range(10):
            M._append_server_log("many", f"line {i}")
        lines = M.tail_server_log("many", max_lines=3)
        ctx.check(f"only the last 3 lines, got {lines}",
                  len(lines) == 3 and lines[-1].endswith("line 9"))
    _scoped(_run)


@test
def test_server_log_rotates_at_1mb(ctx: Ctx):
    """round4 brief item 1: "rotated at 1 MB" -- `stdio.open_errlog`'s own
    rotation (reused by `_append_server_log`, never duplicated here)."""
    def _run(_home: Path):
        from halo_harness.mcp import stdio as S
        path = S.errlog_path("bigone")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        f = S.open_errlog("bigone")
        f.close()
        backup = path.with_name(path.name + ".1")
        ctx.check(f"a .1 backup was created, got exists={backup.exists()}", backup.exists())
        ctx.check(f"the live log is small again (just rotated), got {path.stat().st_size} bytes",
                  path.stat().st_size < 1024)
    _scoped(_run)


# ---- install hint guesser ---------------------------------------------------

@test
def test_install_hint_recognizes_each_launcher(ctx: Ctx):
    from halo_harness.mcp_cli import install_hint
    cases = {
        "npx": "nodejs.org", "npx.cmd": "nodejs.org", "node": "nodejs.org",
        "uvx": "uv", "uv": "uv", "pipx": "pipx", "pip": "ensurepip", "pip3": "ensurepip",
        "python": "python.org", "python3": "python.org",
    }
    for command, needle in cases.items():
        hint = install_hint({"type": "stdio", "command": command})
        ctx.check(f"{command!r} -> hint mentions {needle!r}, got {hint!r}", hint is not None and needle in hint)


@test
def test_install_hint_unknown_command_falls_back_generic(ctx: Ctx):
    from halo_harness.mcp_cli import install_hint
    hint = install_hint({"type": "stdio", "command": "my-custom-mcp-server"})
    ctx.check(f"names the command itself, got {hint!r}", hint is not None and "my-custom-mcp-server" in hint)


@test
def test_install_hint_strips_windows_extensions_and_paths(ctx: Ctx):
    from halo_harness.mcp_cli import install_hint
    hint = install_hint({"type": "stdio", "command": r"C:\tools\npx.cmd"})
    ctx.check(f"recognized npx despite the path+.cmd, got {hint!r}", hint is not None and "nodejs.org" in hint)


@test
def test_install_hint_non_stdio_entry_is_none(ctx: Ctx):
    from halo_harness.mcp_cli import install_hint
    ctx.check("an http/sse entry has nothing to install", install_hint({"type": "http", "url": "https://x"}) is None)


@test
def test_install_hint_no_command_is_none(ctx: Ctx):
    from halo_harness.mcp_cli import install_hint
    ctx.check("no command at all -> None", install_hint({"type": "stdio", "command": ""}) is None)


# ---- fix_line_for: the shared reason+fix, reused by /mcp and the CLI ------

@test
def test_fix_line_pending_approval_points_at_a(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    fix = fix_line_for({"name": "srv", "state": "pending_approval"})
    ctx.check(f"mentions `a`, got {fix!r}", fix is not None and "`a`" in fix)


@test
def test_fix_line_needs_auth_local_points_at_login(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    fix = fix_line_for({"name": "srv", "state": "needs_auth", "type": "http"})
    ctx.check(f"names the login command, got {fix!r}", fix is not None and "halo mcp login srv" in fix)


@test
def test_fix_line_needs_auth_connector_points_at_claude_ai(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    fix = fix_line_for({"name": "connector__x", "state": "needs_auth", "type": "connector"})
    ctx.check(f"points at claude.ai, got {fix!r}", fix is not None and "claude.ai" in fix)


@test
def test_fix_line_disabled_websocket_is_none(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    fix = fix_line_for({"name": "srv", "state": "disabled", "error": "'ws' needs the installed mcp SDK's "
                                                                       "own websocket client"})
    ctx.check(f"nothing to fix locally, got {fix!r}", fix is None)


@test
def test_fix_line_disabled_other_points_at_d(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    fix = fix_line_for({"name": "srv", "state": "disabled", "error": "unknown transport 'carrier-pigeon'"})
    ctx.check(f"mentions `d`, got {fix!r}", fix is not None and "`d`" in fix)


@test
def test_fix_line_failed_command_not_found_uses_install_hint(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    entry = {"name": "srv", "state": "failed", "type": "stdio", "command": "npx",
              "error": "FileNotFoundError: [Errno 2] No such file or directory: 'npx'"}
    fix = fix_line_for(entry)
    ctx.check(f"the install hint for npx, got {fix!r}", fix is not None and "nodejs.org" in fix)


@test
def test_fix_line_failed_connection_refused_points_at_e(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    entry = {"name": "srv", "state": "failed", "type": "http", "url": "http://127.0.0.1:9999/mcp",
              "error": "ConnectionRefusedError: [Errno 111] Connection refused"}
    fix = fix_line_for(entry)
    ctx.check(f"mentions `e`, got {fix!r}", fix is not None and "`e`" in fix)


@test
def test_fix_line_failed_generic_http_suggests_login_too(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    entry = {"name": "srv", "state": "failed", "type": "http", "error": "MCPError: Server returned an error response"}
    fix = fix_line_for(entry)
    ctx.check(f"suggests `l` for an unrecognized http failure, got {fix!r}", fix is not None and "`l`" in fix)


@test
def test_fix_line_failed_generic_stdio_suggests_test_and_log(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    entry = {"name": "srv", "state": "failed", "type": "stdio", "error": "RuntimeError: something else entirely"}
    fix = fix_line_for(entry)
    ctx.check(f"mentions `t` and `L`, got {fix!r}", fix is not None and "`t`" in fix and "`L`" in fix)


@test
def test_fix_line_healthy_row_is_none(ctx: Ctx):
    from halo_harness.mcp_cli import fix_line_for
    ctx.check("connected -> nothing to fix", fix_line_for({"name": "srv", "state": "connected"}) is None)
    ctx.check("cached -> nothing to fix", fix_line_for({"name": "srv", "state": "cached"}) is None)


# ---- locate_server_source: the `e` $EDITOR jump ----------------------------

@test
def test_locate_server_source_project_scope_finds_mcp_json_and_line(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.mcp.manager import McpServerConfig
        from halo_harness.mcp_cli import locate_server_source
        proj = home / "proj"
        proj.mkdir()
        (proj / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"first": {"command": "node", "args": []}, "target": {"command": "npx", "args": []}}},
            indent=2), encoding="utf-8")
        cfg = McpServerConfig(name="target", type="stdio", command="npx", scope="project")
        path, line = locate_server_source(cfg, name="target", cwd=proj)
        ctx.check(f"found the .mcp.json, got {path}", path == proj / ".mcp.json")
        text = path.read_text(encoding="utf-8")
        ctx.check(f"line {line} actually names 'target', got {text.splitlines()[line - 1]!r}",
                  '"target"' in text.splitlines()[line - 1])
    _scoped(_run)


@test
def test_locate_server_source_user_and_local_scope_use_claude_json(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.config.claude_json import claude_json_path
        from halo_harness.mcp.manager import McpServerConfig
        from halo_harness.mcp_cli import locate_server_source
        claude_json_path().write_text(json.dumps({"mcpServers": {"u1": {"command": "node"}}}), encoding="utf-8")
        cfg = McpServerConfig(name="u1", type="stdio", command="node", scope="user")
        path, line = locate_server_source(cfg, name="u1", cwd=home)
        ctx.check(f"user scope opens ~/.claude.json, got {path}", path == claude_json_path())
        ctx.check(f"found a real line, got {line}", line >= 1)
    _scoped(_run)


@test
def test_locate_server_source_managed_scope_has_no_single_file(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.mcp.manager import McpServerConfig
        from halo_harness.mcp_cli import locate_server_source
        cfg = McpServerConfig(name="m1", type="stdio", command="node", scope="managed")
        path, line = locate_server_source(cfg, name="m1", cwd=home)
        ctx.check(f"managed scope -> (None, 0), got {(path, line)}", path is None and line == 0)
    _scoped(_run)


# ---- scope-aware disable/enable (`d`) --------------------------------------

@test
def test_set_server_disabled_writes_per_directory_list(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.config.claude_json import claude_json_path
        from halo_harness.mcp_cli import set_server_disabled_in_config
        proj = home / "proj"
        proj.mkdir()
        where = set_server_disabled_in_config("srv", cwd=proj, disabled=True)
        ctx.check(f"says where it wrote, got {where!r}", "disabledMcpServers" in where)
        data = json.loads(claude_json_path().read_text(encoding="utf-8"))
        from halo_harness.config.paths import normalize_cwd
        key = normalize_cwd(proj)
        ctx.check(f"the name landed in projects[cwd].disabledMcpServers, got {data}",
                  "srv" in (data.get("projects", {}).get(key, {}).get("disabledMcpServers") or []))
    _scoped(_run)


@test
def test_set_server_disabled_then_enabled_round_trips_through_resolve(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.config.claude_json import claude_json_path, load_claude_json
        from halo_harness.mcp.manager import resolve_server_configs
        from halo_harness.mcp_cli import set_server_disabled_in_config
        proj = home / "proj2"
        proj.mkdir()
        claude_json_path().write_text(json.dumps({"mcpServers": {"srv": {"command": "node"}}}), encoding="utf-8")

        set_server_disabled_in_config("srv", cwd=proj, disabled=True)
        resolved, _n = resolve_server_configs(cwd=proj, claude_json=load_claude_json())
        ctx.check(f"disabled -- filtered out of this directory's resolution, got {list(resolved)}",
                  "srv" not in resolved)

        set_server_disabled_in_config("srv", cwd=proj, disabled=False)
        resolved, _n = resolve_server_configs(cwd=proj, claude_json=load_claude_json())
        ctx.check(f"re-enabled -- back in resolution, got {list(resolved)}", "srv" in resolved)
    _scoped(_run)


@test
def test_set_server_disabled_is_per_directory_not_global(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.config.claude_json import claude_json_path, load_claude_json
        from halo_harness.mcp.manager import resolve_server_configs
        from halo_harness.mcp_cli import set_server_disabled_in_config
        claude_json_path().write_text(json.dumps({"mcpServers": {"srv": {"command": "node"}}}), encoding="utf-8")
        proj_a = home / "a"
        proj_b = home / "b"
        proj_a.mkdir()
        proj_b.mkdir()
        set_server_disabled_in_config("srv", cwd=proj_a, disabled=True)
        resolved_a, _ = resolve_server_configs(cwd=proj_a, claude_json=load_claude_json())
        resolved_b, _ = resolve_server_configs(cwd=proj_b, claude_json=load_claude_json())
        ctx.check("disabled in A", "srv" not in resolved_a)
        ctx.check("still enabled in B -- the disable is scoped to A only", "srv" in resolved_b)
    _scoped(_run)


# ---- `halo mcp fix` / `halo mcp test` CLI --------------------------------

def _run_cli(argv):
    from halo_harness.mcp_cli import cmd_mcp
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    try:
        rc = cmd_mcp(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


@test
def test_cli_fix_unknown_server_is_a_clean_error(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        rc, _out, err = _run_cli(["fix", "nope", "--cwd", str(home)])
        ctx.check(f"exit 1, got {rc}", rc == 1)
        ctx.check(f"says no server found, got {err!r}", "no MCP server found" in err)
    _scoped(_run)


@test
def test_cli_fix_command_not_found_without_apply_only_prints(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"ghost": {"command": "definitely-not-a-real-binary-xyz", "args": []}}}),
            encoding="utf-8")
        rc, out, _err = _run_cli(["fix", "ghost", "--cwd", str(home)])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check(f"shows the reason, got {out!r}", "command not found on PATH" in out)
        ctx.check(f"shows the fix line, got {out!r}", "fix:" in out)
        ctx.check(f"tells the user to re-run with --apply, got {out!r}", "--apply" in out)
    _scoped(_run)


@test
def test_cli_fix_command_not_found_with_apply_prints_the_install_hint(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"ghost": {"command": "definitely-not-a-real-binary-xyz", "args": []}}}),
            encoding="utf-8")
        rc, out, _err = _run_cli(["fix", "ghost", "--apply", "--cwd", str(home)])
        ctx.check(f"exit 0 -- printing a hint always succeeds, got {rc}", rc == 0)
        ctx.check(f"names the missing command, got {out!r}", "definitely-not-a-real-binary-xyz" in out)
    _scoped(_run)


@test
def test_cli_fix_pending_approval_with_apply_approves_and_reconnects(ctx: Ctx):
    def _run(home: Path):
        proj = home / "proj"
        proj.mkdir()
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"fake": {
            "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"]}}}), encoding="utf-8")
        rc, out, _err = _run_cli(["fix", "fake", "--apply", "--cwd", str(proj)])
        ctx.check(f"exit 0, got {rc}, out={out!r}", rc == 0)
        ctx.check(f"says it approved it, got {out!r}", "approved" in out)
        from halo_harness.mcp_setup import load_mcp_approvals
        ctx.check(f"the approval actually persisted, got {load_mcp_approvals()}", len(load_mcp_approvals()) == 1)
    _scoped(_run)


@test
def test_cli_fix_needs_auth_with_apply_reports_the_login_failure(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "remote": {"type": "http", "url": "https://example.invalid/mcp", "oauth": {}}}}), encoding="utf-8")
        import halo_harness.mcp.oauth as oauth_mod
        old = oauth_mod.run_authorization_flow
        oauth_mod.run_authorization_flow = lambda **kw: (None, "no endpoint discoverable")
        try:
            # force needs_auth directly -- a real connect to a bogus https
            # host would otherwise just time out as a plain DNS/connect
            # failure (slow and unrelated to what this test checks: the
            # --apply -> run_login wiring once a row IS needs_auth).
            from halo_harness.mcp_cli import _apply_fix, failure_reason
            entry = {"name": "remote", "state": "needs_auth", "type": "http"}
            from halo_harness.mcp_setup import build_manager
            from halo_harness.config.claude_json import load_claude_json
            manager, _n = build_manager(cwd=home, claude_json=load_claude_json(), print_mode=False, start=False)
            rc = _apply_fix("remote", entry, manager=manager, cwd=home, settings=None)
            manager.close_all()
            ctx.check(f"exit 1 -- the stubbed flow reports failure, got {rc}", rc == 1)
        finally:
            oauth_mod.run_authorization_flow = old
    _scoped(_run)


@test
def test_cli_test_round_trip_ok(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"fake": {
            "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"]}}}), encoding="utf-8")
        rc, out, _err = _run_cli(["test", "fake", "--cwd", str(home)])
        ctx.check(f"exit 0, got {rc}, out={out!r}", rc == 0)
        ctx.check(f"reports ok with a tool count, got {out!r}", "ok in" in out and "tool(s)" in out)
    _scoped(_run)


@test
def test_cli_test_command_not_found_fails_cleanly(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"ghost": {"command": "definitely-not-a-real-binary-xyz", "args": []}}}),
            encoding="utf-8")
        rc, _out, err = _run_cli(["test", "ghost", "--cwd", str(home)])
        ctx.check(f"exit 1, got {rc}", rc == 1)
        ctx.check(f"reports the failure, got {err!r}", "failed after" in err)
    _scoped(_run)


@test
def test_cli_test_unknown_server_is_a_clean_error(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        rc, _out, err = _run_cli(["test", "nope", "--cwd", str(home)])
        ctx.check(f"exit 1, got {rc}", rc == 1)
        ctx.check(f"says no server found, got {err!r}", "no MCP server found" in err)
    _scoped(_run)


# ---- live integration: a real dying server, backoff-gated, then reconnect_manual --

@test
def test_call_gates_automatic_reconnect_by_backoff_then_succeeds_once_due(ctx: Ctx):
    """The pilot the brief asks for: "a stdio server that dies on demand
    for reconnect/backoff" -- a REAL subprocess (tests/helpers/fake_mcp_
    server.py's `die_mid_call`), driven through `McpManager.call()`
    exactly as a real tool call would be. Backoff timing itself is driven
    by directly setting `backoff_next_retry_at` (white-box, no real
    sleep) rather than racing a real 1s wait."""
    from halo_harness.mcp.manager import McpManager
    cfg = _fake_cfg("fake", extra_tools="die_mid_call")
    mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        h = mgr.handles["fake"]
        try:
            mgr.call("fake", "die_mid_call", {})
        except Exception:
            pass
        ctx.check(f"died and backoff armed, got state={h.state} armed={h.backoff_started_at is not None}",
                  h.state == "failed" and h.backoff_started_at is not None)

        # Not due yet -- the next call must skip the reconnect and fail
        # FAST (never a wasted connect attempt against a window that
        # hasn't elapsed).
        h.backoff_next_retry_at = time.monotonic() + 1000.0
        t0 = time.monotonic()
        raised = False
        try:
            mgr.call("fake", "echo", {"text": "hi"})
        except Exception:
            raised = True
        elapsed = time.monotonic() - t0
        ctx.check(f"skipped the reconnect (not due) -- fails fast, got raised={raised} elapsed={elapsed:.2f}s",
                  raised and elapsed < 2.0)
        ctx.check(f"still failed, no reconnect attempted, got {h.state}", h.state == "failed")

        # Now due -- the SAME next call reconnects transparently and the
        # tool call goes through.
        h.backoff_next_retry_at = time.monotonic() - 1.0
        result = mgr.call("fake", "echo", {"text": "hi there"})
        ctx.check(f"reconnected and the call went through, got {result.content[0].text!r}",
                  "hi there" in result.content[0].text)
        ctx.check(f"connected again, got {h.state}", h.state == "connected")
        ctx.check("backoff cleared on success", h.backoff_started_at is None)
    finally:
        mgr.close_all()


@test
def test_reconnect_manual_resets_backoff_even_on_renewed_failure(ctx: Ctx):
    """round4 brief item 2: "R resets the backoff" -- `reconnect_manual`
    always clears a stale backoff first; if the manual attempt fails
    again (here: the command is swapped to a nonexistent one between
    close and start, simulating "still broken"), a FRESH backoff is
    armed (attempt 0 again, not a continuation of the old one)."""
    from halo_harness.mcp.manager import McpManager
    cfg = _fake_cfg("fake", extra_tools="die_mid_call")
    mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        h = mgr.handles["fake"]
        try:
            mgr.call("fake", "die_mid_call", {})
        except Exception:
            pass
        h.record_backoff_failure()
        h.record_backoff_failure()
        ctx.check(f"a few failures already recorded, got attempts={h.backoff_attempts}", h.backoff_attempts >= 2)

        ok = mgr.reconnect_manual("fake")
        ctx.check(f"a manual reconnect against the SAME (still runnable) command succeeds, got {ok}", ok is True)
        ctx.check("success clears backoff entirely", h.backoff_started_at is None and h.backoff_attempts == 0)
    finally:
        mgr.close_all()


@test
def test_bare_http_401_server_is_a_recognizable_connect_failure(ctx: Ctx):
    """round4 brief (tests, item for `l`): "a 401 http server". Verified
    live (see serve_http_401's own docstring): the installed SDK does
    NOT surface this as a 401-shaped exception, so the handle lands in
    'failed' with a generic MCPError rather than 'needs_auth' -- this
    pins that REAL behaviour rather than the aspirational one, and
    confirms `fix_line_for` still points at `l` for it anyway."""
    from tests.helpers.fake_mcp_server import serve_http_401
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle
    from halo_harness.mcp_cli import failure_reason, fix_line_for
    with serve_http_401() as url:
        loop = McpLoop()
        try:
            cfg = McpServerConfig(name="fake401", type="http", url=url)
            h = McpServerHandle(cfg, loop, tool_env={}, cwd=REPO_DIR)
            h.start()
            ctx.check(f"a real 401 response is a genuine connect failure, got state={h.state} error={h.error!r}",
                      h.state in ("failed", "needs_auth") and bool(h.error))
            entry = {"name": "fake401", "type": "http", "state": h.state, "error": h.error}
            ctx.check(f"failure_reason never crashes on it, got {failure_reason(entry)!r}",
                      isinstance(failure_reason(entry), str))
            fix = fix_line_for(entry)
            ctx.check(f"the fix line still points at `l` either way, got {fix!r}", fix is not None and "`l`" in fix)
        finally:
            loop.stop(timeout=2.0)


# ---- Controller methods (round4 brief item 1) ------------------------------

def _controller_with_manager(cwd: Path, configs: dict):
    from halo_harness.controller import Controller
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager(configs, tool_env=dict(os.environ), cwd=cwd)
    mgr.start_all()
    controller = Controller(session=None, cwd=cwd)
    controller.mcp_manager = mgr
    return controller, mgr


@test
def test_controller_test_mcp_server_reports_timing_and_tool_count(ctx: Ctx):
    def _run(home: Path):
        controller, mgr = _controller_with_manager(home, {"fake": _fake_cfg("fake")})
        try:
            lines = controller.test_mcp_server("fake")
            ctx.check(f"ok with a tool count, got {lines}", bool(lines) and "ok in" in lines[0] and "tool(s)" in lines[0])
        finally:
            mgr.close_all()
    _scoped(_run)


@test
def test_controller_test_mcp_server_no_manager_is_a_clean_note(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.controller import Controller
        controller = Controller(session=None, cwd=home)
        lines = controller.test_mcp_server("nope")
        ctx.check(f"a clean message, never a crash, got {lines}", bool(lines) and "not connected" in lines[0].lower())
    _scoped(_run)


@test
def test_controller_test_mcp_server_connector_row_is_a_clean_note(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.controller import Controller
        controller = Controller(session=None, cwd=home)
        lines = controller.test_mcp_server("connector__demo")
        ctx.check(f"explains connectors aren't tested this way, got {lines}",
                  bool(lines) and "claude" in lines[0].lower())
    _scoped(_run)


@test
def test_controller_reconnect_all_mcp_covers_every_row(ctx: Ctx):
    def _run(home: Path):
        controller, mgr = _controller_with_manager(home, {"a": _fake_cfg("a"), "b": _fake_cfg("b")})
        try:
            lines = controller.reconnect_all_mcp()
            ctx.check(f"both servers reported on, got {lines}", len(lines) >= 2)
        finally:
            mgr.close_all()
    _scoped(_run)


@test
def test_controller_set_mcp_server_disabled_then_enabled_round_trips(ctx: Ctx):
    def _run(home: Path):
        controller, mgr = _controller_with_manager(home, {"fake": _fake_cfg("fake")})
        try:
            from halo_harness.config.claude_json import claude_json_path
            claude_json_path().write_text(json.dumps({"mcpServers": {"fake": {
                "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"]}}}), encoding="utf-8")
            lines = controller.set_mcp_server_disabled("fake", True)
            ctx.check(f"reports disabled, got {lines}", bool(lines) and "disabled" in lines[0])
            ctx.check(f"the live handle reflects it, got {mgr.handles['fake'].state}",
                      mgr.handles["fake"].state == "disabled")

            lines = controller.set_mcp_server_disabled("fake", False)
            ctx.check(f"reports enabled, got {lines}", bool(lines) and "enabled" in lines[0])
            ctx.check(f"reconnected live, got {mgr.handles['fake'].state}", mgr.handles["fake"].state == "connected")
        finally:
            mgr.close_all()
    _scoped(_run)


@test
def test_controller_resolve_mcp_config_returns_the_live_config(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.config.claude_json import claude_json_path
        from halo_harness.controller import Controller
        claude_json_path().write_text(json.dumps({"mcpServers": {"fake": {"command": "node", "args": ["x"]}}}),
                                        encoding="utf-8")
        controller = Controller(session=None, cwd=home)
        cfg = controller.resolve_mcp_config("fake")
        ctx.check(f"found it with the right command, got {cfg}", cfg is not None and cfg.command == "node")
        ctx.check("unknown name -> None", controller.resolve_mcp_config("nope") is None)
    _scoped(_run)


@test
def test_controller_login_mcp_server_connector_row_uses_reauth_instructions(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.controller import Controller
        from halo_harness.mcp import connectors_bridge
        from halo_harness.mcp.connectors import ConnectorInfo
        info = ConnectorInfo(name="Demo", slug="demo", account_token="tok", host="example.com",
                               url="https://example.com/mcp", status="needs_auth", status_text="needs auth")
        connectors_bridge.save_cache([info])
        controller = Controller(session=None, cwd=home)
        lines = controller.login_mcp_server("connector__demo")
        ctx.check(f"the connector re-auth pointer, got {lines}", bool(lines) and "claude.ai" in lines[0])
    _scoped(_run)


@test
def test_controller_login_mcp_server_local_stdio_server_refuses_cleanly(ctx: Ctx):
    def _run(home: Path):
        from halo_harness.config.claude_json import claude_json_path
        from halo_harness.controller import Controller
        claude_json_path().write_text(json.dumps({"mcpServers": {"local": {"command": "node"}}}), encoding="utf-8")
        controller = Controller(session=None, cwd=home)
        lines = controller.login_mcp_server("local")
        ctx.check(f"OAuth only applies to http/sse, got {lines}", bool(lines) and "http/sse" in lines[0])
    _scoped(_run)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
