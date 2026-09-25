"""tests.test_improve_hint -- H10 Part B4: counters-only hint (never a
model call), fires at most once per session as a `notification` event
(never a card, never a pause -- identical in auto mode since the check
never inspects permission mode at all), and the `e` (edit) flow with
`$VISUAL`/`$EDITOR` mocked.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home

test, TESTS = new_registry()


class _FakeConfig:
    def __init__(self, *, hint=True, threshold=None):
        self.hint = hint
        self.hint_threshold = threshold or {"repairs": 3, "edit_failures": 2, "loop_breaker": 1}


@test
def test_should_hint_thresholds(ctx: Ctx):
    from rolo_claude.improve.hint import count_edit_failures, count_loop_breaker_trips, count_repair_hits, should_hint

    nodes = []
    ctx.check("nothing crosses an empty log", should_hint(nodes, _FakeConfig()) is None)

    repair_nodes = [{"type": "assistant", "tool_meta": {f"t{i}": {"repaired": True, "repair_kind": "rename"}}}
                     for i in range(3)]
    ctx.check(f"3 repair hits, count_repair_hits==3, got {count_repair_hits(repair_nodes)}",
              count_repair_hits(repair_nodes) == 3)
    ctx.check("3 repair hits crosses the default threshold (3)", should_hint(repair_nodes, _FakeConfig()) is not None)
    ctx.check("2 repair hits does NOT cross it", should_hint(repair_nodes[:2], _FakeConfig()) is None)

    edit_fail_nodes = [{"type": "tool_result", "tool": "Edit", "is_error": True, "error_class": "not_found"}
                        for _ in range(2)]
    ctx.check(f"2 edit failures, got {count_edit_failures(edit_fail_nodes)}", count_edit_failures(edit_fail_nodes) == 2)
    ctx.check("2 edit failures crosses the default threshold (2)", should_hint(edit_fail_nodes, _FakeConfig()) is not None)

    lb_nodes = [{"type": "tool_result", "error_class": "loop_breaker"}]
    ctx.check(f"1 loop-breaker trip, got {count_loop_breaker_trips(lb_nodes)}", count_loop_breaker_trips(lb_nodes) == 1)
    ctx.check("1 loop-breaker trip crosses the default threshold (1)", should_hint(lb_nodes, _FakeConfig()) is not None)

    ctx.check("improve.hint=false always returns None regardless of counters",
              should_hint(lb_nodes, _FakeConfig(hint=False)) is None)


@test
def test_hint_fires_once_per_session_never_a_card(ctx: Ctx):
    """Direct unit test of `Session._maybe_yield_improve_hint` -- appends
    enough loop-breaker trips to a real session's log, calls the generator
    twice, and proves it yields exactly one `notification` event the FIRST
    time and NOTHING the second (fires at most once per session), and that
    it never yields anything card/permission-shaped."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session as _Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.theme import set_config_value

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    set_config_value("improve", {"enabled": True, "hint": True, "model": None, "since_days": 7,
                                  "max_candidates": 8, "hint_threshold": {"repairs": 3, "edit_failures": 2, "loop_breaker": 1}})
    session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = _Session(cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                        state_dir=Path(tempfile.mkdtemp(prefix="improve-hint-session-")),
                        model_label="or:mock/model", session_context=session_ctx)
    # Cross the loop_breaker threshold (>= 1 trip).
    session.log.append_tool_result(tool_use_id="t1", content="Loop breaker: denied.", is_error=True,
                                    tool="Bash", error_class="loop_breaker")

    ctx.check("not fired yet", session._improve_hint_fired is False)
    first = list(session._maybe_yield_improve_hint())
    ctx.check(f"exactly one event the first time, got {len(first)}", len(first) == 1)
    ctx.check("it's a notification event, level info", first[0].kind == "notification" and first[0].data.get("level") == "info")
    ctx.check("mentions /improve", "/improve" in first[0].data.get("text", ""))
    ctx.check("never a permission_request or card-shaped event", first[0].kind not in ("permission_request", "plan_review"))
    ctx.check("fired flag now set", session._improve_hint_fired is True)

    second = list(session._maybe_yield_improve_hint())
    ctx.check(f"NOTHING the second time (fires at most once per session), got {len(second)}", len(second) == 0)


@test
def test_hint_disabled_by_config(ctx: Ctx):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session as _Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.theme import set_config_value

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    set_config_value("improve", {"hint": False})
    session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = _Session(cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                        state_dir=Path(tempfile.mkdtemp(prefix="improve-hint-off-session-")),
                        model_label="or:mock/model", session_context=session_ctx)
    session.log.append_tool_result(tool_use_id="t1", content="Loop breaker: denied.", is_error=True,
                                    tool="Bash", error_class="loop_breaker")
    events_out = list(session._maybe_yield_improve_hint())
    ctx.check("improve.hint=false -> never fires", events_out == [])


# ============================================================================
# `e` (edit): $VISUAL/$EDITOR mocked -- no real terminal/subprocess suspend.
# ============================================================================

class _FakeApp:
    """Just enough of `BridgeApp`'s surface for `_edit_candidate_then_apply`:
    `.suspend()` (a no-op context manager -- no real terminal to suspend in
    a unit test), `.notify()`, `.run_worker()` (runs the callable
    SYNCHRONOUSLY so the test can assert on its effect immediately),
    `.call_from_thread()` (already "on" the only thread here, so it's the
    same as calling directly), `.transcript.add_note()`."""

    class _FakeTranscript:
        def __init__(self):
            self.notes = []

        def add_note(self, text, **kwargs):
            self.notes.append((text, kwargs))

    def __init__(self):
        self.notifications = []
        self.worker_calls = []
        self.transcript = self._FakeTranscript()

    def notify(self, text, **kwargs):
        self.notifications.append((text, kwargs))

    def run_worker(self, fn, thread=True, name=None):
        self.worker_calls.append(name)
        fn()

    def call_from_thread(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def suspend(self):
        import contextlib
        return contextlib.nullcontext()


@test
def test_edit_then_apply_with_mocked_editor_changes_the_body(ctx: Ctx):
    from rolo_claude.improve.draft import Candidate
    from rolo_claude.tui.slash import _edit_candidate_then_apply

    cwd = Path(tempfile.mkdtemp(prefix="improve-edit-cwd-"))
    home = Path(tempfile.mkdtemp(prefix="improve-edit-home-"))
    os.environ["BRIDGE_TEST_HOME"] = str(home)

    # A fake "$EDITOR": a small Python one-liner that OVERWRITES the temp
    # file it's given with new content -- stands in for a real interactive
    # editor session.
    editor_script = cwd / "fake_editor.py"
    editor_script.write_text(
        "import sys\nopen(sys.argv[1], 'w', encoding='utf-8').write('EDITED BODY TEXT')\n", encoding="utf-8")
    old_editor = os.environ.get("EDITOR")
    os.environ["EDITOR"] = f'"{sys.executable}" "{editor_script}"'
    try:
        candidate = Candidate(id="c1", kind="rule", title="t", scope="project", path="p.md",
                               body="ORIGINAL BODY", rationale="r", evidence=[], confidence="low")

        session_cwd = str(cwd)

        class _FakeSession:
            cwd = session_cwd
            model_ref = type("R", (), {"raw": "or:mock/model"})()
            session_context = type("C", (), {"settings": None})()

        app = _FakeApp()
        _edit_candidate_then_apply(app, _FakeSession(), candidate)
        ctx.check(f"candidate.body was replaced by the editor's output, got {candidate.body!r}",
                  candidate.body == "EDITED BODY TEXT")
        ctx.check("an apply worker ran", "improve-apply" in app.worker_calls)
        applied_path = cwd / ".claude" / "rules" / "p.md"
        ctx.check("the applied file contains the EDITED body", applied_path.exists()
                  and "EDITED BODY TEXT" in applied_path.read_text(encoding="utf-8"))
    finally:
        if old_editor is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = old_editor


@test
def test_edit_then_apply_without_editor_set_falls_back_to_apply_as_drafted(ctx: Ctx):
    from rolo_claude.improve.draft import Candidate
    from rolo_claude.tui.slash import _edit_candidate_then_apply

    cwd = Path(tempfile.mkdtemp(prefix="improve-noedit-cwd-"))
    home = Path(tempfile.mkdtemp(prefix="improve-noedit-home-"))
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    old_editor, old_visual = os.environ.pop("EDITOR", None), os.environ.pop("VISUAL", None)
    try:
        candidate = Candidate(id="c1", kind="rule", title="t", scope="project", path="p.md",
                               body="UNCHANGED BODY", rationale="r", evidence=[], confidence="low")

        session_cwd = str(cwd)

        class _FakeSession:
            cwd = session_cwd
            model_ref = type("R", (), {"raw": "or:mock/model"})()
            session_context = type("C", (), {"settings": None})()

        app = _FakeApp()
        _edit_candidate_then_apply(app, _FakeSession(), candidate)
        ctx.check("body unchanged with no editor available", candidate.body == "UNCHANGED BODY")
        applied_path = cwd / ".claude" / "rules" / "p.md"
        ctx.check("still applied (as drafted)", applied_path.exists())
    finally:
        if old_editor is not None:
            os.environ["EDITOR"] = old_editor
        if old_visual is not None:
            os.environ["VISUAL"] = old_visual


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
