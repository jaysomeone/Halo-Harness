"""tests.test_hooks_lifecycle -- H4 scope A: rolo_claude/hooks.py's own
protocol implementation (HookDef/HookResult/HookOutcome, normalize_hooks,
matcher/if/dedup/once filtering, every handler type, exit-code/JSON
interpretation, combination rules, caps, timeouts, Stop cap, SessionEnd
budget, CLAUDE_ENV_FILE) exercised directly against the module's own public
API -- independent of agent/loop.py's call-site wiring (that gets its own
integration-level coverage elsewhere), using tests/helpers/hook_scripts.py
as real subprocesses for the `command` handler type.
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude import hooks as H

test, TESTS = new_registry()

# direct file path (never `-m tests.helpers.hook_scripts`): `run_command_hook`
# spawns with `cwd=<some unrelated tempdir>`, so a `-m` invocation would need
# PYTHONPATH threaded through every single test's env just to find the
# `tests` package -- a script run by its own absolute path needs nothing of
# the kind (it adds its own directory to sys.path[0] and the script itself
# imports nothing from the `tests` package).
_HOOK_SCRIPTS_PATH = str(REPO_DIR / "tests" / "helpers" / "hook_scripts.py")


def _SCRIPT_ARGS(mode: str) -> list:
    return [sys.executable, _HOOK_SCRIPTS_PATH, mode]


def _hookdef(mode: str, **kw) -> H.HookDef:
    return H.HookDef(type="command", args=_SCRIPT_ARGS(mode), **kw)


def _runner(hooks_by_event: dict, tmp, **kw) -> H.HookRunner:
    kw.setdefault("session_id", "sess1")
    kw.setdefault("transcript_path", "t.jsonl")
    kw.setdefault("effective_env", dict(os.environ))
    return H.HookRunner(hooks_by_event, cwd=tmp, **kw)


# ---- matcher semantics ------------------------------------------------------

@test
def test_matcher_omitted_star_matches_all(ctx: Ctx):
    ctx.check("None matches", H.matcher_matches(None, "Bash") == (True, None))
    ctx.check("empty matches", H.matcher_matches("", "Bash") == (True, None))
    ctx.check("star matches", H.matcher_matches("*", "Bash") == (True, None))


@test
def test_matcher_exact_list_comma_and_pipe(ctx: Ctx):
    ctx.check("comma list hits", H.matcher_matches("Bash,Edit", "Edit") == (True, None))
    ctx.check("pipe list hits", H.matcher_matches("Bash|Edit", "Bash") == (True, None))
    ctx.check("list misses", H.matcher_matches("Bash,Edit", "Read")[0] is False)


@test
def test_matcher_unanchored_regex(ctx: Ctx):
    ok, warn = H.matcher_matches("^mcp__srv__.*", "mcp__srv__tool")
    ctx.check("regex matches", ok and warn is None)
    # a value shaped ^[A-Za-z0-9_\-,|]*$ is ALWAYS the exact-list form, never
    # a regex (even one with no special chars) -- so "unanchored" is only
    # observable with something that forces the regex branch, e.g. a dot.
    ok2, warn2 = H.matcher_matches("Ba.h", "prefixBashsuffix")
    ctx.check(f"unanchored regex hits mid-string, got ok={ok2} warn={warn2}", ok2 is True and warn2 is None)


@test
def test_matcher_invalid_regex_skips_with_warning(ctx: Ctx):
    ok, warn = H.matcher_matches("(unclosed", "x")
    ctx.check("invalid regex never matches", ok is False)
    ctx.check("invalid regex warns", warn is not None and "invalid" in warn)


# ---- `if` rule filtering -----------------------------------------------------

@test
def test_if_rule_bash_prefix_gates_matching_command(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-if-")
    ctx.check("matches git push", H.if_rule_matches("Bash(git push:*)", "Bash", {"command": "git push origin"}, None, cwd=tmp))
    ctx.check("does not match git status", not H.if_rule_matches("Bash(git push:*)", "Bash", {"command": "git status"}, None, cwd=tmp))


@test
def test_if_rule_absent_or_non_tool_event_always_matches(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-if2-")
    ctx.check("no rule -> always matches", H.if_rule_matches(None, "Bash", {"command": "x"}, None, cwd=tmp))
    ctx.check("non-tool event -> always matches", H.if_rule_matches("Bash(git *)", None, None, None, cwd=tmp))


@test
def test_if_rule_invalid_never_matches(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-if3-")
    ctx.check("invalid `if` fails closed", not H.if_rule_matches("Bash(", "Bash", {"command": "x"}, None, cwd=tmp))


# ---- normalize_hooks ---------------------------------------------------------

@test
def test_normalize_hooks_flattens_settings_shape(ctx: Ctx):
    raw = {
        "PreToolUse": [
            {"matcher": "Bash", "if": "Bash(git *)", "hooks": [
                {"type": "command", "command": "echo 1", "timeout": 12},
                {"type": "command", "command": "echo 2"},
            ], "_source": "userSettings"},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}], "_source": "projectSettings"}],
    }
    norm = H.normalize_hooks(raw)
    ctx.check(f"PreToolUse has 2 defs, got {len(norm.get('PreToolUse', []))}", len(norm.get("PreToolUse", [])) == 2)
    ctx.check("matcher carried onto each def", all(d.matcher == "Bash" for d in norm["PreToolUse"]))
    ctx.check("if carried onto each def", all(d.if_rule == "Bash(git *)" for d in norm["PreToolUse"]))
    ctx.check("timeout carried", norm["PreToolUse"][0].timeout_s == 12)
    ctx.check("source tagged from _source", norm["PreToolUse"][0].source == "userSettings")
    ctx.check("Stop present with its own source", norm["Stop"][0].source == "projectSettings")


@test
def test_normalize_hooks_ignores_malformed_entries(ctx: Ctx):
    ctx.check("non-list groups value -> ignored", H.normalize_hooks({"PreToolUse": "not-a-list"}) == {})
    ctx.check("non-dict group -> skipped", H.normalize_hooks({"PreToolUse": ["not-a-dict"]}) == {})
    ctx.check("empty input -> empty output", H.normalize_hooks({}) == {})
    ctx.check("None input -> empty output", H.normalize_hooks(None) == {})


@test
def test_merge_hook_maps_concatenates(ctx: Ctx):
    m1 = {"Stop": [H.HookDef(type="command", command="a")]}
    m2 = {"Stop": [H.HookDef(type="command", command="b")], "SessionStart": [H.HookDef(type="command", command="c")]}
    merged = H.merge_hook_maps(m1, m2)
    ctx.check("Stop has both", len(merged["Stop"]) == 2)
    ctx.check("SessionStart present", len(merged["SessionStart"]) == 1)


@test
def test_load_plugin_hooks_substitutes_plugin_root(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="hooks-plugin-"))
    hooks_dir = tmp / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/run.sh", "args": None},
        ]}]},
    }), encoding="utf-8")
    norm = H.load_plugin_hooks(tmp)
    ctx.check("SessionStart loaded", "SessionStart" in norm)
    got = norm["SessionStart"][0].command
    ctx.check(f"plugin root substituted, got {got!r}", got == f"{tmp}/run.sh")
    ctx.check("source tagged plugin", norm["SessionStart"][0].source == "plugin")
    ctx.check("plugin_root recorded", norm["SessionStart"][0].plugin_root == str(tmp))


@test
def test_load_plugin_hooks_missing_file_returns_empty(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="hooks-plugin-missing-"))
    ctx.check("missing hooks.json -> {}", H.load_plugin_hooks(tmp) == {})


@test
def test_hooks_from_frontmatter_best_effort_dict_shape(ctx: Ctx):
    fm = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    norm = H.hooks_from_frontmatter(fm, scope="skill", source="skill:deploy")
    ctx.check("parsed", "PreToolUse" in norm)
    ctx.check("scope tagged", norm["PreToolUse"][0].scope == "skill")
    ctx.check("non-dict hooks value -> {}", H.hooks_from_frontmatter({"hooks": "nope"}, scope="skill", source="s") == {})
    ctx.check("no hooks key -> {}", H.hooks_from_frontmatter({}, scope="skill", source="s") == {})


# ---- command handler + exit-code table ---------------------------------------

@test
def test_command_hook_json_allow(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd-")
    result = H.run_command_hook(_hookdef("json_allow"), {"a": 1}, cwd=tmp, env=dict(os.environ))
    ctx.check(f"exit 0, got {result.exit_code}", result.exit_code == 0)
    outcome = H.interpret_hook_result("PreToolUse", result)
    ctx.check("permission_decision allow", outcome.permission_decision == "allow")


@test
def test_command_hook_exit2_blocks_regardless_of_json(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd2-")
    result = H.run_command_hook(_hookdef("exit2_json_allow"), {}, cwd=tmp, env=dict(os.environ))
    ctx.check("exit 2", result.exit_code == 2)
    outcome = H.interpret_hook_result("PreToolUse", result)
    ctx.check("blocked regardless of the JSON saying allow", outcome.blocked is True)
    ctx.check("block reason from stderr", "blocked by exit2_json_allow" in outcome.block_reason)


@test
def test_command_hook_exit1_is_non_blocking(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd3-")
    result = H.run_command_hook(_hookdef("exit1_stderr"), {}, cwd=tmp, env=dict(os.environ))
    ctx.check("exit 1", result.exit_code == 1)
    outcome = H.interpret_hook_result("PreToolUse", result)
    ctx.check("not blocked", outcome.blocked is False)


@test
def test_plain_stdout_context_only_for_context_events(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd4-")
    result = H.run_command_hook(_hookdef("plain_context"), {}, cwd=tmp, env=dict(os.environ))
    for event in ("UserPromptSubmit", "SessionStart", "PostModelSwitch"):
        outcome = H.interpret_hook_result(event, result)
        ctx.check(f"{event}: plain stdout becomes context", outcome.additional_context == "plain-context-from-hook-script")
    outcome_other = H.interpret_hook_result("PreToolUse", result)
    ctx.check("PreToolUse: plain stdout NOT treated as context", outcome_other.additional_context == "")


@test
def test_updated_input_and_block_decision_json_fields(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd5-")
    r1 = H.run_command_hook(_hookdef("updated_input"), {}, cwd=tmp, env=dict(os.environ))
    o1 = H.interpret_hook_result("PreToolUse", r1)
    ctx.check(f"updatedInput applied, got {o1.updated_input}", o1.updated_input == {"command": "echo rewritten"})

    r2 = H.run_command_hook(_hookdef("block_decision"), {}, cwd=tmp, env=dict(os.environ))
    o2 = H.interpret_hook_result("PostToolUse", r2)
    ctx.check("decision:block blocks", o2.blocked is True)
    ctx.check("reason from JSON", "blocked by block_decision" in o2.block_reason)


@test
def test_defer_only_applies_on_pretooluse(ctx: Ctx):
    result = H.HookResult(0, json.dumps({"hookSpecificOutput": {"permissionDecision": "defer"}}), "")
    o_pre = H.interpret_hook_result("PreToolUse", result)
    ctx.check("defer honoured on PreToolUse", o_pre.permission_decision == "defer")
    o_post = H.interpret_hook_result("PostToolUse", result)
    ctx.check("defer ignored elsewhere", o_post.permission_decision is None)


@test
def test_h5b_f13_permission_request_decision_behavior_schema(ctx: Ctx):
    """finding 13 (major, h4-h5-h3c review): PermissionRequest's OWN
    schema is `hookSpecificOutput.decision.behavior` (allow with
    updatedInput/updatedPermissions, or deny with message/interrupt) --
    NOT the PreToolUse-shaped top-level `permissionDecision` this
    function reads for every other event. A real Claude Code
    PermissionRequest auto-approve hook used to be silently ignored
    entirely (the card still appeared) since this schema was never
    parsed at all."""
    allow_result = H.HookResult(0, json.dumps({
        "hookSpecificOutput": {"decision": {
            "behavior": "allow", "updatedInput": {"command": "echo hi"},
            # H5c finding 9: the REAL 2.1.281 wire shape is a LIST of
            # {type, ...} entries -- never a bare {"setMode": ...} dict
            # (the old shape this test used to pin, which never matched
            # anything a real hook could actually send).
            "updatedPermissions": [{"type": "setMode", "mode": "acceptEdits"},
                                    {"type": "addRules", "behavior": "deny",
                                     "rules": [{"toolName": "Bash", "ruleContent": "rm -rf *"}]}],
        }},
    }), "")
    o_allow = H.interpret_hook_result("PermissionRequest", allow_result)
    ctx.check(f"behavior:allow parsed as permission_decision=allow, got {o_allow.permission_decision!r}",
              o_allow.permission_decision == "allow")
    ctx.check(f"updatedInput read from decision.updatedInput, got {o_allow.updated_input}",
              o_allow.updated_input == {"command": "echo hi"})
    ctx.check(f"updatedPermissions[setMode] read from the LIST shape, got {o_allow.set_mode!r}",
              o_allow.set_mode == "acceptEdits")
    ctx.check(f"updatedPermissions[addRules] parsed into a rule/behavior pair, got {o_allow.updated_permissions}",
              o_allow.updated_permissions == [{"rule": "Bash(rm -rf *)", "behavior": "deny"}])

    old_dict_shape = H.HookResult(0, json.dumps({
        "hookSpecificOutput": {"decision": {"behavior": "allow", "updatedPermissions": {"setMode": "acceptEdits"}}},
    }), "")
    o_old_shape = H.interpret_hook_result("PermissionRequest", old_dict_shape)
    ctx.check(f"a non-list updatedPermissions (the old, wrong dict shape) is rejected, not silently "
              f"misapplied, got set_mode={o_old_shape.set_mode!r}", o_old_shape.set_mode is None)

    deny_result = H.HookResult(0, json.dumps({
        "hookSpecificOutput": {"decision": {"behavior": "deny", "message": "no way"}},
    }), "")
    o_deny = H.interpret_hook_result("PermissionRequest", deny_result)
    ctx.check(f"behavior:deny parsed as permission_decision=deny, got {o_deny.permission_decision!r}",
              o_deny.permission_decision == "deny")
    ctx.check(f"message read as the permission_decision_reason, got {o_deny.permission_decision_reason!r}",
              o_deny.permission_decision_reason == "no way")

    interrupt_result = H.HookResult(0, json.dumps({
        "hookSpecificOutput": {"decision": {"behavior": "deny", "message": "stop", "interrupt": True}},
    }), "")
    o_interrupt = H.interpret_hook_result("PermissionRequest", interrupt_result)
    ctx.check(f"interrupt:true stops the whole turn (continue_=False), got {o_interrupt.continue_!r}",
              o_interrupt.continue_ is False)

    # The OLD (wrong) top-level shape must NOT be read for this event --
    # proves the fix isn't just "also accept the new shape" but genuinely
    # uses the RIGHT one.
    old_shape_result = H.HookResult(0, json.dumps({
        "hookSpecificOutput": {"permissionDecision": "allow"},
    }), "")
    o_old_shape = H.interpret_hook_result("PermissionRequest", old_shape_result)
    ctx.check(f"the old top-level permissionDecision shape is NOT read for PermissionRequest, got "
              f"{o_old_shape.permission_decision!r}", o_old_shape.permission_decision is None)


@test
def test_caps_reason_system_message_additional_context(ctx: Ctx):
    long_reason = "x" * 3000
    long_sysmsg = "y" * 5000
    result = H.HookResult(2, "", long_reason)
    outcome = H.interpret_hook_result("Stop", result)
    ctx.check(f"reason capped at {H.REASON_CAP}, got {len(outcome.block_reason)}", len(outcome.block_reason) == H.REASON_CAP)

    payload = json.dumps({"systemMessage": long_sysmsg, "hookSpecificOutput": {"additionalContext": "z" * 9000}})
    outcome2 = H.interpret_hook_result("SessionStart", H.HookResult(0, payload, ""))
    ctx.check(f"systemMessage capped, got {len(outcome2.system_messages[0])}", len(outcome2.system_messages[0]) == H.SYSTEM_MESSAGE_CAP)
    ctx.check(f"additionalContext capped, got {len(outcome2.additional_context)}", len(outcome2.additional_context) == H.ADDITIONAL_CONTEXT_CAP)


@test
def test_command_hook_timeout_is_non_blocking(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd6-")
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("sleep"), timeout_s=0.3)
    env = dict(os.environ)
    env["HOOK_SLEEP_S"] = "5"
    t0 = time.monotonic()
    result = H.run_command_hook(hook, {}, cwd=tmp, env=env)
    dt = time.monotonic() - t0
    ctx.check(f"returned near the timeout, not the full sleep, got {dt:.2f}s", dt < 2.0)
    ctx.check("timeout is non-blocking (never exit 2)", result.exit_code != 2)
    outcome = H.interpret_hook_result("PreToolUse", result)
    ctx.check("not blocked", outcome.blocked is False)


@test
def test_h5b_f14_command_hook_timeout_kills_the_whole_process_group(ctx: Ctx):
    """finding 14 (major, h4-h5-h3c review), point 2: a timeout must kill
    the WHOLE process group, not just the direct child -- a background
    subshell that keeps writing well past the hook's own timeout used to
    survive (the pre-fix `subprocess.run(..., timeout=...)` only ever
    killed the direct child it started)."""
    tmp = tempfile.mkdtemp(prefix="hooks-pg-")
    marker = str(Path(tmp) / "marker.txt")
    command = f'(i=0; while [ $i -lt 20 ]; do echo x >> "{marker}"; sleep 0.2; i=$((i+1)); done) & sleep 5'
    hookdef = H.HookDef(type="command", command=command, timeout_s=0.5)
    t0 = time.monotonic()
    result = H.run_command_hook(hookdef, {}, cwd=tmp, env=dict(os.environ))
    elapsed = time.monotonic() - t0
    ctx.check(f"returns promptly after the timeout, got {elapsed:.2f}s", elapsed < 3.0)
    ctx.check(f"reports the timeout, got {result.stderr!r}", "timed out" in result.stderr)
    size_at_return = Path(marker).stat().st_size if Path(marker).exists() else 0
    time.sleep(1.0)  # give a SURVIVING grandchild time to keep writing, if it escaped the kill
    size_after_wait = Path(marker).stat().st_size if Path(marker).exists() else 0
    ctx.check(f"the background grandchild was ALSO killed (marker stopped growing), "
              f"got {size_at_return} then {size_after_wait} bytes", size_after_wait == size_at_return)


@test
def test_h5b_f14_command_hook_respects_abort(ctx: Ctx):
    """finding 14, point 3: Esc during a slow command hook must actually
    interrupt it (kill the process group) instead of waiting for its own
    timeout/natural completion."""
    import threading as _threading_mod

    tmp = tempfile.mkdtemp(prefix="hooks-abort-")
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("sleep"), timeout_s=30)
    env = dict(os.environ)
    env["HOOK_SLEEP_S"] = "30"
    abort = _threading_mod.Event()

    def _fire_abort_soon():
        time.sleep(0.3)
        abort.set()

    _threading_mod.Thread(target=_fire_abort_soon, daemon=True).start()
    t0 = time.monotonic()
    result = H.run_command_hook(hook, {}, cwd=tmp, env=env, abort=abort)
    elapsed = time.monotonic() - t0
    ctx.check(f"returned promptly once aborted (well under the 30s sleep/timeout), got {elapsed:.2f}s", elapsed < 3.0)
    ctx.check(f"reports interruption, got {result.stderr!r}", "interrupted" in result.stderr)


@test
def test_h5b_f14_command_hook_env_strips_provider_secrets(ctx: Ctx):
    """finding 14, point 1: hook CHILD PROCESSES get `tool_child_env()`-
    stripped env, never the raw, unstripped `effective_env` -- a plugin/
    user hook used to be able to read every provider API key straight out
    of its own environment."""
    tmp = tempfile.mkdtemp(prefix="hooks-envstrip-")
    hooks_by_event = {"PreToolUse": [_hookdef("echo_stdin")]}
    raw_env = dict(os.environ)
    raw_env["OPENROUTER_API_KEY"] = "sk-super-secret-openrouter"
    raw_env["DATABRICKS_TOKEN"] = "dbx-super-secret-token"
    runner = _runner(hooks_by_event, tmp, effective_env=raw_env)
    # `_env_for` is what actually builds a hook child's environment --
    # exercised directly (echo_stdin only ever echoes the JSON PAYLOAD,
    # not its own env, so this checks the env-building function itself).
    built_env = runner._env_for(hooks_by_event["PreToolUse"][0], "PreToolUse")
    ctx.check(f"OPENROUTER_API_KEY stripped, got {'OPENROUTER_API_KEY' in built_env}",
              "OPENROUTER_API_KEY" not in built_env)
    ctx.check(f"DATABRICKS_TOKEN stripped, got {'DATABRICKS_TOKEN' in built_env}",
              "DATABRICKS_TOKEN" not in built_env)
    ctx.check("CLAUDE_PROJECT_DIR still present (a normal, non-secret var)", "CLAUDE_PROJECT_DIR" in built_env)


@test
def test_h5b_f14_shell_bash_runs_real_bash_never_sh(ctx: Ctx):
    """finding 14, point 5: an EXPLICIT `shell: "bash"` must produce a
    real bash invocation (`/bin/bash` on POSIX, Git Bash on win32) --
    never `/bin/sh` (dash on Kali/Debian: `[[ ... ]]` and other bashisms
    fail there)."""
    explicit_bash = H.HookDef(type="command", command="echo hi", shell="bash")
    default_shell = H.HookDef(type="command", command="echo hi", shell=None)
    argv_bash = H._shell_argv(explicit_bash, "echo hi")
    argv_default = H._shell_argv(default_shell, "echo hi")
    if sys.platform == "win32":
        ctx.check(f"win32 explicit bash uses Git Bash, got {argv_bash}", argv_bash[0].lower().endswith("bash.exe"))
        ctx.check(f"win32 default ALSO uses Git Bash (no native /bin/sh on Windows), got {argv_default}",
                  argv_default[0].lower().endswith("bash.exe"))
    else:
        ctx.check(f"POSIX explicit bash uses /bin/bash, got {argv_bash}", argv_bash[0] == "/bin/bash")
        ctx.check(f"POSIX default (no explicit shell) uses /bin/sh, got {argv_default}", argv_default[0] == "/bin/sh")


@test
def test_h5b_f14_session_end_budget_ignores_implicit_type_defaults(ctx: Ctx):
    """finding 14, point 4: only EXPLICIT `timeout` values count towards
    the SessionEnd budget -- a hook with none must contribute its type's
    own implicit default (600s for a command hook), not inflate the
    budget up to the 60s cap the way `effective_timeout_s()` would."""
    old = os.environ.pop("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS", None)
    try:
        no_explicit_timeout = [H.HookDef(type="command", command="echo hi")]  # timeout_s=None
        budget = H.session_end_budget_s(no_explicit_timeout)
        ctx.check(f"falls back to the 1.5s default, not the 600s command-hook implicit default, got {budget}",
                  budget == H.SESSION_END_DEFAULT_BUDGET_S)

        with_explicit_timeout = [H.HookDef(type="command", command="echo hi", timeout_s=10.0)]
        budget2 = H.session_end_budget_s(with_explicit_timeout)
        ctx.check(f"an explicit timeout DOES raise the budget, got {budget2}", budget2 == 10.0)
    finally:
        if old is not None:
            os.environ["CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"] = old


@test
def test_command_hook_exec_form_args_no_shell(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd7-")
    hook = H.HookDef(type="command", args=[sys.executable, "-c", "import sys; sys.exit(0)"])
    result = H.run_command_hook(hook, {}, cwd=tmp, env=dict(os.environ))
    ctx.check("exec-form args run directly", result.exit_code == 0)


@test
def test_command_hook_launch_failure_is_non_blocking(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-cmd8-")
    hook = H.HookDef(type="command", args=["/no/such/executable-xyz-really-not-here"])
    result = H.run_command_hook(hook, {}, cwd=tmp, env=dict(os.environ))
    ctx.check(f"launch failure -> exit 1, got {result.exit_code}", result.exit_code == 1)
    ctx.check("stderr names the failure", result.stderr)


# ---- http handler --------------------------------------------------------------

class _HttpHookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        self.server.last_body = body  # type: ignore[attr-defined]
        self.server.last_headers = dict(self.headers)  # type: ignore[attr-defined]
        out = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "http says no"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # silence
        pass


def _start_http_server():
    server = HTTPServer(("127.0.0.1", 0), _HttpHookHandler)
    server.last_body = None
    server.last_headers = {}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


@test
def test_http_hook_posts_json_and_interpolates_allowed_env_only(ctx: Ctx):
    server, t = _start_http_server()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        hook = H.HookDef(type="http", url=url, headers={"X-Token": "${HOOK_TEST_TOKEN}", "X-Secret": "${HOOK_TEST_SECRET}"},
                          allowed_env_vars=["HOOK_TEST_TOKEN"])
        env = {"HOOK_TEST_TOKEN": "abc123", "HOOK_TEST_SECRET": "should-not-leak"}
        result = H.run_http_hook(hook, {"session_id": "s1"}, timeout_s=5, env=env)
        ctx.check(f"exit 0, got {result}", result.exit_code == 0)
        outcome = H.interpret_hook_result("PreToolUse", result)
        ctx.check("deny decision parsed from response body", outcome.permission_decision == "deny")
        ctx.check("posted the payload", server.last_body.get("session_id") == "s1")
        ctx.check("allowed var interpolated", server.last_headers.get("X-Token") == "abc123")
        ctx.check("non-allowed var left as literal placeholder", server.last_headers.get("X-Secret") == "${HOOK_TEST_SECRET}")
    finally:
        server.shutdown()
        t.join(timeout=5)


@test
def test_http_hook_connection_failure_is_non_blocking(ctx: Ctx):
    hook = H.HookDef(type="http", url="http://127.0.0.1:1/definitely-closed")
    result = H.run_http_hook(hook, {}, timeout_s=2, env={})
    ctx.check(f"non-zero, got {result.exit_code}", result.exit_code != 0)
    ctx.check("never exit 2 on a connection failure", result.exit_code != 2)


# ---- mcp_tool handler ------------------------------------------------------------

@test
def test_mcp_tool_hook_calls_a_real_fake_server(ctx: Ctx):
    from tests.helpers.fake_mcp_server import running_manager
    from rolo_claude.mcp.manager import McpServerConfig

    cfg = McpServerConfig(name="fake", type="stdio", command=sys.executable,
                           args=["-m", "tests.helpers.fake_mcp_server"], env={}, scope="user")
    with running_manager({"fake": cfg}, tool_env=dict(os.environ)) as mgr:
        hook = H.HookDef(type="mcp_tool", server="fake", tool="echo", input={"text": "hi"})
        result = H.run_mcp_tool_hook(hook, {"session_id": "s"}, mcp_manager=mgr, timeout_s=10)
        ctx.check(f"exit 0, got {result}", result.exit_code == 0)
        ctx.check(f"echoed text present, got {result.stdout!r}", "hi" in result.stdout)


@test
def test_mcp_tool_hook_error_tool_is_non_blocking(ctx: Ctx):
    from tests.helpers.fake_mcp_server import running_manager
    from rolo_claude.mcp.manager import McpServerConfig

    cfg = McpServerConfig(name="fake", type="stdio", command=sys.executable,
                           args=["-m", "tests.helpers.fake_mcp_server"], env={}, scope="user")
    with running_manager({"fake": cfg}, tool_env=dict(os.environ)) as mgr:
        hook = H.HookDef(type="mcp_tool", server="fake", tool="error_tool", input={})
        result = H.run_mcp_tool_hook(hook, {}, mcp_manager=mgr, timeout_s=10)
        ctx.check(f"isError -> exit 1 (non-blocking), got {result.exit_code}", result.exit_code == 1)


@test
def test_mcp_tool_hook_no_manager_is_non_blocking(ctx: Ctx):
    hook = H.HookDef(type="mcp_tool", server="fake", tool="echo")
    result = H.run_mcp_tool_hook(hook, {}, mcp_manager=None, timeout_s=5)
    ctx.check("no manager -> exit 1, never 2", result.exit_code == 1)


# ---- prompt/agent handler ---------------------------------------------------------

@test
def test_prompt_hook_uses_injected_caller_and_substitutes_arguments(ctx: Ctx):
    seen = {}

    def _fake_caller(text, timeout_s):
        seen["text"] = text
        seen["timeout"] = timeout_s
        return json.dumps({"ok": True, "reason": "looks fine"})

    hook = H.HookDef(type="prompt", prompt="Judge this: $ARGUMENTS", timeout_s=7)
    result = H.run_prompt_hook(hook, {"tool_name": "Bash"}, prompt_caller=_fake_caller, timeout_s=hook.effective_timeout_s())
    ctx.check("exit 0", result.exit_code == 0)
    ctx.check("ARGUMENTS substituted with the payload JSON", '"tool_name": "Bash"' in seen["text"])
    ctx.check("timeout forwarded", seen["timeout"] == 7)
    ctx.check("reply text is the caller's own JSON", json.loads(result.stdout)["ok"] is True)


@test
def test_prompt_hook_no_caller_is_silent_allow(ctx: Ctx):
    hook = H.HookDef(type="prompt", prompt="$ARGUMENTS")
    result = H.run_prompt_hook(hook, {}, prompt_caller=None, timeout_s=30)
    ctx.check("exit 0 (never a hard failure)", result.exit_code == 0)
    outcome = H.interpret_hook_result("PreToolUse", result)
    ctx.check("no block, no decision", outcome.blocked is False and outcome.permission_decision is None)


@test
def test_prompt_hook_caller_exception_is_non_blocking(ctx: Ctx):
    def _boom(text, timeout_s):
        raise RuntimeError("model unreachable")

    hook = H.HookDef(type="agent", prompt="$ARGUMENTS")
    result = H.run_prompt_hook(hook, {}, prompt_caller=_boom, timeout_s=60)
    ctx.check(f"exit 1, never 2, got {result.exit_code}", result.exit_code == 1)
    ctx.check("error text mentions the exception", "model unreachable" in result.stderr)


@test
def test_default_timeouts_per_handler_type(ctx: Ctx):
    ctx.check("command default 600s", H.default_timeout_s("command") == H.DEFAULT_COMMAND_TIMEOUT_S == 600.0)
    ctx.check("http default 600s", H.default_timeout_s("http") == 600.0)
    ctx.check("mcp_tool default 600s", H.default_timeout_s("mcp_tool") == 600.0)
    ctx.check("prompt default 30s", H.default_timeout_s("prompt") == 30.0)
    ctx.check("agent default 60s", H.default_timeout_s("agent") == 60.0)
    ctx.check("HookDef honours an explicit override", H.HookDef(type="prompt", timeout_s=5).effective_timeout_s() == 5)


# ---- combination rules ------------------------------------------------------------

@test
def test_combine_outcomes_deny_beats_ask_beats_allow(ctx: Ctx):
    combined = H.combine_outcomes([H.HookOutcome(permission_decision="allow"), H.HookOutcome(permission_decision="ask"),
                                    H.HookOutcome(permission_decision="deny")])
    ctx.check("deny wins", combined.permission_decision == "deny")

    combined2 = H.combine_outcomes([H.HookOutcome(permission_decision="allow"), H.HookOutcome(permission_decision="defer")])
    ctx.check("defer beats allow", combined2.permission_decision == "defer")


@test
def test_combine_outcomes_contexts_concatenated_last_updated_input_wins(ctx: Ctx):
    o1 = H.HookOutcome(additional_context="first", updated_input={"a": 1})
    o2 = H.HookOutcome(additional_context="second", updated_input={"a": 2})
    combined = H.combine_outcomes([o1, o2])
    ctx.check(f"contexts concatenated, got {combined.additional_context!r}", combined.additional_context == "first\n\nsecond")
    ctx.check(f"last updatedInput wins, got {combined.updated_input}", combined.updated_input == {"a": 2})


@test
def test_combine_outcomes_any_block_blocks(ctx: Ctx):
    combined = H.combine_outcomes([H.HookOutcome(), H.HookOutcome(blocked=True, block_reason="nope")])
    ctx.check("blocked propagates", combined.blocked is True)
    ctx.check("reason carried", combined.block_reason == "nope")


@test
def test_hookrunner_runs_multiple_matched_hooks_in_parallel_and_combines(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-multi-")
    hooks_by_event = {"PreToolUse": [
        _hookdef("json_ask", matcher="Bash"),
        _hookdef("json_deny", matcher="Bash"),
    ]}
    runner = _runner(hooks_by_event, tmp)
    payload = runner.payload("PreToolUse", extra={"tool_name": "Bash", "tool_input": {"command": "x"}})
    outcome = runner.run("PreToolUse", payload, matched="Bash", tool_name="Bash", tool_input={"command": "x"})
    ctx.check("deny beats ask across two real hooks", outcome.permission_decision == "deny")


# ---- dedup + once --------------------------------------------------------------

@test
def test_hookrunner_dedup_identical_hooks_across_layers(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-dedup-")
    counter_file = Path(tmp) / "counter.txt"
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("once_counter"), matcher="Bash")
    hooks_by_event = {"PreToolUse": [hook, H.HookDef(**{**hook.__dict__})]}  # identical content, two "layers"
    env = dict(os.environ)
    env["HOOK_ONCE_COUNTER_FILE"] = str(counter_file)
    runner = _runner(hooks_by_event, tmp, effective_env=env)
    payload = runner.payload("PreToolUse")
    runner.run("PreToolUse", payload, matched="Bash", tool_name="Bash", tool_input={})
    lines = counter_file.read_text(encoding="utf-8").splitlines() if counter_file.exists() else []
    ctx.check(f"ran exactly once despite two identical entries, got {len(lines)} invocations", len(lines) == 1)


@test
def test_hookrunner_once_only_fires_a_single_time_across_calls(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-once-")
    counter_file = Path(tmp) / "counter.txt"
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("once_counter"), matcher="Bash", once=True)
    runner = _runner({"PreToolUse": [hook]}, tmp)
    env = dict(runner.effective_env)
    env["HOOK_ONCE_COUNTER_FILE"] = str(counter_file)
    runner.effective_env = env
    payload = runner.payload("PreToolUse")
    runner.run("PreToolUse", payload, matched="Bash", tool_name="Bash", tool_input={})
    runner.run("PreToolUse", payload, matched="Bash", tool_name="Bash", tool_input={})
    runner.run("PreToolUse", payload, matched="Bash", tool_name="Bash", tool_input={})
    lines = counter_file.read_text(encoding="utf-8").splitlines() if counter_file.exists() else []
    ctx.check(f"once=True fires exactly one time across 3 calls, got {len(lines)}", len(lines) == 1)


# ---- Stop cap -----------------------------------------------------------------

@test
def test_stop_cap_9th_consecutive_block_overrides(ctx: Ctx):
    import os as _os
    tmp = tempfile.mkdtemp(prefix="hooks-stopcap-")
    # a script that ALWAYS blocks (exit 2), so we can drive the cap deterministically
    always_block = H.HookDef(type="command", args=[sys.executable, "-c", "import sys; sys.exit(2)"])
    runner = _runner({"Stop": [always_block]}, tmp)
    old_cap = _os.environ.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP")
    _os.environ["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "3"
    try:
        outcomes = [runner.run_stop("Stop") for _ in range(4)]
    finally:
        if old_cap is None:
            _os.environ.pop("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", None)
        else:
            _os.environ["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = old_cap
    ctx.check("blocks 1-3", all(o.blocked for o in outcomes[:3]))
    ctx.check("4th (cap+1) overrides and stops blocking", outcomes[3].blocked is False)
    ctx.check("override message present", any("overriding" in m for m in outcomes[3].system_messages))


@test
def test_stop_hook_active_reflected_in_payload(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-stopactive-")
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("echo_stdin"))
    runner = _runner({"Stop": [hook]}, tmp)
    outcome1 = runner.run_stop("Stop")
    ctx.check("first call: not blocked (echo_stdin exits 0)", outcome1.blocked is False)
    ctx.check("stop_block_count reset to 0 after a non-blocking Stop", runner.stop_block_count == 0)


@test
def test_run_stop_script_blocks_once_via_stop_block_once(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-stoponce-")
    counter_file = Path(tmp) / "stopcount.txt"
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("stop_block_once"))
    env = dict(os.environ)
    env["HOOK_STOP_COUNTER_FILE"] = str(counter_file)
    runner = _runner({"Stop": [hook]}, tmp, effective_env=env)
    first = runner.run_stop("Stop")
    ctx.check("first Stop is blocked", first.blocked is True)
    second = runner.run_stop("Stop")
    ctx.check("second Stop (the model continued once) is not blocked", second.blocked is False)


# ---- SessionEnd budget ------------------------------------------------------------

@test
def test_session_end_budget_default_and_override(ctx: Ctx):
    ctx.check("no hooks -> default 1.5s floor", H.session_end_budget_s([]) == 1.5)
    hooks = [H.HookDef(type="command", timeout_s=10)]
    ctx.check("raised to the largest hook timeout", H.session_end_budget_s(hooks) == 10.0)
    hooks_big = [H.HookDef(type="command", timeout_s=999)]
    ctx.check("capped at 60s", H.session_end_budget_s(hooks_big) == 60.0)

    old = os.environ.get("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS")
    os.environ["CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"] = "2500"
    try:
        ctx.check("env override wins outright", H.session_end_budget_s(hooks_big) == 2.5)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS", None)
        else:
            os.environ["CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"] = old


@test
def test_run_session_end_runs_matched_hooks(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-sessend-")
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("json_deny"))
    runner = _runner({"SessionEnd": [hook]}, tmp)
    outcome = runner.run_session_end("quit")
    ctx.check("session end hook ran and its JSON was interpreted", outcome.permission_decision == "deny")


@test
def test_run_session_end_no_hooks_is_a_fast_noop(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-sessend2-")
    runner = _runner({}, tmp)
    outcome = runner.run_session_end("quit")
    ctx.check("empty outcome", outcome.blocked is False and outcome.permission_decision is None)


# ---- CLAUDE_ENV_FILE ------------------------------------------------------------

@test
def test_env_file_writer_and_read_env_file_exports_round_trip(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-envfile-")
    hook = H.HookDef(type="command", args=_SCRIPT_ARGS("env_file_writer"))
    runner = _runner({"SessionStart": [hook]}, tmp, session_id="envfile-sess")
    payload = runner.payload("SessionStart", extra={"source": "startup"})
    runner.run("SessionStart", payload, matched="startup")
    path = H.env_file_path("envfile-sess")
    ctx.check(f"env file was written at {path}", path.exists())
    exports = H.read_env_file_exports(path)
    ctx.check(f"export parsed, got {exports}", exports.get("HOOK_SCRIPT_VAR") == "from-env-file-writer")
    try:
        path.unlink()
    except OSError:
        pass


@test
def test_env_file_only_set_for_the_right_events(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-envfile2-")
    runner = _runner({}, tmp, session_id="envfile-sess2")
    hook = H.HookDef(type="command")
    env_session_start = runner._env_for(hook, "SessionStart")
    env_pretooluse = runner._env_for(hook, "PreToolUse")
    ctx.check("CLAUDE_ENV_FILE set for SessionStart", "CLAUDE_ENV_FILE" in env_session_start)
    ctx.check("CLAUDE_ENV_FILE NOT set for PreToolUse", "CLAUDE_ENV_FILE" not in env_pretooluse)
    ctx.check("CLAUDE_PROJECT_DIR always set", env_pretooluse.get("CLAUDE_PROJECT_DIR") == str(Path(tmp)))


@test
def test_read_env_file_exports_missing_file_is_empty(ctx: Ctx):
    ctx.check("missing file -> {}", H.read_env_file_exports("/no/such/file-xyz.sh") == {})


# ---- disabled / no hooks --------------------------------------------------------

@test
def test_hookrunner_disabled_returns_noop_without_running_anything(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-disabled-")
    hook = _hookdef("json_deny", matcher="Bash")
    runner = _runner({"PreToolUse": [hook]}, tmp, enabled=False)
    outcome = runner.run("PreToolUse", runner.payload("PreToolUse"), matched="Bash", tool_name="Bash", tool_input={})
    ctx.check("disabled -> neutral outcome", outcome.blocked is False and outcome.permission_decision is None)


@test
def test_hookrunner_no_matching_hooks_is_a_fast_noop(ctx: Ctx):
    tmp = tempfile.mkdtemp(prefix="hooks-nomatch-")
    runner = _runner({}, tmp)
    outcome = runner.run("PreToolUse", runner.payload("PreToolUse"), matched="Bash", tool_name="Bash", tool_input={})
    ctx.check("no hooks configured -> neutral outcome", outcome.blocked is False)


@test
def test_build_payload_common_fields(ctx: Ctx):
    payload = H.build_payload("PreToolUse", session_id="s1", transcript_path="/t.jsonl", cwd="/proj",
                               scratchpad_dir="/scratch", permission_mode="auto", effort="high",
                               prompt_id="p1", agent_id="a1", agent_type="reviewer", extra={"tool_name": "Bash"})
    ctx.check("session_id", payload["session_id"] == "s1")
    ctx.check("hook_event_name", payload["hook_event_name"] == "PreToolUse")
    ctx.check("cwd stringified", payload["cwd"] == "/proj")
    ctx.check("effort wrapped", payload["effort"] == {"level": "high"})
    ctx.check("agent fields present", payload["agent_id"] == "a1" and payload["agent_type"] == "reviewer")
    ctx.check("extra merged", payload["tool_name"] == "Bash")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
