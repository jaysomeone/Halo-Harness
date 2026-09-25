"""tests.test_planmode_e2e -- H6 scope C: EnterPlanMode/ExitPlanMode through
a REAL agent.loop.Session, against the mock upstream -- the interactive
plan_review/plan_reply round trip (approve/reject), the `-p` auto-approve
vs. plan-text-as-result branches, and EnterPlanMode's own `-p`-allowed /
interactive-ask behaviours.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns

test, TESTS = new_registry()


def _text_step(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _tool_call_step(name: str, arguments: dict, call_id: str = "call_1") -> list:
    import json
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _new_session(*, mock, model, interactive=False, permission_mode="plan", cwd=None, plans_dir=None):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    cwd = cwd or Path(tempfile.mkdtemp(prefix="rc-plan-e2e-"))
    session_ctx = SessionContext(cwd=cwd, model_label=model, bare=True)
    session = Session(
        cwd=cwd, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="rc-plan-e2e-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=10,
        permission_engine=PermissionEngine(mode=permission_mode, cwd=cwd), agents={}, routes={},
    )
    session.interactive = interactive
    if plans_dir is not None:
        from rolo_claude.agent.planmode import ensure_plan_file

        class _Settings:
            plans_directory = str(plans_dir)
        path = ensure_plan_file(cwd, _Settings())
        session.permission_engine.set_plan_file(path)
    return session


class _ThreadedTurn:
    """Runs `session.turn(prompt)` on a background thread, appending each
    event to a shared list AS IT ARRIVES (never buffering the whole
    generator) -- needed because ExitPlanMode's interactive path genuinely
    BLOCKS the worker thread inside `_await_reply` until the main
    (test) thread calls `session.resolve_plan(...)`."""

    def __init__(self, session, prompt):
        self.events: list = []
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(session, prompt), daemon=True)
        self._thread.start()

    def _run(self, session, prompt):
        for ev in session.turn(prompt):
            self.events.append(ev)
        self.done.set()

    def wait_for(self, kind: str, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for ev in self.events:
                if ev.kind == kind:
                    return ev
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for a {kind!r} event")

    def join(self, timeout: float = 5.0):
        self._thread.join(timeout)
        if not self.done.is_set():
            raise AssertionError("turn did not finish in time")


# ---- interactive round trip: approve -------------------------------------------

@test
def test_exit_plan_mode_interactive_approve_flips_mode(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-approve"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "## Plan\n\nDo the thing."}),
            _text_step("implementing now"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-approve", interactive=True,
                                plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-a-")))
        turn = _ThreadedTurn(session, "please plan it")
        review = turn.wait_for("plan_review")
        ctx.check("plan text carried on the event", "Do the thing" in review.data.get("plan", ""))
        ctx.check("plan file path carried on the event", bool(review.data.get("path")))

        ok = session.resolve_plan({"approved": True, "mode_after": "acceptEdits", "feedback": ""})
        ctx.check("resolve_plan found the waiter", ok is True)
        turn.join()

        ctx.check("mode flipped to acceptEdits", session.permission_engine.mode == "acceptEdits")
        results = [e.data for e in turn.events if e.kind == "tool_result"]
        ctx.check("approval tool_result text",
                  any("approved the plan" in (r.get("content") or "").lower() for r in results))

        plan_path = Path(review.data["path"])
        ctx.check("plan file actually written to disk", "Do the thing" in plan_path.read_text(encoding="utf-8"))
    finally:
        mock.stop()


@test
def test_exit_plan_mode_approve_with_no_mode_after_defaults_to_accept_edits(ctx: Ctx):
    """D8 (`~/.claude/plans/typed-tickling-squirrel.md`): `plan_reply{approved,
    mode_after}` defaults `mode_after` to acceptEdits -- distinct from the
    sibling test above, which always passed an EXPLICIT "acceptEdits" and so
    never actually exercised `decision.get("mode_after") or "acceptEdits"`'s
    own default branch. A UI that omits `mode_after` entirely (or sends
    `None`) on approval must still land in acceptEdits, not stay in `plan`
    or fall back to `default`."""
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-approve-default-mode"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "## Plan\n\nDo the other thing."}),
            _text_step("implementing now"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-approve-default-mode", interactive=True,
                                plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-a-default-")))
        turn = _ThreadedTurn(session, "please plan it")
        turn.wait_for("plan_review")

        ok = session.resolve_plan({"approved": True, "feedback": ""})  # mode_after omitted entirely
        ctx.check("resolve_plan found the waiter", ok is True)
        turn.join()

        ctx.check(f"mode defaulted to acceptEdits, got {session.permission_engine.mode!r}",
                  session.permission_engine.mode == "acceptEdits")
    finally:
        mock.stop()


# ---- interactive round trip: reject with feedback -------------------------------

@test
def test_exit_plan_mode_interactive_reject_keeps_plan_mode(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-reject"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "## Plan\n\nDo the thing."}),
            _text_step("revising the plan"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-reject", interactive=True,
                                plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-r-")))
        turn = _ThreadedTurn(session, "please plan it")
        turn.wait_for("plan_review")
        session.resolve_plan({"approved": False, "feedback": "needs more detail on rollback", "mode_after": None})
        turn.join()

        ctx.check("mode stays plan on rejection", session.permission_engine.mode == "plan")
        results = [e.data for e in turn.events if e.kind == "tool_result"]
        rejection = next(r for r in results if not r.get("ok"))
        ctx.check("rejection is an error result", rejection.get("ok") is False)
        ctx.check("feedback carried into the result",
                  "needs more detail on rollback" in (rejection.get("summary") or rejection.get("content") or ""))
    finally:
        mock.stop()


@test
def test_exit_plan_mode_dismissed_without_a_reply_is_treated_as_not_approved(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-dismiss"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "plan text"}),
            _text_step("ok"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-dismiss", interactive=True,
                                plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-d-")))
        turn = _ThreadedTurn(session, "please plan it")
        turn.wait_for("plan_review")
        session.abort.set()  # Esc/interrupt while the plan review is pending
        turn.join()
        results = [e.data for e in turn.events if e.kind == "tool_result"]
        ctx.check("interrupted review resolves as not-approved (never hangs)",
                  any(not r.get("ok") for r in results))
    finally:
        mock.stop()


# ---- -p (non-interactive) behaviours --------------------------------------------

@test
def test_exit_plan_mode_print_mode_plan_text_is_the_result(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-noninteractive"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "## The Plan\n\nStep one, step two."}),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-noninteractive", interactive=False,
                                permission_mode="plan", plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-np-")))
        events = list(session.turn("plan how to do it"))

        ctx.check("only ONE upstream request (no second model call)", len(mock.requests) == 1)
        text = "".join(e.data.get("text", "") for e in events if e.kind == "text_delta")
        ctx.check("the plan text is the visible final answer", "Step one, step two" in text)
        ctx.check("turn_done reason is end_turn", any(e.kind == "turn_done" and e.data.get("reason") == "end_turn"
                                                        for e in events))
    finally:
        mock.stop()


@test
def test_exit_plan_mode_print_mode_auto_approves_under_accept_edits(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-autoapprove"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "## Plan\n\nJust do it."}),
            _text_step("done implementing"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-autoapprove", interactive=False,
                                permission_mode="acceptEdits", plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-aa-")))
        events = list(session.turn("plan and just do it"))

        ctx.check("TWO upstream requests (continues after auto-approve)", len(mock.requests) == 2)
        results = [e.data for e in events if e.kind == "tool_result"]
        ctx.check("approval text", any("approved the plan" in (r.get("content") or "").lower() for r in results))
        text = "".join(e.data.get("text", "") for e in events if e.kind == "text_delta")
        ctx.check("model continued past the plan", "done implementing" in text)
    finally:
        mock.stop()


@test
def test_exit_plan_mode_print_mode_auto_approves_under_bypass_permissions(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-bypass"] = ScriptedTurns([
            _tool_call_step("ExitPlanMode", {"plan": "plan"}),
            _text_step("implemented"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-bypass", interactive=False,
                                permission_mode="bypassPermissions", plans_dir=Path(tempfile.mkdtemp(prefix="rc-plans-bp-")))
        list(session.turn("go"))
        ctx.check("continues past the plan under bypassPermissions too", len(mock.requests) == 2)
    finally:
        mock.stop()


# ---- EnterPlanMode ----------------------------------------------------------------

@test
def test_enter_plan_mode_allowed_outright_in_print_mode(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-enter-pmode"] = ScriptedTurns([
            _tool_call_step("EnterPlanMode", {}),
            _text_step("researching now"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-enter-pmode", interactive=False,
                                permission_mode="default")
        ctx.check("starts NOT in plan mode", session.permission_engine.mode != "plan")
        events = list(session.turn("please research first"))
        ctx.check("mode is now plan", session.permission_engine.mode == "plan")
        ctx.check("a plan file now exists", session.permission_engine.plan_file is not None
                  and Path(session.permission_engine.plan_file).exists())
        results = [e.data for e in events if e.kind == "tool_result"]
        ctx.check("no denial for EnterPlanMode in print mode", all(r.get("ok") for r in results))
    finally:
        mock.stop()


@test
def test_enter_plan_mode_interactive_succeeds_with_a_notification(ctx: Ctx):
    """`_handle_enter_plan_mode`'s own contract: EnterPlanMode "always
    succeeds" in every mode (it only narrows capability -- there is
    nothing for a permission ask to protect) -- an interactive session
    gets an informational `notification` event instead of a real ask
    round trip, never a blocking PermissionCard."""
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-plan-enter-ask"] = ScriptedTurns([
            _tool_call_step("EnterPlanMode", {}),
            _text_step("researching now"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-plan-enter-ask", interactive=True,
                                permission_mode="default")
        events = list(session.turn("please research first"))
        ctx.check("mode flipped to plan without any blocking ask", session.permission_engine.mode == "plan")
        ctx.check("an informational notification was emitted",
                  any(e.kind == "notification" and "plan mode" in e.data.get("text", "").lower() for e in events))
        ctx.check("never emitted a permission_request for EnterPlanMode",
                  not any(e.kind == "permission_request" for e in events))
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
