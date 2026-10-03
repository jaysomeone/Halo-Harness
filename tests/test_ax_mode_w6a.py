"""tests.test_ax_mode_w6a -- Halo 2.0.1 release fix pass, finding 16
(W6a): `--ax-screen-reader`'s AskUserQuestion rendering (the current
`{questions:[...]}` wire shape, not the pre-H6 flat one) and its slash-
command routing (every typed `/command` except `/quit` used to be sent to
the model as an ordinary prompt instead of through `Controller.run_slash`,
contradicting this module's own docstring that slash commands "behave
identically" to the real TUI).
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- finding 16a: AskUserQuestion's current {questions:[...]} shape ------

class _FakeAskController:
    def __init__(self):
        self.answered = []

    def answer_question(self, request_id, answer):
        self.answered.append((request_id, answer))


@test
def test_ask_question_renders_the_current_questions_shape_single(ctx: Ctx):
    from halo_harness import ax_mode

    fake = _FakeAskController()
    data = {"id": "req1", "input": {"questions": [
        {"question": "Which database?", "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
    ]}}
    import builtins
    real_input = builtins.input
    builtins.input = lambda *a, **k: "2"
    try:
        ax_mode._ask_question(fake, data)
    finally:
        builtins.input = real_input
    ctx.check(f"a single-question answer is a plain string (picking option 2), got {fake.answered}",
              fake.answered == [("req1", "SQLite")])


@test
def test_ask_question_renders_several_questions_as_a_dict_answer(ctx: Ctx):
    """finding 16 (W6a): the OLD code read a single flat `question`/
    `options` pair and showed "[question] (question)" with no text and no
    options at all for the CURRENT multi-question shape. Several
    questions become one prompt each, and a `{question_text: answer}`
    dict -- the exact shape `QuestionCard`'s own docstring documents."""
    from halo_harness import ax_mode

    fake = _FakeAskController()
    data = {"id": "req2", "input": {"questions": [
        {"question": "Which database?", "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
        {"question": "Which cache?", "options": [{"label": "Redis"}, {"label": "Memcached"}]},
    ]}}
    replies = iter(["1", "Memcached"])
    import builtins
    real_input = builtins.input
    builtins.input = lambda *a, **k: next(replies)
    try:
        ax_mode._ask_question(fake, data)
    finally:
        builtins.input = real_input
    ctx.check(f"exactly one answer recorded, got {fake.answered}", len(fake.answered) == 1)
    req_id, answer = fake.answered[0]
    ctx.check(f"request id preserved, got {req_id!r}", req_id == "req2")
    ctx.check(f"a dict answer keyed by each question's own text, got {answer!r}",
              answer == {"Which database?": "Postgres", "Which cache?": "Memcached"})


@test
def test_ask_question_old_flat_shape_still_answers_something_sane(ctx: Ctx):
    """`_normalize_questions` already accepts the pre-H6 flat shape too --
    this must keep working, never regress to an empty/garbled render."""
    from halo_harness import ax_mode

    fake = _FakeAskController()
    data = {"id": "req3", "input": {"question": "Proceed?", "options": [{"label": "Yes"}, {"label": "No"}]}}
    import builtins
    real_input = builtins.input
    builtins.input = lambda *a, **k: "1"
    try:
        ax_mode._ask_question(fake, data)
    finally:
        builtins.input = real_input
    ctx.check(f"answered with the chosen option, got {fake.answered}", fake.answered == [("req3", "Yes")])


# ---- finding 16b: /commands route through controller.run_slash -----------

class _FakeCmd:
    def __init__(self, kind, source="builtin"):
        self.kind = kind
        self.source = source


class _FakeRegistry:
    def __init__(self, commands: dict):
        self._commands = commands

    def resolve(self, name):
        return self._commands.get(name)


class _FakeSlashController:
    """Just enough of the real Controller's surface for `run_ax_screen_
    reader_mode`'s own loop: `.events` (a real Queue, like the real
    Controller), `.registry.resolve`, `.run_slash`, `.submit`, `.quit`,
    `.cwd`. `run_slash` for a "prompt"-kind command queues events onto
    `.events` the way a real submitted turn eventually would -- a direct
    (non-prompt) command answers synchronously with no events at all."""

    def __init__(self):
        from halo_harness import events as ev
        self._ev = ev
        self.events = queue.Queue()
        self.cwd = Path(".")
        self.registry = _FakeRegistry({
            "help": _FakeCmd(kind="builtin"),
            "mycmd": _FakeCmd(kind="prompt", source="custom"),
        })
        self.run_slash_calls = []
        self.submit_calls = []

    def start(self):
        pass

    def submit(self, text):
        self.submit_calls.append(text)
        self.events.put(self._ev.text_delta("model saw: " + text))
        self.events.put(self._ev.turn_done(reason="end_turn"))

    def run_slash(self, name, args=""):
        self.run_slash_calls.append((name, args))
        if name == "help":
            return "halo help text"
        if name == "mycmd":
            self.events.put(self._ev.text_delta("skill output"))
            self.events.put(self._ev.turn_done(reason="end_turn"))
            return ""
        return f"Unknown command: /{name} (try /help)"

    def quit(self):
        return 0

    def answer_permission(self, *a, **k):
        pass

    def answer_question(self, *a, **k):
        pass

    def answer_plan(self, *a, **k):
        pass


def _run_with_scripted_input(monkey_build_controller, lines: list):
    import builtins
    from halo_harness import ax_mode
    import halo_harness.tui.bootstrap as bootstrap_mod

    real_build_controller = bootstrap_mod.build_controller
    bootstrap_mod.build_controller = monkey_build_controller
    real_input = builtins.input
    remaining = list(lines)

    def _fake_input(prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    builtins.input = _fake_input
    try:
        return ax_mode.run_ax_screen_reader_mode(SimpleNamespace(prompt=None, cwd=None))
    finally:
        builtins.input = real_input
        bootstrap_mod.build_controller = real_build_controller


@test
def test_slash_command_with_direct_text_result_routes_through_run_slash(ctx: Ctx):
    """finding 16 (W6a): every typed /command except /quit used to be sent
    to the MODEL as a plain prompt -- `/help` must reach `run_slash`
    ("help", ""), never `submit("/help")`."""
    fake = _FakeSlashController()
    _run_with_scripted_input(lambda args: (fake, None, None), ["/help", "/quit"])
    ctx.check(f"run_slash was called with the parsed name/args, got {fake.run_slash_calls}",
              fake.run_slash_calls == [("help", "")])
    ctx.check(f"submit() was NEVER called with the raw slash text, got {fake.submit_calls}",
              fake.submit_calls == [])


@test
def test_slash_command_that_submits_a_turn_still_drains_its_output(ctx: Ctx):
    """A prompt-kind command/skill (`run_slash` returns "" after QUEUEING
    a turn) must still have its own output drained -- the caller sees the
    skill's real reply, not silence."""
    import io
    import contextlib

    fake = _FakeSlashController()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        _run_with_scripted_input(lambda args: (fake, None, None), ["/mycmd arg1", "/quit"])
    ctx.check(f"run_slash got the args text too, got {fake.run_slash_calls}",
              fake.run_slash_calls == [("mycmd", "arg1")])
    ctx.check(f"the queued turn's own output was drained and shown, got {out.getvalue()!r}",
              "skill output" in out.getvalue())


@test
def test_plain_text_still_submits_an_ordinary_turn(ctx: Ctx):
    """Regression guard: a line that is NOT a slash command is completely
    unaffected by this fix."""
    fake = _FakeSlashController()
    _run_with_scripted_input(lambda args: (fake, None, None), ["hello there", "/quit"])
    ctx.check(f"submit() got the plain text, got {fake.submit_calls}", fake.submit_calls == ["hello there"])
    ctx.check("run_slash was never called for a non-slash line", fake.run_slash_calls == [])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
