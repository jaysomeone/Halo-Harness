"""tests.helpers.fake_claude_cc -- H11/H11b Part C: a fake `claude`
executable for the `cc:` route's tests. Run as a subprocess exactly the
way `agent.cc_process.build_cc_argv` invokes the real one
(`BRIDGE_CLAUDE_EXE` points a test at `"<python> <this file>"`):

  * `<fake> auth status` -- the real `claude auth status` JSON shape,
    controlled by env vars (`FAKE_CLAUDE_CC_LOGGED_IN` default "1",
    `FAKE_CLAUDE_CC_AUTH_METHOD` default "claude.ai").
  * `<fake> --version` -- H11b finding 23: a real `claude --version` line.
  * `<fake> -p --model X --output-format stream-json --input-format
    stream-json ... --mcp-config <json> ...` -- emulates the real
    protocol (including `--replay-user-messages` echoes, cumulative
    `total_cost_usd`, `--resume`/`--session-id` validation) AND actually
    acts as a real MCP client: it spawns the "rolo" stdio server named in
    `--mcp-config` (the REAL `python -m halo_harness.ccbridge`) and
    performs real `tools/list`/`tools/call` over stdio, reacting to a real
    `notifications/tools/list_changed` by re-listing (H11b finding 6).

Per-round behaviour is driven by a trigger grammar in the prompt text:
  * `TOOL:<name>:<json-args>` -- call `mcp__rolo__<name>` once.
  * `TOOL2:<name1>:<json1>|<name2>:<json2>` -- call two tools in sequence;
    BETWEEN calls, any line that has ALREADY arrived on stdin (sent while
    this round is running) is echoed and folded into this SAME round's
    one final result (H11b critical finding 1's "absorbed" shape) --
    a single-call/plain-text round never checks, so a line sent while ONE
    of those is running becomes its own SEPARATE next round instead
    (finding 1's "queued behind a running turn" shape).
  * `WAITFOR:<name>:<json-args>` -- wait (bounded) for a real
    `notifications/tools/list_changed`, re-list, then call `<name>`.
  * `SLEEP:<seconds>` -- sleep before replying.
  * `ERROR:<subtype>` -- an error-shaped reply: only a final `assistant`
    message (no stream deltas) plus `result` with `is_error: true`.
  * anything containing "pong" -- replies "pong"; anything else --
    "noted".

`--session-id`/`--resume` validation (finding 9) is OPT IN, via
`FAKE_CLAUDE_CC_REGISTRY` (a path): unset (every existing test) means
every id is accepted, exactly like before this milestone.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid

_EOF = object()


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
        if a == "--fork-session":
            opts["fork-session"] = True
            i += 1
            continue
        i += 1
    return opts


def _load_registry(path: str) -> set:
    try:
        return set(json.loads(open(path, encoding="utf-8").read()))
    except (OSError, ValueError):
        return set()


def _save_registry(path: str, ids: set) -> None:
    try:
        open(path, "w", encoding="utf-8").write(json.dumps(sorted(ids)))
    except OSError:
        pass


def _validate_session_id(opts: dict) -> "tuple[str, bool]":
    """`(session_id, ok)` -- finding 9's opt-in registry check. `ok=False`
    means "print the real failure text to stderr and exit 1 with NO
    stdout at all" (real claude's own shape for a bad --resume/--session-
    id, verified live)."""
    registry_path = os.environ.get("FAKE_CLAUDE_CC_REGISTRY")
    resume_id, fresh_id = opts.get("resume"), opts.get("session-id")
    if not registry_path:
        return resume_id or fresh_id or str(uuid.uuid4()), True
    ids = _load_registry(registry_path)
    if resume_id:
        if resume_id not in ids:
            print(f"No conversation found with session ID: {resume_id}", file=sys.stderr)
            return resume_id, False
        if opts.get("fork-session"):
            new_id = str(uuid.uuid4())
            ids.add(new_id)
            _save_registry(registry_path, ids)
            return new_id, True
        return resume_id, True
    if fresh_id:
        if fresh_id in ids:
            print(f"Session ID {fresh_id} is already in use.", file=sys.stderr)
            return fresh_id, False
        ids.add(fresh_id)
        _save_registry(registry_path, ids)
        return fresh_id, True
    return str(uuid.uuid4()), True


_TOOL_RE = re.compile(r"^TOOL:([^:]+):(.*)$", re.DOTALL)
_TOOL2_RE = re.compile(r"^TOOL2:(.*)$", re.DOTALL)
_WAITFOR_RE = re.compile(r"^WAITFOR:([^:]+):(.*)$", re.DOTALL)
_SLEEP_RE = re.compile(r"SLEEP:(\d+(?:\.\d+)?)")
_ERROR_RE = re.compile(r"^ERROR:(\w+)$")


def _extract_text(line: dict) -> str:
    msg = line.get("message") or {}
    content = msg.get("content") or []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return ""


def _echo(line: dict, session_id: str) -> None:
    """`--replay-user-messages` -- verbatim echo of what was sent, marked
    `isReplay: true` (live-verified against 2.1.284)."""
    _write({"type": "user", "message": line.get("message") or {"role": "user", "content": []},
             "session_id": session_id, "parent_tool_use_id": None, "uuid": str(uuid.uuid4()), "isReplay": True})


async def _call_tool(mcp_session, wire_name: str, arguments: dict):
    result = await mcp_session.call_tool(wire_name, arguments)
    parts = []
    for c in result.content or []:
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
            continue
        # finding 7 "MCP image content out": a REAL mcp.types.ImageContent
        # -- note its presence/mime type so a test can prove an image
        # block actually flowed through the bridge, never "[image block]".
        data = getattr(c, "data", None)
        if data is not None:
            mime = getattr(c, "mime_type", None) or "?"
            parts.append(f"[image:{mime}:{len(data)}b]")
    return "\n".join(parts), bool(result.is_error)


class _Cost:
    """Cumulative `total_cost_usd` for THIS fake process -- finding 28:
    the real binary's own cost is cumulative per process, never per turn."""
    total = 0.0


async def _run_one_call(mcp_session, session_id: str, msg_id: str, name: str, args: dict) -> str:
    wire_name = f"mcp__rolo__{name}"
    tool_use_id = f"toolu_fake_{uuid.uuid4().hex[:12]}"
    _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                               "content": [{"type": "tool_use", "id": tool_use_id, "name": wire_name,
                                                             "input": args}]},
             "session_id": session_id})
    text, is_error = await _call_tool(mcp_session, name, args)
    _write({"type": "user",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                                         "content": [{"type": "text", "text": text}],
                                                         "is_error": is_error}]},
             "session_id": session_id})
    return text


def _write_result(session_id: str, final_text: str, *, is_error: bool = False, subtype: str = "success") -> None:
    _Cost.total += max(0.0002, len(final_text) * 0.00001)
    usage = {"input_tokens": 2, "output_tokens": max(1, len(final_text.split())),
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    _write({
        "type": "result", "subtype": subtype, "is_error": is_error, "stop_reason": "end_turn",
        "session_id": session_id, "duration_api_ms": 5, "duration_ms": 6,
        "total_cost_usd": round(_Cost.total, 6), "usage": usage, "result": final_text,
        "modelUsage": {"fake-model": {"inputTokens": usage["input_tokens"], "outputTokens": usage["output_tokens"],
                                        "costUSD": round(_Cost.total, 6), "canonicalModel": "fake-model"}},
    })


async def _run_round(mcp_session, session_id: str, lines: list, turn_index: int, line_queue) -> None:
    """One claude-native "round": echoes every line in `lines` (`lines[0]`
    is what arrived when this round started; anything else in it was
    ALREADY queued behind it at that exact moment -- critical finding 1's
    "a batch of queued lines as one combined reply" shape, e.g. two
    steers sent while a plain SLEEP-shaped round was still running both
    land in the FOLLOW-UP round's own `lines` together), does the work
    `lines[0]`'s text asks for, and (ONLY for a TOOL2 multi-call round --
    see module docstring) ALSO checks between calls for a line that
    arrives WHILE this round is already running (arrives late enough
    that it MISSED the batch above), folding each one it finds into THIS
    same round's one final result (finding 1's OTHER shape: one steer
    between tool calls, absorbed into the turn already generating)."""
    msg_id = f"msg_fake_{turn_index}"
    first_line = lines[0]
    for ln in lines:
        _echo(ln, session_id)
    absorbed_notes = [f"(absorbed: {_extract_text(ln)[:40]})" for ln in lines[1:]]
    prompt_text = _extract_text(first_line)

    m = _ERROR_RE.match(prompt_text)
    if m:
        _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                                    "content": [{"type": "text", "text": f"error: {m.group(1)}"}]},
                 "session_id": session_id})
        _write_result(session_id, f"error: {m.group(1)}", is_error=True, subtype=m.group(1))
        return

    if prompt_text == "ENVDUMP":
        # critical finding 2: report which sentinel-shaped keys THIS
        # process (standing in for the real claude subprocess) actually
        # sees -- a test asserts a sentinel value set on the PARENT rolo-
        # claude process never appears here. Goes through `_finish_round`
        # (streamed deltas), never a bare `_write_result`, so it round-
        # trips through the same text_delta path a normal reply would.
        keys = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                 "DATABRICKS_TOKEN", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID")
        seen = {k: os.environ[k] for k in keys if k in os.environ}
        await _finish_round(session_id, msg_id, "ENV:" + json.dumps(seen, sort_keys=True))
        return

    if prompt_text == "REPORT_IMAGE":
        # finding 4/7 "stream-json image content in": did the message WE
        # were sent carry a real Anthropic image block?
        content = (first_line.get("message") or {}).get("content") or []
        images = [b for b in content if isinstance(b, dict) and b.get("type") == "image"]
        mime = (images[0].get("source") or {}).get("media_type", "") if images else None
        await _finish_round(session_id, msg_id, f"SAW_IMAGE:{mime}" if images else "NO_IMAGE")
        return

    sleep_m = _SLEEP_RE.search(prompt_text)
    if sleep_m:
        await asyncio.sleep(float(sleep_m.group(1)))

    wf = _WAITFOR_RE.match(prompt_text)
    if wf and mcp_session is not None:
        name, raw_args = wf.group(1).strip(), wf.group(2)
        args = json.loads(raw_args) if raw_args.strip() else {}
        changed = await _wait_for_list_changed(mcp_session, timeout=8.0)
        listed = await mcp_session.list_tools()
        present = any(t.name == name for t in listed.tools)
        text = await _run_one_call(mcp_session, session_id, msg_id, name, args)
        final_text = f"changed={changed} present={present} result={text}"
    else:
        results = []
        m1, m2 = _TOOL_RE.match(prompt_text), _TOOL2_RE.match(prompt_text)
        if m2:
            calls = []
            for part in m2.group(1).split("|"):
                name, _, raw_args = part.partition(":")
                calls.append((name.strip(), json.loads(raw_args) if raw_args.strip() else {}))
            _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                                        "content": [{"type": "text", "text": "Calling tool(s)."}]},
                     "session_id": session_id})
            for i, (name, args) in enumerate(calls):
                results.append(await _run_one_call(mcp_session, session_id, msg_id, name, args))
                if i < len(calls) - 1:
                    # A real window for a steer sent right after this
                    # round started to actually land BEFORE the next call
                    # -- two real (fast) tool calls back to back leave no
                    # such window at all on their own.
                    await asyncio.sleep(0.5)
                    absorbed_notes.extend(await _drain_absorbed(mcp_session, session_id, msg_id, line_queue))
        elif m1:
            name, raw_args = m1.group(1), m1.group(2)
            args = json.loads(raw_args) if raw_args.strip() else {}
            _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                                        "content": [{"type": "text", "text": "Calling tool."}]},
                     "session_id": session_id})
            results.append(await _run_one_call(mcp_session, session_id, msg_id, name, args))
        elif "pong" in prompt_text.lower():
            results.append("pong")
        else:
            results.append("noted")
        final_text = " | ".join(results + absorbed_notes)

    await _finish_round(session_id, msg_id, final_text)


async def _finish_round(session_id: str, msg_id: str, final_text: str) -> None:
    """Streamed text deltas (so `state.turn_had_deltas` is True on the
    parent side, the normal/common shape) + the closing `assistant`
    message + `result` -- shared by every non-error reply."""
    for chunk in (final_text[:1] or " "), final_text[1:]:
        if chunk:
            _write({"type": "stream_event",
                     "event": {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": chunk}},
                     "session_id": session_id})
    _write({"type": "assistant", "message": {"id": msg_id, "role": "assistant",
                                               "content": [{"type": "text", "text": final_text}]},
             "session_id": session_id})
    _write_result(session_id, final_text)


async def _drain_absorbed(mcp_session, session_id: str, msg_id: str, line_queue) -> list:
    """Non-blocking: everything ALREADY sitting on `line_queue` right now
    (sent while THIS round was running) -- echoed and folded into this
    same round (critical finding 1's "absorbed" shape); a `TOOL:` line
    among them is actually called too."""
    notes = []
    while True:
        try:
            line = line_queue.get_nowait()
        except asyncio.QueueEmpty:
            return notes
        if line is _EOF:
            continue
        _echo(line, session_id)
        text = _extract_text(line)
        m = _TOOL_RE.match(text)
        if m:
            name, raw_args = m.group(1), m.group(2)
            args = json.loads(raw_args) if raw_args.strip() else {}
            notes.append(await _run_one_call(mcp_session, session_id, msg_id, name, args))
        else:
            notes.append(f"(absorbed: {text[:40]})")


class _ListChanged:
    event: "asyncio.Event | None" = None


async def _wait_for_list_changed(mcp_session, *, timeout: float) -> bool:
    if _ListChanged.event is None:
        return False
    try:
        await asyncio.wait_for(_ListChanged.event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _serve_turns(mcp_session, session_id: str, tools_wire: list) -> None:
    _write({"type": "system", "subtype": "init", "session_id": session_id, "tools": tools_wire,
             "mcp_servers": ([{"name": "rolo", "status": "connected"}] if mcp_session is not None else []),
             "model": "fake-model", "permissionMode": "bypassPermissions", "claude_code_version": "0.0.0-fake"})
    loop = asyncio.get_event_loop()
    line_queue: "asyncio.Queue" = asyncio.Queue()

    async def _reader() -> None:
        while True:
            raw_line = await loop.run_in_executor(None, sys.stdin.readline)
            if raw_line == "":
                await line_queue.put(_EOF)
                return
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            await line_queue.put(parsed)

    reader_task = asyncio.create_task(_reader())
    turn_index = 0
    try:
        while True:
            first = await line_queue.get()
            if first is _EOF:
                return
            # critical finding 1 "a batch of queued lines as one combined
            # reply": everything ELSE already sitting on `line_queue` at
            # this exact moment (e.g. two steers sent while a slow prior
            # round was still running, both waiting by the time THIS
            # follow-up round starts) rides along in the SAME round.
            batch = [first]
            while True:
                try:
                    nxt = line_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is _EOF:
                    break
                batch.append(nxt)
            turn_index += 1
            await _run_round(mcp_session, session_id, batch, turn_index, line_queue)
    finally:
        reader_task.cancel()


async def _amain(argv: "list[str]") -> int:
    opts = _parse_argv(argv)
    session_id, ok = _validate_session_id(opts)
    if not ok:
        return 1
    mcp_config_raw = opts.get("mcp-config") or '{"mcpServers":{}}'
    try:
        mcp_config = json.loads(mcp_config_raw)
    except json.JSONDecodeError:
        mcp_config = {"mcpServers": {}}
    rolo_cfg = (mcp_config.get("mcpServers") or {}).get("rolo")

    if not rolo_cfg:
        await _serve_turns(None, session_id, [])
        return 0

    from mcp import ClientSession
    import mcp.types as types
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (rolo_cfg.get("env") or {}).items()})
    params = StdioServerParameters(command=rolo_cfg["command"], args=rolo_cfg.get("args") or [], env=env)
    _ListChanged.event = asyncio.Event()

    async def _message_handler(message) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            _ListChanged.event.set()

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, message_handler=_message_handler) as mcp_session:
            await mcp_session.initialize()
            listed = await mcp_session.list_tools()
            tools_wire = [f"mcp__rolo__{t.name}" for t in listed.tools]
            await _serve_turns(mcp_session, session_id, tools_wire)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "auth" and argv[1] == "status":
        _write(_auth_status_json())
        return 0
    if argv and argv[0] == "--version":
        # finding 23: doctor now reads THIS, never claude auth status's
        # own JSON (which has no version key at all, verified live).
        print("2.1.284-fake (Claude Code)")
        return 0
    if not argv or argv[0] != "-p":
        # Any other invocation -- harmless no-op.
        return 0
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
