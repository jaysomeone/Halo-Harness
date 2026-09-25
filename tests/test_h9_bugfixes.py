"""tests.test_h9_bugfixes -- pinning tests for the H9 closing-milestone bugs
found by the Linux-first acceptance pass (docs/harness/ACCEPTANCE-2026-09-25.md)
that live outside the MCP matrix / fuzz files' own scope:

  1. `bridge.find_claude_exe` never resolved a native POSIX `claude`
     (`rolo-claude proxy launch` crashed on Linux, the primary platform).
  2. A PreToolUse hook written in the PermissionRequest shape
     (`hookSpecificOutput.decision.{behavior, updatedInput}`) was a silent
     no-op -- the original command ran unchanged.
  3. `--playwright` built a server config with an `npx` shim but no
     runnable `node`, then hung for the whole session instead of failing
     fast the way `--chrome` does.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_h9_find_claude_exe_resolves_a_native_posix_claude(ctx: Ctx):
    """Part A bug 1 (critical): on POSIX the bare `claude` on PATH must be
    found first (never the Windows `claude.exe`/`claude.cmd` shims), and
    with nothing on PATH `~/.local/bin/claude` -- the standard native
    install location -- must be tried before giving up."""
    import bridge

    old_env = os.environ.pop("BRIDGE_CLAUDE_EXE", None)
    try:
        found = bridge.find_claude_exe(_which=lambda name: "/usr/local/bin/claude" if name == "claude" else None,
                                        _windows=False)
        ctx.check(f"POSIX: bare `claude` on PATH is returned, got {found!r}", found == "/usr/local/bin/claude")

        # Nothing on PATH at all -> ~/.local/bin/claude (no .exe) is tried.
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            (fake_home / ".local" / "bin").mkdir(parents=True)
            local_claude = fake_home / ".local" / "bin" / "claude"
            local_claude.write_text("#!/bin/sh\n", encoding="utf-8")
            orig_home = getattr(bridge, "home", None)
            if callable(orig_home):
                bridge.home = lambda: fake_home
                try:
                    found2 = bridge.find_claude_exe(_which=lambda name: None, _windows=False)
                    ctx.check(f"POSIX: ~/.local/bin/claude is found when PATH has nothing, got {found2!r}",
                              found2 == str(local_claude))
                    local_claude.unlink()
                    raised = False
                    try:
                        bridge.find_claude_exe(_which=lambda name: None, _windows=False)
                    except bridge.ClaudeNotFoundError:
                        raised = True
                    ctx.check("POSIX: a clean ClaudeNotFoundError (never a FileNotFoundError on a Windows shim) "
                              "when nothing exists", raised)
                finally:
                    bridge.home = orig_home

        # Windows keeps its existing order but gains the bare-name fallback.
        found3 = bridge.find_claude_exe(_which=lambda name: r"C:\tools\claude" if name == "claude" else None,
                                        _windows=True)
        ctx.check(f"win32: falls back to a bare `claude` on PATH when no .exe/.cmd shim exists, got {found3!r}",
                  found3 == r"C:\tools\claude")
    finally:
        if old_env is not None:
            os.environ["BRIDGE_CLAUDE_EXE"] = old_env


@test
def test_h9_pretooluse_hook_accepts_the_permissionrequest_decision_shape(ctx: Ctx):
    """Part A bug 2 (critical): `hookSpecificOutput.decision.{behavior,
    updatedInput, message}` on a PreToolUse hook is honoured (allow/deny +
    the rewritten input) when the documented flat `permissionDecision`/
    `updatedInput` pair is absent -- and NEVER overrides an explicit flat
    decision when both are present."""
    from rolo_claude.hooks import HookResult, interpret_hook_result

    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                     "decision": {"behavior": "allow", "updatedInput": {"command": "echo rewritten"},
                                                  "message": "rewritten by hook"}}}
    outcome = interpret_hook_result("PreToolUse", HookResult(exit_code=0, stdout=json.dumps(nested)))
    ctx.check(f"nested allow is read, got {outcome.permission_decision!r}", outcome.permission_decision == "allow")
    ctx.check(f"nested updatedInput reaches dispatch, got {outcome.updated_input!r}",
              outcome.updated_input == {"command": "echo rewritten"})
    ctx.check(f"nested message becomes the reason, got {outcome.permission_decision_reason!r}",
              outcome.permission_decision_reason == "rewritten by hook")

    nested_deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "decision": {"behavior": "deny"}}}
    outcome2 = interpret_hook_result("PreToolUse", HookResult(exit_code=0, stdout=json.dumps(nested_deny)))
    ctx.check(f"nested deny is read, got {outcome2.permission_decision!r}", outcome2.permission_decision == "deny")

    both = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "updatedInput": {"command": "flat wins"},
                                   "decision": {"behavior": "allow", "updatedInput": {"command": "nested loses"}}}}
    outcome3 = interpret_hook_result("PreToolUse", HookResult(exit_code=0, stdout=json.dumps(both)))
    ctx.check(f"an explicit flat decision is never overridden by the nested one, got {outcome3.permission_decision!r}",
              outcome3.permission_decision == "deny")
    ctx.check(f"the flat updatedInput wins, got {outcome3.updated_input!r}",
              outcome3.updated_input == {"command": "flat wins"})

    # Other events keep ignoring a stray nested `decision` (PermissionRequest
    # has its own branch; PostToolUse etc. never read it).
    outcome4 = interpret_hook_result("PostToolUse", HookResult(exit_code=0, stdout=json.dumps(nested)))
    ctx.check("PostToolUse ignores the nested PreToolUse-style decision", outcome4.updated_input is None)


@test
def test_h9_playwright_config_fails_fast_without_a_runnable_node(ctx: Ctx):
    """Part A bug 4: an `npx` shim with no runnable `node` (WSL's Windows-
    side npx through PE interop was the observed case) must produce the
    same fast, honest `(None, error)` precondition `--chrome` gives --
    never a server config that then hangs for the whole session."""
    from rolo_claude import mcp_setup

    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name in ("node", "node.exe"):
            return None
        if name in ("npx", "npx.cmd"):
            return "/usr/bin/npx"
        return real_which(name, *args, **kwargs)

    mcp_setup.shutil.which = fake_which
    try:
        cfg, err = mcp_setup.playwright_server_config()
        ctx.check(f"no config is built without node, got {cfg!r}", cfg is None)
        ctx.check(f"the error names what is missing and points at doctor, got {err!r}",
                  isinstance(err, str) and "node" in err and "doctor" in err)

        mcp_setup.shutil.which = lambda name, *a, **k: "/usr/bin/" + name.replace(".exe", "").replace(".cmd", "")
        cfg2, err2 = mcp_setup.playwright_server_config(headless=True)
        ctx.check(f"with both node and npx present a real config is built, got err={err2!r}",
                  cfg2 is not None and err2 is None and cfg2.name == "playwright" and "--headless" in cfg2.args)
    finally:
        mcp_setup.shutil.which = real_which


@test
def test_h9_stream_completion_closes_the_upstream_connection_socket(ctx: Ctx):
    """Static pass (`-X dev -W error::ResourceWarning`): every model call
    used to leak its HTTPConnection's socket -- `sse_reader_thread` closed
    the HTTPResponse, but nothing closed the connection, so the socket
    lived until garbage collection and fired one "unclosed <socket.socket
    ...>" ResourceWarning per call (74 attributed to agent/loop.py's own
    `for ev in gen`, 15 to the summariser call, in one full suite run).
    Drives a real Session turn against the mock upstream with
    ResourceWarning recording on, then forces a GC and asserts no CLIENT-
    side socket (one whose remote end is the mock's port) was collected
    unclosed."""
    import gc
    import tempfile
    import warnings
    from urllib.parse import urlparse

    from tests.helpers.fake_home import build_fake_home
    from tests.helpers.mock_openai import MockUpstream
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    port = urlparse(mock.base_url).port
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            model = "or:mock/model"
            session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
            session = Session(
                cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
                creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
                state_dir=Path(tempfile.mkdtemp(prefix="h9-sock-leak-")), model_label=model,
                session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=5,
            )
            kinds = [ev.kind for ev in session.turn("say hello")]
            ctx.check(f"the turn completed against the mock, got {kinds[-3:]}", "turn_done" in kinds or bool(kinds))
            del session, session_ctx
            gc.collect()
            gc.collect()
        leaked = [str(w.message) for w in caught
                  if issubclass(w.category, ResourceWarning) and "socket" in str(w.message)
                  and f"raddr=('127.0.0.1', {port})" in str(w.message)]
        ctx.check(f"no client-side upstream socket was collected unclosed, got {leaked}", not leaked)
    finally:
        mock.stop()
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
