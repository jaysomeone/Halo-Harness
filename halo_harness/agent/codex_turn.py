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
_FAILOVER_TOOL_WAIT_S = 30.0
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


_FAILOVER_CONTINUE = ("The previous Codex account reached its usage limit partway through this turn. "
                      "Continue the provided user request from the saved conversation and tool results. "
                      "Do not restart the task or repeat completed actions. Some tools may already have "
                      "changed files or external state. If a tool's outcome is unknown, inspect the "
                      "current state before deciding whether anything needs retrying.")


def _conversation_before_this_turn(session) -> str:
    """The rendered conversation up to (not including) this turn's own
    user message, which `_turn_inner` already logged and the prompt
    carries anyway."""
    from halo_harness.agent.cc_runtime import _render_conversation_so_far
    nodes = session.log.nodes()
    last_user = max((i for i, n in enumerate(nodes) if n.get("type") == "user"), default=len(nodes))
    return _render_conversation_so_far(session, nodes[:last_user])


def _fit_prompt(parts: list) -> str:
    """Joins the prompt, trimming from the FRONT when it is too long for
    argv, so carried-over context is shortened before the user's own
    message (always last) loses anything."""
    prompt = "\n\n".join(p for p in parts if p)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = ("...(earlier context truncated to stay well under the OS argv limit)...\n\n"
                  + prompt[-_MAX_PROMPT_CHARS:])
    return prompt


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
# `codex exec resume <id>` under a CODEX_HOME that never saw <id> (e.g. the
# thread was started by another account) fails before the turn starts.
_RESUME_MISSING_MARKERS = ("no rollout found", "thread/resume failed")
_USAGE_LIMIT_MARKERS = ("usage limit", "usage_limit", "out of credits", "quota")
_MAX_TOOL_OUTPUT_CHARS = 20_000


def _is_usage_limit(message: str, error: Optional[dict] = None) -> bool:
    """A failure that another Codex account could get past: Codex's own
    `usage_limit_exceeded`, "out of credits", "rate limit reached"."""
    low = " ".join(str(v) for v in ((error or {}).get("codex_error_info"), message) if v).lower()
    return any(m in low for m in _USAGE_LIMIT_MARKERS) or ("rate limit" in low and "reached" in low)


def _native_display(itype: str, item: dict) -> "tuple[str, dict]":
    """The tool card for one of Codex's own actions: a shell command reads
    like Halo's own Bash card."""
    if itype == "command_execution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        return "Bash", {"command": str(command or "")}
    if itype == "file_change":
        paths = [c.get("path") for c in (item.get("changes") or []) if isinstance(c, dict) and c.get("path")]
        return "file_change", {"files": paths}
    return itype, {k: v for k, v in item.items() if k not in ("id", "type", "status", "aggregated_output")}


def _native_result(itype: str, item: dict) -> "tuple[str, bool]":
    status = item.get("status") or "completed"
    ok = status == "completed" and item.get("exit_code") in (None, 0)
    if itype == "command_execution":
        text = item.get("aggregated_output") or ""
        if item.get("exit_code") not in (None, 0):
            text = (text.rstrip("\n") + f"\n(exit code {item['exit_code']})").lstrip("\n")
    else:
        text = _item_text(item)
    text = text or ("done" if ok else status)
    if len(text) > _MAX_TOOL_OUTPUT_CHARS:
        text = text[:_MAX_TOOL_OUTPUT_CHARS] + "\n...(output truncated)"
    return text, ok


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
            session.log.append_meta(cx_session_id=real_id, cx_account=state.account_name or "default")
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
                out.append(events.text_delta("\n\n", turn=turn_no))
        elif itype == "reasoning":
            text = _item_text(item)
            prior = state.cx_thinking_lens.get(item_id, 0)
            if text and len(text) > prior:
                out.append(events.thinking_delta(text[prior:], turn=turn_no))
                state.cx_thinking_lens[item_id] = len(text)
        elif itype == "mcp_tool_call":
            name, args = _item_mcp_call_name_and_args(item)
            if typ == "item.started" and name:
                state.tools_ran = True
                # Codex item ids are local to a subprocess/thread; Halo's
                # transcript ids must stay unique across account switches.
                announce_id = f"cx_mcp_{_uuid_mod.uuid4().hex}"
                record_tool_use_announcement(session, tool_use_id=announce_id, name=name, tool_input=args)
            # item.completed for this type is NOT logged here -- the real
            # tools/call already landed on the bridge and was logged by
            # `codex_runtime._resolve_and_dispatch_bridged_call`/`_drain_
            # finalize` (CODEX-RESEARCH.md section 9).
        elif itype in _LOGGABLE_NATIVE_ITEM_TYPES and typ in ("item.started", "item.completed"):
            # Codex's OWN native tool -- executed in its own sandbox, never
            # dispatched through Halo's permission engine (section 9);
            # logged and shown as a read-only tool card: opened when it
            # starts, filled in with its output when it completes.
            state.tools_ran = True
            synth_id = state.native_started.get(item_id)
            if synth_id is None:
                synth_id = f"cx_native_{_uuid_mod.uuid4().hex}"
                if item_id:
                    state.native_started[item_id] = synth_id
                session.log.append_assistant(content=[{"type": "tool_use", "id": synth_id, "name": itype,
                                                          "input": {k: v for k, v in item.items()
                                                                     if k not in ("id", "type", "aggregated_output")}}])
                display_name, display_input = _native_display(itype, item)
                out.append(events.Event("tool_use_ready", {"id": synth_id, "name": display_name,
                                                             "input": display_input, "repaired": False}, turn=turn_no))
            if typ == "item.completed":
                state.native_started.pop(item_id, None)
                text, ok = _native_result(itype, item)
                session.log.append_tool_result(tool_use_id=synth_id, content=text, is_error=not ok, tool=itype)
                out.append(events.Event("tool_result", {"id": synth_id, "ok": ok, "summary": text[:200],
                                                          "content": text}, turn=turn_no))
        elif itype in ("exec_approval_request", "apply_patch_approval_request"):
            out.append(events.notification(
                f"codex requested approval for a native action ({itype}) -- cx: runs non-interactively "
                f"and cannot answer this; see docs/harness/CODEX-RESEARCH.md section 3.", level="error"))
        return out, None
    if typ == "turn.completed":
        usage = obj.get("usage") or {}
        out.append(session.status_event(phase="idle", turn=turn_no))
        return out, usage
    if typ in ("turn.failed", "error"):
        err = obj.get("error") if isinstance(obj.get("error"), dict) else None
        if typ == "turn.failed":
            msg = str((err or {}).get("message") or obj.get("error") or "codex reported turn.failed")
        else:
            msg = str(obj.get("message") or "codex reported an error")
        if _is_usage_limit(msg, err):
            # Not shown yet: `turn_body_cx` tries another account first.
            state.limit_message = msg
        else:
            out.append(events.error(msg, turn=turn_no, err_type="cx_turn_failed" if typ == "turn.failed" else "cx_error"))
        return out, {"__failed__": True}
    return out, None


def _run_one_cx_subprocess(session, state, turn_no: int, prompt: str, image_paths: list,
                            log_kind: Optional[str]):
    """Runs ONE `codex exec` invocation to completion, yielding events as
    they arrive; returns (via StopIteration.value) the reason string
    (`"end_turn"`/`"error"`/`"interrupted"`, or `"rate_limited"`/
    `"resume_missing"`, which `turn_body_cx` recovers from before any
    error is shown)."""
    from halo_harness.agent.codex_process import cx_subprocess_env
    if log_kind == "steer":
        session.log.append_user([{"type": "text", "text": prompt}], kind="steer")
    state.limit_message, state.tools_ran = None, False
    state.native_started.clear()
    state.cx_text_lens.clear()
    state.cx_thinking_lens.clear()
    with state.lock:
        state.pending_tool_uses.clear()

    bridge_env = state.bridge.child_env()
    mcp_args = build_mcp_override_args(list(bridge_env.keys()))
    argv = build_cx_argv(model=session.model_ref.model, prompt=prompt, resume_id=state.cx_session_id,
                          permission_mode=session.permission_engine.mode, mcp_override_args=mcp_args,
                          image_paths=image_paths)
    effort = getattr(session, "effort", None)
    if effort:
        from halo_harness.providers.profiles import clamp_effort, resolve_profile
        profile = resolve_profile(session.route, state_dir=session.state_dir)
        effort = clamp_effort(effort, profile)
        if effort:
            argv[-1:-1] = ["-c", f"model_reasoning_effort={effort}"]
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
                reason = "rate_limited" if state.limit_message else "error"
                break
            if item == "EOF":
                tail = process.stderr_tail()
                if not state.limit_message and _is_usage_limit(tail):
                    state.limit_message = tail.strip().splitlines()[-1][:300]
                if state.limit_message:
                    reason = "rate_limited"
                    break
                if state.cx_session_id and any(m in tail.lower() for m in _RESUME_MISSING_MARKERS):
                    reason = "resume_missing"
                    break
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
        # A failed response can arrive before the child exits. Stop it
        # before another account is allowed to act on the same workspace.
        if process.wait(timeout=5.0) is None:
            process.interrupt()
            if process.wait(timeout=_KILL_GRACE_S) is None:
                process.kill()
                process.wait(timeout=2.0)
        reader_thread.join(timeout=2.0)
        state.active_queue = None
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
    elif state.cx_session_id is None:
        # No thread for this account yet (an account switch, or every
        # earlier thread belongs to another account): a fresh thread starts
        # with the conversation so far.
        prior = _conversation_before_this_turn(session)
        if prior:
            context_texts.append(prior)

    pending_text = text
    request_to_continue = text
    pending_images = images
    log_kind = None
    overall_reason = "end_turn"
    first_iteration = True
    tmpdir = None
    resume_retried = False
    attempted_accounts = {state.account_name} if state.account_name else set()
    try:
        while True:
            if session.abort.is_set():
                overall_reason = "interrupted"
                break
            parts = list(context_texts) if first_iteration else []
            if not state.preamble_sent:
                state.preamble_sent = True
                parts.append(_cx_preamble(session))
            parts.append(pending_text)
            prompt = _fit_prompt(parts)

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
            if reason == "resume_missing" and not resume_retried:
                # Nothing ran: Codex refused the thread before the turn began.
                resume_retried = True
                state.cx_session_id = None
                yield events.notification("Codex no longer has this conversation's thread for this account -- "
                                          "starting a fresh one with the conversation so far.")
                context_texts = [c for c in (_conversation_before_this_turn(session),) if c]
                first_iteration = True
                state.preamble_sent = False
                log_kind = None
                continue
            if reason == "rate_limited":
                from halo_harness.agent.codex_runtime import switch_to_next_codex_account
                from halo_harness.accounts import codex_failover_unavailable_message
                from halo_harness.agent.invariants import synthesize_missing_results
                # Finish any tool still executing through Halo's bridge
                # before handing its transcript to a fresh model thread.
                deadline = time.monotonic() + _FAILOVER_TOOL_WAIT_S
                while True:
                    with state.lock:
                        running = bool(state.in_flight)
                    if not running or session.abort.is_set() or time.monotonic() >= deadline:
                        break
                    time.sleep(_ABORT_POLL_S)
                if session.abort.is_set():
                    overall_reason = "interrupted"
                    break
                if running:
                    yield events.error("Codex account recovery is paused because a tool is still running. "
                                       "Let that tool finish before continuing.",
                                       turn=turn_no, err_type="cx_failover_tool_running")
                    overall_reason = "error"
                    break
                synthesize_missing_results(
                    session.log, reason="Tool outcome unknown after account interruption; inspect current state before retrying.")
                limit_message = state.limit_message
                next_name = switch_to_next_codex_account(session, state, excluded_names=attempted_accounts)
                if next_name is None:
                    detail = codex_failover_unavailable_message(
                        attempted_accounts, state_dir=getattr(session, "state_dir", None))
                    yield events.error(f"{limit_message} -- {detail}",
                                       turn=turn_no, err_type="cx_usage_exhausted")
                    overall_reason = "error"
                    break
                attempted_accounts.add(next_name)
                yield events.notification(f"Codex account limit reached ({limit_message}) -- "
                                          f"switched to Codex account {next_name!r}; continuing saved work automatically.")
                # A fresh thread on the new account: everything so far,
                # including this turn's own message, rides along.
                from halo_harness.agent.cc_runtime import _render_conversation_so_far
                context_texts = [c for c in (_render_conversation_so_far(session),) if c]
                pending_text = ("<request-to-continue>\n" + request_to_continue
                                + "\n</request-to-continue>\n\n" + _FAILOVER_CONTINUE)
                # Re-materialize the original attachments for the new
                # account; transcript text cannot substitute for images.
                first_iteration = True
                state.preamble_sent = False
                log_kind = None
                continue
            if reason != "end_turn":
                break
            queued = _drain_steer_texts(state)
            if not queued:
                break
            pending_text = "\n\n".join(queued)
            request_to_continue = pending_text
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
