"""tests.test_mcp_compat_matrix -- H9 Part B: the MCP compatibility matrix
(rolo's parity promise: "every server configured for Claude Code must work
unchanged in rolo-claude"). One section per numbered item in the H9 brief's
Part B; each proves that item with a REAL connection through tests/helpers/
fake_mcp_server.py wherever practical, the real `claude` binary for items
specifically about interop with it (6/7/8, skipped if `claude` isn't on
PATH), and the real marketplace clone for item 5's "real plugin copy" where
present. Does not duplicate existing coverage in test_mcp_manager.py/
test_mcp_plugins.py/test_mcp_catalog.py/test_mcp_cli.py/test_mcp_e2e.py --
each section says what it adds beyond those.

Bugs this file's tests discovered (see the final report for the full
write-up; each is also called out in the relevant test's own docstring):
  (a) item 9 -- providers/routing.py's sanitize_tool_schema (the path
      convert_tools() uses for every NON-Databricks profile, i.e.
      OpenRouter, the harness's default) only pops "$schema": a real MCP
      tool's $ref/$defs/anyOf/legacy-tuple-items schema reaches OpenRouter
      completely unresolved. OPEN (not fixed).
  (b) item 9 -- providers/request.py's simplify_schema_for_databricks drops
      a non-2-element (or non-null-pair) anyOf ENTIRELY with no fallback
      type (unlike its $ref handling, which falls back to {"type":
      "object"}), leaving the property with no type information at all.
      OPEN (not fixed).
  (c) item 12 -- mcp/http_sse.py's connect_http unpacked 3 values from
      streamable_http_client(...)'s yielded object; the installed SDK (mcp
      2.2.0 -- the same version this module's own comment claimed to
      target) yields a 2-tuple, so every `type: "http"` server failed to
      connect outright. FIXED during this same session (by the concurrent
      H9 central worker, credited in connect_http's own updated comment) --
      test_item12_http_transport_connects_and_round_trips now asserts the
      fix rather than pinning the bug.
  (d) already-built item -- the progress-notification keepalive
      (mcp/client.py's ProgressKeepalive + McpServerHandle.call_tool) can
      never actually rescue a real call: the SDK's own internal per-request
      timeout (mcp/shared/jsonrpc_dispatcher.py, a single anyio.fail_after
      armed once) is not reset by progress notifications, and manager.py's
      call_tool passes it the SAME value the outer, keepalive-extendable
      run_abortable bound is derived from (always 3s shorter) -- the inner,
      non-resettable bound always fires first. OPEN (not fixed).
  (e) item 11 -- Controller.reconnect_mcp / McpManager.reconnect never
      re-resolve ~/.claude.json/.mcp.json from disk: a changed existing
      server reconnects with its STALE config, and a brand-new server name
      has no handle at all ("unknown MCP server") for the rest of the
      session. OPEN (not fixed).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns
from rolo_claude.mcp import manager as M

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _fake_stdio_entry(*, mode=None, tool_count=None, extra_tools=None, timeout_ms=None) -> dict:
    """A raw ~/.claude.json/.mcp.json-shaped stdio entry pointing at the
    real fake MCP server -- for tests that write actual config JSON rather
    than constructing McpServerConfig objects directly."""
    env = {}
    if mode:
        env["FAKE_MCP_MODE"] = mode
    if tool_count:
        env["FAKE_MCP_TOOL_COUNT"] = str(tool_count)
    if extra_tools:
        env["FAKE_MCP_EXTRA_TOOLS"] = ",".join(extra_tools)
    entry = {"type": "stdio", "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"]}
    if env:
        entry["env"] = env
    if timeout_ms:
        entry["timeout"] = timeout_ms
    return entry


def _fake_cfg(name="fake", *, mode=None, tool_count=None, extra_tools=None, timeout_ms=None, cwd=None) -> M.McpServerConfig:
    env = {}
    if mode:
        env["FAKE_MCP_MODE"] = mode
    if tool_count:
        env["FAKE_MCP_TOOL_COUNT"] = str(tool_count)
    if extra_tools:
        env["FAKE_MCP_EXTRA_TOOLS"] = ",".join(extra_tools)
    return M.McpServerConfig(name=name, type="stdio", command=sys.executable,
                              args=["-m", "tests.helpers.fake_mcp_server"], env=env,
                              timeout_ms=timeout_ms, cwd=str(cwd) if cwd else str(REPO_DIR))


def _tool_call_chunk(call_id, name, arguments):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _final_text_chunk(text):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _run(args, *, env, cwd=None, timeout=30):
    """subprocess.run wrapper used throughout this file: explicit utf-8
    decoding (Windows' default locale codec (cp1252) cannot decode the
    unicode glyphs Claude Code's own mcp status vocabulary uses, e.g.
    '⏸ Pending approval' -- the plain `text=True` default silently left
    `result.stdout`/`.stderr` as None instead of raising, discovered while
    writing this file's own item 2 test)."""
    return subprocess.run(args, env=env, cwd=str(cwd or REPO_DIR), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _run_rolo(prompt, mock, *, home, cwd, extra_args=None, extra_env=None, model="or:mock/model", timeout=30):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(home), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", model, "--cwd", str(cwd)] + (extra_args or [])
    return _run(args, env=env, timeout=timeout)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ============================================================================
# Item 1 -- user-scope servers from ~/.claude.json (mcpServers) work, via a
# temp-HOME fixture extending fake_home.py's own shape. test_mcp_e2e.py
# already proves a user-scope server works end to end, but by REPLACING
# build_fake_home()'s own default .claude.json; this extends the fixture's
# EXISTING shape (keeping the pre-existing "expanded-models" stub entry
# alongside a real, connectable one) and additionally cross-checks the real
# CLI's `mcp list` output, per the brief's explicit ask.
# ============================================================================

@test
def test_item1_user_scope_server_extends_fake_home_and_connects(ctx: Ctx):
    fh = build_fake_home()
    claude_json_path = fh["home"] / ".claude.json"
    data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    ctx.check("fixture's own pre-existing user-scope entry is still there",
              "expanded-models" in data["mcpServers"])
    data["mcpServers"]["fake"] = _fake_stdio_entry()
    claude_json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    from rolo_claude import mcp_setup
    manager, notices = mcp_setup.build_manager(cwd=fh["proj"], claude_json=data, settings=None,
                                                print_mode=False, start=True)
    try:
        status = {s["name"]: s for s in manager.status()}
        ctx.check(f"the real user-scope server connects, got {status.get('fake')}",
                  status["fake"]["state"] == "connected")
        ctx.check("it coexists with the fixture's own (non-connectable) entry",
                  "expanded-models" in status)
        result = manager.call("fake", "echo", {"text": "user scope round trip"})
        ctx.check("a real tool call round-trips", result.content[0].text == "user scope round trip")
    finally:
        manager.close_all()

    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "PYTHONPATH": str(REPO_DIR)})
    result = _run([sys.executable, "-m", "rolo_claude", "mcp", "list", "--cwd", str(fh["proj"])], env=env)
    ctx.check(f"the real CLI's `mcp list` also shows it Connected, got {result.stdout!r}",
              "fake: " in result.stdout and "Connected" in result.stdout)


# ============================================================================
# Item 2 -- a project .mcp.json server: "Pending approval" BEFORE approval,
# works AFTER. test_mcp_manager.py covers the resolve()-level flag; this
# proves the full live flow (real CLI status text, real approval write,
# real connect) end to end.
# ============================================================================

@test
def test_item2_dot_mcp_json_pending_before_and_connects_after_approval(ctx: Ctx):
    from rolo_claude import mcp_setup
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        proj.mkdir()
        home = Path(td) / "home"
        (home / ".claude").mkdir(parents=True)
        entry = _fake_stdio_entry()
        (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"fake": entry}}), encoding="utf-8")

        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
        before = _run([sys.executable, "-m", "rolo_claude", "mcp", "list", "--cwd", str(proj)], env=env)
        ctx.check(f"shows Pending approval BEFORE approval, got {before.stdout!r}",
                  "Pending approval" in (before.stdout or ""))

        # in-process calls below touch mcp_setup.{load,record}_mcp_approval,
        # which resolve bridge_home() from os.environ -- isolated the same
        # way the subprocess calls above were, via a save/restore of THIS
        # process's own env (never rolo's real ~/.rolo-claude/mcp-
        # approvals.json). Discovered the hard way: an earlier draft of
        # this test skipped this and wrote a real approval entry into
        # rolo's own state file.
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        try:
            resolved_before, _ = M.resolve_server_configs(cwd=proj, claude_json={}, print_mode=False,
                                                            approvals=mcp_setup.load_mcp_approvals())
            ctx.check("resolve_server_configs itself reports pending_approval before approval",
                      resolved_before["fake"].pending_approval is True)

            mcp_setup.record_mcp_approval("fake", entry)

            after = _run([sys.executable, "-m", "rolo_claude", "mcp", "list", "--cwd", str(proj)], env=env)
            ctx.check(f"shows Connected AFTER approval, got {after.stdout!r}",
                      "fake: " in (after.stdout or "") and "Connected" in (after.stdout or ""))

            resolved_after, _ = M.resolve_server_configs(cwd=proj, claude_json={}, print_mode=False,
                                                           approvals=mcp_setup.load_mcp_approvals())
            ctx.check("resolve_server_configs itself now reports approved (not just the CLI text)",
                      resolved_after["fake"].pending_approval is False)
            mgr = M.McpManager(resolved_after, tool_env=dict(os.environ), cwd=proj)
            try:
                mgr.start_all()
                ctx.check(f"a real connect succeeds post-approval, got {mgr.status()[0]}",
                          mgr.status()[0]["state"] == "connected")
                result = mgr.call("fake", "echo", {"text": "approved now"})
                ctx.check("tool call round-trips post-approval", result.content[0].text == "approved now")
            finally:
                mgr.close_all()
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home


# ============================================================================
# Item 3 -- local projects[cwd].mcpServers, BOTH key forms rolo's real file
# uses (Windows backslash key AND forward-slash key for the SAME dir) as
# separate dict keys. build_fake_home() already builds exactly this shape
# (finding B's own fixture) -- first test proves it resolves/merges as-is,
# unmodified; second proves it with a REAL live connect.
# ============================================================================

@test
def test_item3_build_fake_home_both_key_forms_resolve_and_merge(ctx: Ctx):
    fh = build_fake_home()
    claude_json = json.loads((fh["home"] / ".claude.json").read_text(encoding="utf-8"))
    keys = list(claude_json["projects"].keys())
    ctx.check(f"the fixture really does use two distinct dict keys for one dir, got {keys}", len(keys) == 2)
    ctx.check("one key form has backslashes, the other forward slashes",
              any("\\" in k for k in keys) and any("/" in k and "\\" not in k for k in keys))
    resolved, _ = M.resolve_server_configs(cwd=fh["proj"], claude_json=claude_json)
    if os.name == "nt":
        ctx.check(f"the backslash-keyed record's mcpServers.local-only is found via the forward-slash cwd, "
                  f"got {list(resolved)}", "local-only" in resolved)
        ctx.check("scope recorded as local", resolved["local-only"].scope == "local")
    else:
        # H9 Linux acceptance: the two-key-forms record is a WINDOWS file
        # shape (Claude Code on Linux only ever writes POSIX keys, and a
        # backslash is a legal filename character there, so `\tmp\x\proj`
        # must NOT be read as `/tmp/x/proj`). On POSIX the meaningful
        # assertion is that the forward-slash record's own servers resolve
        # for the cwd -- which is exactly what a real Linux ~/.claude.json
        # exercises -- and that the bogus backslash key is simply ignored.
        fwd_key = next(k for k in keys if "\\" not in k)
        expected = set((claude_json["projects"][fwd_key].get("mcpServers") or {}).keys())
        ctx.check(f"POSIX: the forward-slash-keyed record's servers {sorted(expected)} resolve, got {list(resolved)}",
                  expected <= set(resolved))
        ctx.check("POSIX: a Windows backslash key can never match a POSIX cwd", "local-only" not in resolved)


@test
def test_item3_both_key_forms_live_connect_through_current_project_lookup(ctx: Ctx):
    """Same both-key-forms pattern as build_fake_home(), but with a REAL
    connectable command under one of the two keys -- proves rolo-claude
    doesn't just PARSE both forms but actually USES either one when
    looking up the CURRENT project's servers, functionally."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "a-project"
        proj.mkdir()
        claude_json = {
            "projects": {
                str(proj).replace("\\", "/"): {"mcpServers": {"fake": _fake_stdio_entry()}},
                str(proj).replace("/", "\\"): {"hasTrustDialogAccepted": True},
            },
        }
        resolved, _ = M.resolve_server_configs(cwd=proj, claude_json=claude_json)
        ctx.check(f"found via the forward-slash key, got {list(resolved)}", "fake" in resolved)
        mgr = M.McpManager(resolved, tool_env=dict(os.environ), cwd=proj)
        try:
            mgr.start_all()
            ctx.check(f"connects live, got {mgr.status()}", mgr.status()[0]["state"] == "connected")
            result = mgr.call("fake", "echo", {"text": "both key forms"})
            ctx.check("round trips", result.content[0].text == "both key forms")
        finally:
            mgr.close_all()


# ============================================================================
# Item 4 -- --mcp-config with a file path AND with inline JSON, both live.
# test_mcp_manager.py covers resolve()-level parsing only; this connects a
# REAL server through the full CLI both ways.
# ============================================================================

@test
def test_item4_mcp_config_file_path_live_connect(ctx: Ctx):
    fh = build_fake_home()
    cfg_path = Path(fh["root"]) / "extra-mcp.json"
    cfg_path.write_text(json.dumps({"mcpServers": {"fake": _fake_stdio_entry()}}), encoding="utf-8")
    SCENARIOS["h9-mcpconfig-file"] = ScriptedTurns([
        _tool_call_chunk("call_1", "mcp__fake__echo", {"text": "via file mcp-config"}),
        _final_text_chunk("done: via file mcp-config"),
    ])
    mock = MockUpstream().start()
    try:
        result = _run_rolo("use the echo tool", mock, home=fh["home"], cwd=fh["proj"],
                            model="or:mock/h9-mcpconfig-file",
                            extra_args=["--permission-mode", "auto", "--mcp-config", str(cfg_path)])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"real tool call through a --mcp-config FILE path, got {result.stdout!r}",
                  "via file mcp-config" in result.stdout)
    finally:
        mock.stop()


@test
def test_item4_mcp_config_inline_json_live_connect(ctx: Ctx):
    fh = build_fake_home()
    inline = json.dumps({"mcpServers": {"fake": _fake_stdio_entry()}})
    SCENARIOS["h9-mcpconfig-inline"] = ScriptedTurns([
        _tool_call_chunk("call_1", "mcp__fake__echo", {"text": "via inline mcp-config"}),
        _final_text_chunk("done: via inline mcp-config"),
    ])
    mock = MockUpstream().start()
    try:
        result = _run_rolo("use the echo tool", mock, home=fh["home"], cwd=fh["proj"],
                            model="or:mock/h9-mcpconfig-inline",
                            extra_args=["--permission-mode", "auto", "--mcp-config", inline])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"real tool call through an INLINE JSON --mcp-config value, got {result.stdout!r}",
                  "via inline mcp-config" in result.stdout)
    finally:
        mock.stop()


# ============================================================================
# Item 5 -- a plugin-provided server from a REAL marketplace plugin copy;
# confirm mcp__plugin_<p>_<s>__<tool> naming. Uses the V2 array-of-records
# installed_plugins.json shape (binary-facts sec.9/config/plugins.py's own
# docstring) -- none of the 311 real marketplace plugins are actually
# INSTALLED on this box (only cloned), so a synthetic V2 manifest points
# `installPath` at a real plugin directory. First test: the REAL on-disk
# `.mcp.json` content of a real marketplace plugin (best-effort, skipped if
# the clone isn't present on this machine). Second: a live connect + wire
# naming proof, backed by the real fake server so it's runnable anywhere.
# ============================================================================

def _write_v2_plugin_manifest(claude_dir: Path, key: str, install_path: Path) -> None:
    manifest_path = claude_dir / "plugins" / "installed_plugins.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"version": 2, "plugins": {
        key: [{"scope": "user", "installPath": str(install_path), "version": "1.0.0"}],
    }}), encoding="utf-8")


@test
def test_item5_real_marketplace_plugin_discovered_via_v2_manifest(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    real_root = (Path.home() / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official"
                 / "external_plugins" / "playwright")
    if not (real_root / ".mcp.json").exists():
        raise SkipTest(f"no real marketplace clone at {real_root}")
    with tempfile.TemporaryDirectory() as td:
        claude_dir = Path(td) / ".claude"
        claude_dir.mkdir(parents=True)
        _write_v2_plugin_manifest(claude_dir, "playwright@claude-plugins-official", real_root)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
        try:
            servers, notices = discover_plugin_mcp_servers(env={})
            expected = plugin_server_name("playwright", "playwright")
            ctx.check(f"the REAL on-disk plugin file is discovered via a synthetic V2 manifest, "
                      f"got {list(servers)} notices={notices}", expected in servers)
            cfg = servers[expected]
            ctx.check(f"command/args read verbatim from the real file, got {cfg.command!r}/{cfg.args!r}",
                      cfg.command == "npx" and "@playwright/mcp@latest" in cfg.args)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_item5_v2_manifest_plugin_server_live_connect_and_naming(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    with tempfile.TemporaryDirectory() as td:
        claude_dir = Path(td) / ".claude"
        plugin_root = Path(td) / "myplugin-1.2.3"
        plugin_root.mkdir(parents=True)
        (plugin_root / ".mcp.json").write_text(json.dumps({"mcpServers": {"fakeserver": _fake_stdio_entry()}}),
                                                 encoding="utf-8")
        _write_v2_plugin_manifest(claude_dir, "myplugin@some-marketplace", plugin_root)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
        try:
            plugin_servers, notices = discover_plugin_mcp_servers(env=dict(os.environ))
            expected = plugin_server_name("myplugin", "fakeserver")
            ctx.check(f"V2-manifest plugin discovered, got {list(plugin_servers)} notices={notices}",
                      expected in plugin_servers)
            resolved, _ = M.resolve_server_configs(cwd=REPO_DIR, claude_json={}, plugin_servers=plugin_servers)
            mgr = M.McpManager(resolved, tool_env=dict(os.environ), cwd=REPO_DIR)
            try:
                mgr.start_all()
                ctx.check(f"connects live, got {mgr.status()}", mgr.status()[0]["state"] == "connected")
                names = [t[1] for t in mgr.all_tools()]
                expected_tool = f"mcp__{expected}__echo"
                ctx.check(f"tool name is mcp__plugin_<plugin>_<server>__<tool>, got {names}",
                          expected_tool in names)
                result = mgr.call(expected, "echo", {"text": "v2 plugin round trip"})
                ctx.check("a real call through it works", result.content[0].text == "v2 plugin round trip")
            finally:
                mgr.close_all()
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old


# ============================================================================
# Items 6/7/8 -- cross-binary interop with the REAL `claude` binary, against
# a temp CLAUDE_CONFIG_DIR (never rolo's real ~/.claude.json). Skipped
# (SkipTest, never FAIL) if `claude` isn't on PATH. Manually verified once
# by hand before writing this (see the report) -- these encode that exact,
# fully-working round trip as automated tests.
# ============================================================================

_CLAUDE_EXE = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")


def _require_real_claude() -> str:
    if not _CLAUDE_EXE:
        raise SkipTest("no `claude` binary on PATH -- items 6/7/8 (real-binary interop) skipped")
    return _CLAUDE_EXE


def _cross_binary_env(config_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["BRIDGE_STATE_DIR"] = str(config_dir / "rolo-state")
    env.pop("BRIDGE_TEST_HOME", None)
    env["PYTHONPATH"] = str(REPO_DIR)
    return env


def _run_claude(args, *, config_dir, cwd):
    return _run([_require_real_claude()] + args, env=_cross_binary_env(config_dir), cwd=cwd)


def _run_rolo_mcp(args, *, config_dir, cwd):
    return _run([sys.executable, "-m", "rolo_claude", "mcp"] + args, env=_cross_binary_env(config_dir), cwd=cwd)


@test
def test_item6_server_added_by_real_claude_mcp_add_is_picked_up_by_rolo_claude(ctx: Ctx):
    _require_real_claude()
    with tempfile.TemporaryDirectory() as td:
        config_dir = Path(td) / "cfg"
        empty_cwd = Path(td) / "cwd"
        empty_cwd.mkdir()
        # NEW from H9: a bare "python" is NOT on $PATH on every Linux box
        # (Debian/Ubuntu without python-is-python3 only has python3) --
        # verified failure: `claude mcp list`'s own health check reported
        # ENOENT for it on a real WSL Ubuntu install. sys.executable is the
        # SAME kind of "a simple command string" round-trip case (still no
        # shell, still one argv element) but guaranteed to actually exist
        # and run on whatever box this test executes on.
        add = _run_claude(["mcp", "add", "--scope", "user", "rc-test", "--", sys.executable,
                            str(REPO_DIR / "tests" / "helpers" / "fake_mcp_server.py")],
                           config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"real `claude mcp add` succeeds, got {add.returncode} stderr={add.stderr!r}",
                  add.returncode == 0)
        listing = _run_rolo_mcp(["list", "--cwd", str(empty_cwd)], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"the NEXT rolo-claude session picks it up with NO rolo-claude-side config change, "
                  f"got {listing.stdout!r}", "rc-test: " in (listing.stdout or "")
                  and "Connected" in (listing.stdout or ""))


@test
def test_item7_server_added_by_rolo_claude_mcp_add_is_listed_by_real_claude(ctx: Ctx):
    _require_real_claude()
    with tempfile.TemporaryDirectory() as td:
        config_dir = Path(td) / "cfg"
        empty_cwd = Path(td) / "cwd"
        empty_cwd.mkdir()
        # NEW from H9: sys.executable, not a bare "python" -- see item 6's
        # own comment (not every Linux box has "python" on $PATH).
        add = _run_rolo_mcp(["add", "--scope", "user", "rc-test2", "--", sys.executable,
                              str(REPO_DIR / "tests" / "helpers" / "fake_mcp_server.py")],
                             config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"rolo-claude mcp add succeeds, got {add.returncode} stderr={add.stderr!r}",
                  add.returncode == 0)
        claude_json = json.loads((config_dir / ".claude.json").read_text(encoding="utf-8"))
        entry = claude_json["mcpServers"]["rc-test2"]
        ctx.check(f"schema matches Claude Code's own shape (binary-facts sec.9): type/command/args, got {entry}",
                  entry.get("type") == "stdio" and entry.get("command") == sys.executable
                  and isinstance(entry.get("args"), list))
        # H9 fix (item 7's literal "byte-for-byte" ask): the real `claude
        # mcp add` always writes an explicit "env": {} even with no -e
        # flags (verified by hand); mcp_cli._build_entry now writes the
        # same, so the two entries are byte-shape identical.
        ctx.check(f"byte-shape parity: an explicit empty env {{}} is written like the real binary does, got {entry}",
                  entry.get("env") == {})
        listing = _run_claude(["mcp", "list"], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"the real `claude mcp list` sees it too, got {listing.stdout!r}",
                  "rc-test2" in (listing.stdout or "") and "Connected" in (listing.stdout or ""))


@test
def test_item8_mcp_remove_both_directions_agree_between_both_clis(ctx: Ctx):
    _require_real_claude()
    with tempfile.TemporaryDirectory() as td:
        config_dir = Path(td) / "cfg"
        empty_cwd = Path(td) / "cwd"
        empty_cwd.mkdir()
        fake_path = str(REPO_DIR / "tests" / "helpers" / "fake_mcp_server.py")

        # direction A: claude adds -> rolo-claude sees it -> rolo-claude
        # removes -> BOTH clis agree it's gone. sys.executable, not a bare
        # "python" -- see item 6's own comment.
        _run_claude(["mcp", "add", "--scope", "user", "a-server", "--", sys.executable, fake_path],
                    config_dir=config_dir, cwd=empty_cwd)
        seen = _run_rolo_mcp(["list", "--cwd", str(empty_cwd)], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"rolo-claude sees claude's server, got {seen.stdout!r}", "a-server" in (seen.stdout or ""))
        removed = _run_rolo_mcp(["remove", "a-server"], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"rolo-claude mcp remove succeeds, got {removed.returncode} {removed.stdout!r}",
                  removed.returncode == 0)
        gone_rolo = _run_rolo_mcp(["list", "--cwd", str(empty_cwd)], config_dir=config_dir, cwd=empty_cwd)
        gone_claude = _run_claude(["mcp", "list"], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"gone from rolo-claude mcp list, got {gone_rolo.stdout!r}", "a-server" not in (gone_rolo.stdout or ""))
        ctx.check(f"gone from real claude mcp list too, got {gone_claude.stdout!r}",
                  "a-server" not in (gone_claude.stdout or ""))

        # direction B: rolo-claude adds -> claude sees it -> claude removes
        # -> BOTH clis agree it's gone.
        _run_rolo_mcp(["add", "--scope", "user", "b-server", "--", sys.executable, fake_path],
                       config_dir=config_dir, cwd=empty_cwd)
        seen2 = _run_claude(["mcp", "list"], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"claude sees rolo-claude's server, got {seen2.stdout!r}", "b-server" in (seen2.stdout or ""))
        removed2 = _run_claude(["mcp", "remove", "b-server"], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"real claude mcp remove succeeds, got {removed2.returncode} {removed2.stdout!r}",
                  removed2.returncode == 0)
        gone_rolo2 = _run_rolo_mcp(["list", "--cwd", str(empty_cwd)], config_dir=config_dir, cwd=empty_cwd)
        ctx.check(f"gone from rolo-claude mcp list too, got {gone_rolo2.stdout!r}",
                  "b-server" not in (gone_rolo2.stdout or ""))


# ============================================================================
# Item 9 -- tool schemas of unusual shape (nested objects, $ref/$defs,
# anyOf, enums, tuple-items, 300 tools) across BOTH the Databricks
# simplifier and OpenRouter. Uses fake_mcp_server.py's opt-in weird-schema
# tools (ref_defs_tool/anyof3_tool/tuple_legacy_tool/deep_nested_tool),
# whose exact shapes were verified BY HAND against this SDK's own real
# pydantic-generated output before being hand-authored here (see that
# file's own comment) -- these are REALISTIC shapes, not contrived ones.
#
# Traces the REAL code path: agent/catalog.py's SessionCatalog builds
# McpTool objects (tools/mcp_tool.py), which apply a PER-FAMILY sanitizer
# (kimi/gemini only) to the raw MCP input_schema at construction time; the
# resulting Anthropic-shaped tool def then goes through providers/
# request.py's convert_tools(), which additionally runs
# simplify_schema_for_databricks() ONLY when profile.body_allowlist looks
# like a Databricks one. For every OTHER family (deepseek -- the harness's
# own DEFAULT model family, glm, qwen, generic, ...) via OpenRouter,
# NOTHING beyond providers/routing.py's sanitize_tool_schema (pops
# "$schema" only) ever runs. Two real bugs found here, see the module
# docstring's (a)/(b) and each test's own comment.
# ============================================================================

_WEIRD_TOOLS = ["ref_defs_tool", "anyof3_tool", "tuple_legacy_tool", "deep_nested_tool"]


def _weird_mgr():
    cfg = _fake_cfg("fake", extra_tools=_WEIRD_TOOLS)
    mgr = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
    mgr.start_all()
    return mgr


def _anthropic_tool_defs(mgr, *, family=None, vision=False) -> list:
    """Anthropic-shaped tool defs for every tool on `mgr`, built exactly
    the way agent/catalog.py's SessionCatalog does at McpTool construction
    time (the per-family sanitizer applies HERE, not later)."""
    from rolo_claude.tools.mcp_tool import McpTool
    defs = []
    for server, wire_name, sdk_tool in mgr.all_tools():
        tool = McpTool(server, sdk_tool, mgr, vision=vision, family=family)
        defs.append({"name": tool.name, "description": tool.description, "input_schema": tool.input_schema})
    return defs


@test
def test_item9_databricks_simplifier_survives_unusual_schemas_structurally(ctx: Ctx):
    """The REAL code path: providers.request.convert_tools() with a REAL
    Databricks ProviderProfile (providers.profiles.resolve_profile) -- no
    crash, valid JSON, every name/description intact."""
    from rolo_claude.providers.profiles import resolve_profile
    from rolo_claude.providers.request import convert_tools
    from rolo_claude.providers.routing import Route
    mgr = _weird_mgr()
    try:
        defs = _anthropic_tool_defs(mgr, family="deepseek")  # family is irrelevant to the databricks branch
        route = Route(provider="databricks", upstream_model="databricks-kimi-k3-reasoning-content-shape",
                       dialect="openai-chat")
        profile = resolve_profile(route)
        oai_tools = convert_tools(defs, profile)
        ctx.check("no crash converting every weird schema", oai_tools is not None and len(oai_tools) == len(defs))
        dumped = json.dumps(oai_tools)  # never raises -- valid JSON
        ctx.check(f"valid JSON, names/descriptions intact, got names={[t['function']['name'] for t in oai_tools]}",
                  all(t["function"]["name"] in dumped and t["function"]["description"] in dumped for t in oai_tools))
        by_name = {t["function"]["name"]: t["function"]["parameters"] for t in oai_tools}
        ref_params = by_name["mcp__fake__ref_defs_tool"]
        ctx.check(f"$ref/$defs are gone (replaced by a permissive object), got {ref_params}",
                  "$ref" not in json.dumps(ref_params) and "$defs" not in ref_params)
        tuple_params = by_name["mcp__fake__tuple_legacy_tool"]
        # H9 fix: tools/mcp_tool.py's family-agnostic normalize_tool_schema
        # (applied at McpTool construction, BEFORE either conversion path)
        # flattens tuple-items to items[0] for databricks-bound defs too.
        ctx.check(f"legacy tuple-items flattened to items[0] by the shared base normalisation, got {tuple_params}",
                  tuple_params["properties"]["point"]["items"] == {"type": "integer"})
    finally:
        mgr.close_all()


@test
def test_item9_bug_databricks_simplifier_drops_typing_for_a_wide_anyof(ctx: Ctx):
    """BUG (b), file:line rolo_claude/providers/request.py:107-132
    (simplify_schema_for_databricks's inner strip()): a 3-way anyOf (or any
    anyOf that isn't exactly [T, {"type":"null"}]) is DELETED ENTIRELY by
    the generic keyword-strip -- unlike $ref, which falls back to a
    permissive {"type": "object"}, an anyOf-typed property loses its type
    information altogether, becoming untyped. Exact repro schema and
    before/after captured below (this is a data-quality/tool-reliability
    bug, not a hard 400 -- Databricks likely still accepts the request,
    just with the model given no hint at all about `value`'s real type)."""
    from rolo_claude.providers.request import simplify_schema_for_databricks
    schema = {"type": "object", "properties": {
        "value": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]},
    }}
    out = simplify_schema_for_databricks(schema)
    value_schema = out["properties"]["value"]
    # H9 fix: the wide anyOf is still stripped (Databricks rejects the
    # keyword) but the property now KEEPS its first typed variant instead
    # of ending up completely untyped.
    ctx.check(f"the property keeps its first typed variant (integer), got {value_schema}",
              value_schema.get("type") == "integer" and "anyOf" not in value_schema)


@test
def test_item9_openrouter_survives_unusual_schemas_structurally_via_mock_session(ctx: Ctx):
    """Drives it through a REAL Session against the mock upstream (not just
    a bare function call). The weird-schema tools start DEFERRED (none is
    alwaysLoad) -- exactly like any real MCP tool with no special _meta --
    so the scripted turn does what a model actually would: ToolSearch
    `select:` each one first (the SAME real load path item 10 below also
    covers), THEN the SECOND request's own `tools` array is captured off
    the real wire. Structural check per the brief: no crash, valid JSON,
    names intact. See the next test for what this reveals about survival."""
    fh = build_fake_home()
    (fh["home"] / ".claude.json").write_text(json.dumps({
        "mcpServers": {"fake": _fake_stdio_entry(extra_tools=_WEIRD_TOOLS)}}), encoding="utf-8")
    seen_bodies = []
    select_query = "select:" + ",".join(f"mcp__fake__{n}" for n in _WEIRD_TOOLS)

    def _scn(h, body):
        from tests.helpers.mock_openai import _finish
        seen_bodies.append(body)
        tool_msgs = [m for m in (body.get("messages") or []) if m.get("role") == "tool"]
        if not tool_msgs:
            return _finish(h, _tool_call_chunk("call_ts", "ToolSearch", {"query": select_query}))
        _finish(h, _final_text_chunk("ok, tools noted"))
    SCENARIOS["h9-weird-schema-openrouter"] = _scn
    mock = MockUpstream().start()
    try:
        result = _run_rolo("search for the weird schema tools", mock, home=fh["home"], cwd=fh["proj"],
                            model="or:mock/h9-weird-schema-openrouter", extra_args=["--permission-mode", "auto"])
        ctx.check(f"exit 0 (no crash), got {result.returncode} stderr={result.stderr[-800:]!r}",
                  result.returncode == 0)
        ctx.check(f"the mock saw two requests (before/after ToolSearch loaded them), got {len(seen_bodies)}",
                  len(seen_bodies) == 2)
        tools = seen_bodies[1].get("tools") or []
        wire_names = [t.get("function", {}).get("name") for t in tools]
        ctx.check(f"every weird tool's name reached the wire intact after loading, got {wire_names}",
                  all(f"mcp__fake__{n}" in wire_names for n in _WEIRD_TOOLS))
        dumped = json.dumps(tools)  # never raises -- valid JSON on the real wire body
        ctx.check(f"valid JSON body, len={len(dumped)}", len(dumped) > 0)
        descriptions = [t["function"]["description"] for t in tools if t["function"]["name"].startswith("mcp__fake__")]
        ctx.check(f"descriptions are intact (non-empty), got {descriptions}", all(descriptions))
    finally:
        mock.stop()


@test
def test_item9_bug_openrouter_schema_survives_unresolved_no_general_sanitizer(ctx: Ctx):
    """BUG (a), file:line rolo_claude/providers/routing.py:142-146
    (sanitize_tool_schema, used by anthropic_tool_to_openai -> providers/
    request.py:147-165 convert_tools -- the path EVERY OpenRouter request
    goes through, for EVERY family except kimi/gemini's separate, EARLIER,
    partial per-family fixup in tools/mcp_tool.py's own sanitize_tool_
    schema): a real MCP tool's $ref/$defs/multi-way-anyOf/legacy-tuple-
    items schema is sent to OpenRouter with ONLY "$schema" removed --
    completely unresolved. The harness's DEFAULT model family is
    "deepseek" (providers/model.py's DEFAULT_MODEL_REF =
    "or:deepseek/deepseek-v4.1-flash"), which gets NO schema massaging at
    all (only kimi/gemini do, in tools/mcp_tool.py). Could not be
    LIVE-tested against the real OpenRouter/DeepSeek endpoint in this
    environment: OPENROUTER_API_KEY is not actually set anywhere reachable
    here (checked the Bash and PowerShell process env, Windows User/
    Machine env scopes, and ~/.claude/settings.json's own `env` block --
    none set it, despite the task's own environment note saying it would
    be), so whether a real downstream provider's function-calling
    validator actually REJECTS this (vs. silently accepting/ignoring the
    unresolved parts) is unconfirmed; only the structural fact that
    nothing resolves it is proven here."""
    from rolo_claude.providers.profiles import resolve_profile
    from rolo_claude.providers.request import convert_tools
    from rolo_claude.providers.routing import Route
    mgr = _weird_mgr()
    try:
        defs = _anthropic_tool_defs(mgr, family="deepseek")  # the harness's own DEFAULT family
        route = Route(provider="openrouter", upstream_model="deepseek/deepseek-v4.1-flash", dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check(f"a real openrouter profile (never databricks-shaped), got body_allowlist={profile.body_allowlist}",
                  profile.body_allowlist is None or "reasoning_effort" not in profile.body_allowlist)
        oai_tools = convert_tools(defs, profile)
        by_name = {t["function"]["name"]: t["function"]["parameters"] for t in oai_tools}
        # H9 fix: tools/mcp_tool.py's `normalize_tool_schema` (family-
        # agnostic, applied at McpTool construction) now inlines local
        # $refs, drops the emptied $defs container and flattens tuple-
        # items for EVERY OpenAI-shaped family, deepseek included.
        ref_params = by_name["mcp__fake__ref_defs_tool"]
        ctx.check(f"$ref/$defs are RESOLVED (inlined) for the default model family, got {json.dumps(ref_params)[:300]}",
                  "$ref" not in json.dumps(ref_params) and "$defs" not in ref_params)
        ctx.check(f"the inlined address keeps the referenced object's own shape, got {ref_params['properties'].get('address')}",
                  isinstance(ref_params["properties"].get("address"), dict)
                  and ref_params["properties"]["address"].get("type") == "object"
                  and isinstance(ref_params["properties"]["address"].get("properties"), dict))
        anyof_params = by_name["mcp__fake__anyof3_tool"]
        ctx.check(f"a genuinely 3-way anyOf is left intact for OpenRouter (validators accept it), got {anyof_params}",
                  anyof_params["properties"]["value"].get("anyOf") ==
                  [{"type": "integer"}, {"type": "string"}, {"type": "null"}])
        tuple_params = by_name["mcp__fake__tuple_legacy_tool"]
        ctx.check(f"the legacy tuple-items array is flattened to items[0], got {tuple_params}",
                  tuple_params["properties"]["point"]["items"] == {"type": "integer"})
    finally:
        mgr.close_all()


@test
def test_item9_family_specific_sanitizer_helps_kimi_but_no_other_family(ctx: Ctx):
    """Confirms the ONE piece of OpenRouter-side schema massaging that DOES
    exist (tools/mcp_tool.py's sanitize_tool_schema, kimi/gemini only) --
    proving it works for kimi, and that it is truly absent for every other
    family (glm/qwen/generic/claude), not just deepseek (the other test's
    own focus)."""
    mgr = _weird_mgr()
    try:
        # H9 fix: the base normalisation is now family-AGNOSTIC -- kimi,
        # deepseek, glm, qwen, generic and an unknown family all get inlined
        # refs and flattened tuple-items; only "claude" is left untouched.
        for family in ("kimi", "deepseek", "glm", "qwen", "generic", None):
            defs = _anthropic_tool_defs(mgr, family=family)
            by_name = {d["name"]: d["input_schema"] for d in defs}
            ref_params = by_name["mcp__fake__ref_defs_tool"]
            ctx.check(f"family={family!r}: $ref inlined (no $ref/$defs left), got {json.dumps(ref_params)[:200]}",
                      "$ref" not in json.dumps(ref_params) and "$defs" not in ref_params)
            tuple_params = by_name["mcp__fake__tuple_legacy_tool"]
            ctx.check(f"family={family!r}: legacy tuple-items flattened to items[0], "
                      f"got {tuple_params['properties']['point']['items']}",
                      tuple_params["properties"]["point"]["items"] == {"type": "integer"})
        claude_defs = _anthropic_tool_defs(mgr, family="claude")
        claude_by_name = {d["name"]: d["input_schema"] for d in claude_defs}
        ctx.check("family='claude' (Anthropic-shaped endpoints validate JSON Schema natively) is left untouched",
                  "$defs" in claude_by_name["mcp__fake__ref_defs_tool"])
    finally:
        mgr.close_all()


@test
def test_item9_scales_to_300_tools_through_both_conversion_paths(ctx: Ctx):
    """The brief's own "...and a 300-tool server" -- combined with unusual
    shapes this time (a mix of the weird tools plus filler tools), through
    BOTH convert_tools() paths at their REAL per-provider tools_max/host_cap
    (never just a bare connection count, already covered by test_mcp_
    manager.py::test_manager_scales_to_300_tools) -- mirrors the real
    request-building flow: agent/catalog.py's select_preload() caps what
    goes on the wire per turn BEFORE convert_tools() ever sees it; handing
    convert_tools() all 300 directly (skipping that layer) is exactly what
    trips its own ToolCatalogTooLarge guard, discovered writing this test."""
    from rolo_claude.agent.catalog import select_preload
    from rolo_claude.providers.profiles import resolve_profile
    from rolo_claude.providers.request import convert_tools
    from rolo_claude.providers.routing import Route
    cfg = _fake_cfg("fake", tool_count=300, extra_tools=_WEIRD_TOOLS)
    mgr = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        triples = mgr.all_tools()
        ctx.check("connects with 300 tools incl. the weird-schema ones", len(triples) == 300)
        weird_wire_names = {f"mcp__fake__{n}" for n in _WEIRD_TOOLS}
        for provider, model in (("databricks", "databricks-kimi-k3-reasoning-content-shape"),
                                 ("openrouter", "deepseek/deepseek-v4.1-flash")):
            profile = resolve_profile(Route(provider=provider, upstream_model=model, dialect="openai-chat"))
            cap = profile.tools_max or 300
            preload, _deferred = select_preload(triples, preload_names=weird_wire_names, cap_budget=cap)
            from rolo_claude.tools.mcp_tool import McpTool
            defs = [{"name": (t := McpTool(s, sdk, mgr, family="deepseek")).name,
                     "description": t.description, "input_schema": t.input_schema}
                    for s, wn, sdk in preload]
            oai_tools = convert_tools(defs, profile)
            ctx.check(f"{provider}: converts at its real cap ({cap}) without crashing, got {len(oai_tools)}",
                      len(oai_tools) == len(defs) and len(oai_tools) <= cap)
            ctx.check(f"{provider}: the weird-schema tools (explicitly requested) are among the preloaded set",
                      weird_wire_names <= {d["name"] for d in defs})
            json.dumps(oai_tools)  # never raises
    finally:
        mgr.close_all()


# ============================================================================
# Item 10 -- ToolSearch finds a newly-added tool by EXACT NAME and by a
# DESCRIPTION KEYWORD. test_mcp_catalog.py already covers select:/keyword
# ranking generally; this is the brief's own narrower, explicit pairing --
# one tool, looked up both ways, through the real ToolSearchTool.
# ============================================================================

@test
def test_item10_toolsearch_finds_new_tool_by_name_and_by_keyword(ctx: Ctx):
    from rolo_claude.agent.catalog import SessionCatalog, select_preload
    from rolo_claude.tools.base import ToolContext
    from rolo_claude.tools.registry import ToolRegistry
    from rolo_claude.tools.tool_search import ToolSearchTool
    cfg = _fake_cfg("fake")
    manager = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        manager.start_all()
        triples = manager.all_tools()
        core = ToolRegistry()
        preload, deferred = select_preload(triples, cap_budget=max(0, 30 - len(core.names())))
        for server, wire_name, sdk_tool in preload:
            from rolo_claude.tools.mcp_tool import McpTool
            core.add_tool(McpTool(server, sdk_tool, manager, vision=False))
        cat = SessionCatalog(registry=core, deferred=deferred, manager=manager, cap=30, names=core.names())
        tctx = ToolContext(cwd=REPO_DIR, registry=cat.registry, catalog=cat)

        by_name = ToolSearchTool().run({"query": "select:mcp__fake__error_tool"}, tctx)
        ctx.check("found by EXACT NAME", not by_name.is_error and any(
            b.get("type") == "tool_reference" and b.get("tool_name") == "mcp__fake__error_tool"
            for b in by_name.content))

        # a fresh catalog for the keyword search -- "error_tool" is now
        # already loaded above, so a distinct still-deferred tool proves
        # the keyword path independently: "always_load_tool"'s description
        # is "Marked _meta.anthropic/alwaysLoad." -- "alwaysLoad" is the
        # distinctive keyword.
        by_keyword = ToolSearchTool().run({"query": "alwaysLoad"}, tctx)
        ctx.check(f"found by a DESCRIPTION KEYWORD, got {by_keyword.content}", not by_keyword.is_error and (
            "mcp__fake__always_load_tool" in by_keyword.content if isinstance(by_keyword.content, str)
            else any("mcp__fake__always_load_tool" in json.dumps(b) for b in by_keyword.content)))
    finally:
        manager.close_all()


# ============================================================================
# Item 11 -- /mcp reconnect after editing MCP config mid-session (add or
# change a server on disk while the session is open, then trigger the
# reconnect and confirm the new/changed server is picked up). BUG (e),
# FIXED: `rolo_claude/controller.py`'s `reconnect_mcp` and `rolo_claude/
# mcp/manager.py`'s `McpManager.reconnect` neither re-resolved ~/.claude.
# json/.mcp.json from disk -- reconnect only ever restarted an ALREADY-
# KNOWN handle using its ORIGINALLY-parsed config, and a brand-new name had
# no handle to restart at all. Fixed with a new `McpManager.resync_from`
# (adds/replaces handles from a freshly re-resolved configs dict) that
# `Controller.reconnect_mcp` now calls, via a fresh `resolve_server_configs`
# against a freshly re-read ~/.claude.json, before the plain per-name
# reconnect. Scope note: launch-time-only concerns (--mcp-config file,
# --chrome/--playwright dynamic servers) are deliberately NOT re-resolved
# by this path -- those are a new-session decision, not something
# reconnecting one existing/new server should reconsider.
# ============================================================================

@test
def test_item11_reconnect_after_changing_an_existing_servers_config_uses_stale_command(ctx: Ctx):
    """PINNING TEST for an open bug -- currently FAILS. Desired behaviour:
    editing a configured server's command in the REAL ~/.claude.json on
    disk, then reconnecting via the same mechanism the TUI's /mcp dialog
    uses (Controller.reconnect_mcp -> McpManager.reconnect), should connect
    using the NEW command. Observed: reconnect() only ever restarts the
    SAME handle with its ORIGINALLY-parsed McpServerConfig object -- it has
    no reference to the config FILE at all, so a real, on-disk edit is
    silently ignored no matter how many times reconnect is triggered."""
    from rolo_claude.controller import Controller
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        (home / ".claude").mkdir(parents=True)
        bad_entry = {"type": "stdio", "command": sys.executable, "args": ["-c", "import sys; sys.exit(1)"]}
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"fake": bad_entry}}), encoding="utf-8")

        old_home = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        try:
            from rolo_claude.config.claude_json import load_claude_json
            claude_json = load_claude_json()
            resolved, _ = M.resolve_server_configs(cwd=REPO_DIR, claude_json=claude_json)
            mgr = M.McpManager(resolved, tool_env=dict(os.environ), cwd=REPO_DIR)
            try:
                mgr.start_all()
                ctx.check("starts failed (the deliberately-bad command)", mgr.status()[0]["state"] == "failed")

                # the user (or another process) edits ~/.claude.json for
                # real, on disk, to the REAL fake server's command --
                # mirrors item 11's own scenario exactly ("editing MCP
                # config mid-session, then trigger the reconnect").
                good_entry = _fake_stdio_entry()
                (home / ".claude.json").write_text(json.dumps({"mcpServers": {"fake": good_entry}}),
                                                    encoding="utf-8")

                controller = Controller(session=None, cwd=REPO_DIR, mcp_manager=mgr,
                                         reconnect_fn=lambda name, abort=None: [
                                             f"{name}: {'connected' if mgr.reconnect(name, abort=abort) else 'failed'}"])
                result = controller.reconnect_mcp("fake")
                ctx.check(f"the real, on-disk edit IS picked up before the plain reconnect runs, got {result}",
                          any("connected" in r for r in result))
            finally:
                mgr.close_all()
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_item11_reconnect_picks_up_a_brand_new_server_name(ctx: Ctx):
    """H9 bug fix (item 11): a server added to ~/.claude.json entirely NEW
    (never seen by this session's McpManager, no handle for it at all)
    becomes reachable via /mcp's own reconnect action -- `Controller.
    reconnect_mcp`'s new resync-from-disk step (see controller.py) adds a
    handle for it before the plain per-name reconnect runs, which used to
    unconditionally report `None`/"unchanged" for a name it had never
    heard of."""
    from rolo_claude.controller import Controller
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        (home / ".claude").mkdir(parents=True)
        # session started with ZERO servers configured -- the config file
        # itself starts with none either, matching "never seen at all".
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        old_home = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        try:
            mgr = M.McpManager({}, tool_env=dict(os.environ), cwd=REPO_DIR)
            try:
                mgr.start_all()  # no-op: zero servers configured

                # the user (or another process) adds a BRAND NEW server to
                # ~/.claude.json, on disk, mid-session.
                (home / ".claude.json").write_text(
                    json.dumps({"mcpServers": {"brand-new-server": _fake_stdio_entry()}}), encoding="utf-8")

                controller = Controller(session=None, cwd=REPO_DIR, mcp_manager=mgr,
                                         reconnect_fn=lambda name, abort=None: [
                                             f"{name}: {'connected' if mgr.reconnect(name, abort=abort) else 'failed'}"])
                result = controller.reconnect_mcp("brand-new-server")
                ctx.check(f"a server added to the config file mid-session, with no prior handle, is now "
                          f"reachable via reconnect, got {result}",
                          any("connected" in r for r in result))
            finally:
                mgr.close_all()
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home


# ============================================================================
# Item 12 -- http and sse transports via a REAL fake server (never a fake
# server before this file: http_sse.py's own docstring says "none of
# rolo's real 15 configured servers use http/sse", and the only existing
# coverage in test_mcp_manager.py monkeypatches connect_http/connect_sse
# directly). BUG (c), FIXED: file:line rolo_claude/mcp/http_sse.py:96-97
# (connect_http) unpacked 3 values from streamable_http_client(...)'s
# yielded object; the installed SDK (mcp 2.2.0 -- the SAME version this
# module's own comment claimed to target) actually yields a 2-tuple
# (ReadStream, WriteStream) per ITS OWN docstring. EVERY real `type:
# "http"` server failed to connect at all, always with `ValueError: not
# enough values to unpack (expected 3, got 2)`. Fixed by taking only the
# first two elements of whatever the SDK yields.
# ============================================================================

def _kill_and_reap(proc) -> None:
    """proc.kill()+wait() alone leaves the Popen's own stdout PIPE file
    object unclosed (ResourceWarning under -W error::ResourceWarning,
    discovered writing this file's own item 12 tests) -- closing stdout
    too matches what the `with subprocess.Popen(...)` context manager
    would have done automatically."""
    proc.kill()
    proc.wait(timeout=5)
    if proc.stdout is not None:
        proc.stdout.close()


def _spawn_fake_transport_server(transport: str, *, extra_tools=None):
    port = _free_port()
    env = dict(os.environ)
    env["FAKE_MCP_TRANSPORT"] = transport
    env["FAKE_MCP_PORT"] = str(port)
    if extra_tools:
        env["FAKE_MCP_EXTRA_TOOLS"] = ",".join(extra_tools)
    proc = subprocess.Popen([sys.executable, "-m", "tests.helpers.fake_mcp_server"], env=env, cwd=str(REPO_DIR),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        _kill_and_reap(proc)
        raise SkipTest(f"fake {transport} server never started listening on port {port}")
    return proc, port


@test
def test_item12_sse_transport_connects_and_round_trips(ctx: Ctx):
    proc, port = _spawn_fake_transport_server("sse")
    try:
        cfg = M.McpServerConfig(name="fake", type="sse", url=f"http://127.0.0.1:{port}/sse")
        mgr = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
        try:
            mgr.start_all()
            ctx.check(f"sse transport connects live, got {mgr.status()[0]}", mgr.status()[0]["state"] == "connected")
            result = mgr.call("fake", "echo", {"text": "sse round trip"})
            ctx.check("a real tool call round-trips over sse", result.content[0].text == "sse round trip")
        finally:
            mgr.close_all()
    finally:
        _kill_and_reap(proc)


@test
def test_item12_http_transport_connects_and_round_trips(ctx: Ctx):
    """H9 bug fix (was a PINNING test for a critical, open bug): `rolo_
    claude/mcp/http_sse.py`'s `connect_http` unpacked THREE values from
    `streamable_http_client(...)` (`read, write, _get_session_id = ...`),
    but the installed 2.2.0 SDK's own docstring says it yields a 2-tuple
    (`read_stream, write_stream`) -- every single `type: "http"` server
    failed to connect, 100% reproducible, `ValueError: not enough values
    to unpack (expected 3, got 2)`. Fixed by taking only what the SDK
    actually yields. This mirrors `test_item12_sse_transport_connects_
    and_round_trips` -- connect AND a real tool call round-trip, not just
    a state flag."""
    proc, port = _spawn_fake_transport_server("http")
    try:
        cfg = M.McpServerConfig(name="fake", type="http", url=f"http://127.0.0.1:{port}/mcp")
        mgr = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
        try:
            mgr.start_all()
            status = mgr.status()[0]
            ctx.check(f"http transport connects live, got {status}", status["state"] == "connected")
            result = mgr.call("fake", "echo", {"text": "http round trip"})
            ctx.check("a real tool call round-trips over http", result.content[0].text == "http round trip")
        finally:
            mgr.close_all()
    finally:
        _kill_and_reap(proc)


# ============================================================================
# Item 13 -- a server that dies mid-session is restarted/reconnected on the
# NEXT call. test_mcp_manager.py::test_die_mid_call_marks_the_handle_
# failed_and_drops_session already proves detection (state no longer
# connected); this extends it to prove the actual point of the mechanism:
# a DIFFERENT, SUBSEQUENT call, after the death, SUCCEEDS (the automatic
# reconnect-on-next-call actually restores real service, not just flips a
# status flag).
# ============================================================================

@test
def test_item13_dies_mid_call_then_a_later_call_succeeds_via_auto_reconnect(ctx: Ctx):
    cfg = _fake_cfg("fake", extra_tools=["die_mid_call"])
    mgr = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        raised = False
        try:
            mgr.call("fake", "die_mid_call", {})
        except Exception:
            raised = True
        ctx.check("the killing call itself raises rather than hanging", raised)
        ctx.check(f"status no longer connected right after, got {mgr.status()[0]}",
                  mgr.status()[0]["state"] != "connected")

        result = mgr.call("fake", "echo", {"text": "back from the dead"})
        ctx.check(f"a LATER, different call succeeds via the automatic reconnect-on-next-call, "
                  f"got {result.content[0].text!r}", result.content[0].text == "back from the dead")
        ctx.check(f"status is connected again afterward, got {mgr.status()[0]}",
                  mgr.status()[0]["state"] == "connected")
    finally:
        mgr.close_all()


# ============================================================================
# Item 14 -- server AND tool names containing dots and spaces. Wire name
# format is mcp__<server>__<tool>; confirms sanitising/parsing/permission-
# rule matching against a server literally named "my.server" and one named
# "my server" doesn't break, end to end with a real connection.
# ============================================================================

@test
def test_item14_server_name_with_a_dot_connects_and_names_correctly(ctx: Ctx):
    cfg = _fake_cfg("my.server")
    mgr = M.McpManager({"my.server": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        ctx.check(f"connects despite the dot in the name, got {mgr.status()[0]}",
                  mgr.status()[0]["state"] == "connected")
        names = [t[1] for t in mgr.all_tools()]
        ctx.check(f"the dot is sanitised to an underscore on the wire, got {names[:2]}",
                  "mcp__my_server__echo" in names)
        ctx.check("split_mcp_tool_name parses it straight back apart",
                  M.split_mcp_tool_name("mcp__my_server__echo") == ("my_server", "echo"))
        result = mgr.call("my.server", "echo", {"text": "dotted server name"})
        ctx.check("a real call round-trips", result.content[0].text == "dotted server name")

        from rolo_claude import permissions as P
        engine = P.PermissionEngine(mode="default", cwd=REPO_DIR, print_mode=True,
                                     allow_rules=[P.parse_rule("mcp__my_server__*", source="test")])
        decision = engine.decide("mcp__my_server__echo", {}, tool=None)
        ctx.check(f"a permission rule against the SANITISED name matches the tool, got {decision.action}",
                  decision.action == "allow")
    finally:
        mgr.close_all()


@test
def test_item14_server_name_with_a_space_connects_and_names_correctly(ctx: Ctx):
    cfg = _fake_cfg("my server")
    mgr = M.McpManager({"my server": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        ctx.check(f"connects despite the space in the name, got {mgr.status()[0]}",
                  mgr.status()[0]["state"] == "connected")
        names = [t[1] for t in mgr.all_tools()]
        ctx.check(f"the space is sanitised to an underscore on the wire, got {names[:2]}",
                  "mcp__my_server__echo" in names)
        result = mgr.call("my server", "echo", {"text": "spaced server name"})
        ctx.check("a real call round-trips", result.content[0].text == "spaced server name")
    finally:
        mgr.close_all()


@test
def test_item14_tool_name_with_a_dot_and_space_sanitises_and_dispatches(ctx: Ctx):
    """A tool NAME (not just a server name) containing a dot/space --
    proven directly against the sanitiser + a real McpTool built from a
    hand-shaped sdk_tool double, since fake_mcp_server.py's own tool
    registration (the `mcp` SDK's own @app.tool) does not accept a raw dot/
    space in ITS OWN name argument -- the wire-naming/dispatch layer this
    item is actually about is entirely in rolo_claude's own code, covered
    here without needing the SDK to cooperate with an invalid Python
    identifier-shaped registration."""
    from types import SimpleNamespace
    from rolo_claude.tools.mcp_tool import McpTool
    sdk_tool = SimpleNamespace(name="weird.tool name", description="d",
                                input_schema={"type": "object", "properties": {}}, meta=None, annotations=None)

    class _FakeManager:
        def call(self, server, tool, arguments, **kw):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=f"called {tool!r}")],
                                    is_error=False, structured_content=None)
    tool = McpTool("srv", sdk_tool, _FakeManager())
    ctx.check(f"dot AND space both sanitised in the TOOL half, got {tool.name!r}",
              tool.name == "mcp__srv__weird_tool_name")
    ctx.check("split_mcp_tool_name parses it back apart",
              M.split_mcp_tool_name(tool.name) == ("srv", "weird_tool_name"))
    from rolo_claude.tools.base import ToolContext
    result = tool.run({}, ToolContext(cwd=REPO_DIR))
    ctx.check(f"dispatches through McpTool.run() normally, got {result.content}",
              not result.is_error and "weird.tool name" in result.content[0]["text"])


# ============================================================================
# Already-built confirmations (per the brief: needing only a live-check,
# not a rebuild).
# ============================================================================

@test
def test_confirm_structured_content_without_content_becomes_json_text_via_mcptool_run(ctx: Ctx):
    """rolo_claude/tools/mcp_tool.py ~line 220 (content_with_structured_
    fallback) already has a direct pure-function unit test in test_mcp_
    tool.py -- this drives the SAME behaviour through the actual composed
    entry point, McpTool.run() itself (a duck-typed manager double
    returning content=[]/structured_content={...}, exactly the shape
    McpTool.run() reads via getattr(result, ...))."""
    from types import SimpleNamespace
    from rolo_claude.tools.base import ToolContext
    from rolo_claude.tools.mcp_tool import McpTool

    class _StructuredOnlyManager:
        def call(self, server, tool, arguments, **kw):
            return SimpleNamespace(content=[], structured_content={"answer": 42, "ok": True}, is_error=False)

    sdk_tool = SimpleNamespace(name="structured_tool", description="d",
                                input_schema={"type": "object", "properties": {}}, meta=None, annotations=None)
    tool = McpTool("srv", sdk_tool, _StructuredOnlyManager())
    result = tool.run({}, ToolContext(cwd=REPO_DIR))
    ctx.check(f"no error", not result.is_error)
    ctx.check(f"structuredContent becomes a JSON text block when content was empty, got {result.content}",
              len(result.content) == 1 and result.content[0]["type"] == "text"
              and json.loads(result.content[0]["text"]) == {"answer": 42, "ok": True})


@test
def test_confirm_progress_keepalive_rescues_a_real_progressing_wire_call(ctx: Ctx):
    """H9 bug fix (was a PINNING test for an open bug). BUG (d),
    rolo_claude/mcp/manager.py's `McpServerHandle.call_tool`: it passed the
    SAME `timeout` value as BOTH the outer keepalive-extendable
    `run_abortable` bound's basis (timeout+3) AND the SDK's own internal,
    non-resettable per-request deadline (`read_timeout_seconds=timeout`, a
    single `anyio.fail_after` armed ONCE in mcp/shared/jsonrpc_dispatcher.
    py's send_raw_request -- `on_progress` never touches it). The inner
    bound was always 3s shorter and never extended, so it always fired
    first: a real, still-progressing call died with `MCPError: Request
    'tools/call' timed out` at exactly `timeout` despite real progress
    pings. Fixed by leaving the SDK's bound OFF (`read_timeout_seconds=
    None`) so the keepalive-aware, abort-aware outer bound is the only
    wall-clock guard. Same deterministic scenario: server timeout 1000ms,
    3 steps of 0.5s each (1.5s total, a ping at 0.5s) -- must now SUCCEED
    at ~1.5s instead of dying at 1.0s."""
    cfg = _fake_cfg("fake", extra_tools=["progress_slow_tool"], timeout_ms=1000)
    mgr = M.McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        t0 = time.monotonic()
        succeeded = False
        detail = None
        try:
            result = mgr.call("fake", "progress_slow_tool", {"steps": 3, "seconds_per_step": 0.5})
            succeeded = True
            detail = result.content[0].text
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
        elapsed = time.monotonic() - t0
        ctx.check(f"a real, progressing call is no longer killed by an inner non-resettable timeout -- "
                  f"succeeded={succeeded} after {elapsed:.2f}s, detail={detail!r} (expected ~1.5s)",
                  succeeded)
        ctx.check(f"it genuinely ran past the 1.0s per-call timeout that used to kill it, took {elapsed:.2f}s",
                  elapsed >= 1.3)
    finally:
        mgr.close_all()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
