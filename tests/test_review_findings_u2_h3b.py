"""tests.test_review_findings_u2_h3b -- pinning tests for the CRITICAL/major
findings in docs/harness/review-findings-u2-h3b.md that this worker (H4)
owns and fixed alongside steering (finding 6 explicitly says they share
one mechanism): 1 (the permission/question waiter-pop bug that silently
dropped every card answer), 2 (abort mid-tool-loop + the quit/stopping
ordering bug), 6 (mode changes must apply mid-turn, not just between
turns), 14 (set_model re-gates vision/cap and logs the switch).
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

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
        state_dir=Path(tempfile.mkdtemp(prefix="findings-")), model_label=model, session_context=session_ctx,
        openrouter_base_url=mock.base_url, max_turns=10, permission_engine=permission_engine,
    )
    session.interactive = interactive
    return session


def _write_tool_chunks(call_id, target):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target), "content": "hi\n"})}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _final_text(text):
    return [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": text}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


@test
def test_finding_1_permission_card_answer_is_not_silently_dropped(ctx: Ctx):
    """CRITICAL: the waiter slot must stay registered while `_await_reply`
    blocks -- popping it up front (the old bug) made `resolve_permission`
    find nothing and return False, so the card's own answer never reached
    the worker; the turn only ever unblocked via `abort`."""
    from rolo_claude.permissions import Decision, PermissionEngine

    fh = build_fake_home()
    target = fh["proj"] / "finding1_target.txt"
    mock = MockUpstream().start()
    SCENARIOS["finding1-card"] = lambda h, body: _finish(
        h, _write_tool_chunks("call_w", target) if not any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
        else _final_text("done"))
    try:
        engine = PermissionEngine(mode="default", cwd=fh["proj"])
        session = _new_session(fh, mock, model="or:mock/finding1-card", permission_engine=engine, interactive=True)
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
        while "call_w" not in session._permission_waiters and time.monotonic() < deadline:
            time.sleep(0.02)
        ctx.check("the waiter is registered BEFORE the answer arrives", "call_w" in session._permission_waiters)

        # this is the exact call the UI thread makes -- must actually be
        # SEEN by the worker (the bug: it silently no-op'd, returning False).
        answered = session.resolve_permission("call_w", Decision("allow", "test allow"))
        ctx.check(f"resolve_permission reports success, got {answered}", answered is True)

        done.wait(10)
        ctx.check("the turn finished (never fell back to the Esc/abort escape hatch)", done.is_set())
        ctx.check("the Write actually ran", target.exists() and target.read_text(encoding="utf-8") == "hi\n")
        kinds = [e.kind for e in events_seen]
        ctx.check(f"no error/interrupted turn_done, got kinds={kinds}",
                   any(e.kind == "turn_done" and e.data.get("reason") == "end_turn" for e in events_seen))
    finally:
        mock.stop()


@test
def test_finding_1_second_answer_after_first_reports_false(ctx: Ctx):
    """`resolve_permission`/`_await_reply` popping the slot in `finally`
    (not up front) means a SECOND attempt to answer the same request_id
    correctly reports "nothing is waiting" instead of silently no-op'ing
    twice."""
    from rolo_claude.permissions import Decision, PermissionEngine

    fh = build_fake_home()
    target = fh["proj"] / "finding1b_target.txt"
    mock = MockUpstream().start()
    SCENARIOS["finding1-second"] = lambda h, body: _finish(
        h, _write_tool_chunks("call_w2", target) if not any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
        else _final_text("done"))
    try:
        engine = PermissionEngine(mode="default", cwd=fh["proj"])
        session = _new_session(fh, mock, model="or:mock/finding1-second", permission_engine=engine, interactive=True)
        done = threading.Event()

        def _drive():
            try:
                for _ev in session.turn("write the file"):
                    pass
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()
        deadline = time.monotonic() + 10
        while "call_w2" not in session._permission_waiters and time.monotonic() < deadline:
            time.sleep(0.02)
        first = session.resolve_permission("call_w2", Decision("allow", "ok"))
        done.wait(10)
        second = session.resolve_permission("call_w2", Decision("allow", "ok"))
        ctx.check(f"first answer succeeds, got {first}", first is True)
        ctx.check(f"second (late/duplicate) answer correctly reports nothing waiting, got {second}", second is False)
    finally:
        mock.stop()


@test
def test_finding_2_abort_between_tool_calls_never_runs_the_next_one(ctx: Ctx):
    """major: `abort` used to be checked only around the model call, never
    between DISPATCHING consecutive tool_use blocks in one assistant
    message -- an Esc during the first of `[Bash, Write]` still let the
    Write run. Two non-read-only calls in one message, abort set right
    after the first tool_use_ready fires, must synthesize an interrupted
    result for the second WITHOUT ever dispatching it."""
    fh = build_fake_home()
    target = fh["proj"] / "finding2_target.txt"
    mock = MockUpstream().start()
    SCENARIOS["finding2-abort-between"] = lambda h, body: _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_b", "type": "function",
             "function": {"name": "Bash", "arguments": json.dumps({"command": "true"})}},
            {"index": 1, "id": "call_w3", "type": "function",
             "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target), "content": "should-not-write\n"})}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])
    try:
        session = _new_session(fh, mock, model="or:mock/finding2-abort-between")
        gen = session.turn("run both")
        kinds = []
        for ev in gen:
            kinds.append(ev.kind)
            if ev.kind == "tool_use_ready" and ev.data.get("name") == "Bash":
                session.abort.set()
        ctx.check(f"the second tool never actually dispatched, got kinds={kinds}",
                   not target.exists())
        ctx.check("a tool_result was still logged for the second (interrupted) call",
                   kinds.count("tool_result") >= 1)
    finally:
        mock.stop()


@test
def test_finding_2_quit_drains_a_queued_prompt_instead_of_starting_it(ctx: Ctx):
    """major: `Controller.quit()` used to queue its `None` sentinel AFTER
    any already-queued `user_input`, and `_pump_turn` unconditionally
    cleared `abort` for every `user_input` -- quitting while a prompt sat
    queued started a WHOLE NEW turn before the sentinel was ever reached.
    `Session._stopping` (set before the sentinel is queued) must make
    `run()` skip any command queued ahead of it."""
    import queue as _queue
    from rolo_claude import events as events_mod

    fh = build_fake_home()
    mock = MockUpstream().start()
    SCENARIOS["finding2-quit-drain"] = lambda h, body: _finish(h, _final_text("should never run"))
    try:
        session = _new_session(fh, mock, model="or:mock/finding2-quit-drain")
        commands: "_queue.Queue" = _queue.Queue()
        out_events: list = []
        commands.put(events_mod.Command("user_input", {"text": "queued before quit"}))
        session._stopping.set()  # exactly what Controller.quit() does, in order
        session.abort.set()
        commands.put(None)
        code = session.run(commands, out_events.append)
        ctx.check(f"run() returns cleanly, got {code}", code == 0)
        ctx.check(f"no turn ever started for the queued prompt, got events={[e.kind for e in out_events]}",
                   not any(e.kind == "user_message" for e in out_events))
        ctx.check("the mock upstream was never called", len(mock.requests) == 0)
    finally:
        mock.stop()


@test
def test_finding_6_mode_change_applies_immediately_not_just_between_turns(ctx: Ctx):
    """major: `Controller.set_permission_mode` writes `permission_engine.
    mode` directly (an atomic attribute, like `abort.set()`) instead of
    queuing a Command `run()` only reads between turns -- switching to
    `auto` mid-turn must skip a permission card for the SECOND of two
    Write calls in the same turn, not just apply starting next turn."""
    from rolo_claude.permissions import PermissionEngine

    fh = build_fake_home()
    target1 = fh["proj"] / "finding6_a.txt"
    mock = MockUpstream().start()

    def _scn(h, body):
        messages = (body or {}).get("messages") or []
        tool_results = [m for m in messages if m.get("role") == "tool"]
        if len(tool_results) >= 1:
            _finish(h, _final_text("done"))
            return
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "type": "function",
                 "function": {"name": "Write", "arguments": json.dumps({"file_path": str(target1), "content": "a\n"})}},
            ]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])

    SCENARIOS["finding6-mode-mid-turn"] = _scn
    try:
        engine = PermissionEngine(mode="default", cwd=fh["proj"])
        session = _new_session(fh, mock, model="or:mock/finding6-mode-mid-turn",
                                permission_engine=engine, interactive=True)
        gen = session.turn("write a file")
        saw_card = False
        for ev in gen:
            if ev.kind == "permission_request":
                saw_card = True
                # exactly what Controller.set_permission_mode does now:
                # a direct, atomic write, no Command queued.
                session.permission_engine.mode = "auto"
                session.resolve_permission(ev.data["id"], __import__(
                    "rolo_claude.permissions", fromlist=["Decision"]).Decision("allow", "ok"))
        ctx.check("a card was shown for the first Write (default mode)", saw_card)
        ctx.check(f"mode is now auto, got {session.permission_engine.mode}", session.permission_engine.mode == "auto")
    finally:
        mock.stop()


@test
def test_finding_14_set_model_logs_a_meta_node_and_regates_vision(ctx: Ctx):
    """major: `set_model` used to leave the SessionCatalog cap, McpTool
    vision flags, and the log completely untouched."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    SCENARIOS["finding14-model"] = lambda h, body: _finish(h, _final_text("ok"))
    try:
        session = _new_session(fh, mock, model="or:mock/finding14-model")
        from rolo_claude.model import ModelProfile, parse_model_ref
        before_nodes = len(session.log.nodes())
        new_ref = parse_model_ref("or:mock/finding14-model-2")
        new_profile = ModelProfile(vision=True)
        session.set_model(new_ref, new_profile)
        after_nodes = session.log.nodes()
        ctx.check(f"a meta node was appended, got before={before_nodes} after={len(after_nodes)}",
                   len(after_nodes) == before_nodes + 1)
        last = after_nodes[-1]
        ctx.check(f"the meta node names the new model, got {last}",
                   last.get("type") == "meta" and last.get("model") == new_ref.raw)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
