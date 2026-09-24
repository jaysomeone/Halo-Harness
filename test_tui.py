"""test_tui.py -- the Textual TUI's own suite (U2, D-TUI Testing), run
separately from `python tests/run_all.py` (that discoverer only globs
`tests/test_*.py`, matching `test_bridge.py`'s own repo-root placement) --
see `docs/harness/INSTALL.md`/the U2 report for the exact command. Same
Ctx/@test/run_all pattern as `test_bridge.py`/`tests/helpers/runner.py`
(`tests.helpers.runner.new_registry`); every pilot test is a plain sync
`@test` function that calls `asyncio.run(...)` itself, per the brief.

textual/rich are real imports here (this file's whole job is exercising
them) -- never imported at `rolo_claude` package scope elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns

from rolo_claude import events as ev
from rolo_claude.testing.fake_controller import FakeController, default_demo_turns
from rolo_claude.tui.app import BridgeApp
from rolo_claude.tui.events import drain_queue
from rolo_claude.tui.widgets.cards import PermissionCard, PlanCard, QuestionCard, ToolCard
from rolo_claude.tui.widgets.transcript import AssistantText, UserMessage

test, TESTS = new_registry()

SNAPSHOT_DIR = REPO_DIR / "docs" / "harness" / "tui-snapshots"


def _cwd() -> str:
    return str(REPO_DIR)


async def _mounted(controller, **kwargs):
    """Build a `BridgeApp`, enter `run_test`, return `(app, pilot_cm)` --
    callers use `async with app.run_test(...) as pilot:` themselves; this
    just centralises the constructor defaults every test shares."""
    return BridgeApp(controller, cwd=kwargs.pop("cwd", _cwd()), **kwargs)


async def _drain_a_few(app, pilot, n: int = 15, pause: float = 0.02) -> None:
    for _ in range(n):
        await app._drain()
        await pilot.pause(pause)


async def _type(pilot, text: str) -> None:
    for ch in text:
        await pilot.press(ch)


# ============================================================================
# Pure: drain_queue coalescing (tui/events.py) -- no textual/app needed.
# ============================================================================

@test
def test_drain_queue_coalesces_consecutive_deltas(ctx: Ctx):
    raw = [
        ev.text_delta("a", index=0, turn=1),
        ev.text_delta("b", index=0, turn=1),
        ev.Event("tool_use_start", {"id": "t1", "name": "Bash"}, turn=1),
        ev.text_delta("c", index=0, turn=1),
        ev.thinking_delta("x", index=0, turn=1),
        ev.thinking_delta("y", index=0, turn=1),
        ev.text_delta("d", index=1, turn=1),  # different index -- must NOT merge with index 0
    ]
    out = drain_queue(raw)
    # a+b merge (1), tool_use_start passes through (1), c starts a NEW run
    # (the intervening non-delta event breaks the chain, so it never
    # rejoins "ab") (1), x+y merge (1), d (a different index) never merges
    # into anything (1) = 5, not 7.
    ctx.check(f"5 events after coalescing (was 7), got {len(out)}", len(out) == 5)
    ctx.check("first text_delta merged a+b", out[0].kind == "text_delta" and out[0].data["text"] == "ab")
    ctx.check("tool_use_start passed through untouched", out[1].kind == "tool_use_start")
    ctx.check("text_delta after a non-delta event starts a new run (c alone, not rejoined with ab)",
              out[2].kind == "text_delta" and out[2].data["text"] == "c")
    ctx.check("thinking_delta run merged x+y", out[3].kind == "thinking_delta" and out[3].data["text"] == "xy")
    ctx.check("a different index never merges", out[4].data["index"] == 1 and out[4].data["text"] == "d")


# ============================================================================
# submit/stream + tool card lifecycle
# ============================================================================

@test
def test_submit_streams_text_and_mounts_tool_card(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "Show me a quick demo")
            await pilot.press("enter")
            await _drain_a_few(app, pilot)
            kinds = [type(w).__name__ for w in app.transcript.children]
            ctx.check(f"transcript has UserMessage, AssistantText, ToolCard, AssistantText, got {kinds}",
                      kinds == ["UserMessage", "AssistantText", "ToolCard", "AssistantText"])
            assistant_texts = [w for w in app.transcript.children if isinstance(w, AssistantText)]
            ctx.check("first assistant block streamed the scripted text",
                      "Sure -- let me check something first." in assistant_texts[0].raw_text)
            ctx.check("second assistant block streamed the final line",
                      "scripted demo turn" in assistant_texts[1].raw_text)
            ctx.check("fake controller recorded the submission", fake.submitted == ["Show me a quick demo"])
    asyncio.run(body())


@test
def test_tool_card_same_widget_start_to_result_plus_ctrl_o(ctx: Ctx):
    """A `FakeController` script hands its whole turn to `_local_events` in
    one shot (no real per-event timing to catch "mid-flight" through the 30
    Hz drain timer), so this drives `dispatch.apply_event` directly, one
    event at a time, to observe the SAME `ToolCard` object transition
    running -> ok -- the real invariant this test is about."""
    from rolo_claude.tui.dispatch import apply_event

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await apply_event(app, ev.Event("tool_use_ready", {
                "id": "tu1", "name": "Bash", "input": {"command": "echo hi"}, "repaired": False,
            }, turn=1))
            card = app.transcript.tool_cards.get("tu1")
            ctx.check("a ToolCard was mounted for tu1", card is not None)
            ctx.check(f"it starts running, got {card.status}", card.status == "running")
            ctx.check("not expanded by default", card.expanded is False)

            await apply_event(app, ev.Event("tool_result", {"id": "tu1", "ok": True, "summary": "hi"}, turn=1))
            ctx.check("the SAME widget instance now shows ok", app.transcript.tool_cards["tu1"] is card)
            ctx.check(f"status flipped to ok, got {card.status}", card.status == "ok")

            only_cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
            ctx.check(f"exactly one ToolCard widget exists (no duplicate mount), got {len(only_cards)}",
                      len(only_cards) == 1)

            await pilot.press("ctrl+o")
            ctx.check("real Ctrl+O keypress expands the card", card.expanded is True)
            ctx.check("app.verbose toggled on", app.verbose is True)
    asyncio.run(body())


# ============================================================================
# PermissionCard: 1/2/3/4 keys, session vs "always" (real disk write, with
# Claude Code's own escaping preserved verbatim), deny + feedback message.
# ============================================================================

class _RuleWritingController(FakeController):
    """`FakeController` plus a REAL `add_permission_rule` (writes through
    `permissions.add_allow_rule` against a real temp cwd) -- everything
    else about it stays scripted/fake."""

    def __init__(self, *, cwd: Path, **kwargs):
        super().__init__(**kwargs)
        self._cwd = cwd

    def add_permission_rule(self, rule: str, scope: str):
        self.added_rules.append((rule, scope))
        if scope == "session":
            return None
        from rolo_claude.permissions import add_allow_rule
        return add_allow_rule(rule, scope, cwd=self._cwd)


def _permission_turn(suggested_rule: str) -> list:
    return [[
        ev.user_message("do something", turn=1),
        ev.Event("tool_use_ready", {"id": "tu1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"},
                                     "repaired": False}, turn=1),
        ev.Event("permission_request", {"id": "tu1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"},
                                         "reason": "not covered by an existing rule",
                                         "suggested_rule": suggested_rule}, turn=1),
    ]]


@test
def test_permission_card_allow_once(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            # scope 0(c) (rolo, binding): the prompt stays ENABLED while a
            # card is pending -- same as any other running turn -- so
            # steering still works underneath the card; focus defaults to
            # the card itself so its own digit/Esc keys keep working.
            ctx.check("prompt input stays enabled while a card is pending", app.prompt_input.disabled is False)
            ctx.check("focus defaults to the pending card", app.focused is app.pending_card)
            await pilot.press("1")
            await pilot.pause(0.05)
            ctx.check(f"answered allow once, got {fake.permission_replies}",
                      fake.permission_replies == [("tu1", {"action": "allow", "reason": "", "rule": None, "message": ""})])
            ctx.check("no rule added for a one-time allow", fake.added_rules == [])
            ctx.check("pending card cleared", app.pending_card is None)
            ctx.check("focus returns to the prompt once the card clears", app.focused is app.prompt_input)
    asyncio.run(body())


@test
def test_permission_card_allow_session_teaches_the_rule_without_writing_disk(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            await pilot.press("2")
            await pilot.pause(0.05)
            reply = fake.permission_replies[-1]
            ctx.check(f"session reply carries the rule text, got {reply}",
                      reply == ("tu1", {"action": "allow", "reason": "", "rule": "Bash(rm:*)", "message": ""}))
            ctx.check("session scope never writes to disk", fake.added_rules == [])
    asyncio.run(body())


@test
def test_permission_card_allow_always_writes_escaped_rule_to_settings_local_json(ctx: Ctx):
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            # Claude Code's own escaping (permissions._escape_rule_content):
            # a literal "(" / ")" inside a rule's CONTENT is backslash-escaped.
            suggested = r"Bash(echo \(hello\):*)"
            fake = _RuleWritingController(cwd=cwd, turns=_permission_turn(suggested))
            app = await _mounted(fake, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "do something")
                await pilot.press("enter")
                await _drain_a_few(app, pilot, n=6)
                await pilot.press("3")
                await _drain_a_few(app, pilot, n=3)
            settings_path = cwd / ".claude" / "settings.local.json"
            ctx.check(f"settings.local.json was written at {settings_path}", settings_path.exists())
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            allow = data.get("permissions", {}).get("allow", [])
            ctx.check(f"the exact, already-escaped suggested rule text was written verbatim, got {allow}",
                      suggested in allow)
            ctx.check(f"added_rules recorded the local-scope write, got {fake.added_rules}",
                      fake.added_rules == [(suggested, "local")])
    asyncio.run(body())


@test
def test_permission_card_deny_with_feedback_message(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            await pilot.press("4")
            await pilot.pause(0.05)
            ctx.check("now borrowing the prompt input for feedback", app._borrowing_card is not None)
            ctx.check(f"placeholder invites feedback, got {app.prompt_input.placeholder!r}",
                      "differently" in app.prompt_input.placeholder)
            await _type(pilot, "use a safer command")
            await pilot.press("enter")
            await pilot.pause(0.05)
            reply = fake.permission_replies[-1]
            ctx.check(f"deny + message recorded, got {reply}",
                      reply == ("tu1", {"action": "deny", "reason": "", "rule": None,
                                         "message": "use a safer command"}))
            ctx.check("no longer borrowing the input", app._borrowing_card is None)
            ctx.check("pending card cleared", app.pending_card is None)
    asyncio.run(body())


# ============================================================================
# /model, Shift+Tab mode cycle, Esc interrupt, Ctrl+C x2 quit, paste, history
# ============================================================================

@test
def test_model_command_with_explicit_ref_sets_fake_model(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/model or:moonshotai/kimi-k3")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"fake.model updated to the requested ref, got {fake.model}",
                      fake.model == "or:moonshotai/kimi-k3")
    asyncio.run(body())


@test
def test_model_picker_opens_on_bare_slash_model_and_esc_dismisses(ctx: Ctx):
    from rolo_claude.tui.dialogs.model_picker import ModelPicker

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/model")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"ModelPicker is the active screen, got {type(app.screen).__name__}",
                      isinstance(app.screen, ModelPicker))
            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check("Esc dismisses the picker", not isinstance(app.screen, ModelPicker))
    asyncio.run(body())


@test
def test_shift_tab_cycles_the_status_text_through_all_four_modes(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            seen = [app.status_bar.mode]
            for _ in range(4):
                await pilot.press("shift+tab")
                seen.append(app.status_bar.mode)
            ctx.check(f"cycled default -> acceptEdits -> plan -> auto -> default, got {seen}",
                      seen == ["default", "acceptEdits", "plan", "auto", "default"])
            ctx.check(f"the controller's own mode matches the last cycle step, got {fake.permission_mode}",
                      fake.permission_mode == "default")
    asyncio.run(body())


@test
def test_esc_interrupts_a_running_turn(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("escape")
            await pilot.pause(0.05)
            ctx.check(f"exactly one interrupt recorded, got {fake.interrupts}", fake.interrupts == 1)
    asyncio.run(body())


@test
def test_ctrl_c_twice_quits_cleanly(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            await pilot.press("ctrl+c")
            await pilot.pause(0.3)
        ctx.check("controller.quit() was called", fake.quit_called is True)
        ctx.check(f"app exited with return_code 0, got {app.return_code}", app.return_code == 0)
    asyncio.run(body())


@test
def test_paste_4_or_more_lines_becomes_a_placeholder(ctx: Ctx):
    from textual import events as tevents

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            pasted = "line1\nline2\nline3\nline4\nline5"
            app.prompt_input.post_message(tevents.Paste(pasted))
            await pilot.pause(0.1)
            ctx.check(f"a 5-line paste becomes a placeholder, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "[Pasted text #1 +5 lines]")
            ctx.check("the original text is tracked for later expansion",
                      app.prompt_input.pasted == {1: pasted})

            app.prompt_input.clear_submitted()
            app.prompt_input.post_message(tevents.Paste("short\ntext"))
            await pilot.pause(0.1)
            ctx.check(f"a 2-line paste inserts literally (below the threshold), got {app.prompt_input.text!r}",
                      app.prompt_input.text == "short\ntext")
    asyncio.run(body())


@test
def test_history_up_recalls_from_both_claude_code_and_rolo_claude_files(ctx: Ctx):
    async def body():
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as state_dir:
            old_home, old_state = os.environ.get("BRIDGE_TEST_HOME"), os.environ.get("BRIDGE_STATE_DIR")
            os.environ["BRIDGE_TEST_HOME"], os.environ["BRIDGE_STATE_DIR"] = home_dir, state_dir
            try:
                cwd_str = str(REPO_DIR)
                claude_dir = Path(home_dir) / ".claude"
                claude_dir.mkdir(parents=True, exist_ok=True)
                (claude_dir / "history.jsonl").write_text(json.dumps({
                    "display": "from claude code history", "pastedContents": {}, "project": cwd_str,
                    "sessionId": "s1", "timestamp": 1.0,
                }) + "\n", encoding="utf-8")
                rolo_dir = Path(state_dir)
                rolo_dir.mkdir(parents=True, exist_ok=True)
                (rolo_dir / "history.jsonl").write_text(json.dumps({
                    "display": "from rolo-claude history", "pastedContents": {}, "project": cwd_str,
                    "sessionId": "s2", "timestamp": 2.0,
                }) + "\n", encoding="utf-8")

                fake = FakeController()
                app = await _mounted(fake)
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.click("#prompt-input")
                    await pilot.press("up")
                    await pilot.pause(0.05)
                    first = app.prompt_input.text
                    await pilot.press("up")
                    await pilot.pause(0.05)
                    second = app.prompt_input.text
                ctx.check(f"the newest (rolo-claude's own file) entry recalls first, got {first!r}",
                          first == "from rolo-claude history")
                ctx.check(f"the older (Claude Code's own file) entry recalls next, got {second!r}",
                          second == "from claude code history")
            finally:
                for key, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_STATE_DIR", old_state)):
                    if old is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old
    asyncio.run(body())


# ============================================================================
# QuestionCard (multi-question + tab strip + "Other..."), PlanCard, folding
# ============================================================================

@test
def test_question_card_multi_question_tab_navigation_and_other(ctx: Ctx):
    async def body():
        fake = FakeController(turns=[[
            ev.user_message("configure", turn=1),
            ev.Event("question", {"id": "q1", "name": "AskUserQuestion", "input": {
                "questions": [
                    {"question": "Which color?", "options": ["Red", "Blue"]},
                    {"question": "Which size?", "options": ["Small", "Large"]},
                ],
            }}, turn=1),
        ]])
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "configure")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            card = app.pending_card
            ctx.check("a QuestionCard is pending", isinstance(card, QuestionCard))
            ctx.check(f"both questions loaded, got {len(card.questions)}", len(card.questions) == 2)
            ctx.check("question 0 active first", card.active_index == 0)

            await pilot.press("tab")
            ctx.check(f"Tab (the tab strip) moves to question 1, got {card.active_index}", card.active_index == 1)
            await pilot.press("down")  # "Small" -> "Large"
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check(f"answering question 1 returns focus to the remaining one (0), got {card.active_index}",
                      card.active_index == 0)

            await pilot.press("down")
            await pilot.press("down")  # "Red" -> "Blue" -> "Other..."
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check("'Other...' borrows the prompt input for free text", app._borrowing_card is card)
            await _type(pilot, "Purple")
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check(f"both answers delivered as one dict, got {fake.question_replies}",
                      fake.question_replies == [("q1", {"Which color?": "Purple", "Which size?": "Large"})])
            ctx.check("pending card cleared", app.pending_card is None)
    asyncio.run(body())


@test
def test_plan_card_keep_planning_with_feedback(ctx: Ctx):
    async def body():
        fake = FakeController(turns=[[
            ev.user_message("make a plan", turn=1),
            ev.Event("plan_review", {"id": "p1", "plan": "1. Do X\n2. Do Y"}, turn=1),
        ]])
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "make a plan")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PlanCard is pending", isinstance(app.pending_card, PlanCard))
            await pilot.press("3")
            await pilot.pause(0.05)
            ctx.check("keep-planning borrows the prompt input", app._borrowing_card is not None)
            await _type(pilot, "add step 3")
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check(f"plan reply recorded, got {fake.plan_replies}",
                      fake.plan_replies == [{"approved": False, "feedback": "add step 3", "mode_after": None}])
            ctx.check("pending card cleared", app.pending_card is None)
    asyncio.run(body())


@test
def test_folding_after_300_widgets(ctx: Ctx):
    from rolo_claude.tui.widgets.transcript import FoldedHistory

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            for i in range(320):
                await app.transcript.add_note(f"note {i}")
            ctx.check(f"150 widgets folded (half of 300), got {app.transcript.folded_count}",
                      app.transcript.folded_count == 150)
            ctx.check(f"live widget count is well under 320, got {len(app.transcript.children)}",
                      len(app.transcript.children) < 320)
            ctx.check("a FoldedHistory placeholder sits at the top",
                      isinstance(app.transcript.children[0], FoldedHistory))
    asyncio.run(body())


@test
def test_stress_500_turns_stays_responsive(ctx: Ctx):
    """Acceptance: `python -m rolo_claude --demo --stress 500` must stay
    responsive. An interactive full-screen session can't be driven headless
    from a test runner, so this is the automated, deterministic proxy: push
    the exact same `stress_turns(500)` scripted event volume (~2,500 events
    across 501 turns) through the REAL drain/apply_event pipeline (not a
    micro-benchmark of `drain_queue` alone) and assert it stays within a
    generous wall-clock budget, with the transcript's live widget count kept
    bounded by folding (D-TUI: fold after 300 widgets) rather than growing
    unboundedly for the whole run."""
    import time

    from rolo_claude.testing.fake_controller import stress_turns

    async def body():
        fake = FakeController(turns=stress_turns(500))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            start = time.monotonic()
            for i in range(len(fake.turns)):
                for e in fake.submit(f"stress {i}"):
                    app._local_events.put(e)
                await app._drain()
            await _drain_a_few(app, pilot, n=5)
            elapsed = time.monotonic() - start
            ctx.check(f"501 turns (~2,500 events) processed within budget, got {elapsed:.2f}s", elapsed < 30.0)
            ctx.check(f"live widget count stays bounded by folding, got {len(app.transcript.children)}",
                      len(app.transcript.children) < 400)
            ctx.check(f"folding actually kicked in under this load, folded_count={app.transcript.folded_count}",
                      app.transcript.folded_count > 0)
            ctx.check("fake controller recorded every submission", len(fake.submitted) == len(fake.turns))
    asyncio.run(body())


# ============================================================================
# SVG snapshots (D-TUI: "export_screenshot, normalised, UPDATE_SNAPSHOTS=1").
# Always (re)written (`docs/harness/tui-snapshots/*.svg` -- the paths the U2
# report attaches); regenerated freely rather than byte-diffed against a
# checked-in golden copy, since box-drawing/font metrics can legitimately
# differ between the Windows and WSL/Linux terminals this suite runs on
# (D-TUI: "snapshots generated on Linux (WSL)") -- the substantive check is
# that each screen's expected TEXT content actually rendered.
# ============================================================================

def _write_snapshot(app, name: str) -> "tuple[Path, str]":
    """Writes the raw SVG (the report's screenshot path) and returns
    `(path, normalised_text)` -- `export_screenshot` renders each word as
    its own `<text>` run joined by `&#160;` (non-breaking space) entities,
    so a caller doing substring assertions on rendered TEXT wants the
    normalised (real-space, tag-stripped) form, not the raw markup."""
    import re

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.svg"
    svg = app.export_screenshot(title=f"rolo-claude -- {name}", simplify=True)
    path.write_text(svg, encoding="utf-8")
    normalised = svg.replace("&#160;", " ").replace("\xa0", " ")
    normalised = re.sub(r"<[^>]+>", "", normalised)
    return path, normalised


@test
def test_svg_snapshots_main_permission_question_model_picker(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "Show me a quick demo")
            await pilot.press("enter")
            await _drain_a_few(app, pilot)
            path, svg = _write_snapshot(app, "main-screen")
            ctx.check(f"main screen snapshot written to {path}", path.exists() and len(svg) > 0)
            ctx.check("main screen SVG contains the streamed reply", "scripted demo turn" in svg)

        fake2 = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app2 = await _mounted(fake2)
        async with app2.run_test(size=(100, 40)) as pilot2:
            await pilot2.click("#prompt-input")
            await _type(pilot2, "do something")
            await pilot2.press("enter")
            await _drain_a_few(app2, pilot2, n=6)
            path2, svg2 = _write_snapshot(app2, "permission-card")
            ctx.check(f"permission card snapshot written to {path2}", path2.exists())
            ctx.check("permission card SVG mentions the prompt", "Permission needed" in svg2)

        fake3 = FakeController(turns=[[
            ev.user_message("pick", turn=1),
            ev.Event("question", {"id": "q1", "name": "AskUserQuestion",
                                   "input": {"question": "Which color?", "options": ["Red", "Blue"]}}, turn=1),
        ]])
        app3 = await _mounted(fake3)
        async with app3.run_test(size=(100, 40)) as pilot3:
            await pilot3.click("#prompt-input")
            await _type(pilot3, "pick")
            await pilot3.press("enter")
            await _drain_a_few(app3, pilot3, n=6)
            path3, svg3 = _write_snapshot(app3, "question-card")
            ctx.check(f"question card snapshot written to {path3}", path3.exists())
            ctx.check("question card SVG shows the question text", "Which color?" in svg3)

        fake4 = FakeController()
        app4 = await _mounted(fake4)
        async with app4.run_test(size=(100, 40)) as pilot4:
            await pilot4.click("#prompt-input")
            await _type(pilot4, "/model")
            await pilot4.press("enter")
            await pilot4.pause(0.15)
            path4, svg4 = _write_snapshot(app4, "model-picker")
            ctx.check(f"model picker snapshot written to {path4}", path4.exists())
            ctx.check("model picker SVG shows the filter box", "Filter models" in svg4)

        # U5: which-key overlay + a git-shadow RewindCard.
        fake5 = FakeController()
        app5 = await _mounted(fake5)
        async with app5.run_test(size=(100, 40)) as pilot5:
            await pilot5.press("ctrl+x")
            await pilot5.pause(0.1)
            path5, svg5 = _write_snapshot(app5, "which-key-overlay")
            ctx.check(f"which-key overlay snapshot written to {path5}", path5.exists())
            ctx.check("which-key SVG lists a real chord action", "session:export" in svg5)

        fake6 = FakeController()
        app6 = await _mounted(fake6)
        async with app6.run_test(size=(100, 40)) as pilot6:
            from rolo_claude.tui.widgets.cards import RewindCard
            card = RewindCard(step={"id": "abc123", "ts": 0, "label": "Write(file.txt)", "files": ["file.txt"]},
                              verb="undo", on_decide=lambda _confirmed: None)
            await app6.transcript.mount_widget(card)
            app6.set_pending_card(card)
            await pilot6.pause(0.1)
            path6, svg6 = _write_snapshot(app6, "rewind-card")
            ctx.check(f"rewind card snapshot written to {path6}", path6.exists())
            ctx.check("rewind card SVG names the step", "Write(file.txt)" in svg6)
    asyncio.run(body())


# ============================================================================
# Real end-to-end pilot: a REAL Session/Controller (tui/bootstrap.py) against
# the mock upstream -- NOT the FakeController. Type a prompt, see streamed
# text and a Read tool card.
# ============================================================================

def _tool_call_chunk(call_id: str, name: str, arguments: dict) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _final_text_chunk(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


@test
def test_real_e2e_pilot_against_mock_upstream_streams_text_and_read_tool_card(ctx: Ctx):
    import argparse

    async def body():
        fh = build_fake_home()
        target = fh["proj"] / "e2e_read_target.txt"
        target.write_text("line1\nline2\nline3\n", encoding="utf-8")
        SCENARIOS["tui-e2e-read"] = ScriptedTurns([
            _tool_call_chunk("call_r", "Read", {"file_path": str(target)}),
            _final_text_chunk("The file has 3 lines."),
        ])
        mock = MockUpstream().start()
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_OPENROUTER_BASE_URL", "OPENROUTER_API_KEY")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        controller = None
        try:
            from rolo_claude.tui.bootstrap import build_controller

            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=True,
                tools=None, add_dir=None, model="or:mock/tui-e2e-read", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "read the file and tell me how many lines it has")
                await pilot.press("enter")
                for _ in range(80):
                    await app._drain()
                    await pilot.pause(0.05)
                    if any(isinstance(w, AssistantText) and "3 lines" in w.raw_text
                           for w in app.transcript.children):
                        break
                texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                ctx.check(f"the real model's streamed final answer mentions 3 lines, got {texts}",
                          any("3 lines" in t for t in texts))
                cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check(f"a Read tool card is present, got headers={[c.header for c in cards]}",
                          any("Read" in c.header for c in cards))
                ctx.check(f"the Read tool card resolved ok, got statuses={[c.status for c in cards]}",
                          all(c.status == "ok" for c in cards if "Read" in c.header))
        finally:
            if controller is not None:
                controller.quit()
            mock.stop()
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    asyncio.run(body())


@test
def test_slash_model_switches_the_real_session_and_next_reply_uses_it(ctx: Ctx):
    """Regression: `Controller.set_model` sends `events.Command("set_model",
    ...)`, which `Session.run`'s command pump has always handled -- but
    `events.COMMAND_KINDS` never listed `"set_model"` as a valid kind, so
    `Command.__post_init__` raised `ValueError` the instant a REAL `/model
    <ref>` ran (a `FakeController` never touches `events.Command` at all,
    which is why `test_model_command_with_explicit_ref_sets_fake_model`
    above never caught this). Exercises the real Controller/Session (not the
    fake) against the mock upstream end to end: switch model mid-session,
    confirm the status bar reflects it, and confirm the NEXT turn's reply
    actually comes from the new model, not just the status bar cosmetically
    changing."""
    import argparse

    async def body():
        fh = build_fake_home()
        SCENARIOS["tui-model-a"] = ScriptedTurns([_final_text_chunk("first model reply")])
        SCENARIOS["tui-model-b"] = ScriptedTurns([_final_text_chunk("second model reply")])
        mock = MockUpstream().start()
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_OPENROUTER_BASE_URL", "OPENROUTER_API_KEY")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        controller = None
        try:
            from rolo_claude.tui.bootstrap import build_controller
            from rolo_claude.tui.slash import handle_slash

            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=True,
                tools=None, add_dir=None, model="or:mock/tui-model-a", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "hello")
                await pilot.press("enter")
                for _ in range(80):
                    await app._drain()
                    await pilot.pause(0.05)
                    if any(isinstance(w, AssistantText) and "first model reply" in w.raw_text
                           for w in app.transcript.children):
                        break
                ctx.check(f"status bar shows the starting model, got {app.status_bar.model!r}",
                          app.status_bar.model == "or:mock/tui-model-a")

                await handle_slash(app, "model", "or:mock/tui-model-b")  # the line that used to raise
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if app.status_bar.model == "or:mock/tui-model-b":
                        break
                ctx.check(f"status bar reflects the switched model, got {app.status_bar.model!r}",
                          app.status_bar.model == "or:mock/tui-model-b")

                await _type(pilot, "hello again")
                await pilot.press("enter")
                for _ in range(80):
                    await app._drain()
                    await pilot.pause(0.05)
                    if any(isinstance(w, AssistantText) and "second model reply" in w.raw_text
                           for w in app.transcript.children):
                        break
                texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                ctx.check(f"the NEXT turn's reply actually came from the switched model, got {texts}",
                          any("second model reply" in t for t in texts))
        finally:
            if controller is not None:
                controller.quit()
            mock.stop()
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    asyncio.run(body())


# ============================================================================
# U5: keymap parsing (chords + which-key), palette filtering, @file#L
# parsing, statusLine command output -- all pure, no textual App needed.
# ============================================================================

@test
def test_keymap_defaults_merge_with_user_keybindings_json_chords_and_unbind(ctx: Ctx):
    from rolo_claude.tui import keys as tui_keys

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "keybindings.json"
        path.write_text(json.dumps({
            "bindings": [
                {"context": "Global", "bindings": {
                    "ctrl+o": None,  # unbind our default toggleVerbose
                    "ctrl+x ctrl+s": "session:export",  # same as default, harmless re-declare
                    "ctrl+k": "app:commandPalette",  # a brand-new remap
                }},
            ],
        }), encoding="utf-8")
        keymap = tui_keys.load_keymap(path)
        ctx.check("ctrl+o was unbound by the user file", "ctrl+o" not in keymap["Global"])
        ctx.check(f"ctrl+k now maps to the palette, got {keymap['Global'].get('ctrl+k')}",
                  keymap["Global"].get("ctrl+k") == "app:commandPalette")
        ctx.check("an untouched default chord survives the merge",
                  keymap["Global"].get("ctrl+x ctrl+r") == "session:rename")
        ctx.check("chord key normalization: ctrl+x ctrl+s still present",
                  keymap["Global"].get("ctrl+x ctrl+s") == "session:export")

        # A missing/invalid file is a no-op -- defaults come through unchanged.
        empty = tui_keys.load_keymap(Path(tmp) / "does-not-exist.json")
        ctx.check("missing keybindings.json falls back to pure defaults",
                  empty["Global"].get("ctrl+o") == "app:toggleVerbose")


@test
def test_keymap_normalize_keystroke_folds_aliases_and_modifier_order(ctx: Ctx):
    from rolo_claude.tui import keys as tui_keys

    ctx.check("control/opt/cmd aliases fold to ctrl/alt/meta",
              tui_keys.normalize_keystroke("Control+Opt+S") == "ctrl+alt+s")
    ctx.check("modifier order is canonicalized",
              tui_keys.normalize_keystroke("shift+ctrl+p") == tui_keys.normalize_keystroke("ctrl+shift+p"))
    ctx.check("esc/return aliases fold", tui_keys.normalize_keystroke("Esc") == "escape")
    ctx.check("a chord normalizes each keystroke independently",
              tui_keys.normalize_chord("Control+X Control+S") == "ctrl+x ctrl+s")


@test
def test_which_key_continuations_and_overlay_widget(ctx: Ctx):
    from rolo_claude.tui import keys as tui_keys
    from rolo_claude.tui.widgets.whichkey import WhichKeyOverlay

    keymap = tui_keys.default_bindings_flat()
    continuations = tui_keys.chord_continuations(keymap["Global"], "ctrl+x")
    ctx.check(f"ctrl+x has multiple live continuations, got {sorted(continuations)}", len(continuations) >= 5)
    ctx.check("ctrl+x is recognised as a live chord prefix",
              tui_keys.is_chord_prefix(keymap["Global"], "ctrl+x") is True)
    ctx.check("a key with no chords under it is NOT a prefix",
              tui_keys.is_chord_prefix(keymap["Global"], "f1") is False)
    formatted = tui_keys.format_which_key(continuations)
    ctx.check("formatted which-key text lists a real action", "session:export" in formatted)

    overlay = WhichKeyOverlay()
    ctx.check("hidden by default", overlay.display is False)
    overlay.show_for("ctrl+x", continuations)
    ctx.check("shown after show_for", overlay.display is True)
    overlay.hide()
    ctx.check("hidden again after hide()", overlay.display is False)


@test
def test_palette_filter_items_prefix_then_substring(ctx: Ctx):
    from rolo_claude.tui.dialogs.palette import filter_items

    items = [
        {"kind": "command", "label": "/model", "detail": "Show or change the model", "value": "model"},
        {"kind": "command", "label": "/mcp", "detail": "List MCP servers", "value": "mcp"},
        {"kind": "session", "label": "fix the bug", "detail": "abc123", "value": "abc123"},
        {"kind": "file", "label": "README.md", "detail": "", "value": "README.md"},
    ]
    ctx.check("empty query returns everything, in order", filter_items(items, "") == items)
    prefix_hits = filter_items(items, "/m")
    ctx.check(f"a prefix match ranks first for both /model and /mcp, got {[i['label'] for i in prefix_hits]}",
              {i["label"] for i in prefix_hits} == {"/model", "/mcp"})
    substr_hits = filter_items(items, "bug")
    ctx.check(f"a substring-only match (in the session label) is still found, got {substr_hits}",
              len(substr_hits) == 1 and substr_hits[0]["value"] == "abc123")
    ctx.check("no match at all -> empty list", filter_items(items, "zzz-nope") == [])


@test
def test_at_mention_line_range_parsing(ctx: Ctx):
    from rolo_claude.tui.completion import parse_at_mentions

    mentions = parse_at_mentions("look at @src/main.py#L10-20 and also @README.md and @a/b#L5 please")
    ctx.check(f"three mentions found, got {mentions}", len(mentions) == 3)
    ctx.check("a #L10-20 range parses (start, end)", mentions[0] == ("src/main.py", 10, 20))
    ctx.check("a bare @path has no line range", mentions[1] == ("README.md", None, None))
    ctx.check("a #L5 (no dash) sets start==end", mentions[2] == ("a/b", 5, 5))
    ctx.check("no @ at all -> no mentions", parse_at_mentions("plain text, no mentions here") == [])


@test
def test_statusline_command_receives_claude_code_json_contract(ctx: Ctx):
    from rolo_claude import statusline as sl

    payload = sl.build_payload(session_id="sid1", cwd="/proj", model_id="or:x/y", cost_usd=1.5)
    ctx.check("payload carries Claude Code's own field names",
              payload["hook_event_name"] == "Status" and payload["model"]["id"] == "or:x/y"
              and payload["workspace"]["current_dir"] == "/proj"
              and payload["cost"]["total_cost_usd"] == 1.5)

    # A trivial script that echoes back the model id it was FED on stdin --
    # proves the JSON contract round-trips, not just that SOME text comes back.
    script = 'import json,sys; d=json.load(sys.stdin); print("model=" + d["model"]["id"])'
    out = sl.run_statusline_command(f'"{sys.executable}" -c \'{script}\'', payload, cwd=".")
    if os.name == "nt":
        # Windows cmd doesn't like single-quoted -c bodies the same way;
        # retry with a temp file to keep this test host-independent.
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(script)
            script_path = f.name
        out = sl.run_statusline_command(f'"{sys.executable}" "{script_path}"', payload, cwd=".")
        os.unlink(script_path)
    ctx.check(f"the command's stdout, fed the real JSON payload, echoes it back, got {out!r}",
              out == "model=or:x/y")
    ctx.check("a nonexistent command returns None, never raises",
              sl.run_statusline_command("this-command-does-not-exist-xyz", payload, cwd=".") is None)


# ============================================================================
# U5 pilots: chord + which-key dispatch, `!cmd`, `@file#L` ingestion,
# git-shadow rewind, rename/fork/export/stats -- a REAL `Controller` wired
# to a minimal (non-network) fake `Session` stand-in, since none of these
# need a model call: just `session.permission_engine` + `session.log`
# (a real `SessionLog`), exactly what `Controller`'s own U5 methods touch.
# ============================================================================

def _real_controller(cwd: Path, *, mode: str = "bypassPermissions"):
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.controller import Controller
    from rolo_claude.permissions import PermissionEngine

    class _MinimalSession:
        def __init__(self) -> None:
            self.permission_engine = PermissionEngine(mode=mode, print_mode=False, cwd=cwd)
            self.log = SessionLog(cwd)
            self.busy = False  # Controller.submit reads this; no real turn ever runs in these pilots
            self.interactive = False

        def run(self, commands, emit, mcp_status_fn=None) -> int:
            return 0  # Controller.start()'s worker thread target; nothing is ever queued to it here

        def _fire_session_end(self, _reason) -> None:
            pass

    return Controller(session=_MinimalSession(), cwd=cwd)


@test
def test_chord_ctrl_x_which_key_then_dispatches_and_suppresses_the_plain_binding(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.add_note("marker")
            await pilot.press("ctrl+x")
            await pilot.pause(0.05)
            ctx.check(f"which-key overlay shown, pending={app._pending_chord!r}", app.which_key.display is True)
            await pilot.press("ctrl+l")  # ctrl+x ctrl+l -> session:resume (a FakeController stub)
            await pilot.pause(0.1)
            ctx.check("which-key overlay closes after the second keystroke", app.which_key.display is False)
            ctx.check(f"pending chord cleared, got {app._pending_chord!r}", app._pending_chord is None)
            kinds = [type(w).__name__ for w in app.transcript.children]
            ctx.check(f"the marker note is UNTOUCHED -- bare ctrl+l's own clear_view did NOT also fire, "
                      f"got {kinds}", kinds == ["SystemNote"])
    asyncio.run(body())


@test
def test_chord_timeout_cancels_pending_chord(ctx: Ctx):
    from rolo_claude.tui import keys as tui_keys

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause(0.05)
            ctx.check("chord pending", app._pending_chord == "ctrl+x")
            await pilot.pause(tui_keys.CHORD_TIMEOUT_S + 0.3)
            ctx.check("chord auto-cancels after the timeout", app._pending_chord is None)
            ctx.check("which-key overlay hides on timeout", app.which_key.display is False)
    asyncio.run(body())


@test
def test_bang_command_runs_the_real_bash_tool_and_shows_a_tool_card(ctx: Ctx):
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            controller = _real_controller(cwd)
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "!echo hello-from-bang")
                await pilot.press("enter")
                for _ in range(30):
                    await app._drain()
                    await pilot.pause(0.05)
                    cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                    if cards and cards[0].status != "running":
                        break
                cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check(f"a ToolCard was mounted for the inline command, got {len(cards)}", len(cards) == 1)
                ctx.check(f"it ran through the REAL Bash tool, got body={cards[0].body_text!r}",
                          "hello-from-bang" in cards[0].body_text)
                ctx.check(f"it resolved ok, got status={cards[0].status!r}", cards[0].status == "ok")
                nodes = controller.session.log.nodes()
                tool_uses = [n for n in nodes if n.get("type") == "assistant"
                            for b in n.get("content", []) if b.get("type") == "tool_use"]
                ctx.check("the inline command was logged as a synthetic tool_use (context for the NEXT turn)",
                          any(True for _ in tool_uses) or any(
                              n.get("type") == "assistant" and any(
                                  b.get("type") == "tool_use" and b.get("name") == "Bash"
                                  for b in n.get("content", []))
                              for n in nodes))
    asyncio.run(body())


@test
def test_at_mention_with_line_range_is_ingested_as_a_log_snapshot(ctx: Ctx):
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            target = cwd / "notes.txt"
            target.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
            controller = _real_controller(cwd)
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "@notes.txt#L2-3 summarize this")
                await pilot.press("enter")
                await pilot.pause(0.3)
                nodes = controller.session.log.nodes()
                snaps = [n for n in nodes if n.get("type") == "snapshot" and n.get("kind") == "at_mention"]
                ctx.check(f"one at_mention snapshot logged, got {len(snaps)}", len(snaps) == 1)
                text = snaps[0]["content"][0]["text"]
                ctx.check(f"it carries the requested range's own lines (2-3), got {text!r}",
                          "beta" in text and "gamma" in text and "alpha" not in text and "delta" not in text)
    asyncio.run(body())


@test
def test_write_then_rewind_undo_restores_the_file_via_real_shadow_hook(ctx: Ctx):
    """The REAL dispatch.py hook (`_maybe_record_shadow_step`), not a
    hand-built ShadowStore call -- a Write's `tool_result` triggers a
    snapshot automatically, `/undo` shows a RewindCard, confirming it
    restores the file on disk."""
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            target = cwd / "doc.txt"
            target.write_text("version 1\n", encoding="utf-8")
            controller = _real_controller(cwd)
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                from rolo_claude.tui.dispatch import apply_event

                await apply_event(app, ev.Event("tool_use_ready", {
                    "id": "w1", "name": "Write", "input": {"file_path": str(target), "content": "version 1\n"},
                    "repaired": False}, turn=1))
                await apply_event(app, ev.Event("tool_result", {"id": "w1", "ok": True, "summary": "wrote"}, turn=1))

                target.write_text("version 2\n", encoding="utf-8")
                await apply_event(app, ev.Event("tool_use_ready", {
                    "id": "w2", "name": "Write", "input": {"file_path": str(target), "content": "version 2\n"},
                    "repaired": False}, turn=1))
                await apply_event(app, ev.Event("tool_result", {"id": "w2", "ok": True, "summary": "wrote"}, turn=1))

                ctx.check(f"2 shadow steps recorded automatically, got {len(controller.shadow_steps())}",
                          len(controller.shadow_steps()) == 2)

                target.write_text("uncommitted local edit\n", encoding="utf-8")
                await pilot.click("#prompt-input")
                await _type(pilot, "/undo")
                await pilot.press("enter")
                await pilot.pause(0.15)
                ctx.check(f"a RewindCard is pending, got {type(app.pending_card).__name__}",
                          type(app.pending_card).__name__ == "RewindCard")
                await pilot.press("1")  # confirm restore
                await pilot.pause(0.2)
                ctx.check(f"the file was restored to the PREVIOUS step's content, got {target.read_text()!r}",
                          target.read_text(encoding="utf-8") == "version 1\n")
                ctx.check("pending card cleared", app.pending_card is None)
                nodes = controller.session.log.nodes()
                ctx.check("a 'rewind' log node was written",
                          any(n.get("type") == "rewind" and n.get("verb") == "undo" for n in nodes))
    asyncio.run(body())


@test
def test_rename_fork_export_stats_via_fake_controller(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/rename my great session")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"FakeController recorded the rename, got {fake.renames}", fake.renames == ["my great session"])

            await _type(pilot, "/fork")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"FakeController recorded a fork, got forks={fake.forks}", fake.forks == 1)

            await _type(pilot, "/export --sanitize out.md")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"FakeController recorded the export with sanitize+path, got {fake.exports}",
                      fake.exports == [{"sanitize": True, "path": "out.md"}])

            await _type(pilot, "/stats")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"/stats rendered a note mentioning Turns, got plain_log tail={app.transcript.plain_log[-3:]}",
                      any("Turns" in line for line in app.transcript.plain_log))
    asyncio.run(body())


@test
def test_rename_and_stats_via_one_real_controller_pilot(ctx: Ctx):
    """"one real-Controller pilot" (brief): a REAL Controller/SessionLog,
    no FakeController -- /rename actually writes a `meta` node, /stats
    actually reads real `usage` nodes back out."""
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            controller = _real_controller(cwd)
            controller.session.log.append_usage({"input_tokens": 100, "output_tokens": 20}, 0.01)
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/rename Fix the login bug")
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"the REAL session log now carries the title, got {controller.get_title()!r}",
                          controller.get_title() == "Fix the login bug")

                await _type(pilot, "/stats")
                await pilot.press("enter")
                await pilot.pause(0.1)
                stats = controller.session_stats()
                ctx.check(f"real usage node counted, got {stats}", stats["total_cost_usd"] == 0.01)
    asyncio.run(body())


# ============================================================================
# Rendering: thinking ABOVE the answer regardless of arrival order;
# auto-grow counts WRAPPED rows, not logical lines.
# ============================================================================

@test
def test_thinking_block_renders_above_answer_even_when_text_arrives_first(ctx: Ctx):
    """review/U5 must-do: "OpenAI-dialect thinking mounts below the
    answer" -- simulates exactly that arrival order (text_delta before
    thinking_delta for the SAME message) and asserts the MOUNTED widget
    order still puts ThinkingBlock first."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            app.transcript.begin_message(1)
            await app.transcript.append_text(1, 0, "The answer is 42.")
            await app.transcript.append_thinking(1, 0, "Let me think about this...")
            kinds = [type(w).__name__ for w in app.transcript.children]
            ctx.check(f"ThinkingBlock is mounted BEFORE AssistantText despite arriving second, got {kinds}",
                      kinds == ["ThinkingBlock", "AssistantText"])
    asyncio.run(body())


@test
def test_auto_grow_counts_wrapped_rows_not_logical_lines(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(40, 40)) as pilot:  # narrow terminal so a long line visibly wraps
            await pilot.click("#prompt-input")
            long_line = "word " * 40  # ~200 chars, ONE logical line, several wrapped rows at width 40
            await _type(pilot, long_line)
            await pilot.pause(0.1)
            ctx.check(f"exactly one logical line (no newlines typed), got {app.prompt_input.document.line_count}",
                      app.prompt_input.document.line_count == 1)
            height = app.prompt_input.styles.height.value if app.prompt_input.styles.height else 0
            ctx.check(f"auto-grow reflects WRAPPED rows (>1), not the single logical line, got height={height}",
                      height > 1)
    asyncio.run(body())


@test
def test_bang_command_ask_mode_shows_a_permission_card_first(ctx: Ctx):
    """`default` permission mode asks for a bare Bash call with no
    matching rule -- `!cmd` must show the SAME PermissionCard (not run
    immediately) and only execute once confirmed."""
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            controller = _real_controller(cwd, mode="default")
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                # NOT `echo` -- that's in the permission engine's own
                # read-only whitelist and would be allowed outright even
                # in `default` mode, defeating the point of this test.
                await _type(pilot, "!touch should-ask-first.txt")
                await pilot.press("enter")
                await pilot.pause(0.2)
                ctx.check(f"a PermissionCard is pending BEFORE anything runs, got {type(app.pending_card).__name__}",
                          isinstance(app.pending_card, PermissionCard))
                cards_before = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check("no ToolCard exists yet (the command hasn't run)", cards_before == [])
                await pilot.press("1")  # allow once
                for _ in range(30):
                    await app._drain()
                    await pilot.pause(0.05)
                    if any(isinstance(w, ToolCard) and w.status != "running" for w in app.transcript.children):
                        break
                cards_after = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check(f"confirming runs it, got {len(cards_after)} card(s)", len(cards_after) == 1)
                ctx.check(f"it actually ran, got status={cards_after[0].status!r}", cards_after[0].status == "ok")
                ctx.check("the file it touched really exists on disk",
                          (cwd / "should-ask-first.txt").exists())
    asyncio.run(body())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped, label="TUI tests"))
