"""tests.test_mcp_e2e -- H3 end to end: `-p "use the fake server's echo
tool"` through the mock upstream with `ScriptedTurns`, driving a REAL MCP
tool dispatch through the full agent loop (permission decide -> dispatch ->
truncate -> log). Also: fix (a) permission_denials includes a plain deny-
rule hit (not just ask->deny), and the two checksum/settings-stability
invariants (`~/.claude.json` byte-identical, settings.json untouched) with
MCP servers actually configured and connected.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


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


def _write_fake_mcp_only_claude_json(home: Path) -> None:
    """Replace build_fake_home()'s default `.claude.json` with ONE clean,
    fast, genuinely-connectable server -- keeps these tests from also
    waiting out the fixture's `expanded-models` entry (a command that
    doesn't exist on this machine)."""
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"fake": {"type": "stdio", "command": sys.executable,
                                 "args": ["-m", "tests.helpers.fake_mcp_server"]}},
    }), encoding="utf-8")


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, extra_env=None, model="or:mock/model"):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_mcp_e2e_echo_tool_via_scripted_turns(ctx: Ctx):
    """The brief's own acceptance shape: a real MCP tool dispatch through
    the whole H1-H3 pipeline, scripted upstream, real subprocess server."""
    fh = build_fake_home()
    _write_fake_mcp_only_claude_json(fh["home"])
    steps = [
        _tool_call_chunk("call_echo", "mcp__fake__echo", {"text": "hello from mcp"}),
        _final_text_chunk("the tool said: hello from mcp"),
    ]
    SCENARIOS["h3-mcp-echo"] = ScriptedTurns(steps)
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "use the fake server's echo tool", model="or:mock/h3-mcp-echo",
                           extra_args=["--permission-mode", "auto", "--verbose"], timeout=30)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-1200:]!r}", result.returncode == 0)
        ctx.check(f"real answer through the MCP tool, got {result.stdout!r}",
                  "hello from mcp" in result.stdout)
        ctx.check(f"--verbose shows the mcp tool call line, got stdout={result.stdout!r}",
                  "mcp__fake__echo" in result.stdout)
    finally:
        mock.stop()


@test
def test_mcp_e2e_isError_tool_result_reaches_the_model(ctx: Ctx):
    fh = build_fake_home()
    _write_fake_mcp_only_claude_json(fh["home"])

    def _scn(h, body):
        from tests.helpers.mock_openai import _finish
        tool_msgs = [m for m in (body.get("messages") or []) if m.get("role") == "tool"]
        if not tool_msgs:
            return _finish(h, _tool_call_chunk("call_err", "mcp__fake__error_tool", {}))
        content = str(tool_msgs[-1].get("content", ""))
        saw_error = "error_tool always fails" in content or "Error executing tool" in content
        _finish(h, _final_text_chunk("saw-the-error" if saw_error else "did-not-see-it"))
    SCENARIOS["h3-mcp-error"] = _scn

    mock = MockUpstream().start()
    try:
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "rolo_claude", "-p", "call the error tool", "--model", "or:mock/h3-mcp-error",
                "--cwd", str(fh["proj"]), "--permission-mode", "auto"]
        result = subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"the model saw the real error text, got {result.stdout!r}", result.stdout.strip() == "saw-the-error")
    finally:
        mock.stop()


# ---- fix (a): permission_denials includes a plain deny-RULE hit ----------

@test
def test_permission_denials_includes_a_plain_deny_rule_hit(ctx: Ctx):
    """Brief fix (a): a `--disallowedTools` PARAMETERISED rule (not a bare
    name -- those remove the tool from the catalog instead, a separate
    mechanism) must show up in the `-p` JSON result's `permission_denials`
    array even though it was never an ask-turned-into-deny. Reproduces via
    `permissions.PermissionEngine.decide` directly (fast, no subprocess) --
    the exact bug fixed at `decide()`'s single entry point."""
    from rolo_claude import permissions as P
    engine = P.PermissionEngine(
        mode="auto", cwd=Path("/tmp/x"), print_mode=True,
        deny_rules=[P.parse_rule("Bash(rm -rf *)", source="cli_disallow")],
    )
    decision = engine.decide("Bash", {"command": "rm -rf /"}, tool=None)
    ctx.check(f"denied, got {decision.action}", decision.action == "deny")
    ctx.check("permission_denial populated for a PLAIN deny-rule hit (the actual bug)",
              decision.permission_denial is not None)
    ctx.check("carries the tool name/input/reason", decision.permission_denial["tool_name"] == "Bash"
              and decision.permission_denial["tool_input"]["command"] == "rm -rf /")


@test
def test_permission_denials_not_populated_outside_print_mode(ctx: Ctx):
    """The backfill is print-mode-only (matches the pre-existing ask->deny
    behaviour it generalises) -- an interactive/non-print session has no
    JSON `permission_denials` array to populate."""
    from rolo_claude import permissions as P
    engine = P.PermissionEngine(
        mode="auto", cwd=Path("/tmp/x"), print_mode=False,
        deny_rules=[P.parse_rule("Bash(rm -rf *)", source="cli_disallow")],
    )
    decision = engine.decide("Bash", {"command": "rm -rf /"}, tool=None)
    ctx.check("still denied", decision.action == "deny")
    ctx.check("but permission_denial stays None outside print mode", decision.permission_denial is None)


@test
def test_permission_denials_end_to_end_in_json_result(ctx: Ctx):
    """The same fix, proven end to end through a real -p JSON result: a
    Bash deny rule (via --disallowedTools) blocks a real call and the
    tool never runs, with the denial recorded in the JSON envelope."""
    fh = build_fake_home()
    SCENARIOS["h3-deny-rule"] = ScriptedTurns([
        _tool_call_chunk("call_b", "Bash", {"command": "rm -rf /tmp/nope"}),
        _final_text_chunk("acknowledged the denial"),
    ])
    mock = MockUpstream().start()
    try:
        result = _run_cli(
            fh, mock, "try to run the risky command", model="or:mock/h3-deny-rule",
            extra_args=["--permission-mode", "auto", "--disallowedTools", "Bash(rm -rf *)",
                        "--output-format", "json"],
        )
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        payload = json.loads(result.stdout)
        denials = payload.get("permission_denials") or []
        ctx.check(f"permission_denials carries the deny-rule hit, got {payload.get('permission_denials')!r}",
                  len(denials) == 1 and denials[0].get("tool_name") == "Bash")
    finally:
        mock.stop()


# ---- checksum / settings stability with MCP servers actually configured ---

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@test
def test_claude_json_checksum_unchanged_after_a_session_with_mcp(ctx: Ctx):
    fh = build_fake_home()
    _write_fake_mcp_only_claude_json(fh["home"])
    claude_json_path = fh["home"] / ".claude.json"
    before = _sha256(claude_json_path)
    settings_path = fh["claude_dir"] / "settings.json"
    settings_before = _sha256(settings_path)

    SCENARIOS["h3-checksum-probe"] = ScriptedTurns([_final_text_chunk("no tools needed")])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "just answer directly", model="or:mock/h3-checksum-probe",
                           extra_args=["--permission-mode", "auto"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check("~/.claude.json byte-identical before/after (the harness never rewrites it)",
                  _sha256(claude_json_path) == before)
        ctx.check("settings.json untouched", _sha256(settings_path) == settings_before)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
