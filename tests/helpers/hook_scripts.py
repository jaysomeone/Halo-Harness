"""tests.helpers.hook_scripts -- small stdin-JSON-in / stdout-behavior-out
scripts a test's own `hooks.HookDef(type="command", args=[sys.executable,
"-m", "tests.helpers.hook_scripts", "<mode>"])` can point at, for testing
every handler-type/exit-code/JSON-output combination the hooks protocol
needs to get right -- portable across Windows/WSL/Kali (no .sh/.ps1 files),
same pattern as this project's other fake_* test servers.

Modes (argv[1], or the HOOK_SCRIPT_MODE env var):
  exit2_json_allow -- exit 2 (hard block) but ALSO prints a JSON
                      hookSpecificOutput.permissionDecision=allow (proves
                      exit 2 blocks REGARDLESS of what the JSON says).
  json_deny        -- exit 0, JSON hookSpecificOutput.permissionDecision=deny.
  json_allow       -- exit 0, JSON hookSpecificOutput.permissionDecision=allow.
  json_ask         -- exit 0, JSON hookSpecificOutput.permissionDecision=ask.
  plain_context    -- exit 0, plain (non-JSON) stdout text -- a context-only
                      event (UserPromptSubmit/UserPromptExpansion/
                      SessionStart/PostModelSwitch) should pick this up as
                      additionalContext; any other event should NOT.
  sleep            -- sleeps HOOK_SLEEP_S seconds (default 5.0) then exits 0
                      -- for timeout tests.
  exit1_stderr     -- exit 1 (non-blocking) with a stderr message.
  env_file_writer  -- writes `export NAME=value` lines to the path named by
                      the CLAUDE_ENV_FILE env var it was invoked with (a
                      real SessionStart/Setup/CwdChanged/FileChanged hook's
                      own contract).
  updated_input    -- exit 0, JSON hookSpecificOutput.updatedInput that
                      rewrites tool_input["command"] to "echo rewritten".
  updated_input_no_decision -- exit 0, updatedInput rewrites the command to
                      "echo rewritten-dangerous", with NO permissionDecision
                      of its own (finding 13: the loop must re-decide
                      against the rewritten input).
  block_decision   -- exit 0, JSON {"decision": "block", "reason": "..."}
                      (the generic block shape, not permissionDecision).
  stop_block_once  -- exit 2 the FIRST time it's called (tracked via the
                      HOOK_STOP_COUNTER_FILE env var/file), exit 0 every
                      time after -- for the "Stop hook exits 2 once" case.
  echo_stdin       -- exit 0, echoes the parsed stdin payload back as JSON
                      (so a test can see EXACTLY what payload a call site
                      built, field for field).
  once_counter     -- appends one "1" line to the file named by
                      HOOK_ONCE_COUNTER_FILE each time it actually runs
                      (counting REAL invocations is the only reliable way
                      to prove a hook did or didn't run twice) -- exit 0.
  permission_request_allow_setmode_addrules -- exit 0, PermissionRequest-
                      shaped JSON: behavior=allow + updatedPermissions as
                      the REAL 2.1.281 list shape (a setMode entry and an
                      addRules/deny entry).
  permission_request_updated_input_redecide -- exit 0, PermissionRequest-
                      shaped JSON with only updatedInput (no behavior of
                      its own) -- the loop must re-decide against it.
  permission_request_interrupt -- exit 0, PermissionRequest-shaped JSON:
                      behavior=deny + interrupt=true.
"""
from __future__ import annotations

import json
import os
import sys
import time


def _read_stdin_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = (argv[0] if argv else None) or os.environ.get("HOOK_SCRIPT_MODE") or "echo_stdin"
    payload = _read_stdin_payload()

    if mode == "exit2_json_allow":
        _print_json({"hookSpecificOutput": {"permissionDecision": "allow",
                                             "permissionDecisionReason": "should be ignored"}})
        print("blocked by exit2_json_allow", file=sys.stderr)
        return 2

    if mode == "json_deny":
        _print_json({"hookSpecificOutput": {"permissionDecision": "deny",
                                             "permissionDecisionReason": "denied by json_deny script"}})
        return 0

    if mode == "json_allow":
        _print_json({"hookSpecificOutput": {"permissionDecision": "allow"}})
        return 0

    if mode == "json_ask":
        _print_json({"hookSpecificOutput": {"permissionDecision": "ask",
                                             "permissionDecisionReason": "asked by json_ask script"}})
        return 0

    if mode == "plain_context":
        print("plain-context-from-hook-script")
        return 0

    if mode == "sleep":
        time.sleep(float(os.environ.get("HOOK_SLEEP_S", "5.0")))
        return 0

    if mode == "exit1_stderr":
        print("non-blocking failure from exit1_stderr", file=sys.stderr)
        return 1

    if mode == "env_file_writer":
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write("export HOOK_SCRIPT_VAR=from-env-file-writer\n")
        return 0

    if mode == "env_file_writer_var_expansion":
        # H5b finding 10: Claude Code's own SessionStart docs example,
        # verbatim -- `export PATH="$PATH:/some/dir"` must APPEND to the
        # shell's real inherited PATH via $VAR expansion, never overwrite
        # it with the literal four-character string "$PATH:/some/dir".
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write('export PATH="$PATH:/rolo-h5b-f10-marker"\n')
        return 0

    if mode == "env_file_writer_secret_leak_probe":
        # H9 whole-tree review finding 5: this hook process ITSELF already
        # gets `tool_child_env()`-stripped env (HookRunner always builds
        # it that way -- $OPENROUTER_API_KEY is genuinely unset right
        # here, nothing to leak from THIS process). The bug was one level
        # removed: the LINE this hook appends to $CLAUDE_ENV_FILE is not
        # run by this process at all -- it's sourced LATER, by halo
        # itself (agent/loop.py's `_fire_session_start` ->
        # hooks.read_env_file_exports), and that sourcing subprocess used
        # to inherit the harness's RAW, unstripped os.environ as its own
        # `base_env` default. Writing a `$VAR` REFERENCE (never reading it
        # here) is what proves which environment actually did the
        # expansion.
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write('export ROLO_H9B_LEAK_PROBE="$OPENROUTER_API_KEY"\n')
        return 0

    if mode == "updated_input":
        _print_json({"hookSpecificOutput": {"permissionDecision": "allow",
                                             "updatedInput": {"command": "echo rewritten"}}})
        return 0

    if mode == "updated_input_no_decision":
        # finding 13 (h4-h5-h3c review): rewrites the command but sets NO
        # permission_decision of its own -- the loop must re-run decide()
        # against the REWRITTEN input rather than trusting whatever was
        # decided for the ORIGINAL one.
        _print_json({"hookSpecificOutput": {"updatedInput": {"command": "echo rewritten-dangerous"}}})
        return 0

    if mode == "block_decision":
        _print_json({"decision": "block", "reason": "blocked by block_decision script"})
        return 0

    if mode == "stop_block_once":
        counter_file = os.environ.get("HOOK_STOP_COUNTER_FILE")
        count = 0
        if counter_file and os.path.exists(counter_file):
            try:
                count = int(open(counter_file, encoding="utf-8").read().strip() or "0")
            except (ValueError, OSError):
                count = 0
        if counter_file:
            with open(counter_file, "w", encoding="utf-8") as f:
                f.write(str(count + 1))
        if count == 0:
            print("stop blocked once", file=sys.stderr)
            return 2
        return 0

    if mode == "once_counter":
        counter_file = os.environ.get("HOOK_ONCE_COUNTER_FILE")
        if counter_file:
            with open(counter_file, "a", encoding="utf-8") as f:
                f.write("1\n")
        return 0

    if mode == "permission_request_allow_setmode_addrules":
        # H5c finding 9: PermissionRequest's real `updatedPermissions` wire
        # shape is a LIST of {type, ...} entries -- setMode changes the
        # session's own mode, addRules teaches it a new deny rule, both
        # alongside an "allow" for THIS one call.
        _print_json({"hookSpecificOutput": {"decision": {
            "behavior": "allow",
            "updatedPermissions": [
                {"type": "setMode", "mode": "acceptEdits"},
                {"type": "addRules", "behavior": "deny",
                 "rules": [{"toolName": "Bash", "ruleContent": "rm -rf *"}]},
            ],
        }}})
        return 0

    if mode == "permission_request_updated_input_redecide":
        # H5c finding 9: rewrites the command to something a user's OWN
        # deny rule matches, with NO explicit behavior of its own -- the
        # loop must re-run decide() against the REWRITTEN input (same
        # reasoning as PreToolUse's `updated_input_no_decision` above)
        # rather than letting the hook's silence fall through to an allow.
        _print_json({"hookSpecificOutput": {"decision": {
            "updatedInput": {"command": "rm -rf /rewritten-by-hook"},
        }}})
        return 0

    if mode == "permission_request_interrupt":
        # H5c finding 9: `interrupt: true` on a deny must end the WHOLE
        # turn, not just refuse this one call.
        _print_json({"hookSpecificOutput": {"decision": {
            "behavior": "deny", "message": "stop everything right now", "interrupt": True,
        }}})
        return 0

    # echo_stdin (default): round-trips the payload so a test can assert on it directly.
    _print_json({"received": payload})
    return 0


if __name__ == "__main__":
    sys.exit(main())
