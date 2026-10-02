"""tests.test_mcp_subcommands -- halo_harness/mcp_cli.py (Halo 2.0.1
gap-list brief, W4b item 2): `add-from-claude-desktop`, `reset-project-
choices`, and the `login`/`logout` CLI wiring (the OAuth flow itself is
pinned end to end in tests/test_mcp_oauth.py). All run against an isolated
BRIDGE_TEST_HOME -- never a real ~/.claude.json or %APPDATA%.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness import mcp_cli as C

test, TESTS = new_registry()


def _scoped(fn):
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("BRIDGE_TEST_HOME")
        old_cfg = os.environ.get("BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            return fn(Path(td))
        finally:
            for key, val in (("BRIDGE_TEST_HOME", old), ("BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG", old_cfg)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


# ---- add-from-claude-desktop -------------------------------------------------

def _write_desktop_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {
        "airtable": {"command": "npx", "args": ["-y", "airtable-mcp"]},
        "sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"},
    }}), encoding="utf-8")


@test
def test_add_from_claude_desktop_imports_into_local_scope_by_default(ctx: Ctx):
    def _run(home: Path):
        desktop_cfg = home / "fake-desktop" / "claude_desktop_config.json"
        _write_desktop_config(desktop_cfg)
        os.environ["BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG"] = str(desktop_cfg)
        cwd = home / "proj"
        cwd.mkdir()
        rc = C.cmd_mcp(["add-from-claude-desktop", "--cwd", str(cwd)])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        from halo_harness.config.paths import normalize_cwd
        servers = data["projects"][normalize_cwd(cwd)]["mcpServers"]
        ctx.check(f"both imported, got {sorted(servers)}", sorted(servers) == ["airtable", "sentry"])
        ctx.check("stdio entry preserved", servers["airtable"]["command"] == "npx")
        ctx.check("http entry preserved", servers["sentry"]["url"] == "https://mcp.sentry.dev/mcp")
    _scoped(_run)


@test
def test_add_from_claude_desktop_dry_run_writes_nothing(ctx: Ctx):
    def _run(home: Path):
        desktop_cfg = home / "fake-desktop" / "claude_desktop_config.json"
        _write_desktop_config(desktop_cfg)
        os.environ["BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG"] = str(desktop_cfg)
        rc = C.cmd_mcp(["add-from-claude-desktop", "--dry-run", "--cwd", str(home)])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("no ~/.claude.json written at all", not (home / ".claude.json").exists())
    _scoped(_run)


@test
def test_add_from_claude_desktop_user_scope(ctx: Ctx):
    def _run(home: Path):
        desktop_cfg = home / "fake-desktop" / "claude_desktop_config.json"
        _write_desktop_config(desktop_cfg)
        os.environ["BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG"] = str(desktop_cfg)
        rc = C.cmd_mcp(["add-from-claude-desktop", "-s", "user", "--cwd", str(home)])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        ctx.check("written to top-level mcpServers (user scope)",
                  sorted(data.get("mcpServers", {})) == ["airtable", "sentry"])
    _scoped(_run)


@test
def test_add_from_claude_desktop_no_config_found_is_a_clean_error(ctx: Ctx):
    def _run(home: Path):
        os.environ["BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG"] = str(home / "nope" / "claude_desktop_config.json")
        rc = C.cmd_mcp(["add-from-claude-desktop", "--cwd", str(home)])
        ctx.check(f"exit 1, got {rc}", rc == 1)
    _scoped(_run)


# ---- reset-project-choices ---------------------------------------------------

@test
def test_reset_project_choices_forgets_approvals_for_current_mcp_json(ctx: Ctx):
    def _run(home: Path):
        cwd = home / "proj"
        cwd.mkdir()
        entry = {"type": "stdio", "command": "node", "args": ["server.js"]}
        (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": entry}}), encoding="utf-8")
        from halo_harness.mcp_setup import load_mcp_approvals, record_mcp_approval
        from halo_harness.mcp.manager import mcp_approval_key
        record_mcp_approval("srv", entry)
        ctx.check("approved beforehand", mcp_approval_key(entry) in load_mcp_approvals())

        rc = C.cmd_mcp(["reset-project-choices", "--cwd", str(cwd)])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("approval forgotten", mcp_approval_key(entry) not in load_mcp_approvals())
    _scoped(_run)


@test
def test_reset_project_choices_no_mcp_json_is_a_clean_noop(ctx: Ctx):
    def _run(home: Path):
        cwd = home / "empty-proj"
        cwd.mkdir()
        rc = C.cmd_mcp(["reset-project-choices", "--cwd", str(cwd)])
        ctx.check(f"exit 0 (never a crash), got {rc}", rc == 0)
    _scoped(_run)


# ---- login / logout CLI wiring ------------------------------------------------

@test
def test_login_unknown_server_is_a_clean_error(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        rc = C.cmd_mcp(["login", "nope", "--cwd", str(home)])
        ctx.check(f"exit 1, got {rc}", rc == 1)
    _scoped(_run)


@test
def test_login_refuses_a_stdio_server(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"local-tool": {"command": "node", "args": ["x.js"]}}}), encoding="utf-8")
        rc = C.cmd_mcp(["login", "local-tool", "--cwd", str(home)])
        ctx.check(f"exit 1, got {rc}", rc == 1)
    _scoped(_run)


@test
def test_logout_with_nothing_stored_is_a_clean_noop(ctx: Ctx):
    def _run(home: Path):
        rc = C.cmd_mcp(["logout", "never-logged-in"])
        ctx.check(f"exit 0, got {rc}", rc == 0)
    _scoped(_run)


@test
def test_login_cli_resolves_the_named_server_and_saves_tokens_on_success(ctx: Ctx):
    """The CLI glue specifically (resolving `name` -> its config -> the
    right `server_url`/`oauth_cfg`/`open_browser`, then saving whatever
    `run_authorization_flow` returns) -- the flow function itself is
    already driven end to end against a real fake OAuth server in
    tests/test_mcp_oauth.py; stubbing it here keeps this test fast and
    deterministic rather than racing a second real HTTP round trip."""
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "remote-srv": {"type": "http", "url": "https://x/mcp",
                            "oauth": {"client_id": "cid", "authorization_endpoint": "https://x/authorize",
                                       "token_endpoint": "https://x/token"}},
        }}), encoding="utf-8")

        import halo_harness.mcp.oauth as oauth_mod
        calls = []

        def _fake_flow(**kwargs):
            calls.append(kwargs)
            return {"access_token": "tok-123"}, None

        old = oauth_mod.run_authorization_flow
        oauth_mod.run_authorization_flow = _fake_flow
        try:
            rc = C.cmd_mcp(["login", "remote-srv", "--no-browser", "--cwd", str(home)])
        finally:
            oauth_mod.run_authorization_flow = old

        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check(f"exactly one call to the flow, got {len(calls)}", len(calls) == 1)
        call = calls[0]
        ctx.check("server_name forwarded", call["server_name"] == "remote-srv")
        ctx.check("server_url forwarded", call["server_url"] == "https://x/mcp")
        ctx.check("oauth_cfg forwarded verbatim", call["oauth_cfg"].get("client_id") == "cid")
        ctx.check("open_browser honours --no-browser", call["open_browser"] is False)

        tokens = oauth_mod.load_tokens("remote-srv")
        ctx.check(f"tokens actually saved, got {tokens!r}",
                  tokens is not None and tokens.get("access_token") == "tok-123")

        rc = C.cmd_mcp(["logout", "remote-srv"])
        ctx.check(f"logout exit 0, got {rc}", rc == 0)
        ctx.check("tokens cleared", oauth_mod.load_tokens("remote-srv") is None)
    _scoped(_run)


@test
def test_login_failure_from_the_flow_is_reported_and_nothing_is_saved(ctx: Ctx):
    def _run(home: Path):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "remote-srv": {"type": "sse", "url": "https://x/sse", "oauth": {}}}}), encoding="utf-8")
        import halo_harness.mcp.oauth as oauth_mod
        old = oauth_mod.run_authorization_flow
        oauth_mod.run_authorization_flow = lambda **kw: (None, "no endpoint discoverable")
        try:
            rc = C.cmd_mcp(["login", "remote-srv", "--no-browser", "--cwd", str(home)])
        finally:
            oauth_mod.run_authorization_flow = old
        ctx.check(f"exit 1, got {rc}", rc == 1)
        ctx.check("nothing saved", oauth_mod.load_tokens("remote-srv") is None)
    _scoped(_run)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
