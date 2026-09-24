"""tests.test_fake_controller -- rolo_claude/testing/fake_controller.py
(U0 scope E): the scripted Controller stand-in (submit/interrupt/quit/... +
call recording) and `run_demo` (`--demo`/`--stress`'s replay through the
real text/json print-mode sinks).
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude import events as ev
from rolo_claude.testing.fake_controller import (
    FakeController, default_demo_turns, run_demo, stress_turns,
)

test, TESTS = new_registry()


@test
def test_submit_returns_scripted_events_in_order(ctx: Ctx):
    fc = FakeController()
    events = list(fc.submit("hello"))
    ctx.check("submit recorded the text", fc.submitted == ["hello"])
    ctx.check("first event is user_message", events[0].kind == "user_message")
    ctx.check("last event is turn_done", events[-1].kind == "turn_done")
    ctx.check("every item is a real Event", all(isinstance(e, ev.Event) for e in events))


@test
def test_submit_loops_once_turns_exhausted(ctx: Ctx):
    fc = FakeController(turns=[[ev.turn_done(turn=1, reason="end_turn")]])
    first = list(fc.submit("a"))
    second = list(fc.submit("b"))
    ctx.check("single-turn script replays on the second call too",
              first[0].kind == second[0].kind == "turn_done")
    ctx.check("both submissions recorded", fc.submitted == ["a", "b"])


@test
def test_submit_with_no_turns_returns_empty(ctx: Ctx):
    fc = FakeController(turns=[])
    ctx.check("empty turns -> empty iterator, not an exception", list(fc.submit("x")) == [])


@test
def test_interrupt_counter(ctx: Ctx):
    fc = FakeController()
    fc.interrupt()
    fc.interrupt()
    fc.interrupt()
    ctx.check(f"interrupts counted, got {fc.interrupts}", fc.interrupts == 3)


@test
def test_quit_records_and_returns_zero(ctx: Ctx):
    fc = FakeController()
    ctx.check("quit_called starts False", fc.quit_called is False)
    rc = fc.quit()
    ctx.check("quit() returns 0", rc == 0)
    ctx.check("quit_called flips True", fc.quit_called is True)


@test
def test_set_model_and_permission_mode(ctx: Ctx):
    fc = FakeController(model="or:x/y", permission_mode="default")
    fc.set_model("or:a/b")
    fc.set_permission_mode("auto")
    ctx.check("model updated", fc.model == "or:a/b")
    ctx.check("permission_mode updated", fc.permission_mode == "auto")
    ctx.check("list_models reflects the new model", fc.list_models() == ["or:a/b"])


@test
def test_add_permission_rule_and_answers_recorded(ctx: Ctx):
    fc = FakeController()
    fc.add_permission_rule("Bash(git *)", "session")
    fc.answer_permission("req1", "allow_once")
    fc.answer_question("req2", "yes")
    ctx.check("rule recorded", fc.added_rules == [("Bash(git *)", "session")])
    ctx.check("permission reply recorded", fc.permission_replies == [("req1", "allow_once")])
    ctx.check("question reply recorded", fc.question_replies == [("req2", "yes")])


@test
def test_run_slash_recorded_and_returns_text(ctx: Ctx):
    fc = FakeController()
    out = fc.run_slash("model", "or:x/y")
    ctx.check("call recorded", fc.slash_calls == [("model", "or:x/y")])
    ctx.check(f"returns a non-empty string, got {out!r}", isinstance(out, str) and out)


@test
def test_reconnect_mcp_counter(ctx: Ctx):
    fc = FakeController()
    fc.reconnect_mcp("expanded-models")
    fc.reconnect_mcp("expanded-models")
    ctx.check(f"reconnects counted, got {fc.reconnects}", fc.reconnects == 2)


@test
def test_mcp_status_shape(ctx: Ctx):
    fc = FakeController()
    status = fc.mcp_status()
    ctx.check("has connected/total keys", set(status) == {"connected", "total"})


@test
def test_stress_turns_extends_default(ctx: Ctx):
    base = len(default_demo_turns())
    extended = stress_turns(5)
    ctx.check(f"base + 5 extra turns, got {len(extended)}", len(extended) == base + 5)
    ctx.check("stress_turns(0) == the default transcript only", len(stress_turns(0)) == base)


# ---- run_demo (the actual --demo/--stress print-mode replay) --------------

@test
def test_run_demo_text_output(ctx: Ctx):
    buf = io.StringIO()
    rc = run_demo(output_format="text", stream=buf)
    ctx.check(f"exit 0, got {rc}", rc == 0)
    out = buf.getvalue()
    ctx.check(f"final scripted text present, got {out!r}", "scripted demo turn" in out)


@test
def test_run_demo_json_output_shape(ctx: Ctx):
    buf = io.StringIO()
    rc = run_demo(output_format="json", stream=buf)
    ctx.check(f"exit 0, got {rc}", rc == 0)
    obj = json.loads(buf.getvalue().strip())
    ctx.check("type is result", obj.get("type") == "result")
    ctx.check("subtype is success", obj.get("subtype") == "success")
    ctx.check("not an error", obj.get("is_error") is False)
    ctx.check("session_id is the demo one", obj.get("session_id") == "demo-session")


@test
def test_run_demo_stress_extends_the_transcript(ctx: Ctx):
    buf = io.StringIO()
    rc = run_demo(output_format="text", stress=3, stream=buf)
    ctx.check(f"exit 0, got {rc}", rc == 0)
    out = buf.getvalue()
    ctx.check(f"the LAST stress turn's text is the final result, got {out!r}", "stress reply 2" in out)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
