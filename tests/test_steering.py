"""tests.test_steering -- scope 0(c): steering. A real `Session` (bare,
`auto` mode unless a test builds its own `PermissionEngine`) driven by
manually pulling `session.turn(text)`'s generator one event at a time via
`next()` -- since the generator is fully SUSPENDED between `next()` calls,
this gives deterministic control over exactly when `session.steer(...)`
fires relative to streaming/dispatch, with no extra threading needed
(a `pending_ask`/`question` wait genuinely blocks a worker thread inside
`_await_reply`'s poll loop, so ONLY that one test runs the turn on a
background thread instead).
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish, send_json_response
from rolo_claude import events as harness_events

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
_HOOK_SCRIPT_ARGV = [sys.executable, "-m", "tests.helpers.hook_scripts"]


def _new_session(fh, mock, *, model="or:mock/model", permission_engine=None, interactive=False, hook_runner=None):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="steering-")), model_label=model, session_context=session_ctx,
        openrouter_base_url=mock.base_url, max_turns=10, permission_engine=permission_engine,
        hook_runner=hook_runner,
    )
    session.interactive = interactive
    return session


def _hook_runner(fh, *, hooks_by_event):
    """H5c finding 23: same construction `test_hooks_loop_integration.py`
    uses -- the hook scripts are spawned as `python -m
    tests.helpers.hook_scripts`, which only resolves with PYTHONPATH
    pointing at the repo root."""
    from rolo_claude.hooks import HookRunner
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    return HookRunner(hooks_by_event, cwd=fh["proj"], session_id="test-session",
                       transcript_path=str(fh["proj"] / "transcript.jsonl"), effective_env=env)


def _role_chunk():
    return {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}


def _text_chunk(piece):
    return {"choices": [{"index": 0, "delta": {"content": piece}}]}


def _stop_chunk():
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


def _tool_chunks(call_id, name, arguments):
    return [
        _role_chunk(),
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _messages_have_text(body, text) -> bool:
    for m in (body or {}).get("messages") or []:
        content = m.get("content")
        if isinstance(content, str) and text in content:
            return True
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and text in (b.get("text") or ""):
                    return True
    return False


def _drain_until(gen, predicate, *, max_events=200):
    """Pull events one at a time until `predicate(event)` is True (which is
    then returned too) or the generator ends (returns None). Every event
    seen along the way is returned as the second element."""
    seen = []
    for _ in range(max_events):
        try:
            ev = next(gen)
        except StopIteration:
            return None, seen
        seen.append(ev)
        if predicate(ev):
            return ev, seen
    raise AssertionError("predicate never satisfied within max_events")


@test
def test_steer_mid_stream_changes_the_next_request(ctx: Ctx):
    """scope 0(c): a steer queued WHILE the model is still streaming text
    cuts that call at the next chunk (no tool call ever formed), gets
    applied as a user message, and the loop calls the model AGAIN with it
    -- `steer_queued`/`steer_applied` both fire, in that order."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "actually do X instead"):
            _finish(h, [_role_chunk(), _text_chunk("ok, doing X"), _stop_chunk()])
            return
        _finish(h, [_role_chunk(), _text_chunk("first "), _text_chunk("chunk "), _text_chunk("second "),
                     _text_chunk("chunk "), _text_chunk("third chunk"), _stop_chunk()])

    SCENARIOS["steer-mid-stream"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-mid-stream")
        gen = session.turn("do the original thing")
        # let a couple of text_delta chunks through so the call is
        # genuinely IN PROGRESS, then steer.
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "text_delta")
        ctx.check("session reports busy while the turn is running", session.busy)
        queued = session.steer("actually do X instead")
        ctx.check("steer() accepted while busy", queued is True)

        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
        ctx.check(f"steer_queued fired, got kinds={kinds}", "steer_queued" in kinds)
        ctx.check("steer_applied fired", "steer_applied" in kinds)
        ctx.check("steer_queued precedes steer_applied",
                   kinds.index("steer_queued") < kinds.index("steer_applied"))
        ctx.check("turn_done fired exactly once (the loop continued, not two turns)",
                   kinds.count("turn_done") == 1)
        ctx.check(f"the model was called a second time reflecting the steer, got {calls['n']} calls",
                   calls["n"] == 2)

        logged_texts = "".join(
            b.get("text", "") for n in session.log.nodes() if n.get("type") == "user"
            for b in (n.get("content") or []) if isinstance(b, dict)
        )
        ctx.check(f"the steer text is logged as a user message, got {logged_texts!r}",
                   "actually do X instead" in logged_texts)
    finally:
        mock.stop()


@test
def test_steer_during_tool_call_applies_after_the_tool_result(ctx: Ctx):
    """scope 0(c): a steer queued once the model has already committed to
    a tool call never cuts the tool short -- it runs to completion (its
    tool_result is logged) and the steer is applied only afterward."""
    fh = build_fake_home()
    target = fh["proj"] / "steer_tool_target.txt"
    target.write_text("hello\n", encoding="utf-8")
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "steer after tool"):
            _finish(h, [_role_chunk(), _text_chunk("got it"), _stop_chunk()])
            return
        _finish(h, _tool_chunks("call_r", "Read", {"file_path": str(target)}))

    SCENARIOS["steer-during-tool"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-during-tool")
        gen = session.turn("read the file")
        # stop right when the tool call is fully formed and ready, BEFORE
        # dispatch (the next `next()` call triggers `_dispatch_tools`).
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "tool_use_ready")
        queued = session.steer("steer after tool")
        ctx.check("steer accepted mid-turn (tool not dispatched yet)", queued is True)

        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
        ctx.check(f"the tool actually ran (tool_result present), got kinds={kinds}", "tool_result" in kinds)
        ctx.check("steer_applied fired", "steer_applied" in kinds)
        ctx.check("tool_result precedes steer_applied (tools allowed to finish first)",
                   kinds.index("tool_result") < kinds.index("steer_applied"))
        ctx.check(f"the model was re-called reflecting the steer, got {calls['n']} calls", calls["n"] == 2)
    finally:
        mock.stop()


@test
def test_two_steers_apply_in_order(ctx: Ctx):
    """scope 0(c): two steers queued before either is applied are both
    delivered, in the order they were typed."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "second steer"):
            _finish(h, [_role_chunk(), _text_chunk("done"), _stop_chunk()])
            return
        _finish(h, [_role_chunk(), _text_chunk("part one "), _text_chunk("part two"), _stop_chunk()])

    SCENARIOS["steer-two-in-order"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-two-in-order")
        gen = session.turn("start")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "text_delta")
        session.steer("first steer")
        session.steer("second steer")

        applied_order = []
        for ev in gen:
            if ev.kind == "steer_applied":
                applied_order.append(ev.data.get("text"))
        ctx.check(f"both steers applied, in order, got {applied_order}",
                   applied_order == ["first steer", "second steer"])
    finally:
        mock.stop()


@test
def test_h5c_f23_steer_is_gated_by_userpromptsubmit_and_honours_blocked(ctx: Ctx):
    """H5c finding 23: a steer becomes a user-role message through the SAME
    UserPromptSubmit gate the very first message of a turn already runs
    through (see `turn()`'s own hook call right before `append_user`).
    Before this fix, typing while a turn ran was a standing bypass of a
    user's own UserPromptSubmit hook (a prompt filter/annotator that could
    otherwise only ever see the first message of a turn) -- this predates
    H5b per finding 23. A hook that blocks must drop the steer text (never
    logged, the model never sees it, no second model call happens for it)
    while still telling the user why; `steer_queued`/`steer_applied` still
    both fire so the TUI's dedup pairing (finding 19) never hangs waiting
    for an `steer_applied` that would otherwise never come."""
    from rolo_claude.hooks import HookDef

    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        _finish(h, [_role_chunk(), _text_chunk("first "), _text_chunk("chunk"), _stop_chunk()])

    SCENARIOS["steer-blocked-by-hook"] = _scn
    try:
        # No hook_runner yet -- `block_decision` blocks UNCONDITIONALLY, so
        # attaching it before the turn starts would block the FIRST message
        # ("do the original thing") too and this test wants to isolate the
        # STEER's own gating. Attached to the live Session below, once the
        # first message has already gone through cleanly.
        session = _new_session(fh, mock, model="or:mock/steer-blocked-by-hook")
        gen = session.turn("do the original thing")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "text_delta")
        hooks_by_event = {"UserPromptSubmit": [HookDef(type="command", args=_HOOK_SCRIPT_ARGV + ["block_decision"])]}
        session.hook_runner = _hook_runner(fh, hooks_by_event=hooks_by_event)
        queued = session.steer("smuggle this past the hook")
        ctx.check("steer() accepted while busy", queued is True)

        seen_after = list(gen)
        kinds = [ev.kind for ev in seen_after]
        ctx.check(f"steer_queued still fires (TUI dedup pairing), got kinds={kinds}", "steer_queued" in kinds)
        ctx.check("steer_applied still fires (pairs with steer_queued even though blocked)",
                   "steer_applied" in kinds)
        ctx.check("no user_message event for the blocked steer text", "user_message" not in kinds)
        notif_texts = [ev.data.get("text", "") for ev in seen_after if ev.kind == "notification"]
        ctx.check(f"a notification names the block reason, got {notif_texts}",
                   any("blocked by block_decision script" in t for t in notif_texts))
        ctx.check(f"only ONE model call total -- the blocked steer never triggered a second, got {calls['n']}",
                   calls["n"] == 1)
        logged_texts = "".join(
            b.get("text", "") for n in session.log.nodes() if n.get("type") == "user"
            for b in (n.get("content") or []) if isinstance(b, dict)
        )
        ctx.check(f"the blocked steer text was never logged, got {logged_texts!r}",
                   "smuggle this past the hook" not in logged_texts)
    finally:
        mock.stop()


@test
def test_h5c_f23_two_steers_each_get_their_own_userpromptsubmit_context(ctx: Ctx):
    """H5c finding 23: UserPromptSubmit runs on EACH steer individually
    (not once for the whole batch, not skipped after the first) -- two
    queued steers with a context-adding hook must produce TWO hook_context
    snapshots, correctly interleaved before their own steer's user text."""
    from rolo_claude.hooks import HookDef

    fh = build_fake_home()
    mock = MockUpstream().start()

    def _scn(h, body):
        if _messages_have_text(body, "second steer"):
            _finish(h, [_role_chunk(), _text_chunk("done"), _stop_chunk()])
            return
        _finish(h, [_role_chunk(), _text_chunk("part one "), _text_chunk("part two"), _stop_chunk()])

    SCENARIOS["steer-two-with-context-hook"] = _scn
    try:
        hooks_by_event = {"UserPromptSubmit": [HookDef(type="command", args=_HOOK_SCRIPT_ARGV + ["plain_context"])]}
        session = _new_session(fh, mock, model="or:mock/steer-two-with-context-hook",
                                hook_runner=_hook_runner(fh, hooks_by_event=hooks_by_event))
        gen = session.turn("start")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "text_delta")
        session.steer("first steer")
        session.steer("second steer")
        for _ev in gen:
            pass

        all_nodes = session.log.nodes()
        # The turn's OWN first message ("start") runs through the very
        # same UserPromptSubmit hook (see `turn()`) and gets its own
        # hook_context snapshot too -- excluded here since this test is
        # about the STEERS' contexts specifically (covered already by
        # test_userpromptsubmit_hook_context_is_visible_in_the_session_log
        # for the first-message case).
        start_idx = next(i for i, n in enumerate(all_nodes)
                          if n.get("type") == "user"
                          and any(isinstance(b, dict) and b.get("text") == "start" for b in (n.get("content") or [])))
        relevant = [n for n in all_nodes[start_idx + 1:]
                    if (n.get("type") == "snapshot" and n.get("kind") == "hook_context") or n.get("type") == "user"]

        def _kind_and_text(n):
            if n.get("type") == "user":
                return "user", "".join(b.get("text", "") for b in (n.get("content") or []) if isinstance(b, dict))
            return "context", "".join(b.get("text", "") for b in (n.get("content") or []) if isinstance(b, dict))

        sequence = [_kind_and_text(n) for n in relevant]
        context_count = sum(1 for kind, _ in sequence if kind == "context")
        ctx.check(f"exactly TWO hook_context snapshots (one per steer), got {context_count}: {sequence}",
                   context_count == 2)
        # Every context snapshot must precede the steer text it belongs to,
        # and "first steer" (with its own context) must precede "second
        # steer" (with ITS own context) -- proves per-steer pairing, not a
        # single hook run reused/duplicated across both.
        texts_in_order = [text for kind, text in sequence]
        first_idx = next(i for i, t in enumerate(texts_in_order) if "first steer" in t)
        second_idx = next(i for i, t in enumerate(texts_in_order) if "second steer" in t)
        ctx.check(f"'first steer' precedes 'second steer', sequence={sequence}", first_idx < second_idx)
        ctx.check("a hook_context snapshot immediately precedes 'first steer'",
                   sequence[first_idx - 1][0] == "context")
        ctx.check("a hook_context snapshot immediately precedes 'second steer'",
                   sequence[second_idx - 1][0] == "context")
    finally:
        mock.stop()


@test
def test_steer_during_a_pending_card_does_not_answer_the_card(ctx: Ctx):
    """scope 0(c): a steer that arrives while a permission card is pending
    must not be mistaken for the card's own answer -- the tool stays
    pending until a REAL `resolve_permission` call, and the steer is
    applied only afterward (once the loop is back at a safe point)."""
    from rolo_claude.permissions import Decision, PermissionEngine

    fh = build_fake_home()
    target = fh["proj"] / "steer_card_target.txt"
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "steer during card"):
            _finish(h, [_role_chunk(), _text_chunk("ok"), _stop_chunk()])
            return
        _finish(h, _tool_chunks("call_w", "Write", {"file_path": str(target), "content": "hi\n"}))

    SCENARIOS["steer-during-card"] = _scn
    try:
        engine = PermissionEngine(mode="default", cwd=fh["proj"])  # Write in cwd -> "ask" in default mode
        session = _new_session(fh, mock, model="or:mock/steer-during-card",
                                permission_engine=engine, interactive=True)
        events_seen: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("write the file"):
                    events_seen.append(ev)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()
        deadline = time.monotonic() + 10
        while not any(e.kind == "permission_request" for e in events_seen) and time.monotonic() < deadline:
            time.sleep(0.05)
        ctx.check("a permission_request card appeared", any(e.kind == "permission_request" for e in events_seen))

        queued = session.steer("steer during card")
        ctx.check("steer accepted while a card is pending (session is busy)", queued is True)
        time.sleep(0.2)
        ctx.check("the card is STILL pending (steer did not answer it)", "call_w" in session._permission_waiters)

        resolved = session.resolve_permission("call_w", Decision("allow", "test allow"))
        ctx.check("the real answer resolves the card", resolved is True)
        done.wait(10)
        ctx.check("the turn finished", done.is_set())
        kinds = [e.kind for e in events_seen]
        ctx.check(f"the tool ran after being allowed, got kinds={kinds}", "tool_result" in kinds)
        ctx.check("the steer was applied (not silently dropped)", "steer_applied" in kinds)
        ctx.check(f"the model was re-called with the steer, got {calls['n']} calls", calls["n"] == 2)
    finally:
        mock.stop()


@test
def test_h5b_f03_steer_during_first_of_two_writes_pairs_every_tool_use(ctx: Ctx):
    """finding 3 (critical): a steer noticed right after the FIRST of two
    solo-dispatched (non-read-only) tool_use calls finish must not leave
    the SECOND one unanswered -- `_dispatch_tools` used to `break` with no
    result for it at all, and `_apply_pending_steers_events` then appended
    the steer as the very next user turn, leaving an assistant node with
    an unpaired tool_use followed by a later turn (a permanent 400 on
    `ant:`/Databricks Claude routes, and undetectable by the OLD
    last-node-only `find_unpaired_tool_use_ids`)."""
    from rolo_claude.agent.invariants import find_unpaired_tool_use_ids

    fh = build_fake_home()
    target_a = fh["proj"] / "steer_pair_a.txt"
    target_b = fh["proj"] / "steer_pair_b.txt"
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "steer mid dispatch"):
            _finish(h, [_role_chunk(), _text_chunk("ok, stopped"), _stop_chunk()])
            return
        # TWO non-read-only Write calls in the SAME assistant message --
        # both are dispatched solo, one after another (never batched).
        _finish(h, [
            _role_chunk(),
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "type": "function",
                 "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target_a), "content": "a\n"})}},
                {"index": 1, "id": "call_b", "type": "function",
                 "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target_b), "content": "b\n"})}},
            ]}}]},
            _stop_chunk(),
        ])

    SCENARIOS["steer-two-writes"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-two-writes")
        gen = session.turn("write two files")
        # Stop right when the FIRST call's own tool_result lands (its
        # tool_use_ready/tool_result pair is the 2nd/3rd "ready" boundary
        # -- drain until we see the first tool_result specifically).
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "tool_result")
        queued = session.steer("steer mid dispatch")
        ctx.check("steer accepted right after the first tool's own result", queued is True)

        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
        ctx.check(f"turn_done fired exactly once, got kinds={kinds}", kinds.count("turn_done") == 1)
        ctx.check("steer_applied fired", "steer_applied" in kinds)

        nodes = session.log.nodes()
        tool_results = {n.get("tool_use_id"): n for n in nodes if n.get("type") == "tool_result"}
        ctx.check("call_a (the one that actually ran) has a result", "call_a" in tool_results)
        ctx.check(f"call_b (never dispatched) STILL got a synthesized result, got {sorted(tool_results)}",
                  "call_b" in tool_results)
        ctx.check("call_b's synthesized result is an error, never a fabricated success",
                  "call_b" in tool_results and tool_results["call_b"].get("is_error") is True)
        ctx.check("call_b's synthesized text names why (a new message, not an interrupt)",
                  "call_b" in tool_results and "new message" in (tool_results["call_b"].get("content") or ""))
        ctx.check(f"NO tool_use is left unpaired anywhere in the log, got {find_unpaired_tool_use_ids(session.log)}",
                  find_unpaired_tool_use_ids(session.log) == [])
        ctx.check(f"the model was re-called reflecting the steer, got {calls['n']} calls", calls["n"] == 2)
    finally:
        mock.stop()


@test
def test_h5c_f06_steer_queued_before_dispatch_starts_stops_every_call(ctx: Ctx):
    """H5c finding 6: the per-call steer check used to run only AFTER each
    solo dispatch, never BEFORE the first one -- a steer already queued the
    moment `_dispatch_tools` starts (typed during the stream tail, during
    `_maybe_auto_compact`, or during an earlier PreToolUse hook) let the
    first not-yet-started call run anyway. Verified in the review with a
    steer queued at `message_end`: the pending `Write` still ran and the
    file was created. Drains to `message_end` (the assistant reply is
    fully formed with its tool_use blocks, but `_dispatch_tools` has not
    been entered yet), steers there, and proves NEITHER of two pending
    Write calls ever runs."""
    from rolo_claude.agent.invariants import find_unpaired_tool_use_ids

    fh = build_fake_home()
    target_a = fh["proj"] / "f06_pre_dispatch_a.txt"
    target_b = fh["proj"] / "f06_pre_dispatch_b.txt"
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "STOP-DO-NOT-WRITE"):
            _finish(h, [_role_chunk(), _text_chunk("ok, stopped"), _stop_chunk()])
            return
        _finish(h, [
            _role_chunk(),
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_pre_a", "type": "function",
                 "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target_a), "content": "a\n"})}},
                {"index": 1, "id": "call_pre_b", "type": "function",
                 "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target_b), "content": "b\n"})}},
            ]}}]},
            _stop_chunk(),
        ])

    SCENARIOS["steer-before-dispatch"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-before-dispatch")
        gen = session.turn("write two files")
        # Drain to message_end -- the reply (both tool_use blocks) is fully
        # formed, but dispatch has not started at all yet.
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "message_end")
        queued = session.steer("STOP-DO-NOT-WRITE")
        ctx.check("steer accepted right at message_end, before dispatch starts", queued is True)

        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
        ctx.check(f"turn_done fired exactly once, got kinds={kinds}", kinds.count("turn_done") == 1)
        ctx.check("steer_applied fired", "steer_applied" in kinds)

        ctx.check("neither file was ever written", not target_a.exists() and not target_b.exists())
        nodes = session.log.nodes()
        tool_results = {n.get("tool_use_id"): n for n in nodes if n.get("type") == "tool_result"}
        for call_id in ("call_pre_a", "call_pre_b"):
            ctx.check(f"{call_id} got a synthesized 'not run' result, got {tool_results.get(call_id)}",
                      call_id in tool_results and tool_results[call_id].get("is_error") is True
                      and "new message" in (tool_results[call_id].get("content") or ""))
        ctx.check(f"NO tool_use is left unpaired anywhere in the log, got {find_unpaired_tool_use_ids(session.log)}",
                  find_unpaired_tool_use_ids(session.log) == [])
        ctx.check(f"the model was re-called reflecting the steer, got {calls['n']} calls", calls["n"] == 2)
    finally:
        mock.stop()


@test
def test_h5c_f06_batched_read_only_calls_ahead_of_a_queued_steer_do_not_run(ctx: Ctx):
    """H5c finding 6: "batched read-only calls ahead of a queued steer do
    not run" -- two Read calls (read-only, always accumulated into
    `pending_batch` rather than dispatched solo) must NOT be dispatched at
    all when a steer was already queued before `_dispatch_tools` even
    looked at the first one; `dispatch()` is never called for either, so
    "running tools finish" never applied to them in the first place."""
    from rolo_claude.agent.invariants import find_unpaired_tool_use_ids

    fh = build_fake_home()
    file_a = fh["proj"] / "f06_batch_a.txt"
    file_b = fh["proj"] / "f06_batch_b.txt"
    file_a.write_text("real content a\n", encoding="utf-8")
    file_b.write_text("real content b\n", encoding="utf-8")
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "STOP-BEFORE-READING"):
            _finish(h, [_role_chunk(), _text_chunk("ok, stopped"), _stop_chunk()])
            return
        _finish(h, [
            _role_chunk(),
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_read_a", "type": "function",
                 "function": {"name": "Read", "arguments": json.dumps({"file_path": str(file_a)})}},
                {"index": 1, "id": "call_read_b", "type": "function",
                 "function": {"name": "Read", "arguments": json.dumps({"file_path": str(file_b)})}},
            ]}}]},
            _stop_chunk(),
        ])

    SCENARIOS["steer-before-batch"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-before-batch")
        gen = session.turn("read both files")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "message_end")
        queued = session.steer("STOP-BEFORE-READING")
        ctx.check("steer accepted right at message_end, before the batch is dispatched", queued is True)

        for ev in gen:
            pass  # drain to completion

        nodes = session.log.nodes()
        tool_results = {n.get("tool_use_id"): n for n in nodes if n.get("type") == "tool_result"}
        for call_id in ("call_read_a", "call_read_b"):
            result = tool_results.get(call_id)
            ctx.check(f"{call_id} was never actually run (no real file content in its result), got {result}",
                      result is not None and "real content" not in (result.get("content") or ""))
            ctx.check(f"{call_id} is a synthesized 'not run' error result, got {result}",
                      result is not None and result.get("is_error") is True
                      and "new message" in (result.get("content") or ""))
        ctx.check(f"NO tool_use is left unpaired anywhere in the log, got {find_unpaired_tool_use_ids(session.log)}",
                  find_unpaired_tool_use_ids(session.log) == [])
        ctx.check(f"the model was re-called reflecting the steer, got {calls['n']} calls", calls["n"] == 2)
    finally:
        mock.stop()


@test
def test_h5b_f05_steer_after_last_checkpoint_resubmits_as_next_turn(ctx: Ctx):
    """finding 5 (major): a steer that lands AFTER `_turn_body`'s very
    last checkpoint is never silently dropped. Reproduced deterministically
    (no thread timing needed): the test's own `out` callback calls
    `session.steer(...)` SYNCHRONOUSLY while handling the first turn's
    `turn_done` event -- at that exact point `turn()`'s generator has
    yielded turn_done but has not yet resumed to run its `finally` (that
    needs one more `next()`, which `_pump_turn`'s own `for` loop only
    calls after `out()` returns), so `_busy` is still set and `steer()`
    still accepts it, exactly reproducing "landed after the last
    checkpoint but before the turn actually finished". `Session.run()`
    must resubmit it as a genuine SECOND turn rather than losing it."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "late steer"):
            _finish(h, [_role_chunk(), _text_chunk("handled the late steer"), _stop_chunk()])
            return
        _finish(h, [_role_chunk(), _text_chunk("first turn done"), _stop_chunk()])

    SCENARIOS["steer-late-checkpoint"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-late-checkpoint")
        commands: "queue.Queue" = queue.Queue()
        events_seen: list = []
        state = {"turn_done_count": 0}

        def _out(ev):
            events_seen.append(ev)
            if ev.kind == "turn_done":
                state["turn_done_count"] += 1
                if state["turn_done_count"] == 1:
                    queued = session.steer("late steer")
                    ctx.check("steer() still accepts it (busy has not cleared yet)", queued is True)
                elif state["turn_done_count"] == 2:
                    commands.put(None)

        commands.put(harness_events.Command("user_input", {"text": "start"}))
        exit_code = session.run(commands, _out)

        ctx.check(f"run() exits cleanly, got {exit_code}", exit_code == 0)
        ctx.check(f"exactly two model calls happened, got {calls['n']}", calls["n"] == 2)
        ctx.check(f"exactly two turn_done events fired (a real second turn, not a cut first one), got {state['turn_done_count']}",
                   state["turn_done_count"] == 2)
        user_texts = [ev.data.get("text") for ev in events_seen if ev.kind == "user_message"]
        ctx.check(f"the late steer became its own fresh turn's prompt, got {user_texts}", "late steer" in user_texts)
        ctx.check(f"the FIRST turn's own text was never cut short, got "
                  f"{''.join(e.data.get('text','') for e in events_seen if e.kind=='text_delta')!r}",
                  any(e.kind == "text_delta" and "first turn done" in e.data.get("text", "") for e in events_seen))
    finally:
        mock.stop()


@test
def test_h5c_f05_steer_at_message_start_logs_no_empty_assistant_node(ctx: Ctx):
    """H5b/H5c finding 5: a steer noticed at/before `message_start` (before
    ANY content block even started forming) used to still write
    `{"role": "assistant", "content": []}` to the log -- Anthropic rejects
    a non-final assistant message with empty content, so every LATER
    request on that route 400s forever. Nothing must be logged for this
    cut step; the loop must instead apply the steer and make a second,
    real model call."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "redirected after message_start"):
            _finish(h, [_role_chunk(), _text_chunk("ok, redirected"), _stop_chunk()])
            return
        _finish(h, [_role_chunk(), _text_chunk("would have said this"), _stop_chunk()])

    SCENARIOS["steer-at-message-start"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-at-message-start")
        gen = session.turn("original prompt")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "message_start")
        queued = session.steer("redirected after message_start")
        ctx.check("steer() accepted right after message_start", queued is True)

        kinds = []
        texts = []
        for ev in gen:
            kinds.append(ev.kind)
            if ev.kind == "text_delta":
                texts.append(ev.data.get("text"))
        ctx.check(f"the cut first call's own text never streamed at all, got {texts}",
                   "would have said this" not in "".join(texts))
        ctx.check(f"the model was called a second time, got {calls['n']} calls", calls["n"] == 2)

        assistant_nodes = [n for n in session.log.nodes() if n.get("type") == "assistant"]
        ctx.check(f"no empty-content assistant node was ever logged, got {[n.get('content') for n in assistant_nodes]}",
                   all(n.get("content") for n in assistant_nodes))
        node_types = [n.get("type") for n in session.log.nodes()]
        # exactly one assistant node (the SECOND call's real reply) -- the
        # cut first call contributed NOTHING to the log.
        ctx.check(f"exactly one assistant node logged (the cut call logged nothing), got {node_types}",
                   node_types.count("assistant") == 1)
        user_texts = [b.get("text") for n in session.log.nodes() if n.get("type") == "user"
                      for b in (n.get("content") or []) if isinstance(b, dict)]
        ctx.check(f"the steer text is logged as its own user message, got {user_texts}",
                   "redirected after message_start" in user_texts)
    finally:
        mock.stop()


@test
def test_h5c_f05_steer_during_retry_wait_cuts_retried_call_with_no_empty_node(ctx: Ctx):
    """H5b/H5c finding 5: "a steer typed during a retry wait always takes
    this path: the new request is cut at its first chunk" -- a 429 forces
    a retry-after wait; a steer queued WHILE that wait is sleeping must
    still be there the moment the retried request starts, cutting it at
    `message_start` with (per this same finding) no empty assistant node
    logged for the cut retried attempt."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if calls["n"] == 1:
            send_json_response(h, 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                                {"Retry-After": "1"})
            return
        if _messages_have_text(body, "redirect during retry wait"):
            _finish(h, [_role_chunk(), _text_chunk("ok, redirected"), _stop_chunk()])
            return
        _finish(h, [_role_chunk(), _text_chunk("original reply"), _stop_chunk()])

    SCENARIOS["steer-during-retry-wait"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/steer-during-retry-wait")
        events_seen: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("original prompt"):
                    events_seen.append(ev)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()
        # Give the first (429) attempt time to fail and enter its
        # retry-after sleep, then queue the steer WHILE it is still
        # sleeping -- `_abort_sleep` only reacts to Esc/Ctrl+C, never to a
        # queued steer, so the full ~1s wait always elapses first.
        deadline = time.monotonic() + 5
        while not session.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.25)
        queued = session.steer("redirect during retry wait")
        ctx.check("steer() accepted while the retry wait is sleeping", queued is True)
        done.wait(10)
        ctx.check("the turn finished", done.is_set())

        ctx.check(f"3 requests reached the mock (429, steered-cut retry, steer's own reply), got {calls['n']}",
                   calls["n"] == 3)
        assistant_nodes = [n for n in session.log.nodes() if n.get("type") == "assistant"]
        ctx.check(f"no empty-content assistant node was ever logged, got {[n.get('content') for n in assistant_nodes]}",
                   all(n.get("content") for n in assistant_nodes))
        ctx.check(f"exactly one assistant node logged (the retried, steered call logged nothing), got "
                  f"{[n.get('type') for n in session.log.nodes()]}", len(assistant_nodes) == 1)
        kinds = [e.kind for e in events_seen]
        ctx.check(f"the steer was applied, got kinds={kinds}", "steer_applied" in kinds)
    finally:
        mock.stop()


@test
def test_steer_noop_when_no_turn_running(ctx: Ctx):
    """`Session.steer` is a safe no-op (never queues anything, never
    raises) when nothing is running to steer."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock)
        ctx.check("not busy before any turn", session.busy is False)
        result = session.steer("hello")
        ctx.check("steer() returns False when idle", result is False)
        ctx.check("nothing queued", session._pending_steer() is False)
    finally:
        mock.stop()


@test
def test_h5c_f14_mention_queued_while_busy_never_races_and_orders_after_tool_result(ctx: Ctx):
    """H5c finding 14: `queue_log_write` (what `Controller.
    ingest_at_mentions`/the prompt-kind slash command's own `@path`
    snapshot/`run_inline_shell` all now call instead of writing to
    `session.log` directly) must never write from a non-worker thread
    while the session is busy. Verified (wire shape) in the review: a
    steer carrying `@b.txt`, typed while a Read tool is running, made the
    NEXT request's user message `['text', 'tool_result', 'text']` (text
    before tool_result, which Anthropic 400s on). Simulates the UI thread
    calling `queue_log_write` + `steer()` at the exact moment a Read call
    is in flight (mid-turn, `session.busy` True)."""
    from rolo_claude.agent.derive import derive_request

    fh = build_fake_home()
    target = fh["proj"] / "b.txt"
    target.write_text("file content for the mention\n", encoding="utf-8")
    mock = MockUpstream().start()
    calls = {"n": 0}

    def _scn(h, body):
        calls["n"] += 1
        if _messages_have_text(body, "redirect after mention"):
            _finish(h, [_role_chunk(), _text_chunk("ok, redirected"), _stop_chunk()])
            return
        _finish(h, _tool_chunks("call_read", "Read", {"file_path": str(target)}))

    SCENARIOS["mention-while-read"] = _scn
    try:
        session = _new_session(fh, mock, model="or:mock/mention-while-read")
        gen = session.turn("read the file")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "tool_use_ready")

        # Simulate the UI thread: an @mention snapshot ingested from the
        # steer's own text, queued WHILE the Read call is in flight.
        queued = session.queue_log_write(
            "snapshot", {"blocks": [{"type": "text", "text": "@b.txt\nfile content for the mention"}],
                         "snapshot_kind": "at_mention"})
        ctx.check("queued (not applied immediately) -- the session is busy", queued is True)
        nodes_before = session.log.nodes()
        ctx.check("nothing was written to the log yet (no race)", not any(
            n.get("type") == "snapshot" and n.get("kind") == "at_mention" for n in nodes_before))

        steer_queued = session.steer("redirect after mention")
        ctx.check("the steer itself was also accepted", steer_queued is True)

        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
        ctx.check(f"steer_applied fired, got kinds={kinds}", "steer_applied" in kinds)
        ctx.check(f"the model was re-called reflecting the steer, got {calls['n']} calls", calls["n"] == 2)

        nodes = session.log.nodes()
        ctx.check("the mention snapshot was eventually applied", any(
            n.get("type") == "snapshot" and n.get("kind") == "at_mention" for n in nodes))

        _system, messages, _tools = derive_request(session.log, tools=None)
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content") or []
            types = [b.get("type") for b in content if isinstance(b, dict)]
            if "tool_result" in types:
                ctx.check(f"tool_result comes FIRST in this user message, got types={types}",
                          types[0] == "tool_result")
                texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                ctx.check(f"the mention text made it in, got {texts}",
                          any("file content for the mention" in (t or "") for t in texts))
                ctx.check(f"the steer text made it in too, got {texts}",
                          any("redirect after mention" in (t or "") for t in texts))
                break
        else:
            raise AssertionError(f"no user message with a tool_result found, messages={messages}")
    finally:
        mock.stop()


@test
def test_h5c_f14_inline_shell_queued_while_busy_logs_a_paired_tool_use_and_result(ctx: Ctx):
    """H5c finding 14, the `!cmd` half: `queue_log_write("inline_shell", ...)`
    (what `Controller.run_inline_shell` now calls from its OWN Textual
    worker thread, never the session's) must, once applied at a safe
    point, log a PROPERLY PAIRED `tool_use` + `tool_result` -- never one
    without the other (an inline `!cmd` landing between an assistant
    `tool_use` and its own `tool_result` used to break pairing on every
    route)."""
    from rolo_claude.agent.invariants import find_unpaired_tool_use_ids

    fh = build_fake_home()
    mock = MockUpstream().start()
    SCENARIOS["inline-shell-while-busy"] = lambda h, body: _finish(h, [_role_chunk(), _text_chunk("ok"), _stop_chunk()])
    try:
        session = _new_session(fh, mock, model="or:mock/inline-shell-while-busy")
        gen = session.turn("say ok")
        _ev, _seen = _drain_until(gen, lambda e: e.kind == "message_start")

        queued = session.queue_log_write("inline_shell", {
            "tool_use_id": "inline_abc123", "command": "echo from inline",
            "content": "from inline\n", "is_error": False,
        })
        ctx.check("queued while busy, not applied immediately", queued is True)
        ctx.check("nothing written to the log yet", not any(
            n.get("type") in ("assistant", "tool_result")
            and json.dumps(n).find("inline_abc123") != -1 for n in session.log.nodes()))

        for _ev2 in gen:
            pass  # drain to completion

        ctx.check("no tool_use is left unpaired anywhere in the log",
                  find_unpaired_tool_use_ids(session.log) == [])
        tool_use_nodes = [n for n in session.log.nodes() if n.get("type") == "assistant"
                           and any(b.get("id") == "inline_abc123" for b in (n.get("content") or [])
                                   if isinstance(b, dict))]
        result_nodes = [n for n in session.log.nodes() if n.get("type") == "tool_result"
                         and n.get("tool_use_id") == "inline_abc123"]
        ctx.check(f"exactly one tool_use logged for the inline command, got {tool_use_nodes}",
                  len(tool_use_nodes) == 1)
        ctx.check(f"exactly one matching tool_result logged, got {result_nodes}", len(result_nodes) == 1)
        ctx.check(f"the result carries the real output, got {result_nodes}",
                  "from inline" in (result_nodes[0].get("content") or ""))
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
