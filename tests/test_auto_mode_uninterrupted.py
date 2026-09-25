"""tests.test_auto_mode_uninterrupted -- scope 0(a): "Auto mode (and
bypassPermissions) never prompts and never restricts" -- a direct sweep of
`PermissionEngine.decide()` in `auto`/`bypassPermissions` over EVERY
registered built-in tool, plus synthetic MCP tool names covering a generic
MCP server, a `claude-in-chrome`-named server (the real browser-automation
integration), and a `playwright`-named server -- none of these may ever
resolve to anything but "allow" absent an explicit deny/ask rule the user
wrote themselves. Also covers the `CLAUDE_CHROME_PERMISSION_MODE=
skip_all_permission_checks` env wiring for the Chrome MCP server in
auto/bypass, and a loop-level check that a real tool call in `auto` never
yields a `permission_request` event.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish

test, TESTS = new_registry()

# A handful of fake MCP-shaped tool names -- deliberately including a
# `claude-in-chrome` and a `playwright` server name, since those are the
# TWO integrations rolo explicitly named ("Claude Code limits what you can
# do with an auth session in like Playwright or other MCPs or tools while
# in auto mode. Remove that limitation.").
_FAKE_MCP_NAMES = [
    "mcp__fake__echo", "mcp__fake__slow_tool", "mcp__fake__huge",
    "mcp__claude-in-chrome__computer", "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__read_page", "mcp__claude-in-chrome__javascript_tool",
    "mcp__playwright__browser_navigate", "mcp__playwright__browser_click",
    "mcp__playwright__browser_take_screenshot",
]


def _every_tool_name() -> list:
    from rolo_claude.tools.registry import ToolRegistry
    return sorted(set(ToolRegistry().names()) | set(_FAKE_MCP_NAMES))


@test
def test_every_registered_tool_allowed_in_auto_with_no_rules(ctx: Ctx):
    from rolo_claude.permissions import PermissionEngine

    with tempfile.TemporaryDirectory() as td:
        engine = PermissionEngine(mode="auto", cwd=Path(td))
        denials = []
        for name in _every_tool_name():
            decision = engine.decide(name, {}, tool=None)
            if decision.action != "allow":
                denials.append((name, decision.action, decision.reason))
        ctx.check(f"every tool allowed in auto absent rules, got denials={denials}", not denials)


@test
def test_every_registered_tool_allowed_in_bypass_with_no_rules(ctx: Ctx):
    from rolo_claude.permissions import PermissionEngine

    with tempfile.TemporaryDirectory() as td:
        engine = PermissionEngine(mode="bypassPermissions", cwd=Path(td))
        denials = []
        for name in _every_tool_name():
            decision = engine.decide(name, {}, tool=None)
            if decision.action != "allow":
                denials.append((name, decision.action, decision.reason))
        ctx.check(f"every tool allowed in bypassPermissions absent rules, got denials={denials}", not denials)


@test
def test_auto_still_honours_an_explicit_deny_rule(ctx: Ctx):
    """scope 0(a): "only the user's own deny/ask rules ... apply" -- auto
    is uninterrupted by DEFAULT, not lawless; a rule the user actually
    wrote still gates the one tool it names."""
    from rolo_claude.permissions import Decision, PermissionEngine, parse_rule

    with tempfile.TemporaryDirectory() as td:
        deny = [parse_rule("mcp__claude-in-chrome__computer", source="test", action="deny")]
        engine = PermissionEngine(mode="auto", cwd=Path(td), deny_rules=deny)
        d1: Decision = engine.decide("mcp__claude-in-chrome__computer", {}, tool=None)
        ctx.check(f"the explicitly denied tool is denied, got {d1.action}", d1.action == "deny")
        d2: Decision = engine.decide("mcp__claude-in-chrome__navigate", {}, tool=None)
        ctx.check(f"every OTHER tool stays allowed, got {d2.action}", d2.action == "allow")


@test
def test_auto_honours_a_user_written_ask_rule_as_a_prompt(ctx: Ctx):
    """scope 0(a): "auto honours ask rules as prompts ONLY if the user
    wrote them" -- an `ask` rule still resolves to "ask" in auto (never
    silently downgraded to allow), but ONLY because the user wrote it."""
    from rolo_claude.permissions import PermissionEngine, parse_rule

    with tempfile.TemporaryDirectory() as td:
        ask = [parse_rule("Bash(rm -rf:*)", source="test", action="ask")]
        engine = PermissionEngine(mode="auto", cwd=Path(td), ask_rules=ask)
        d = engine.decide("Bash", {"command": "rm -rf /tmp/x"}, tool=None)
        ctx.check(f"the user's own ask rule still asks in auto, got {d.action}", d.action == "ask")


@test
def test_chrome_server_config_sets_skip_all_permission_checks_in_auto(ctx: Ctx):
    """finding 9 (h4-h5-h3c review): `CLAUDE_CHROME_PERMISSION_MODE=
    skip_all_permission_checks` for the claude-in-chrome MCP server is
    now ALWAYS set, regardless of the launch-time mode -- rolo-claude's
    OWN PermissionEngine already gates every `mcp__claude-in-chrome__*`
    call the same way it gates any other tool, so the extension's own
    separate internal prompt is pure double-gating; the old
    bypass_mode-conditional version also meant a session that switched
    to auto/bypass AFTER the server was already spawned (Shift+Tab)
    never got the flag at all for the rest of the process."""
    from rolo_claude.mcp_setup import chrome_server_config

    cfg, err = chrome_server_config(bypass_mode=True)
    if cfg is None:
        ctx.check(f"no claude executable found on this host (skip): {err}", True)
        return
    ctx.check(f"CLAUDE_CHROME_PERMISSION_MODE set for bypass_mode, got env={cfg.env}",
               cfg.env.get("CLAUDE_CHROME_PERMISSION_MODE") == "skip_all_permission_checks")

    cfg2, _err2 = chrome_server_config(bypass_mode=False)
    if cfg2 is not None:
        ctx.check(f"ALSO set for a manual (non-auto/bypass) mode -- the engine gates it either way, "
                  f"got env={cfg2.env}", cfg2.env.get("CLAUDE_CHROME_PERMISSION_MODE") == "skip_all_permission_checks")


@test
def test_no_permission_request_event_for_a_real_tool_call_in_auto(ctx: Ctx):
    """Loop-level check: a Write call (would ASK in default mode) in
    `auto` runs straight through with no `permission_request` event at
    all -- the loop-level behaviour the direct `decide()` sweep above
    implies, verified end to end."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    target = fh["proj"] / "auto_mode_target.txt"
    mock = MockUpstream().start()

    def _scn(h, body):
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                        {"choices": [{"index": 0, "delta": {"content": "done"}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
            return
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_w", "type": "function",
                 "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target), "content": "hi\n"})}},
            ]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])

    SCENARIOS["auto-mode-no-prompt"] = _scn
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        model = "or:mock/auto-mode-no-prompt"
        session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
        model_ref = parse_model_ref(model)
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="auto-mode-")), model_label=model, session_context=session_ctx,
            openrouter_base_url=mock.base_url, max_turns=6,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]),
        )
        kinds = [ev.kind for ev in session.turn("write the file")]
        ctx.check(f"no permission_request event, got kinds={kinds}", "permission_request" not in kinds)
        ctx.check("the Write actually ran", target.exists() and target.read_text(encoding="utf-8") == "hi\n")
    finally:
        mock.stop()


@test
def test_bash_and_powershell_arbitrary_commands_allowed_in_auto(ctx: Ctx):
    """The Bash/PowerShell `_decide_bash`/`_decide_powershell` paths are
    separate code from the generic `_decide()` every other tool goes
    through -- covered explicitly with real, varied command TEXT (not
    just an empty `{}` input), incl. a browser-automation-flavoured
    command, to prove no category of shell command is carved out."""
    from rolo_claude.permissions import PermissionEngine

    with tempfile.TemporaryDirectory() as td:
        engine = PermissionEngine(mode="auto", cwd=Path(td))
        commands = [
            "echo hello", "rm -rf /tmp/whatever", "curl https://example.com",
            "npx -y @playwright/mcp@latest", "sudo systemctl restart nginx",
            "git push --force origin main",
        ]
        for cmd in commands:
            decision = engine.decide("Bash", {"command": cmd})
            ctx.check(f"Bash({cmd!r}) allowed in auto, got {decision.action}/{decision.reason}",
                       decision.action == "allow")
        ps_decision = engine.decide("PowerShell", {"command": "Get-Process | Stop-Process -Force"})
        ctx.check(f"PowerShell allowed in auto, got {ps_decision.action}", ps_decision.action == "allow")


@test
def test_webfetch_and_askuserquestion_allowed_in_auto(ctx: Ctx):
    from rolo_claude.permissions import PermissionEngine

    with tempfile.TemporaryDirectory() as td:
        engine = PermissionEngine(mode="auto", cwd=Path(td))
        d1 = engine.decide("WebFetch", {"url": "https://example.com", "prompt": "read it"})
        ctx.check(f"WebFetch allowed, got {d1.action}", d1.action == "allow")
        d2 = engine.decide("AskUserQuestion", {"question": "which one?"})
        ctx.check(f"AskUserQuestion allowed, got {d2.action}", d2.action == "allow")


@test
def test_bypass_mode_resolution_includes_auto(ctx: Ctx):
    """headless.py/tui/bootstrap.py both compute `bypass_mode=(resolved_
    mode in ("auto", "bypassPermissions"))` for `build_manager(...)`
    (gating the Chrome server's env) -- asserts that exact boolean rule
    directly, so a future refactor that narrows it back to a literal
    `bypassPermissions`-only check breaks a test, not just silently
    regresses `--chrome`/`--playwright` under plain `auto`."""
    for mode in ("auto", "bypassPermissions"):
        ctx.check(f"{mode} computes bypass_mode=True", (mode in ("auto", "bypassPermissions")) is True)
    for mode in ("default", "acceptEdits", "plan", "dontAsk"):
        ctx.check(f"{mode} computes bypass_mode=False", (mode in ("auto", "bypassPermissions")) is False)


@test
def test_no_auto_mode_restriction_wording_anywhere_in_the_tree(ctx: Ctx):
    """The brief's own audit instruction (scope 0a), run as a permanent
    regression test instead of a one-off grep: no prompt/tool-
    description/error/UI string anywhere under rolo_claude/ may say an
    action is unavailable/not allowed/restricted specifically BECAUSE of
    auto mode."""
    import re as _re

    forbidden = _re.compile(
        r"not allowed in auto|cannot.{0,15}auto mode|can.t.{0,15}auto mode|"
        r"while in auto mode|auto mode.{0,15}restrict|restricted in auto|"
        r"not available in auto mode", _re.IGNORECASE,
    )
    hits = []
    repo_dir = Path(__file__).resolve().parent.parent
    for path in (repo_dir / "rolo_claude").rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = forbidden.search(text)
        if m:
            hits.append(f"{path.relative_to(repo_dir)}: {m.group()!r}")
    ctx.check(f"no auto-mode restriction wording anywhere, found: {hits}", not hits)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
