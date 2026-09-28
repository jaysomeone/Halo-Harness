"""tests.helpers.fake_claude_cc -- H11 Part C: a fake `claude` executable
for the `cc:` route's tests. Run as a subprocess exactly the way
`agent.cc_process.build_cc_argv` invokes the real one (`BRIDGE_CLAUDE_EXE`
points a test at `"<python> <this file>"`):

  * `<fake> auth status` -- prints the same JSON shape the real `claude
    auth status` does (see H11 report's live capture), controlled by env
    vars (`FAKE_CLAUDE_CC_LOGGED_IN` default "1", `FAKE_CLAUDE_CC_AUTH_
    METHOD` default "claude.ai").
  * `<fake> -p --model X --output-format stream-json --input-format
    stream-json ... --mcp-config <json> ...` -- emulates the real
    protocol AND actually acts as a real MCP client: it parses the
    `--mcp-config` it was given, spawns the "rolo" stdio server it names
    (the REAL `python -m rolo_claude.ccbridge`, exactly like the real
    claude would), and performs real `tools/list` + `tools/call` over
    stdio against it -- so a Part C test exercises the WHOLE bridge
    (ToolBridgeServer <-> ccbridge child <-> this fake) end to end, not a
    mock of it.

Per-turn behaviour is driven by a small trigger grammar embedded in the
prompt text (a real model isn't running here, so tests need a
deterministic way to ask for a tool call):
  * `TOOL:<name>:<json-args>` -- call `mcp__rolo__<name>` with the given
    arguments once, then report its text content back.
  * `TOOL2:<name1>:<json1>|<name2>:<json2>` -- call two tools in
    sequence in ONE turn (exercises the id-correlation FIFO with two
    different names).
  * `SLEEP:<seconds>` -- sleep before replying (Esc/interrupt tests).
  * anything containing "pong" (case-insensitive) -- replies "pong"
    (mirrors the real live acceptance line).
  * anything else -- replies "noted".
Every turn emits `system/init` (first turn only), `stream_event` text
deltas, the `assistant` tool_use/text lines, the `user` tool_result echo,
and a `result` line with `usage`/`total_cost_usd`/`modelUsage` shaped like
the real thing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid


def _auth_status_json() -> dict:
    logged_in = os.environ.get("FAKE_CLAUDE_CC_LOGGED_IN", "1").strip().lower() not in ("0", "false", "no", "")
    return {
        "loggedIn": logged_in,
        "authMethod": os.environ.get("FAKE_CLAUDE_CC_AUTH_METHOD", "claude.ai"),
        "apiProvider": "firstParty",
        "email": os.environ.get("FAKE_CLAUDE_CC_EMAIL", "test@example.com"),
        "subscriptionType": os.environ.get("FAKE_CLAUDE_CC_SUBSCRIPTION_TYPE", "max"),
    }


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _parse_argv(argv: "list[str]") -> dict:
    opts: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--model", "--session-id", "--resume", "--mcp-config", "--settings",
                  "--append-system-prompt", "--permission-mode", "--max-turns"):
            opts[a.lstrip("-")] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a == "--tools":
            opts["tools"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        i += 1
    return opts


_TOOL_RE = re.compile(r"^TOOL:([^:]+):(.*)$", re.DOTALL)
_TOOL2_RE = re.compile(r"^TOOL2:(.*)$", re.DOTALL)
_SLEEP_RE = re.compile(r"SLEEP:(\d+(?:\.\d+)?)")


def _extract_prompt_text(line: dict) -> str:
    msg = line.get("message") or {}
    content = msg.get("content") or []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return ""


async def _call_tool(mcp_session, wire_name: str, arguments: dict):
    result = await mcp_session.call_tool(wire_name, arguments)
    text_parts = [getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None) is not None]
    return "\n".join(text_parts), bool(result.is_error)


async def _run_one_turn(mcp_session, session_id: str, prompt_text: str, turn_index: int) -> None:
    msg_id = f"msg_fake_{turn_index}"

    sleep_m = _SLEEP_RE.search(prompt_text)
    if sleep_m:
        await asyncio.sleep(float(sleep_m.group(1)))

    tool_calls: "list[tuple[str, dict]]" = []
    m2 = _TOOL2_RE.match(prompt_text)
    m1 = _TOOL_RE.match(prompt_text)
    if m2:
        for part in m2.group(1).split("|"):
            name, _, raw_args = part.partition(":")
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append((name.strip(), args))
    elif m1:
        name, raw_args = m1.group(1), m1.group(2)
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
        tool_calls.append((name.strip(), args))

    if tool_calls:
        _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                                    "content": [{"type": "text", "text": "Calling tool(s)."}]},
                 "session_id": session_id})
        results = []
        for name, args in tool_calls:
            wire_name = f"mcp__rolo__{name}"
            tool_use_id = f"toolu_fake_{uuid.uuid4().hex[:12]}"
            _write({"type": "assistant",
                     "message": {"id": msg_id, "role": "assistant",
                                 "content": [{"type": "tool_use", "id": tool_use_id, "name": wire_name, "input": args}]},
                     "session_id": session_id})
            text, is_error = await _call_tool(mcp_session, name, args)
            _write({"type": "user",
                     "message": {"role": "user",
                                 "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                              "content": [{"type": "text", "text": text}], "is_error": is_error}]},
                     "session_id": session_id})
            results.append(text)
        final_text = " | ".join(results)
    elif "pong" in prompt_text.lower():
        final_text = "pong"
    else:
        final_text = "noted"

    for chunk in (final_text[:1] or " "), final_text[1:]:
        if chunk:
            _write({"type": "stream_event",
                     "event": {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": chunk}},
                     "session_id": session_id})
    _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                               "content": [{"type": "text", "text": final_text}]},
             "session_id": session_id})

    usage = {"input_tokens": 2, "output_tokens": max(1, len(final_text.split())),
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    _write({
        "type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
        "session_id": session_id, "duration_api_ms": 5, "duration_ms": 6,
        "total_cost_usd": 0.0001, "usage": usage, "result": final_text,
        "modelUsage": {"fake-model": {"inputTokens": usage["input_tokens"], "outputTokens": usage["output_tokens"],
                                        "costUSD": 0.0001, "canonicalModel": "fake-model"}},
    })


async def _amain(argv: "list[str]") -> int:
    opts = _parse_argv(argv)
    session_id = opts.get("session-id") or opts.get("resume") or str(uuid.uuid4())
    mcp_config_raw = opts.get("mcp-config") or '{"mcpServers":{}}'
    try:
        mcp_config = json.loads(mcp_config_raw)
    except json.JSONDecodeError:
        mcp_config = {"mcpServers": {}}
    servers = (mcp_config.get("mcpServers") or {})
    rolo_cfg = servers.get("rolo")

    tools_wire: "list[str]" = []
    turn_index = 0

    async def _serve_turns(mcp_session=None) -> None:
        nonlocal turn_index
        if mcp_session is not None:
            listed = await mcp_session.list_tools()
            tools_wire.extend(f"mcp__rolo__{t.name}" for t in listed.tools)
        _write({"type": "system", "subtype": "init", "session_id": session_id, "tools": tools_wire,
                 "mcp_servers": ([{"name": "rolo", "status": "connected"}] if rolo_cfg else []),
                 "model": opts.get("model", "fake-model"), "permissionMode": opts.get("permission-mode", "default"),
                 "claude_code_version": "0.0.0-fake"})
        loop = asyncio.get_event_loop()
        while True:
            raw_line = await loop.run_in_executor(None, sys.stdin.readline)
            if raw_line == "":
                return
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            turn_index += 1
            prompt_text = _extract_prompt_text(parsed)
            await _run_one_turn(mcp_session, session_id, prompt_text, turn_index)

    if rolo_cfg:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (rolo_cfg.get("env") or {}).items()})
        params = StdioServerParameters(command=rolo_cfg["command"], args=rolo_cfg.get("args") or [], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                await _serve_turns(mcp_session)
    else:
        await _serve_turns(None)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "auth" and argv[1] == "status":
        _write(_auth_status_json())
        return 0
    if not argv or argv[0] != "-p":
        # Any other invocation (e.g. a bare version probe) -- harmless no-op.
        return 0
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
