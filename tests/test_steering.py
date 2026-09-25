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
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish
from rolo_claude import events as harness_events

test, TESTS = new_registry()


def _new_session(fh, mock, *, model="or:mock/model", permission_engine=None, interactive=False):
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
    )
    session.interactive = interactive
    return session


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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
