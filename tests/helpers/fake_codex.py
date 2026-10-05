"""tests.helpers.fake_codex -- Halo 2.0.3 round 5i part 2: a fake `codex`
executable for the `cx:` route's tests, the Codex counterpart of
`fake_claude_cc.py` (same job, simpler protocol -- CODEX-RESEARCH.md
section 7: each invocation is ONE bounded turn, never a held-open stdin
stream). Run as a subprocess exactly the way `agent.codex_process.
build_cx_argv` invokes the real one (`HALO_CODEX_EXE` points a test at
`"<python>" "<this file>"`):

  * `<fake> login status` -- plain text, exit 0/1, controlled by
    `FAKE_CODEX_LOGIN_STATUS` (default "Logged in using ChatGPT").
  * `<fake> --version` -- a real `codex --version` line.
  * `<fake> exec [resume <id>] --json ... [-c mcp_servers.halo.<field>=
    <value> ...] <prompt>` -- emits the JSONL event protocol documented in
    CODEX-RESEARCH.md section 6, and ACTS as a real MCP client against
    whatever `mcp_servers.halo` names (the REAL `python -m halo_harness.
    ccbridge`, same as the real codex would) when the prompt asks for a
    tool call.

Trigger grammar in the prompt text (bare prompt, or the text after the
one-time preamble `codex_turn._cx_preamble` prepends -- this fake looks
for the triggers ANYWHERE in the prompt, not just at its start, so the
preamble never masks them):
  * `TOOL:<name>:<json-args>` -- call `mcp__halo__<name>` once.
  * `SLEEP:<seconds>` -- sleep before replying (steer-window tests).
  * `ERROR:<message>` -- a `turn.failed` event instead of `turn.completed`.
  * `REFUSE_MODEL` -- (combined with `-m <id>` containing this model id)
    a top-level `error` event and nonzero exit, simulating an unknown
    model id.
  * anything containing "pong" -- replies "pong"; anything else -- "noted".

`FAKE_CODEX_ARGV_LOG` (a path), when set, gets one JSON-array line per
invocation -- lets a test assert the exact argv a real caller built.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid

_THREAD_REGISTRY_ENV = "FAKE_CODEX_THREAD_REGISTRY"  # path, optional: tracks resumed ids


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log_argv(argv: "list[str]") -> None:
    path = os.environ.get("FAKE_CODEX_ARGV_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _cmd_login_status() -> int:
    text = os.environ.get("FAKE_CODEX_LOGIN_STATUS", "Logged in using ChatGPT")
    print(text)
    return 0 if "logged in" in text.lower() and "not logged in" not in text.lower() else 1


def _parse_exec_argv(argv: "list[str]") -> dict:
    """`argv` is everything after `exec` (e.g. `["resume", "<id>", "--json",
    ..., "<prompt>"]`)."""
    opts = {"resume_id": None, "model": None, "mcp": {"command": None, "args": [], "env_vars": []}, "prompt": ""}
    i = 0
    if argv and argv[0] == "resume":
        opts["resume_id"] = argv[1] if len(argv) > 1 else None
        i = 2
    positional = []
    while i < len(argv):
        a = argv[i]
        if a == "-m":
            opts["model"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a == "-c" and i + 1 < len(argv):
            kv = argv[i + 1]
            key, _, value = kv.partition("=")
            if key == "mcp_servers.halo.command":
                opts["mcp"]["command"] = json.loads(value)
            elif key == "mcp_servers.halo.args":
                opts["mcp"]["args"] = json.loads(value)
            elif key == "mcp_servers.halo.env_vars":
                opts["mcp"]["env_vars"] = json.loads(value)
            i += 2
            continue
        if a in ("--json", "--skip-git-repo-check", "--ephemeral"):
            i += 1
            continue
        if a == "-s" or a == "-i" or a == "-o":
            i += 2
            continue
        positional.append(a)
        i += 1
    opts["prompt"] = positional[-1] if positional else ""
    return opts


async def _call_tool(name: str, args: dict, mcp_cfg: dict):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    for var in mcp_cfg.get("env_vars") or []:
        if var in os.environ:
            env[var] = os.environ[var]
    params = StdioServerParameters(command=mcp_cfg["command"], args=mcp_cfg.get("args") or [], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            text = "".join(getattr(b, "text", "") for b in result.content)
            return text, bool(result.is_error)


def _next_item_id() -> str:
    return f"item_{uuid.uuid4().hex[:8]}"


async def _run_exec(opts: dict) -> int:
    thread_id = opts["resume_id"] or str(uuid.uuid4())
    _write({"type": "thread.started", "thread_id": thread_id})
    _write({"type": "turn.started"})

    prompt = opts["prompt"]
    sleep_m = re.search(r"SLEEP:(\d+(?:\.\d+)?)", prompt)
    if sleep_m:
        time.sleep(float(sleep_m.group(1)))
    if "REFUSE_MODEL" in prompt:
        _write({"type": "error", "message": f"model not found: {opts.get('model')}"})
        return 1
    error_m = re.search(r"ERROR:(\S+)", prompt)
    tool_m = re.search(r"TOOL:([A-Za-z0-9_]+):(\{.*\})(?:$|\s)", prompt)

    if tool_m and opts["mcp"]["command"]:
        name, args_json = tool_m.group(1), tool_m.group(2)
        call_id = _next_item_id()
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError:
            args = {}
        _write({"type": "item.started", "item": {"id": call_id, "type": "mcp_tool_call", "tool": name,
                                                    "arguments": args}})
        text, is_error = await _call_tool(name, args, opts["mcp"])
        _write({"type": "item.completed", "item": {"id": call_id, "type": "mcp_tool_call", "tool": name,
                                                      "arguments": args, "result": text, "is_error": is_error}})
        reply = f"tool said: {text}"
    elif error_m:
        _write({"type": "turn.failed", "error": {"message": error_m.group(1)}})
        return 1
    elif "pong" in prompt:
        reply = "pong"
    else:
        reply = "noted"

    msg_id = _next_item_id()
    _write({"type": "item.started", "item": {"id": msg_id, "type": "agent_message", "text": ""}})
    _write({"type": "item.completed", "item": {"id": msg_id, "type": "agent_message", "text": reply}})
    _write({"type": "turn.completed", "usage": {"input_tokens": 42, "cached_input_tokens": 0, "output_tokens": 7}})
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = sys.argv[1:] if argv is None else argv
    _log_argv(argv)
    if not argv:
        return 0
    if argv[0] == "login" and len(argv) > 1 and argv[1] == "status":
        return _cmd_login_status()
    if argv[0] == "--version":
        print("codex-cli 0.155.1-fake")
        return 0
    if argv[0] == "exec":
        opts = _parse_exec_argv(argv[1:])
        return asyncio.run(_run_exec(opts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
