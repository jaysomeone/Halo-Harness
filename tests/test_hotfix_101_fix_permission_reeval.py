"""tests.test_hotfix_101_fix_permission_reeval -- 1.0.1 fixpass finding 4:
`Session.reevaluate_pending_permission`/`Controller.reevaluate_pending_
permission` re-run the REAL `permission_engine.decide()` for a parked ask
under a newly-changed mode, instead of `action_choose_once()` blindly
allowing it: (a) an explicit `ask:` rule still asks in `auto` (only
`bypassPermissions` skips it); (b) `acceptEdits` resolves a pending
in-workdir edit/write card the engine now allows. `PermissionCard.resolve_
with_message`'s `done` guard and the new `resolve_externally` (updates
display without re-firing `on_decide`) are pinned directly on the widget.
The app-level (Shift+Tab key, `_borrowing_card`/awaiting_feedback
interaction) pilots live in test_tui.py.
"""
from __future__ import annotations

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


class _Env:
    """`Session.__init__` builds a real `SessionLog`, which opens/writes
    under `bridge_home()` the moment the Session exists -- NEVER derived
    from `fh["home"]`/`cwd` on its own. Without BRIDGE_TEST_HOME actually
    set, that falls back to the REAL ~/.halo/sessions on whatever
    machine runs the suite."""

    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
        os.environ["BRIDGE_TEST_HOME"] = str(self._fh["home"])
        os.environ.pop("BRIDGE_STATE_DIR", None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_session(fh, mock, *, model="or:mock/model", permission_engine=None, interactive=False):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-permreeval-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=10,
        permission_engine=permission_engine,
    )
    session.interactive = interactive
    return session


def _final_text(text):
    return [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": text}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


def _tool_call_chunks(call_id, name, arguments: dict):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _drive_and_wait_for_waiter(session, prompt: str, request_id: str, *, timeout: float = 10.0):
    """Runs `session.turn(prompt)` on a background thread (it blocks
    waiting for the permission answer) and waits for `request_id` to be
    registered in `_permission_waiters` -- same pattern test_review_
    findings_u2_h3b.py's own critical-finding-1 tests use."""
    events_seen: list = []
    done = threading.Event()

    def _drive():
        try:
            for ev in session.turn(prompt):
                events_seen.append(ev)
        finally:
            done.set()

    t = threading.Thread(target=_drive, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while request_id not in session._permission_waiters and time.monotonic() < deadline:
        time.sleep(0.02)
    return events_seen, done, t


# ---------------------------------------------------------------------------
# (a) an explicit ask: rule still asks in auto.
# ---------------------------------------------------------------------------

@test
def test_explicit_ask_rule_still_asks_under_auto(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine, parse_rule

    fh = build_fake_home()
    with _Env(fh):
        mock = MockUpstream().start()
        SCENARIOS["finding4a-ask-rule"] = lambda h, body: _finish(
            h, _tool_call_chunks("call_push", "Bash", {"command": "git push origin main"})
            if not any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
            else _final_text("done"))
        try:
            rule = parse_rule("Bash(git push:*)", source="user", base_dir=fh["proj"])
            engine = PermissionEngine(mode="default", cwd=fh["proj"], ask_rules=[rule])
            session = _new_session(fh, mock, model="or:mock/finding4a-ask-rule", permission_engine=engine,
                                    interactive=True)
            events_seen, done, _t = _drive_and_wait_for_waiter(session, "push my changes", "call_push")
            ctx.check("the waiter is registered (an ask card is pending)", "call_push" in session._permission_waiters)

            # Shift+Tab default -> ... -> auto: only bypassPermissions skips
            # an explicit ask: rule -- auto must NOT approve this.
            engine.mode = "auto"
            action = session.reevaluate_pending_permission("call_push")
            ctx.check(f"still 'ask' under auto -- the explicit rule is not skipped, got {action!r}", action is None)
            ctx.check("the waiter is still parked, unresolved", "call_push" in session._permission_waiters)

            # bypassPermissions is the one mode that DOES skip it -- confirms
            # the fix re-decides for real, it doesn't just always say no.
            engine.mode = "bypassPermissions"
            action2 = session.reevaluate_pending_permission("call_push")
            ctx.check(f"bypassPermissions DOES skip the ask rule, got {action2!r}", action2 == "allow")
            done.wait(10)
            ctx.check("the turn finished normally once actually resolved", done.is_set())
        finally:
            mock.stop()


# ---------------------------------------------------------------------------
# (b) acceptEdits resolves a pending in-workdir edit/write card.
# ---------------------------------------------------------------------------

@test
def test_accept_edits_resolves_a_pending_in_workdir_write_card(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine

    fh = build_fake_home()
    target = fh["proj"] / "finding4b_target.txt"
    with _Env(fh):
        mock = MockUpstream().start()
        SCENARIOS["finding4b-accept-edits"] = lambda h, body: _finish(
            h, _tool_call_chunks("call_w", "Write", {"file_path": str(target), "content": "hi\n"})
            if not any(m.get("role") == "tool" for m in (body or {}).get("messages") or [])
            else _final_text("done"))
        try:
            engine = PermissionEngine(mode="default", cwd=fh["proj"])  # default: edit_write_in_workdir -> ask
            session = _new_session(fh, mock, model="or:mock/finding4b-accept-edits", permission_engine=engine,
                                    interactive=True)
            events_seen, done, _t = _drive_and_wait_for_waiter(session, "write the file", "call_w")
            ctx.check("the waiter is registered (an edit card is pending)", "call_w" in session._permission_waiters)
            ctx.check("not written yet -- still waiting for an answer", not target.exists())

            engine.mode = "acceptEdits"
            action = session.reevaluate_pending_permission("call_w")
            ctx.check(f"acceptEdits now allows this in-workdir edit, got {action!r}", action == "allow")
            done.wait(10)
            ctx.check("the turn finished (the write actually ran)", done.is_set())
            ctx.check("the file was actually written",
                      target.exists() and target.read_text(encoding="utf-8") == "hi\n")
        finally:
            mock.stop()


@test
def test_nothing_pending_reevaluates_to_none(ctx: Ctx):
    """A stale/unknown request_id (already answered, or never existed) must
    report None, never raise -- the caller (app.py) treats that exactly
    like "still ask": leave whatever's currently pending alone."""
    from halo_harness.permissions import PermissionEngine
    fh = build_fake_home()
    with _Env(fh):
        mock = MockUpstream().start()
        try:
            engine = PermissionEngine(mode="auto", cwd=fh["proj"])
            session = _new_session(fh, mock, permission_engine=engine)
            ctx.check("no waiter at all -> None, not a crash",
                      session.reevaluate_pending_permission("nonexistent-request-id") is None)
        finally:
            mock.stop()


# ---------------------------------------------------------------------------
# Controller-level thin wrapper.
# ---------------------------------------------------------------------------

@test
def test_controller_reevaluate_pending_permission_delegates_to_session(ctx: Ctx):
    from halo_harness.controller import Controller

    class _FakeSessionWithReeval:
        def __init__(self):
            self.calls = []

        def reevaluate_pending_permission(self, request_id):
            self.calls.append(request_id)
            return "deny"

    session = _FakeSessionWithReeval()
    ctrl = Controller(session=session, cwd=Path.cwd(), state_dir=Path(tempfile.mkdtemp()), routes={})
    result = ctrl.reevaluate_pending_permission("req-1")
    ctx.check(f"delegates and returns the session's own verdict, got {result!r}", result == "deny")
    ctx.check(f"called with the same request_id, got {session.calls}", session.calls == ["req-1"])


# ---------------------------------------------------------------------------
# PermissionCard: resolve_with_message's `done` guard, resolve_externally
# never re-fires on_decide.
# ---------------------------------------------------------------------------

@test
def test_resolve_with_message_done_guard_fires_on_decide_at_most_once(ctx: Ctx):
    from halo_harness.tui.widgets.cards import PermissionCard
    decisions = []
    card = PermissionCard(request_id="r1", summary="Bash(x)", reason="", suggested_rule=None,
                           on_decide=decisions.append)
    card.resolve_with_message("first")
    card.resolve_with_message("second (must be ignored)")
    ctx.check(f"on_decide fired exactly once, got {len(decisions)} calls: {decisions}", len(decisions) == 1)
    ctx.check(f"the FIRST message won, got {decisions[0]}", decisions[0]["message"] == "first")
    ctx.check("the card is done", card.done is True)


@test
def test_resolve_externally_updates_display_without_calling_on_decide(ctx: Ctx):
    """1.0.1 fixpass finding 4: the request was ALREADY resolved directly
    against the permission engine (Controller.reevaluate_pending_
    permission) -- resolve_externally must only update the card's own
    display, never call on_decide a second time (that would re-resolve the
    SAME waiter/rule bookkeeping the engine call already did)."""
    from halo_harness.tui.widgets.cards import PermissionCard
    decisions = []
    card = PermissionCard(request_id="r2", summary="Write(x)", reason="", suggested_rule=None,
                           on_decide=decisions.append)
    card.resolve_externally("allow")
    ctx.check(f"on_decide was NEVER called, got {decisions}", decisions == [])
    ctx.check("the card is done", card.done is True)
    ctx.check(f"the summary reflects the outcome, got {card.summary!r}", "allowed" in card.summary)
    # A late resolve_with_message (e.g. a stray Enter after a mode-change
    # auto-resolve) must still be a no-op -- the done guard applies here too.
    card.resolve_with_message("too late")
    ctx.check(f"still never called on_decide, got {decisions}", decisions == [])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
