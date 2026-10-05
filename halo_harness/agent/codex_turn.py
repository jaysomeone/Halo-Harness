"""halo_harness.agent.codex_turn -- Halo 2.0.3 round 5i part 2: turn
execution for a `cx:` session. Unlike `cc_runtime.turn_body_cc` (one
long-held `claude` process, fed one stdin line per turn), this module
spawns a FRESH `codex exec [resume <id>]` subprocess per turn and runs it
to completion (docs/harness/CODEX-RESEARCH.md section 7) -- there is no
live channel into an already-running turn, so steering is a documented
fallback: `steer_cx` queues text; the moment the CURRENT subprocess exits,
`turn_body_cx`'s own loop drains the queue and sends it as its own
follow-up `codex exec resume` call (logged as a `steer` node), repeating
until the queue is empty, before yielding exactly one `turn_done` for this
Halo turn -- "finish the turn, then send", literally.
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import uuid as _uuid_mod
from pathlib import Path
from typing import Optional

from halo_harness import events
from halo_harness.agent.codex_process import CodexExecProcess, build_cx_argv, build_mcp_override_args
from halo_harness.agent.codex_runtime import (
    CodexUnavailable, _cx_child_env, _emit, ensure_cx_state, record_tool_use_announcement,
)

_ABORT_POLL_S = 0.05
_KILL_GRACE_S = 3.0
# cc_process.py's own command-line-length finding applies here too (a real
# subprocess argv, not a pipe) -- a long preamble/steer chain is capped
# well under the practical Windows argv budget rather than risking "the
# command line is too long" and the subprocess never starting at all.
_MAX_PROMPT_CHARS = 20_000


def prepare_conversation_so_far_cx(session) -> None:
    """Called from `Session.set_model` when switching INTO cx: mid-session
    (provider was something else, now "codex") -- mirrors `cc_runtime.
    prepare_conversation_so_far` exactly (same renderer, same stash-then-
    drain shape), since codex ALSO has no codex thread to `resume` yet for
    history that happened under a different provider. Stashed on
    `session._cx_pending_context`, drained into the NEXT turn's prompt by
    `turn_body_cx` -- never read reactively from "is the log non-empty",
    which would misfire on every brand-new session's own first turn
    (`_turn_inner` already logs THAT turn's own user message before
    `turn_body_cx` ever runs, so the log is never truly empty)."""
    from halo_harness.agent.cc_runtime import _render_conversation_so_far
    text = _render_conversation_so_far(session)
    if text:
        session._cx_pending_context = text


def _cx_preamble(session) -> str:
    """One-time context Codex has no other way to learn -- sent prepended
    to the FIRST turn's prompt only (`CxState.preamble_sent`). Unlike `cc:`'s
    `--append-system-prompt`, there is no system-prompt flag on `codex
    exec` (CODEX-RESEARCH.md section 3), so this rides as plain prompt text
    instead; unlike `cc:`'s addendum, it does NOT claim Halo's built-ins are
    disabled -- Codex keeps its own native tools (section 9)."""
    _NAME_DESC_CHARS = 100
    parts = ["You are running inside halo, a harness that ALSO exposes tools through an MCP "
             "server named \"halo\" (mcp__halo__<Name>), alongside your own built-in tools."]
    try:
        from halo_harness.commands.skills import discover_all_skills
        skills = discover_all_skills(session.cwd)
        if skills:
            lines = [f"- {name}: {(getattr(cmd, 'description', '') or '')[:_NAME_DESC_CHARS]}"
                      for name, cmd in sorted(skills.items())]
            parts.append("Skills available via the halo Skill tool:\n" + "\n".join(lines))
    except Exception:
        pass
    if session.session_catalog is not None and session.session_catalog.deferred:
        deferred_names = sorted(session.session_catalog.deferred)
        parts.append("The following halo tools are deferred -- load via ToolSearch before calling: "
                      + ", ".join(deferred_names))
    if session.permission_engine.mode == "plan":
        from halo_harness.agent.planmode import PLAN_MODE_NOTE
        parts.append(PLAN_MODE_NOTE)
    return "\n\n".join(parts)


def _write_images_to_tempdir(images: Optional[list]) -> "tuple[list, Optional[str]]":
    """`-i/--image <FILE>` needs real paths (CODEX-RESEARCH.md section 3) --
    Anthropic-shaped base64 image blocks are written to a temp dir, deleted
    by the caller once the subprocess exits. Returns `([], None)` on any
    block this turn's images list doesn't carry base64 data for (never
    raises -- a turn with an un-writable image just loses that one image,
    same "degrade, don't crash" contract as the rest of this codebase)."""
    import base64
    if not images:
        return [], None
    tmpdir = tempfile.mkdtemp(prefix="halo-cx-img-")
    paths = []
    for i, img in enumerate(images):
        try:
            source = (img or {}).get("source") or {}
            data = source.get("data")
            media_type = source.get("media_type") or "image/png"
            ext = media_type.split("/")[-1] or "png"
            if not data:
                continue
            path = Path(tmpdir) / f"img{i}.{ext}"
            path.write_bytes(base64.b64decode(data))
            paths.append(str(path))
        except Exception:
            continue
    return paths, tmpdir


def steer_cx(session, text: str) -> bool:
    """Queues `text`; returns True (accepted) whenever a `cx:` turn is
    actually running. See module docstring for delivery timing."""
    state = getattr(session, "_cx_state", None)
    if state is None or not session.busy:
        return False
    with state.lock:
        state.pending_steer_texts.append(text)
    _emit(session, events.notification(
        "Codex has no live mid-turn channel -- this will be sent as soon as the current turn finishes."))
    return True


def _drain_steer_texts(state) -> list:
    with state.lock:
        out = list(state.pending_steer_texts)
        state.pending_steer_texts.clear()
    return out


_LOGGABLE_NATIVE_ITEM_TYPES = ("command_execution", "file_change", "web_search_call", "todo_list")


def _item_text(item: dict) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text
    message = item.get("message")
    if isinstance(message, dict) and isinstance(message.get("text"), str):
        return message["text"]
    return ""


def _item_mcp_call_name_and_args(item: dict) -> "tuple[str, dict]":
    """Best-effort, UNCONFIRMED exact field names (CODEX-RESEARCH.md
    section 6) -- tries several plausible shapes, never raises."""
    name = item.get("tool") or item.get("name") or ""
    server = item.get("server") or item.get("server_name") or ""
    if isinstance(name, str) and "__" in name and not server:
        # a glued "server__tool" or "mcp__server__tool" shape, same
        # convention `mcp.manager.split_mcp_tool_name` parses for claude.
        name = name.rsplit("__", 1)[-1]
    args = item.get("arguments") or item.get("input") or {}
    return (name if isinstance(name, str) else ""), (args if isinstance(args, dict) else {})


def _events_for_cx_obj(session, turn_no: int, obj: dict, state) -> "tuple[list, Optional[str]]":
    """One parsed JSONL line -> (events, usage_dict_or_None_if_this_was_
    turn.completed). Defensive by design (CODEX-RESEARCH.md section 6:
    several field names are UNCONFIRMED) -- an unrecognized shape produces
    no events rather than raising."""
    typ = obj.get("type")
    out: list = []
    if typ == "thread.started":
        real_id = obj.get("thread_id") or obj.get("id")
        if isinstance(real_id, str) and real_id and real_id != state.cx_session_id:
            state.cx_session_id = real_id
            session.log.append_meta(cx_session_id=real_id)
        return out, None
    if typ in ("turn.started",):
        return out, None
    if typ == "item.started" or typ == "item.updated" or typ == "item.completed":
        item = obj.get("item") or {}
        itype = item.get("type")
        item_id = item.get("id") or ""
        if itype == "agent_message":
            text = _item_text(item)
            prior = state.cx_text_lens.get(item_id, 0)
            if text and len(text) > prior:
                out.append(events.text_delta(text[prior:], turn=turn_no))
                state.cx_text_lens[item_id] = len(text)
            if typ == "item.completed" and text:
                session.log.append_assistant(content=[{"type": "text", "text": text}])
        elif itype == "reasoning":
            text = _item_text(item)
            prior = state.cx_thinking_lens.get(item_id, 0)
            if text and len(text) > prior:
                out.append(events.thinking_delta(text[prior:], turn=turn_no))
                state.cx_thinking_lens[item_id] = len(text)
        elif itype == "mcp_tool_call":
            name, args = _item_mcp_call_name_and_args(item)
            if typ == "item.started" and name:
                announce_id = item_id or f"cx_{_uuid_mod.uuid4().hex[:8]}"
                record_tool_use_announcement(session, tool_use_id=announce_id, name=name, tool_input=args)
            # item.completed for this type is NOT logged here -- the real
            # tools/call already landed on the bridge and was logged by
            # `codex_runtime._resolve_and_dispatch_bridged_call`/`_drain_
            # finalize` (CODEX-RESEARCH.md section 9).
        elif itype in _LOGGABLE_NATIVE_ITEM_TYPES and typ == "item.completed":
            # Codex's OWN native tool -- already executed in its own
            # sandbox by the time we see this; logged read-only for
            # transcript visibility, never dispatched through Halo's
            # permission engine (section 9).
            synth_id = f"cx_native_{item_id or _uuid_mod.uuid4().hex[:8]}"
            session.log.append_assistant(content=[{"type": "tool_use", "id": synth_id, "name": itype,
                                                      "input": {k: v for k, v in item.items()
                                                                 if k not in ("id", "type")}}])
            session.log.append_tool_result(tool_use_id=synth_id, content=_item_text(item) or itype,
                                             is_error=False, tool=itype)
        elif itype in ("exec_approval_request", "apply_patch_approval_request"):
            out.append(events.notification(
                f"codex requested approval for a native action ({itype}) -- cx: runs non-interactively "
                f"and cannot answer this; see docs/harness/CODEX-RESEARCH.md section 3.", level="error"))
        return out, None
    if typ == "turn.completed":
        usage = obj.get("usage") or {}
        out.append(session.status_event(phase="idle", turn=turn_no))
        return out, usage
    if typ == "turn.failed":
        msg = (obj.get("error") or {}).get("message") if isinstance(obj.get("error"), dict) else obj.get("error")
        out.append(events.error(str(msg or "codex reported turn.failed"), turn=turn_no, err_type="cx_turn_failed"))
        return out, {"__failed__": True}
    if typ == "error":
        out.append(events.error(str(obj.get("message") or "codex reported an error"), turn=turn_no,
                                  err_type="cx_error"))
        return out, {"__failed__": True}
    return out, None


def _run_one_cx_subprocess(session, state, turn_no: int, prompt: str, image_paths: list,
                            log_kind: Optional[str]):
    """Runs ONE `codex exec` invocation to completion, yielding events as
    they arrive; returns (via StopIteration.value) the reason string
    (`"end_turn"`/`"error"`/`"interrupted"`)."""
    from halo_harness.agent.codex_process import cx_subprocess_env
    if log_kind == "steer":
        session.log.append_user([{"type": "text", "text": prompt}], kind="steer")

    bridge_env = state.bridge.child_env()
    mcp_args = build_mcp_override_args(list(bridge_env.keys()))
    argv = build_cx_argv(model=session.model_ref.model, prompt=prompt, resume_id=state.cx_session_id,
                          permission_mode=session.permission_engine.mode, mcp_override_args=mcp_args,
                          image_paths=image_paths)
    env = cx_subprocess_env(_cx_child_env(session), bridge_env)
    try:
        process = CodexExecProcess(argv, cwd=session.cwd, env=env)
    except OSError as e:
        yield events.error(f"could not start codex: {e}", turn=turn_no, err_type="cx_spawn")
        return "error"

    q: "queue.Queue" = queue.Queue()
    state.active_queue = q
    turn_finished = threading.Event()

    def watch_abort() -> None:
        while not turn_finished.wait(_ABORT_POLL_S):
            if session.abort.is_set():
                process.interrupt()
                q.put("ABORTED")
                return

    def reader() -> None:
        try:
            while True:
                obj = process.read_event()
                if obj is None:
                    q.put("EOF")
                    return
                evs, usage = _events_for_cx_obj(session, turn_no, obj, state)
                for ev in evs:
                    q.put(ev)
                if obj.get("type") == "turn.completed" and isinstance(usage, dict):
                    turn_cost = session.cost_meter.add_usage("cx", usage)
                    q.put(events.message_end(turn=turn_no, stop_reason="end_turn", usage=usage, cost_usd=turn_cost))
                    q.put("DONE")
                    return
                if isinstance(usage, dict) and usage.get("__failed__"):
                    q.put("DONE_FAILED")
                    return
        except Exception as e:
            q.put(("READER_ERROR", e))

    reader_thread = threading.Thread(target=reader, daemon=True, name=f"cx-reader-{turn_no}")
    reader_thread.start()
    threading.Thread(target=watch_abort, daemon=True, name=f"cx-watch-{turn_no}").start()

    reason = "end_turn"
    try:
        while True:
            item = q.get()
            if isinstance(item, events.Event):
                yield item
                continue
            if item == "DONE":
                break
            if item == "DONE_FAILED":
                reason = "error"
                break
            if item == "EOF":
                tail = process.stderr_tail()
                msg = "the codex subprocess ended unexpectedly"
                if tail.strip():
                    msg += f" -- {tail.strip().splitlines()[-1][:300]}"
                yield events.error(msg, turn=turn_no, err_type="cx_eof")
                reason = "error"
                break
            if item == "ABORTED":
                if process.wait(timeout=_KILL_GRACE_S) is None:
                    process.kill()
                    process.wait(timeout=2.0)
                reader_thread.join(timeout=2.0)
                reason = "interrupted"
                break
            if isinstance(item, tuple) and item[0] == "READER_ERROR":
                yield events.error(f"cx: reader failed: {item[1]}", turn=turn_no)
                reason = "error"
                break
    finally:
        turn_finished.set()
        state.active_queue = None
        process.wait(timeout=5.0)
    return reason


def turn_body_cx(session, turn_no: int, text: str, *, images: Optional[list] = None,
                  hook_context: Optional[str] = None):
    """The cx: equivalent of `Session._turn_body`/`cc_runtime.turn_body_cc`
    -- hooked from `Session.turn()`'s dispatch point (agent/loop.py). The
    main `text`/`images` were ALREADY logged by `_turn_inner` before this
    is called (same contract `cc_runtime.turn_body_cc` documents); this
    function only additionally logs a drained STEER as its own `steer`
    node. Always ends by yielding `turn_done`."""
    try:
        state = ensure_cx_state(session)
    except CodexUnavailable as e:
        yield events.error(str(e), turn=turn_no, err_type="cx_unavailable")
        yield events.turn_done(turn=turn_no, reason="error")
        return

    context_texts = []
    for ev in session._apply_pending_job_notices(turn_no):
        if ev.kind == "user_message":
            context_texts.append(ev.data.get("text", ""))
        yield ev
    for ev in session._apply_pending_agent_notices(turn_no):
        if ev.kind == "user_message":
            context_texts.append(ev.data.get("text", ""))
        yield ev
    if hook_context:
        context_texts.append(hook_context)
    pending_ctx = getattr(session, "_cx_pending_context", None)
    if pending_ctx:
        session._cx_pending_context = None
        context_texts.append(pending_ctx)

    pending_text = text
    pending_images = images
    log_kind = None
    overall_reason = "end_turn"
    first_iteration = True
    tmpdir = None
    try:
        while True:
            prompt = pending_text
            if not state.preamble_sent:
                preamble = _cx_preamble(session)
                state.preamble_sent = True
                if preamble:
                    prompt = preamble + "\n\n" + prompt
            if first_iteration and context_texts:
                prompt = "\n\n".join(context_texts) + "\n\n" + prompt
            if len(prompt) > _MAX_PROMPT_CHARS:
                prompt = prompt[:_MAX_PROMPT_CHARS] + "\n...(truncated to stay well under the OS argv limit)"

            image_paths, tmpdir = _write_images_to_tempdir(pending_images)
            try:
                gen = _run_one_cx_subprocess(session, state, turn_no, prompt, image_paths, log_kind)
                reason = yield from gen
            finally:
                if tmpdir:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    tmpdir = None
            overall_reason = reason
            first_iteration = False
            if reason != "end_turn":
                break
            queued = _drain_steer_texts(state)
            if not queued:
                break
            pending_text = "\n\n".join(queued)
            pending_images = None
            log_kind = "steer"
    finally:
        from halo_harness.agent.invariants import synthesize_missing_results
        synth_reason = {"interrupted": "Tool call interrupted by user",
                          "error": "Tool call never completed (the codex subprocess ended or errored)"}.get(
            overall_reason, "Tool call never received a result")
        synthesize_missing_results(session.log, reason=synth_reason)
    yield events.turn_done(turn=turn_no, reason=overall_reason)


def extract_final_text_from_jsonl(stdout_text: str) -> Optional[str]:
    """One-shot helper for `codex_runtime.one_shot_cx_call`: the last
    `agent_message` item's text across every parsed JSONL line, or None if
    nothing parsed as an `agent_message`."""
    last_text = None
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = obj.get("item") if isinstance(obj, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                last_text = text
    return last_text
