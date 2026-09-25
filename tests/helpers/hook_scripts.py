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
  set_mode         -- exit 0, JSON hookSpecificOutput.updatedPermissions.
                      setMode="acceptEdits".
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

    if mode == "set_mode":
        _print_json({"hookSpecificOutput": {"updatedPermissions": {"setMode": "acceptEdits"}}})
        return 0

    # echo_stdin (default): round-trips the payload so a test can assert on it directly.
    _print_json({"received": payload})
    return 0


if __name__ == "__main__":
    sys.exit(main())
