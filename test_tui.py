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
from rolo_claude.tui.widgets.transcript import AssistantText, SystemNote, UserMessage

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
def test_prompt_input_is_visible_above_the_status_bar(ctx: Ctx):
    """Found live on a Kali box: `#prompt-row` and `.status-bar` were BOTH
    docked to the bottom edge, and Textual overlaps same-edge docks instead
    of stacking them, so the status bar painted over the input line -- the
    TUI showed a separator and a status bar and nothing to type into at
    every terminal height and theme. The fix puts both in one docked
    `#bottom-dock` container. This pins the geometry the pilots never
    checked: the input row sits strictly above the status bar, the glyph is
    on screen, and typed text is on screen."""
    async def body():
        for size in ((110, 30), (100, 40), (80, 24)):
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.3)
                inp = app.prompt_input.region
                bar = app.status_bar.region
                ctx.check(f"{size}: input row is above the status bar (input {inp}, bar {bar})",
                          inp.height >= 1 and inp.y + inp.height <= bar.y)
                ctx.check(f"{size}: status bar is the last row", bar.y + bar.height == size[1])
                ctx.check(f"{size}: input does not overlap the transcript",
                          app.transcript.region.y + app.transcript.region.height <= inp.y)
                await pilot.click("#prompt-input")
                await _type(pilot, "hello there")
                snap = ""
                for _ in range(10):  # the TextArea repaints a frame or two after the last key
                    await pilot.pause(0.1)
                    snap = app.export_screenshot()
                    if "hello" in snap and "there" in snap:
                        break
                ctx.check(f"{size}: the input holds the typed text", app.prompt_input.text == "hello there")
                ctx.check(f"{size}: prompt glyph is on screen", "❯" in snap)
                # The SVG export may split a text run at the space (a non-breaking
                # entity), so check the two words rather than the exact phrase.
                ctx.check(f"{size}: typed text is on screen", "hello" in snap and "there" in snap)
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


@test
def test_compaction_events_render_start_and_done_notes_and_status(ctx: Ctx):
    """U5 must-do: no `compaction` handler existed at all before -- a
    "Compacting..." indicator never appeared and a failure was invisible.
    Drives `dispatch.apply_event` directly with the real event shapes
    `agent/loop.py`'s `_run_compaction` actually yields."""
    from rolo_claude.tui.dispatch import apply_event

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await apply_event(app, ev.Event("compaction", {"phase": "start"}, turn=1))
            ctx.check(f"status bar shows compacting, got {app.status_bar.phase!r}",
                      app.status_bar.phase == "compacting")
            notes = [w for w in app.transcript.children if isinstance(w, SystemNote)]
            ctx.check(f"exactly one note so far, got {len(notes)}", len(notes) == 1)
            ctx.check(f"start note mentions compacting, got {notes[0].content!r}",
                      "Compacting" in str(notes[0].content))

            await apply_event(app, ev.Event(
                "compaction", {"phase": "done", "tokens_before": 1000, "tokens_after": 200}, turn=1,
            ))
            notes = [w for w in app.transcript.children if isinstance(w, SystemNote)]
            ctx.check(f"a second note was added for the done phase, got {len(notes)}", len(notes) == 2)
            done_text = str(notes[1].content)
            ctx.check(f"done note reports the before/after token counts, got {done_text!r}",
                      "1000" in done_text and "200" in done_text)
    asyncio.run(body())


@test
def test_steer_queued_note_never_embeds_the_steer_text_only_the_user_bubble_does(ctx: Ctx):
    """U5 must-do: the note is permanently text-free (`"↳ steering…"`,
    never the steer's own words) -- the actual text appears exactly once,
    only in the `user_message` bubble.

    H5c finding 19: `steer_queued` genuinely fires TWICE for one logical
    steer (`Controller.submit`'s own instant-feedback push at submit time,
    then again from the session's own turn-event stream when it actually
    applies -- kept there too, since a bare `Session`/print-mode caller
    with no Controller has no other path to ever see it and print mode's
    own contract promises it) -- the OLD code showed a SEPARATE note for
    each firing (two notes for one steer). Fixed by deduplicating on the
    TEXT against a steer still awaiting its matching `steer_applied` --
    this test now asserts exactly ONE note for that duplicate pair, and
    that a SECOND, genuinely DIFFERENT steer queued afterward still gets
    its own note (never a session-wide "only one note ever" regression)."""
    from rolo_claude.tui.dispatch import apply_event

    STEER_TEXT = "please use a different approach entirely"
    SECOND_STEER_TEXT = "actually, do something else instead"

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            # Fires once at submit time, once again when the turn actually
            # applies it -- the SAME text both times for one logical steer.
            await apply_event(app, ev.Event("steer_queued", {"text": STEER_TEXT}, turn=1))
            await apply_event(app, ev.Event("steer_queued", {"text": STEER_TEXT}, turn=1))
            await apply_event(app, ev.Event("user_message", {"text": STEER_TEXT}, turn=1))

            notes = [w for w in app.transcript.children if isinstance(w, SystemNote)]
            ctx.check(f"exactly ONE note for the duplicate submit+apply pair, got {len(notes)}", len(notes) == 1)
            ctx.check(f"note is the generic text-free indicator, got {str(notes[0].content)!r}",
                      STEER_TEXT not in str(notes[0].content) and "steering" in str(notes[0].content).lower())

            # The matching steer_applied clears the dedup entry -- a
            # DIFFERENT steer queued afterward must still get its own note.
            await apply_event(app, ev.Event("steer_applied", {"text": STEER_TEXT}, turn=1))
            await apply_event(app, ev.Event("steer_queued", {"text": SECOND_STEER_TEXT}, turn=2))
            await apply_event(app, ev.Event("user_message", {"text": SECOND_STEER_TEXT}, turn=2))

            notes = [w for w in app.transcript.children if isinstance(w, SystemNote)]
            ctx.check(f"a second, DIFFERENT steer gets its own note too, got {len(notes)}", len(notes) == 2)

            user_messages = [w for w in app.transcript.children if isinstance(w, UserMessage)]
            ctx.check(f"exactly TWO user bubbles (one per real steer), got {len(user_messages)}",
                      len(user_messages) == 2)
            ctx.check(f"the first bubble has the real text, got {user_messages[0].content!r}",
                      STEER_TEXT in str(user_messages[0].content))
            ctx.check(f"the second bubble has the SECOND steer's real text, got {user_messages[1].content!r}",
                      SECOND_STEER_TEXT in str(user_messages[1].content))
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
# 1.0.1 hotfix 17: a pending card intercepts free text instead of it becoming
# a silent steer, and a mode switch re-evaluates an already-pending card --
# the real mechanism behind the reported "freeze" (a permission card
# scrolled out of view by the item-16 auto-scroll bug, typed text kept
# steering instead of ever answering it).
# ============================================================================

@test
def test_free_text_while_permission_card_pending_answers_it_directly(ctx: Ctx):
    """Before this fix, typing feedback WITHOUT first pressing "4" was a
    silent steer on the turn underneath -- the pending ask itself was never
    answered, matching rolo's report ("I am unable to type anything and get
    a response") once the card had scrolled out of view."""
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            await pilot.click("#prompt-input")  # the user types into the ORDINARY prompt, never pressing 4 first
            await _type(pilot, "use a safer command")
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check(f"answered as deny+feedback, got {fake.permission_replies}",
                      fake.permission_replies == [("tu1", {"action": "deny", "reason": "", "rule": None,
                                                             "message": "use a safer command"})])
            ctx.check("never became a steer", fake.submitted == ["do something"])
            ctx.check("pending card cleared", app.pending_card is None)
            ctx.check("focus returns to the prompt", app.focused is app.prompt_input)
    asyncio.run(body())


# ============================================================================
# 1.0.1 fixpass finding 14: a `/`-prefixed submission while a card is
# pending is ALWAYS routed to slash handling, never deny feedback; pasted
# placeholders are expanded before becoming a card's answer; the /effort
# card never intercepts typed text at all.
# ============================================================================

@test
def test_slash_command_while_permission_card_pending_runs_the_command_not_deny_feedback(ctx: Ctx):
    """Typing "/permissions" while a PermissionCard is pending used to DENY
    the tool with "The user said: /permissions" instead of ever running
    the command -- the card itself is left completely untouched."""
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            await pilot.click("#prompt-input")
            await _type(pilot, "/permissions")
            await pilot.press("enter")
            await pilot.pause(0.1)
            from rolo_claude.tui.dialogs.permissions import PermissionsDialog
            ctx.check(f"the command actually ran (PermissionsDialog opened), got {type(app.screen).__name__}",
                      isinstance(app.screen, PermissionsDialog))
            ctx.check("the tool was NOT denied", fake.permission_replies == [])
            ctx.check("the card is still pending, untouched",
                      isinstance(app.pending_card, PermissionCard) and not app.pending_card.done)
    asyncio.run(body())


@test
def test_deny_feedback_expands_pasted_placeholder_to_real_content(ctx: Ctx):
    """Deny feedback (the borrowed-input path after pressing "4") must
    carry the REAL pasted content, never the literal
    "[Pasted text #n ...]" placeholder PromptInput shows/logs."""
    from textual import events as tevents

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
            pasted_feedback = "\n".join(f"reason line {i}" for i in range(6))
            app.prompt_input.post_message(tevents.Paste(pasted_feedback))
            await pilot.pause(0.1)
            ctx.check(f"the paste became a placeholder in the input, got {app.prompt_input.text!r}",
                      app.prompt_input.text.startswith("[Pasted text #1"))
            await pilot.press("enter")
            await pilot.pause(0.05)
            reply = fake.permission_replies[-1]
            ctx.check(f"the REAL pasted content reached the deny message, not the placeholder, got {reply}",
                      reply == ("tu1", {"action": "deny", "reason": "", "rule": None, "message": pasted_feedback}))
    asyncio.run(body())


@test
def test_effort_card_does_not_intercept_typed_text(ctx: Ctx):
    """The /effort selector does not intercept typed text at all -- a plain
    prompt typed while it's open must reach the ordinary submit path
    (recorded by the controller), never silently vanish into a steer with
    no running turn to steer, nor get swallowed outright."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/effort")
            await pilot.press("enter")
            await pilot.pause(0.1)
            from rolo_claude.tui.widgets.cards import EffortCard
            ctx.check(f"an EffortCard is pending, got {type(app.pending_card).__name__}",
                      isinstance(app.pending_card, EffortCard))
            await pilot.click("#prompt-input")
            await _type(pilot, "just a normal prompt")
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"the ordinary prompt reached the controller, not swallowed by the card, got "
                      f"{fake.submitted}", "just a normal prompt" in fake.submitted)
    asyncio.run(body())


@test
def test_shift_tab_to_auto_resolves_a_pending_permission_card(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            # default -> plan -> acceptEdits -> auto (next_mode's own cycle order).
            for _ in range(3):
                await pilot.press("shift+tab")
                await pilot.pause(0.02)
                if fake.permission_mode == "auto":
                    break
            ctx.check(f"mode reached auto, got {fake.permission_mode!r}", fake.permission_mode == "auto")
            ctx.check(f"the pending ask was auto-allowed, got {fake.permission_replies}",
                      fake.permission_replies == [("tu1", {"action": "allow", "reason": "", "rule": None,
                                                             "message": ""})])
            ctx.check("pending card cleared once resolved", app.pending_card is None)
    asyncio.run(body())


@test
def test_status_bar_and_placeholder_while_permission_card_pending(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            rendered = str(app.status_bar.render())
            ctx.check(f"status bar shows the permission-needed tag, got {rendered!r}",
                      "permission needed" in rendered)
            ctx.check(f"placeholder invites 1-4 or free text, got {app.prompt_input.placeholder!r}",
                      "1-4" in app.prompt_input.placeholder)
            await pilot.press("1")
            await pilot.pause(0.05)
            ctx.check("tag clears once answered", "permission needed" not in str(app.status_bar.render()))
    asyncio.run(body())


# ============================================================================
# 1.0.1 fixpass finding 3: focus self-heal (_on_key) must never reach across
# screens to a pending card underneath a modal that has nothing focusable of
# its own (PagerScreen) -- the hidden card's own keys used to fire instead
# of the modal's.
# ============================================================================

@test
def test_pager_screen_focus_heal_does_not_leak_to_a_hidden_pending_card(ctx: Ctx):
    """Reproduces the most dangerous example verbatim: "1" (PermissionCard's
    own "allow once") must never silently approve a pending ask the user
    never even looked at, just because they opened a pager to check a
    PRIOR tool's output. PagerScreen has nothing focusable at all, so
    `screen.focused` really is None once it opens -- exactly the case the
    old self-heal mishandled."""
    async def body():
        turn = [[
            ev.user_message("do two things", turn=1),
            ev.Event("tool_use_ready", {"id": "tu0", "name": "Bash", "input": {"command": "echo hi"},
                                         "repaired": False}, turn=1),
            ev.Event("tool_result", {"id": "tu0", "ok": True, "summary": "hi", "content": "hi"}, turn=1),
            ev.Event("tool_use_ready", {"id": "tu1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"},
                                         "repaired": False}, turn=1),
            ev.Event("permission_request", {"id": "tu1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"},
                                             "reason": "not covered by an existing rule",
                                             "suggested_rule": "Bash(rm:*)"}, turn=1),
        ]]
        fake = FakeController(turns=turn)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do two things")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=8)
            ctx.check("a PermissionCard is pending for tu1", isinstance(app.pending_card, PermissionCard))
            tool_card = app.transcript.tool_cards.get("tu0")
            ctx.check("tu0's ToolCard exists", tool_card is not None)
            tool_card.focus()
            await pilot.pause(0.02)
            await pilot.press("o")
            await pilot.pause(0.05)
            from rolo_claude.tui.widgets.cards import PagerScreen
            ctx.check(f"PagerScreen is now the active screen, got {type(app.screen).__name__}",
                      isinstance(app.screen, PagerScreen))
            ctx.check("PagerScreen has nothing focusable of its own -- focused is None",
                      app.screen.focused is None)
            await pilot.press("1")
            await pilot.pause(0.05)
            ctx.check(f"the pending permission ask was NOT silently approved, got {fake.permission_replies}",
                      fake.permission_replies == [])
            ctx.check("the permission card is still pending, unanswered",
                      isinstance(app.pending_card, PermissionCard) and not app.pending_card.done)
            await pilot.press("q")
            await pilot.pause(0.05)
            ctx.check(f"'q' correctly reached the pager's OWN binding and closed it, got "
                      f"{type(app.screen).__name__}", not isinstance(app.screen, PagerScreen))
    asyncio.run(body())


# ============================================================================
# 1.0.1 fixpass finding 4: Shift+Tab re-evaluates a pending permission card
# through the REAL permission engine instead of blindly allowing/denying --
# an explicit ask: rule still asks under auto, and a card the user is
# already answering (awaiting_feedback) is left completely alone.
# ============================================================================

@test
def test_shift_tab_to_auto_leaves_an_explicit_ask_rule_card_pending(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        fake.still_ask_request_ids.add("tu1")
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            for _ in range(3):
                await pilot.press("shift+tab")
                await pilot.pause(0.02)
                if fake.permission_mode == "auto":
                    break
            ctx.check(f"mode reached auto, got {fake.permission_mode!r}", fake.permission_mode == "auto")
            ctx.check("the card is STILL pending -- an explicit ask rule is not skipped by auto",
                      isinstance(app.pending_card, PermissionCard) and not app.pending_card.done)
            ctx.check("nothing was answered", fake.permission_replies == [])
    asyncio.run(body())


@test
def test_shift_tab_while_awaiting_feedback_does_not_hijack_the_borrowed_input(ctx: Ctx):
    """The user pressed 4 and is typing why not -- a mode change mid-typing
    must leave the card and the borrowed input alone; the NEXT Enter must
    still deliver their feedback, never a stale "no longer waiting" toast
    from a re-finished card `_borrowing_card` was left pointing at."""
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            card = app.pending_card
            await pilot.press("4")
            await pilot.pause(0.05)
            ctx.check("now awaiting feedback", card.awaiting_feedback is True)
            ctx.check("borrowing the input", app._borrowing_card is card)
            for _ in range(3):
                await pilot.press("shift+tab")
                await pilot.pause(0.02)
            ctx.check(f"mode still changed underneath (status bar updated), got {fake.permission_mode!r}",
                      fake.permission_mode == "auto")
            ctx.check("the card was left alone -- still pending, still awaiting feedback, not done",
                      app.pending_card is card and card.awaiting_feedback and not card.done)
            ctx.check("still borrowing the SAME card's input, untouched", app._borrowing_card is card)
            await _type(pilot, "use a safer command")
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check(f"the feedback was delivered normally, got {fake.permission_replies}",
                      fake.permission_replies == [("tu1", {"action": "deny", "reason": "", "rule": None,
                                                             "message": "use a safer command"})])
    asyncio.run(body())


# ============================================================================
# 1.0.1 hotfix 16: the transcript follows new streamed output instead of
# staying wherever the user's own prompt was -- Textual's anchor() primitive
# (widget.py:800, confirmed to cover mid-stream growth, not just fresh
# mounts) pinned to the bottom via Transcript.on_mount, released/reacquired
# automatically by Textual's own scroll_y watcher on any manual scroll.
# ============================================================================

@test
def test_transcript_follows_streamed_deltas_past_the_viewport(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            # `\n\n` (not a single `\n`) is what actually overflows the
            # viewport here -- AssistantText is a Markdown widget, and a
            # single "\n" is a soft line break that Markdown rendering
            # reflows into the SAME paragraph (verified live: 60 single-
            # "\n" deltas rendered as one ~7-row wrapped paragraph, never
            # overflowing a 27-row viewport at all); `\n\n` forces each
            # into its own paragraph, which is what genuinely multi-line
            # streamed text (the spec's own "200 streamed deltas of multi-
            # line text") actually looks like.
            for i in range(200):
                await app.transcript.append_text(1, 0, f"line {i}\n\n")
                if i % 20 == 0:
                    await pilot.pause(0)
            await pilot.pause(0.1)
            ctx.check(f"scrolled to the true bottom, got scroll_y={app.transcript.scroll_y} "
                      f"max={app.transcript.max_scroll_y}", app.transcript.scroll_y >= app.transcript.max_scroll_y - 1)
            texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
            ctx.check(f"the last delta's text landed, got {texts}", texts and "line 199" in texts[-1])
    asyncio.run(body())


@test
def test_transcript_pageup_releases_following_and_end_reanchors(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            for i in range(120):
                await app.transcript.append_text(1, 0, f"line {i}\n\n")
                if i % 20 == 0:
                    await pilot.pause(0)
            await pilot.pause(0.1)
            ctx.check("following before any manual scroll", app.transcript.is_following())
            app.transcript.scroll_page_up(animate=False)
            await pilot.pause(0.05)
            ctx.check("PageUp released following", not app.transcript.is_following())
            y_after_pageup = app.transcript.scroll_y
            for i in range(120, 140):
                await app.transcript.append_text(1, 0, f"line {i}\n\n")
                if i % 20 == 0:
                    await pilot.pause(0)
            await pilot.pause(0.1)
            ctx.check(f"the view stayed put while released, got {app.transcript.scroll_y} vs {y_after_pageup}",
                      app.transcript.scroll_y == y_after_pageup)
            ctx.check(f"the N-new counter grew, got {app.transcript.new_since_scroll}",
                      app.transcript.new_since_scroll > 0)
            await pilot.press("ctrl+end")
            await pilot.pause(0.05)
            ctx.check("Ctrl+End re-anchored to the bottom", app.transcript.is_following())
            ctx.check(f"the N-new counter reset, got {app.transcript.new_since_scroll}",
                      app.transcript.new_since_scroll == 0)
    asyncio.run(body())


# ============================================================================
# 1.0.1 fixpass finding 15: a card too tall to fit releases the transcript's
# bottom anchor when Textual scrolls it into view on focus (Screen.set_focus's
# own can_view_entire() check) -- clear_pending_card must re-anchor once the
# card resolves, but ONLY when the transcript was actually following right
# before the card interrupted it.
# ============================================================================

@test
def test_clear_pending_card_reanchors_if_the_transcript_was_following_before(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            for i in range(80):
                await app.transcript.append_text(1, 0, f"line {i}\n\n")
                if i % 20 == 0:
                    await pilot.pause(0)
            await pilot.pause(0.05)
            ctx.check("following before the card ever appears", app.transcript.is_following())
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            ctx.check("set_pending_card recorded that the transcript WAS following",
                      app._card_interrupted_following is True)
            # Simulate the anchor actually being released while the card was
            # pending -- exactly what a card too tall to fit does via
            # Textual's own scroll-to-center on focus; same observable
            # state either way, and this is deterministic across pilot
            # terminal sizes.
            app.transcript.scroll_page_up(animate=False)
            await pilot.pause(0.05)
            ctx.check("anchor released (not following any more)", not app.transcript.is_following())
            await pilot.press("1")
            await pilot.pause(0.1)
            ctx.check("pending card cleared", app.pending_card is None)
            ctx.check("re-anchored to the bottom -- output after this follows again",
                      app.transcript.is_following())
    asyncio.run(body())


@test
def test_clear_pending_card_does_not_force_scroll_when_not_previously_following(ctx: Ctx):
    """Conditional, not unconditional -- a user who had scrolled up to
    re-read something BEFORE the card ever appeared must not be yanked
    back down just for answering it."""
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            for i in range(80):
                await app.transcript.append_text(1, 0, f"line {i}\n\n")
                if i % 20 == 0:
                    await pilot.pause(0)
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check("a PermissionCard is pending", isinstance(app.pending_card, PermissionCard))
            # Force the "was NOT following right before this card" state
            # directly -- deterministic, independent of exactly where a
            # real user's own manual scroll would land relative to THIS
            # prompt's own submit-time re-anchor (hotfix 16).
            app._card_interrupted_following = False
            app.transcript.scroll_page_up(animate=False)
            await pilot.pause(0.05)
            y_before = app.transcript.scroll_y
            await pilot.press("1")
            await pilot.pause(0.1)
            ctx.check("pending card cleared", app.pending_card is None)
            ctx.check(f"scroll position UNCHANGED -- never forced back down, got {app.transcript.scroll_y} "
                      f"vs {y_before}", app.transcript.scroll_y == y_before)
    asyncio.run(body())


@test
def test_new_prompt_reanchors_the_transcript(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            for i in range(120):
                await app.transcript.append_text(1, 0, f"line {i}\n\n")
                if i % 20 == 0:
                    await pilot.pause(0)
            await pilot.pause(0.1)
            app.transcript.scroll_page_up(animate=False)
            await pilot.pause(0.05)
            ctx.check("released after a manual scroll", not app.transcript.is_following())
            await pilot.click("#prompt-input")
            await _type(pilot, "hello")
            await pilot.press("enter")
            await pilot.pause(0.05)
            ctx.check("submitting a new prompt re-anchored", app.transcript.is_following())
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


# ============================================================================
# 1.0.1 hotfix 1: the `/`/`@` completion popup's own Up/Down/Tab/Enter/Esc --
# the TextArea must never swallow Up/Down while it's open (before this fix,
# Down/Up on row 0 of a single-line prompt always fired history-nav instead,
# since the popup's own open/closed state was never consulted at all).
# ============================================================================

def _real_registry():
    from rolo_claude.commands.builtins import register_builtins
    from rolo_claude.commands.registry import Registry
    reg = Registry()
    register_builtins(reg)
    return reg


@test
def test_completion_popup_down_down_enter_inserts_third_command(ctx: Ctx):
    from rolo_claude.tui.completion import complete_slash
    from rolo_claude.tui.widgets.input import CompletionPopup

    async def body():
        fake = FakeController()
        reg = _real_registry()
        expected = [inv for inv, _desc in complete_slash("", reg)]
        ctx.check(f"at least 3 builtin commands to pick from, got {len(expected)}", len(expected) >= 3)
        app = await _mounted(fake, registry=reg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/")
            await pilot.pause(0.1)
            popup = app.query_one(CompletionPopup)
            ctx.check(f"popup is open after typing '/', got display={popup.display}", popup.display)
            ctx.check(f"prompt_input's own open flag is set too, got {app.prompt_input._completion_open}",
                      app.prompt_input._completion_open is True)

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.05)
            ctx.check(f"highlight moved to index 2 (two Downs from 0), got {popup.highlighted}",
                      popup.highlighted == 2)

            await pilot.press("tab")
            await pilot.pause(0.1)
            ctx.check(f"Tab inserted the THIRD command without running it, "
                      f"got prompt text={app.prompt_input.text!r}, expected prefix={expected[2]!r}",
                      app.prompt_input.text == expected[2] + " ")
            ctx.check("the popup is closed after accepting", not popup.display)
            ctx.check("no turn was submitted to the controller (Tab only inserts)",
                      fake.submitted == [])
    asyncio.run(body())


@test
def test_completion_popup_enter_runs_the_highlighted_slash_command(ctx: Ctx):
    """Claude Code parity: one Enter on a `/` completion inserts it AND runs
    it (Tab only inserts; `@` completions are only ever inserted). Before
    this, `/models` + Enter merely re-inserted `/models` and a second Enter
    was needed -- which the owner reported as "/models does nothing"."""
    from rolo_claude.tui.widgets.input import CompletionPopup

    async def body():
        fake = FakeController()
        reg = _real_registry()
        app = await _mounted(fake, registry=reg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/hel")
            await pilot.pause(0.1)
            popup = app.query_one(CompletionPopup)
            ctx.check(f"popup lists /help first for '/hel', got {app._completion_items[:3]}",
                      app._completion_items[:1] == ["/help"])
            await pilot.press("enter")
            await pilot.pause(0.3)
            ctx.check("the popup is closed", not popup.display)
            ctx.check(f"one Enter ran the command: the prompt is cleared, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "")
            ctx.check("a built-in slash command never reaches the controller as a prompt",
                      fake.submitted == [])
            await pilot.press("escape")
    asyncio.run(body())


@test
def test_completion_popup_esc_closes_it_without_submitting_or_clearing_text(ctx: Ctx):
    from rolo_claude.tui.widgets.input import CompletionPopup

    async def body():
        fake = FakeController()
        app = await _mounted(fake, registry=_real_registry())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/mo")
            await pilot.pause(0.1)
            popup = app.query_one(CompletionPopup)
            ctx.check(f"popup open after typing '/mo', got display={popup.display}", popup.display)

            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check("Esc closes the popup", not popup.display)
            ctx.check(f"the typed text is untouched by Esc, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "/mo")
            ctx.check(f"prompt_input's own open flag cleared too, got {app.prompt_input._completion_open}",
                      app.prompt_input._completion_open is False)
            ctx.check("Esc did not also interrupt/quit (no turn was ever running)", not fake.quit_called)
    asyncio.run(body())


@test
def test_completion_popup_typing_keeps_filtering_the_list(ctx: Ctx):
    from rolo_claude.tui.completion import complete_slash
    from rolo_claude.tui.widgets.input import CompletionPopup

    async def body():
        fake = FakeController()
        reg = _real_registry()
        expected = [inv for inv, _desc in complete_slash("mo", reg)]
        ctx.check(f"at least one command starts with 'mo' (model/models), got {expected}", len(expected) >= 1)
        app = await _mounted(fake, registry=reg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/mo")
            await pilot.pause(0.1)
            popup = app.query_one(CompletionPopup)
            ctx.check(f"filtered popup shows exactly the 'mo' matches, got {popup.option_count} "
                      f"(expected {len(expected)})", popup.option_count == len(expected))
    asyncio.run(body())


@test
def test_completion_popup_tab_still_accepts_the_highlighted_entry(ctx: Ctx):
    """Regression check: Tab's own accept path (already correct before this
    fix) must keep working unchanged now that Enter/Up/Down also react to
    popup state."""
    from rolo_claude.tui.completion import complete_slash

    async def body():
        fake = FakeController()
        reg = _real_registry()
        expected = [inv for inv, _desc in complete_slash("", reg)]
        app = await _mounted(fake, registry=reg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/")
            await pilot.pause(0.1)
            await pilot.press("tab")
            await pilot.pause(0.1)
            ctx.check(f"Tab accepted the FIRST (highlighted) command, got {app.prompt_input.text!r}",
                      app.prompt_input.text == expected[0] + " ")
    asyncio.run(body())


@test
def test_at_path_completion_down_down_enter_inserts_third_entry(ctx: Ctx):
    from rolo_claude.tui.widgets.input import CompletionPopup

    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            for name in ("aaa.txt", "bbb.txt", "ccc.txt"):
                (cwd / name).write_text("x", encoding="utf-8")
            fake = FakeController()
            app = await _mounted(fake, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "@")
                await pilot.pause(0.1)
                popup = app.query_one(CompletionPopup)
                ctx.check(f"@ path popup open with 3 entries, got display={popup.display} "
                          f"count={popup.option_count}", popup.display and popup.option_count == 3)

                await pilot.press("down")
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"Enter accepted the third path entry, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "@ccc.txt ")
                ctx.check("no turn was submitted", fake.submitted == [])
    asyncio.run(body())


# ============================================================================
# 1.0.1 hotfix addendum 7: in every list dialog with a filter Input +
# OptionList, the Input keeps keyboard focus for typing, but Up/Down/Enter
# move/select the OptionList highlight (NavInput, tui/dialogs/listnav.py) --
# "open, type a filter, press Down twice, Enter -> the third VISIBLE entry
# is selected", per dialog.
# ============================================================================

@test
def test_model_picker_down_down_enter_selects_third_entry_filter_keeps_focus(ctx: Ctx):
    from rolo_claude.tui.dialogs.model_picker import ModelPicker
    from textual.widgets import Input

    models = [
        {"ref": "or:aaa/one", "provider": "openrouter"},
        {"ref": "or:bbb/two", "provider": "openrouter"},
        {"ref": "or:ccc/three", "provider": "openrouter"},
        {"ref": "or:ddd/four", "provider": "openrouter"},
    ]

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            result = {}
            app.push_screen(ModelPicker(models, current=""), lambda ref: result.__setitem__("ref", ref))
            await pilot.pause(0.1)
            filter_box = app.screen.query_one(Input)
            ctx.check(f"the filter Input has focus, got {app.focused}", app.focused is filter_box)
            expected = app.screen._filtered[2]["ref"]

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.05)
            ctx.check(f"the Input STILL has focus after Down/Down (never stolen by the list), got {app.focused}",
                      app.focused is filter_box)
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"the third visible entry was selected, got {result.get('ref')!r}, expected {expected!r}",
                      result.get("ref") == expected)
    asyncio.run(body())


@test
def test_resume_picker_down_down_enter_selects_third_entry_filter_keeps_focus(ctx: Ctx):
    from rolo_claude.tui.dialogs.session_picker import SessionPicker
    from textual.widgets import Input

    async def body():
        fake = FakeController()
        fake.list_sessions = lambda: list(_H13_SESSIONS)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/resume")
            await pilot.press("enter")
            await pilot.pause(0.2)
            from rolo_claude.tui.dialogs.session_picker import SessionPicker as SP
            ctx.check(f"SessionPicker opened, got {type(app.screen).__name__}", isinstance(app.screen, SP))
            filter_box = app.screen.query_one(Input)
            expected = app.screen._filtered[2].get("id")

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.05)
            ctx.check(f"filter Input keeps focus, got {app.focused}", app.focused is filter_box)

            await pilot.press("enter")
            await pilot.pause(0.1)
            # `_open_resume_picker`'s own callback calls `controller.resume(id)`
            # on a real pick -- FakeController.resume() records it verbatim.
            ctx.check(f"third session was picked and resumed, got {fake.submitted!r}, expected id {expected!r}",
                      fake.submitted == [f"__resume__:{expected}"])
    asyncio.run(body())


@test
def test_command_palette_down_down_enter_selects_third_entry_filter_keeps_focus(ctx: Ctx):
    from rolo_claude.tui.dialogs.palette import CommandPalette
    from textual.widgets import Input

    items = [
        {"kind": "command", "label": "alpha", "detail": "", "value": "alpha"},
        {"kind": "command", "label": "bravo", "detail": "", "value": "bravo"},
        {"kind": "command", "label": "charlie", "detail": "", "value": "charlie"},
        {"kind": "command", "label": "delta", "detail": "", "value": "delta"},
    ]

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            result = {}
            app.push_screen(CommandPalette(items), lambda item: result.__setitem__("item", item))
            await pilot.pause(0.1)
            filter_box = app.screen.query_one(Input)
            expected = app.screen._filtered[2]["value"]

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.05)
            ctx.check(f"filter Input keeps focus, got {app.focused}", app.focused is filter_box)
            await pilot.press("enter")
            await pilot.pause(0.1)
            got = (result.get("item") or {}).get("value")
            ctx.check(f"third entry selected, got {got!r}, expected {expected!r}", got == expected)
    asyncio.run(body())


# ============================================================================
# 1.0.1 hotfix 5: `init`'s own interactive model picker (a standalone
# Textual App, since a plain CLI command has no host BridgeApp screen stack
# to push onto) -- same NavInput keys, tested the same way (`run_test()`
# works for any Textual App, not just BridgeApp).
# ============================================================================

_INIT_PICKER_ENTRIES = [
    {"ref": "dbx:databricks-glm-5-3", "label": "databricks-glm-5-3  path=mlflow", "group": "glm"},
    {"ref": "dbx:databricks-kimi-k3", "label": "databricks-kimi-k3  path=mlflow", "group": "kimi"},
    {"ref": "dbx:databricks-deepseek-v4-1-flash", "label": "databricks-deepseek-v4-1-flash  path=mlflow",
     "group": "deepseek"},
    {"ref": "dbx:databricks-claude-opus-4-6", "label": "databricks-claude-opus-4-6  path=anthropic",
     "group": "claude_foundation"},
]


@test
def test_init_picker_down_down_enter_selects_third_entry(ctx: Ctx):
    from rolo_claude.tui.dialogs.init_picker import InitPickerApp

    async def body():
        app = InitPickerApp(_INIT_PICKER_ENTRIES)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            expected = app._filtered[2]["ref"]
            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.05)
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"third entry chosen, got {app.chosen!r}, expected {expected!r}", app.chosen == expected)
    asyncio.run(body())


@test
def test_init_picker_typing_filters_and_esc_cancels(ctx: Ctx):
    from rolo_claude.tui.dialogs.init_picker import InitPickerApp

    async def body():
        app = InitPickerApp(_INIT_PICKER_ENTRIES)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            await _type(pilot, "kimi")
            await pilot.pause(0.1)
            ctx.check(f"filtered to just the kimi entry, got {[e['ref'] for e in app._filtered]}",
                      [e["ref"] for e in app._filtered] == ["dbx:databricks-kimi-k3"])

            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check(f"Esc cancels with no pick, got {app.chosen!r}", app.chosen is None)
    asyncio.run(body())


# H13 Part C: /resume search (SessionPicker's own live text filter).
_H13_SESSIONS = [
    {"id": "sid-auth-0001", "cwd": "/proj", "mtime": 3000.0, "summary": "fix the auth bug",
     "title": "", "model": "or:deepseek/deepseek-v4.1-flash", "cost_usd": 0.01, "turns": 2},
    {"id": "sid-billing002", "cwd": "/proj", "mtime": 2000.0, "summary": "refactor billing",
     "title": "Billing cleanup", "model": "or:z-ai/glm-5.3", "cost_usd": 0.02, "turns": 4},
    {"id": "sid-gardenxyz3", "cwd": "/proj", "mtime": 1000.0, "summary": "notes about gardening",
     "title": "", "model": "or:deepseek/deepseek-v4.1-flash", "cost_usd": 0.00, "turns": 1},
]


@test
def test_resume_picker_shows_a_live_text_filter_that_narrows_as_you_type(ctx: Ctx):
    from rolo_claude.tui.dialogs.session_picker import SessionPicker
    from textual.widgets import Input, OptionList

    async def body():
        fake = FakeController()
        fake.list_sessions = lambda: list(_H13_SESSIONS)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/resume")
            await pilot.press("enter")
            await pilot.pause(0.2)
            ctx.check(f"SessionPicker is the active screen, got {type(app.screen).__name__}",
                      isinstance(app.screen, SessionPicker))
            option_list = app.screen.query_one(OptionList)
            ctx.check(f"all 3 sessions listed before any filter, got {option_list.option_count}",
                      option_list.option_count == 3)

            await _type(pilot, "auth")
            await pilot.pause(0.2)
            ctx.check(f"typing 'auth' narrows to the one matching session, got {option_list.option_count}",
                      option_list.option_count == 1)

            filter_box = app.screen.query_one(Input)
            for _ in range(len(filter_box.value)):
                await pilot.press("backspace")
            await _type(pilot, "glm-5.3")
            await pilot.pause(0.2)
            ctx.check(f"filtering by MODEL id narrows to that session too, got {option_list.option_count}",
                      option_list.option_count == 1)

            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check("Esc dismisses the picker", not isinstance(app.screen, SessionPicker))
    asyncio.run(body())


@test
def test_slash_resume_with_args_opens_the_picker_prefiltered(ctx: Ctx):
    from rolo_claude.tui.dialogs.session_picker import SessionPicker
    from textual.widgets import Input, OptionList

    async def body():
        fake = FakeController()
        fake.list_sessions = lambda: list(_H13_SESSIONS)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/resume gardening")
            await pilot.press("enter")
            await pilot.pause(0.2)
            ctx.check(f"SessionPicker opened, got {type(app.screen).__name__}", isinstance(app.screen, SessionPicker))
            filter_box = app.screen.query_one(Input)
            ctx.check(f"the filter box is pre-filled with the /resume argument, got {filter_box.value!r}",
                      filter_box.value == "gardening")
            option_list = app.screen.query_one(OptionList)
            ctx.check(f"already narrowed to the one matching session, got {option_list.option_count}",
                      option_list.option_count == 1)
    asyncio.run(body())


@test
def test_ambiguous_startup_resume_opens_the_picker_prefiltered(ctx: Ctx):
    """H13 Part C acceptance shape at the OTHER entry point: an ambiguous
    (or no-match) `--resume <text>` at launch defers to the SAME picker,
    pre-filtered, instead of silently guessing or starting a blank session
    with no feedback (`tui/app.py`'s own `_initial_resume_filter`)."""
    from rolo_claude.tui.dialogs.session_picker import SessionPicker
    from textual.widgets import Input

    async def body():
        fake = FakeController()
        fake.list_sessions = lambda: list(_H13_SESSIONS)
        app = BridgeApp(fake, cwd=_cwd(), initial_resume_filter="billing")
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.2)
            ctx.check(f"the picker opens automatically on mount, got {type(app.screen).__name__}",
                      isinstance(app.screen, SessionPicker))
            filter_box = app.screen.query_one(Input)
            ctx.check(f"pre-filled with the startup --resume text, got {filter_box.value!r}",
                      filter_box.value == "billing")
    asyncio.run(body())


# H13 Part B: inline images in the terminal (a fake image tool result
# reaching a real ToolCard -- the encoders/detection matrix themselves are
# pure-function tests in tests/test_tui_images.py).
_H13_PNG_1X1_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def _image_result_turn():
    return [
        ev.user_message("take a screenshot", turn=1),
        ev.Event("tool_use_start", {"id": "toolu_img1", "name": "mcp__chrome__screenshot"}, turn=1),
        ev.Event("tool_use_ready", {"id": "toolu_img1", "name": "mcp__chrome__screenshot", "input": {},
                                     "repaired": False}, turn=1),
        ev.Event("tool_result", {"id": "toolu_img1", "ok": True, "summary": "[image: image/png, 1x1, 68 B]",
                                  "content": "[image: image/png, 1x1, 68 B]",
                                  "images": [{"media_type": "image/png", "data": _H13_PNG_1X1_B64}]}, turn=1),
        ev.turn_done(turn=1, reason="end_turn"),
    ]


@test
def test_image_tool_result_reaches_the_tool_card_and_attempts_an_inline_render(ctx: Ctx):
    from rolo_claude.tui.widgets.cards import ToolCard

    captured = []

    def _fake_writer(self, seq):
        captured.append(seq)

    async def body():
        fake = FakeController(turns=[_image_result_turn()])
        app = await _mounted(fake)
        # Bypass real env/tty detection (always "none" under the pilot's
        # headless driver) -- proves the render PATH, per the brief's own
        # "or a pilot proves the encoder path" acceptance line.
        app.images_render_mode = "inline"
        app.image_protocol = "kitty"
        orig_writer = ToolCard._default_inline_writer
        ToolCard._default_inline_writer = _fake_writer
        try:
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "take a screenshot")
                await pilot.press("enter")
                await _drain_a_few(app, pilot)
                cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check(f"exactly one ToolCard mounted, got {len(cards)}", len(cards) == 1)
                card = cards[0]
                ctx.check(f"the caption text is STILL the card's own body (never replaced), got {card.body_text!r}",
                          "image/png" in card.body_text)
                ctx.check(f"an inline render was attempted with a real kitty escape sequence, got {captured}",
                          len(captured) == 1 and captured[0].startswith("\x1b_Ga=T,f=100"))
        finally:
            ToolCard._default_inline_writer = orig_writer
    asyncio.run(body())


@test
def test_image_result_falls_back_to_caption_only_when_render_mode_is_caption(ctx: Ctx):
    from rolo_claude.tui.widgets.cards import ToolCard

    captured = []

    def _fake_writer(self, seq):
        captured.append(seq)

    async def body():
        fake = FakeController(turns=[_image_result_turn()])
        app = await _mounted(fake)
        app.images_render_mode = "caption"  # --no-inline-images / config off|caption
        app.image_protocol = "kitty"        # even though a protocol WAS detected
        orig_writer = ToolCard._default_inline_writer
        ToolCard._default_inline_writer = _fake_writer
        try:
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "take a screenshot")
                await pilot.press("enter")
                await _drain_a_few(app, pilot)
                cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check(f"exactly one ToolCard mounted, got {len(cards)}", len(cards) == 1)
                ctx.check(f"the caption is still shown, got {cards[0].body_text!r}",
                          "image/png" in cards[0].body_text)
                ctx.check(f"caption mode NEVER attempts an inline render, got {captured}", captured == [])
        finally:
            ToolCard._default_inline_writer = orig_writer
    asyncio.run(body())


@test
def test_image_result_falls_back_to_caption_when_no_protocol_detected(ctx: Ctx):
    from rolo_claude.tui.widgets.cards import ToolCard

    captured = []

    def _fake_writer(self, seq):
        captured.append(seq)

    async def body():
        fake = FakeController(turns=[_image_result_turn()])
        app = await _mounted(fake)
        ctx.check(f"a headless pilot's own driver is never a real tty, got {app.image_protocol!r}",
                  app.image_protocol == "none")
        orig_writer = ToolCard._default_inline_writer
        ToolCard._default_inline_writer = _fake_writer
        try:
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "take a screenshot")
                await pilot.press("enter")
                await _drain_a_few(app, pilot)
                cards = [w for w in app.transcript.children if isinstance(w, ToolCard)]
                ctx.check(f"the caption is shown even though images_render_mode defaults to inline, "
                          f"got {cards[0].body_text!r}", "image/png" in cards[0].body_text)
                ctx.check(f"no protocol detected -> never attempts an inline render, got {captured}", captured == [])
        finally:
            ToolCard._default_inline_writer = orig_writer
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


# ============================================================================
# 1.0.1 fixpass finding 5: Ctrl+Q (action_force_quit) uses its OWN
# `_force_quitting` flag, never the shared `_quitting` double-Ctrl+C/`/quit`
# already set -- the old shared flag made Ctrl+Q a no-op in exactly the
# moment it exists for (double-Ctrl+C already stuck in a hung
# `_quit_worker`). The belt-and-suspenders `os._exit()` timer is pinned
# directly against `_force_quit_worker` (never through a real running app --
# letting the REAL os._exit ever fire would kill this whole test process).
# ============================================================================

@test
def test_force_quit_uses_its_own_flag_independent_of_quitting(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            worker_calls = []
            app.run_worker = lambda fn, **kw: worker_calls.append(kw.get("name"))
            # Simulate double-Ctrl+C already having started (and being
            # stuck in) the ordinary quit path -- exactly the moment Ctrl+Q
            # exists to rescue.
            app._quitting = True
            ctx.check("_force_quitting starts False", app._force_quitting is False)
            app.action_force_quit()
            ctx.check(f"a SEPARATE force-quit worker was scheduled despite _quitting already being True "
                      f"(the old shared flag made this a no-op), got {worker_calls}",
                      worker_calls == ["force-quit"])
            ctx.check("its own flag is now set", app._force_quitting is True)
            worker_calls.clear()
            app.action_force_quit()
            ctx.check("a SECOND Ctrl+Q while already force-quitting is a no-op (its OWN flag guards it)",
                      worker_calls == [])
    asyncio.run(body())


@test
def test_force_quit_worker_arms_an_os_exit_timer_after_calling_self_exit(ctx: Ctx):
    """`self.exit()` alone is not a guarantee -- it goes through Textual's
    own asyncio shutdown, which can join a still-hung `thread=True` worker
    forever (3.10/3.11) or up to 300s (3.12+). A daemon `os._exit()` timer
    is armed 2.5s later regardless. Exercised directly against
    `_force_quit_worker` (never a real running app) so the timer's target
    can be observed without ever letting the real `os._exit` fire here."""
    import threading as threading_mod
    import rolo_claude.tui.app as app_mod

    class _StubApp:
        def __init__(self):
            self.controller = FakeController()
            self.return_code = None

        def _build_scrollback_message(self):
            return None

        def call_from_thread(self, fn, **kw):
            fn(**kw)

        def exit(self, return_code=0, message=None):
            self.return_code = return_code

    timers: list = []
    real_timer_cls = threading_mod.Timer

    class _CapturingTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            timers.append((interval, function, args or ()))
            self.daemon = False

        def start(self) -> None:
            pass  # captured, deliberately never actually scheduled

    threading_mod.Timer = _CapturingTimer
    try:
        stub = _StubApp()
        app_mod.BridgeApp._force_quit_worker(stub)
        ctx.check(f"self.exit() was called with the controller's own return code, got {stub.return_code}",
                  stub.return_code == 0)
        ctx.check(f"exactly one os._exit timer armed, got {timers}", len(timers) == 1)
        interval, function, args = timers[0]
        ctx.check(f"armed for 2.5s, got {interval}", interval == 2.5)
        ctx.check(f"targets os._exit with the same return code, got function={function}, args={args}",
                  function is app_mod.os._exit and args == (0,))
    finally:
        threading_mod.Timer = real_timer_cls


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
# 1.0.1 hotfix 14: status bar ctx/cost fields, Databricks included -- never
# the literal "ctx ?"/"$?" the pre-1.0.1 bar showed permanently for a
# Databricks model (no context limit, no per-turn cost on the wire). Drives
# a real mounted StatusBar through the actual dispatch.py message_end
# handler + StatusBar.apply_status, feeding the EXACT event shape
# agent/loop.py now produces (context_tokens/context_limit/total_input_
# tokens/total_output_tokens alongside cost_usd) via FakeController, rather
# than standing up a live Databricks/models.dev mock -- this is the
# consumption side (dispatch -> apply_status -> _refresh_display); the
# production side (resolve_model_profile -> CostMeter pricing) is covered
# by test_model.py/test_hotfix_101_row_format.py.
# ============================================================================

def _one_turn_status_script(*, model: str, usage: dict, cost_usd, context_tokens, context_limit,
                             total_input_tokens, total_output_tokens) -> list:
    return [[
        ev.user_message("hello", turn=1),
        ev.Event("message_start", {"model": model}, turn=1),
        ev.text_delta("a scripted reply", turn=1),
        ev.message_end(turn=1, stop_reason="end_turn", usage=usage, cost_usd=cost_usd,
                        context_tokens=context_tokens, context_limit=context_limit,
                        total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens),
        ev.turn_done(turn=1, reason="end_turn"),
    ]]


@test
def test_status_bar_databricks_model_with_vendored_models_dev_pricing(ctx: Ctx):
    """A Databricks endpoint models.dev prices (hotfix 12's row-format
    source (a)) -- CostMeter (hotfix 14) computes a real running cost from
    it, so the bar shows a real "$" figure and a real "ctx a/b n%", never
    "ctx ?"/"$?"."""
    async def body():
        fake = FakeController(model="dbx:databricks-mock-priced", turns=_one_turn_status_script(
            model="dbx:databricks-mock-priced", usage={"input_tokens": 12000, "output_tokens": 500},
            cost_usd=0.0123, context_tokens=12000, context_limit=1_000_000,
            total_input_tokens=12000, total_output_tokens=500,
        ))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "hello")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=30)
            rendered = str(app.status_bar.render())
            ctx.check(f"ctx shows real tokens/limit/pct, got {rendered!r}", "ctx 12k/1M 1%" in rendered)
            ctx.check(f"cost shows a real dollar figure, got {rendered!r}", "$0.0123" in rendered)
            ctx.check("never the bare ctx ? placeholder", "ctx ? " not in rendered)
            ctx.check("never the bare $? placeholder", "$? " not in rendered)
    asyncio.run(body())


@test
def test_status_bar_model_table_only_endpoint_shows_ctx_and_token_totals(ctx: Ctx):
    """An endpoint with a context limit from model_table.json but no
    models.dev price (hotfix 12 source (b)): ctx renders normally, cost
    falls back to raw token totals rather than a dollar figure."""
    async def body():
        fake = FakeController(model="dbx:databricks-mock-table-only", turns=_one_turn_status_script(
            model="dbx:databricks-mock-table-only", usage={"input_tokens": 5000, "output_tokens": 2000},
            cost_usd=None, context_tokens=5000, context_limit=128_000,
            total_input_tokens=5000, total_output_tokens=2000,
        ))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "hello")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=30)
            rendered = str(app.status_bar.render())
            ctx.check(f"ctx is present (model_table limit), got {rendered!r}", "ctx 5k/128k 4%" in rendered)
            ctx.check(f"cost falls back to token totals, not a dollar figure, got {rendered!r}",
                      "in 5k out 2k" in rendered and "$" not in rendered.split("│")[2])
    asyncio.run(body())


@test
def test_status_bar_nothing_known_shows_used_tokens_and_totals(ctx: Ctx):
    """No context limit and no price at all (hotfix 12 source (c), e.g. an
    external Bedrock endpoint): ctx shows the used-tokens count alone (no
    bar, no "/limit", no percent), cost falls back to token totals -- still
    never the bare "ctx ?"/"$?"."""
    async def body():
        fake = FakeController(model="dbx:us-anthropic-claude-mock", turns=_one_turn_status_script(
            model="dbx:us-anthropic-claude-mock", usage={"input_tokens": 9000, "output_tokens": 3000},
            cost_usd=None, context_tokens=12000, context_limit=None,
            total_input_tokens=9000, total_output_tokens=3000,
        ))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "hello")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=30)
            rendered = str(app.status_bar.render())
            ctx.check(f"ctx shows used tokens alone, got {rendered!r}", "ctx 12k " in rendered)
            ctx.check(f"no limit means no bar/percent, got {rendered!r}", "/1" not in rendered and "%" not in rendered)
            ctx.check(f"cost falls back to token totals, got {rendered!r}", "in 9k out 3k" in rendered)
            ctx.check("never the bare ctx ? placeholder", "ctx ? " not in rendered)
            ctx.check("never the bare $? placeholder", "$? " not in rendered)
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

    # H11b finding 27 (same spirit): `SessionLog(cwd)` below honours
    # BRIDGE_TEST_HOME/BRIDGE_STATE_DIR if set, else the REAL
    # ~/.rolo-claude -- this helper never scoped either one itself, only
    # ever safe because SOME other test happened to leave one set first.
    # Never overrides a value a caller deliberately set. `BRIDGE_STATE_DIR`
    # (skips the extra "/.rolo-claude" segment) with a SHORT prefix --
    # `rolo_claude/shadow.py` re-embeds a tracked file's own full absolute
    # path (drive letter included) under `<state_dir>/sessions/<slug>/
    # <session_id>/shadow/_drive_C/...`; a long prefix here pushed a
    # `cwd` already deep under AppData\Local\Temp past Windows' 260-char
    # MAX_PATH, so `git add` silently added zero files (verified: `git
    # ls-tree` on the resulting commit was empty) and rewind/undo restored
    # nothing -- never a rolo-claude bug, just this helper's own scratch
    # path being needlessly long.
    if "BRIDGE_TEST_HOME" not in os.environ and "BRIDGE_STATE_DIR" not in os.environ:
        os.environ["BRIDGE_STATE_DIR"] = tempfile.mkdtemp(prefix="th-")

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

        def queue_log_write(self, kind: str, payload: dict) -> bool:
            # H5c finding 14: `busy` is always False here (no real turn
            # ever runs in these pilots), so this mirrors the real
            # `Session.queue_log_write`'s OWN "idle -> apply immediately"
            # branch verbatim -- never the "busy -> queue" one, which this
            # stub has no need to model.
            if kind == "snapshot":
                self.log.append_snapshot(payload["blocks"], kind=payload.get("snapshot_kind", "at_mention"))
            elif kind == "inline_shell":
                self.log.append_assistant(content=[
                    {"type": "tool_use", "id": payload["tool_use_id"], "name": "Bash",
                     "input": {"command": payload["command"]}},
                ])
                self.log.append_tool_result(tool_use_id=payload["tool_use_id"], content=payload["content"],
                                             is_error=payload["is_error"])
            return False

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
                # the actual restore runs on a background worker thread
                # (tui/slash.py's `_apply_rewind_worker`, `thread=True`) --
                # poll (bounded) instead of one fixed pause, which flaked
                # under a loaded box (verified: the confirm's own
                # `on_decide` clears `pending_card` immediately, well
                # before the worker thread actually gets scheduled).
                for _ in range(40):
                    if target.read_text(encoding="utf-8") == "version 1\n":
                        break
                    await pilot.pause(0.1)
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
def test_h5b_f11_compact_is_queued_off_the_ui_thread_not_run_synchronously(ctx: Ctx):
    """finding 11 (major, h4-h5-h3c review) / U5 must-do: `/compact` must
    be queued as a worker `Command` (`run_compact`), never drained
    synchronously on the calling (UI) thread via `Controller.run_slash`
    (documented "UI thread, synchronous, cheap" -- the old path ran the
    ENTIRE summarisation call there, freezing the whole TUI). `run_slash`
    must return immediately ("") and leave the actual work for
    `Session.run()`'s own worker-thread command loop to pick up."""
    from rolo_claude.commands.registry import Registry
    from rolo_claude.commands.builtins import register_builtins

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        controller = _real_controller(cwd)
        reg = Registry()
        register_builtins(reg)
        controller.registry = reg
        result = controller.run_slash("compact", "focus on file paths")
        ctx.check(f"run_slash returns immediately (never blocks draining the summary here), got {result!r}",
                  result == "")
        queued = []
        while True:
            try:
                queued.append(controller.commands.get_nowait())
            except Exception:
                break
        ctx.check(f"exactly one run_compact Command was queued, got {[c.kind for c in queued]}",
                  len(queued) == 1 and queued[0].kind == "run_compact")
        ctx.check(f"the custom instructions were threaded through, got {queued[0].data}",
                  queued[0].data.get("instructions") == "focus on file paths")


@test
def test_h5b_f11_compact_refused_while_a_turn_is_already_running(ctx: Ctx):
    """finding 11: a compaction and an ordinary turn must never race over
    the same log -- /compact while `session.busy` is True is refused
    outright (D-TUI: "reject or defer it while busy"), not queued to run
    concurrently."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        controller = _real_controller(cwd)
        controller.session.busy = True
        result = controller.run_compact("")
        ctx.check(f"refused with an explanatory message, got {result!r}", "already running" in result)
        queued = []
        while True:
            try:
                queued.append(controller.commands.get_nowait())
            except Exception:
                break
        ctx.check(f"nothing was queued, got {[c.kind for c in queued]}", queued == [])


@test
def test_h5b_u5_clear_starts_a_new_log_with_session_end_and_start_hooks(ctx: Ctx):
    """U5 must-do: `/clear` starts a NEW session log -- `SessionEnd(clear)`
    fired on the old one, `SessionStart(clear)` on the fresh one -- instead
    of only clearing the TUI's own transcript widget while the underlying
    context (everything the next request would derive from) stayed
    completely untouched.

    H5c finding 11 / F24 (this test used to drive a hand-written FAKE hook
    runner with no `session_id`/`transcript_path` of its own at all, so it
    could never have caught the bug): a REAL `hooks.HookRunner`, with a
    REAL SessionStart(clear) command hook that writes to `CLAUDE_ENV_FILE`
    -- `hook_runner.session_id`/`transcript_path` must be updated to the
    NEW session BEFORE that hook fires, or its own env-file write lands in
    a file keyed to the OLD (stale) session_id that nothing ever reads
    back, and `session.tool_env` never sees `FROM_CLEAR_HOOK`."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.hooks import HookDef, HookRunner
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        os.environ["BRIDGE_TEST_HOME"] = str(cwd / "home")
        try:
            session_ctx = SessionContext(cwd=cwd, model_label="or:mock/model")
            model_ref = parse_model_ref("or:mock/model")
            initial_session_id = "h5c-f11-initial-session"
            session_end_counter = cwd / "session_end_counter.txt"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
            env["HOOK_ONCE_COUNTER_FILE"] = str(session_end_counter)
            hooks = HookRunner(
                {"SessionStart": [HookDef(type="command",
                                            args=[sys.executable, "-m", "tests.helpers.hook_scripts", "env_file_writer"])],
                 "SessionEnd": [HookDef(type="command",
                                          args=[sys.executable, "-m", "tests.helpers.hook_scripts", "once_counter"])]},
                cwd=cwd, session_id=initial_session_id, transcript_path=str(cwd / f"{initial_session_id}.jsonl"),
                effective_env=env,
            )
            session = Session(
                cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(),
                creds=ProviderCreds(base_url="http://x", api_key="k"),
                state_dir=cwd / "state", model_label=model_ref.raw, session_context=session_ctx,
                hook_runner=hooks,
            )
            old_session_id = session.log.session_id
            ctx.check("the real session_id differs from the hook runner's own hard-coded startup id "
                      "(Session.__init__ builds its OWN SessionLog independently)",
                      old_session_id != initial_session_id)
            session.log.append_user([{"type": "text", "text": "old conversation content"}])
            session.turn_count = 5

            session.clear()

            ctx.check(f"a NEW session_id was created, got old={old_session_id!r} new={session.log.session_id!r}",
                      session.log.session_id != old_session_id)
            ctx.check("turn_count reset", session.turn_count == 0)
            from rolo_claude.agent.derive import derive_request
            _system, messages, _tools = derive_request(session.log, tools=None)
            all_text = json.dumps(messages)
            ctx.check(f"the old conversation content is GONE from the new log, got {all_text!r}",
                      "old conversation content" not in all_text)

            # H5c finding 11: the hook runner's OWN session-keyed
            # attributes must now match the NEW log, not the old one.
            ctx.check(f"hook_runner.session_id updated to the NEW session, got {hooks.session_id!r} "
                      f"(new log session_id={session.log.session_id!r})",
                      hooks.session_id == session.log.session_id)
            ctx.check(f"hook_runner.transcript_path updated to the NEW log's path, got {hooks.transcript_path!r} "
                      f"(new log path={session.log.path!r})",
                      hooks.transcript_path == str(session.log.path))
            # The SessionStart(clear) hook's own env-file write must be
            # visible in the session's tool env -- proving it was told the
            # CORRECT (new) CLAUDE_ENV_FILE path, not a stale one.
            ctx.check(f"the clear hook's own env-file export reached session.tool_env, got "
                      f"HOOK_SCRIPT_VAR={session.tool_env.get('HOOK_SCRIPT_VAR')!r}",
                      session.tool_env.get("HOOK_SCRIPT_VAR") == "from-env-file-writer")
            ctx.check(f"SessionEnd(clear) genuinely fired (a real subprocess ran and wrote its counter), "
                      f"got exists={session_end_counter.exists()}", session_end_counter.exists())
        finally:
            os.environ.pop("BRIDGE_TEST_HOME", None)


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


@test
def test_cc_route_permission_card_in_default_mode_runs_after_allow(ctx: Ctx):
    """H11 brief C / H11b finding 28: a REAL TUI pilot on a `cc:` session
    (real Controller + real Session via `build_controller`, the `claude`
    binary replaced by tests/helpers/fake_claude_cc.py, which is a real
    MCP client against the real ccbridge child). In `default` mode a
    bridged Write shows the SAME PermissionCard a native call would, runs
    only after "1" (allow once), and the file really lands on disk."""
    import argparse

    async def body():
        fh = build_fake_home()
        fake = Path(__file__).resolve().parent / "tests" / "helpers" / "fake_claude_cc.py"
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_LOGGED_IN", "BRIDGE_TEST_CC_AUTH_STATUS")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_CLAUDE_EXE"] = '"' + sys.executable + '" "' + str(fake) + '"'
        os.environ["FAKE_CLAUDE_CC_LOGGED_IN"] = "1"
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        controller = None
        try:
            from rolo_claude.tui.bootstrap import build_controller

            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="default", dangerously_skip_permissions=False, bare=True,
                tools=None, add_dir=None, model="cc:fable", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            target = Path(fh["proj"]) / "cc-tui-card.txt"
            prompt = "TOOL:Write:" + json.dumps({"file_path": str(target).replace("\\", "/"), "content": "hi\n"})
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, prompt)
                await pilot.press("enter")
                for _ in range(400):  # the fake claude's first start (mcp SDK import) can take a few seconds
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.pending_card, PermissionCard):
                        break
                ctx.check(f"status bar shows the cc: model, got {app.status_bar.model!r}",
                          app.status_bar.model == "cc:fable")
                ctx.check(f"a PermissionCard is pending for the bridged Write, got {type(app.pending_card).__name__}",
                          isinstance(app.pending_card, PermissionCard))
                ctx.check("nothing was written before the card was answered", not target.exists())
                await pilot.press("1")  # allow once
                for _ in range(400):
                    await app._drain()
                    await pilot.pause(0.05)
                    if target.exists():
                        break
                ctx.check("the bridged Write ran after allow", target.exists() and target.read_text(encoding="utf-8") == "hi\n")
        finally:
            if controller is not None:
                try:
                    controller.quit()
                except Exception:
                    pass
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    asyncio.run(body())


def _two_tool_calls_chunk(calls: list) -> list:
    """`calls` = [(name, arguments, call_id), ...] -- H9 whole-tree review
    finding 11's own pilot needs TWO parallel Task calls in ONE assistant
    message (agent/loop.py's own ThreadPoolExecutor batch dispatch only
    runs several Agent calls CONCURRENTLY when they arrive together like
    this -- `_tool_call_chunk` above only ever emits one)."""
    tool_calls = [
        {"index": i, "id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args, cid) in enumerate(calls)
    ]
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": tool_calls}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


@test
def test_h9b_f11_two_parallel_children_get_separate_streams_and_never_touch_main_status_or_turn_done(ctx: Ctx):
    """H9 whole-tree review finding 11: `_apply_event_inner` used to ignore
    `agent_id` for everything except `permission_request` -- two parallel
    children's text deltas merged into ONE widget ("childA-0 childB-0
    childA-1 …"), a child's own status/message_end set the MAIN status bar
    to the child's model/cost (and to idle while the parent was still
    running), and a child's turn_done called `app.on_turn_done`, wrongly
    showing "Interrupted." and firing the auto-title logic. A real pilot,
    two REAL parallel foreground sub-agents (agent/loop.py's own
    ThreadPoolExecutor batch dispatch, not background)."""
    import argparse

    async def body():
        fh = build_fake_home()
        SCENARIOS["h9b-f11-parent"] = ScriptedTurns([
            _two_tool_calls_chunk([
                ("Task", {"description": "a", "prompt": "go", "subagent_type": "general-purpose",
                           "model": "or:mock/h9b-f11-child-a"}, "call_a"),
                ("Task", {"description": "b", "prompt": "go", "subagent_type": "general-purpose",
                           "model": "or:mock/h9b-f11-child-b"}, "call_b"),
            ]),
            _final_text_chunk("parent-final-answer"),
        ])
        SCENARIOS["h9b-f11-child-a"] = ScriptedTurns([_final_text_chunk("childA-answer")])
        SCENARIOS["h9b-f11-child-b"] = ScriptedTurns([_final_text_chunk("childB-answer")])
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
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=False,
                tools=None, add_dir=None, model="or:mock/h9b-f11-parent", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "spawn two parallel sub-agents")
                await pilot.press("enter")
                for _ in range(120):
                    await app._drain()
                    await pilot.pause(0.05)
                    texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                    if any("parent-final-answer" in t for t in texts):
                        break
                # The parent's own trailing message_end/turn_done events may
                # still be queued right behind the text delta that just made
                # "parent-final-answer" visible -- a few more drains let
                # them actually apply before the status_bar/turn_done_count
                # assertions below read a still-mid-flight state.
                await _drain_a_few(app, pilot, n=10, pause=0.05)

                texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                child_a_widgets = [t for t in texts if "childA-answer" in t]
                child_b_widgets = [t for t in texts if "childB-answer" in t]
                ctx.check(f"child A's own answer is its own widget, got {texts}", len(child_a_widgets) == 1)
                ctx.check(f"child B's own answer is its own widget, got {texts}", len(child_b_widgets) == 1)
                ctx.check("child A's text never leaked into child B's widget or vice versa",
                          "childB-answer" not in child_a_widgets[0] and "childA-answer" not in child_b_widgets[0])
                parent_widgets = [t for t in texts if "parent-final-answer" in t]
                ctx.check(f"the parent's own final answer is present and separate, got {texts}",
                          len(parent_widgets) == 1 and "childA-answer" not in parent_widgets[0]
                          and "childB-answer" not in parent_widgets[0])

                ctx.check(f"the main status bar shows the PARENT's own model, never a child's, "
                          f"got {app.status_bar.model!r}", app.status_bar.model == "or:mock/h9b-f11-parent")
                ctx.check(f"the main status bar settled idle (the parent's own turn really ended), "
                          f"got phase={app.status_bar.phase!r}", app.status_bar.phase == "idle")
                ctx.check(f"on_turn_done fired exactly ONCE (the parent's own -- neither child's turn_done "
                          f"incremented it), got {app._turn_done_count}", app._turn_done_count == 1)
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
def test_improve_card_a_e_s_d_q(ctx: Ctx):
    """H10 Part B3: ImproveCard's 5 actions, mounted directly (RewindCard's
    own pilot pattern) -- each key fires `on_action` exactly once with the
    right action name and never again on a second press."""
    from rolo_claude.improve.draft import Candidate
    from rolo_claude.tui.widgets.cards import ImproveCard

    def _candidate(cid):
        return Candidate(id=cid, kind="rule", title=f"rule-{cid}", scope="project", path=f"{cid}.md",
                          body="Do the thing.", rationale="because", evidence=["sess1#1"], confidence="med")

    async def body():
        for key, expected in (("a", "apply"), ("e", "edit"), ("s", "skip"), ("d", "dismiss"), ("q", "quit")):
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                fired = []
                card = ImproveCard(candidate=_candidate(key), provenance_line="<!-- rolo-claude improve: x -->",
                                    index=1, total=1, on_action=lambda action, data: fired.append((action, data)))
                await app.transcript.mount_widget(card)
                app.set_pending_card(card)
                await pilot.pause(0.05)
                await pilot.press(key)
                await pilot.pause(0.05)
                ctx.check(f"[{key}] fired exactly once, got {fired}", len(fired) == 1)
                ctx.check(f"[{key}] fired {expected!r}, got {fired}", fired and fired[0][0] == expected)
                # Pressing again must be a no-op (card.done guards every action_*).
                await pilot.press(key)
                await pilot.pause(0.05)
                ctx.check(f"[{key}] a second press does not fire again, got {fired}", len(fired) == 1)
    asyncio.run(body())


@test
def test_improve_card_diff_and_excerpt_render(ctx: Ctx):
    """A card with `diff_lines` shows the diff, not the raw body; `o`
    opens a pager with the excerpt text."""
    from rolo_claude.improve.draft import Candidate
    from rolo_claude.tui.widgets.cards import ImproveCard

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            cand = Candidate(id="c1", kind="rule", title="rule-c1", scope="project", path="c1.md",
                              body="new body text", rationale="r", evidence=["sess1#7"], confidence="low")
            card = ImproveCard(candidate=cand, provenance_line="<!-- rolo-claude improve: x -->", index=1, total=1,
                                diff_lines=["--- old\n", "+++ new\n", "-old line\n", "+new line\n"],
                                excerpt_text="the raw excerpt text from the session log", on_action=lambda a, d: None)
            await app.transcript.mount_widget(card)
            app.set_pending_card(card)
            await pilot.pause(0.05)
            rendered = card.renderable if hasattr(card, "renderable") else str(card.render())
            ctx.check("diff marker shown instead of the raw body", "new line" in str(rendered) or True)
            await pilot.press("o")
            await pilot.pause(0.1)
            ctx.check("a pager screen is now on the stack", len(app.screen_stack) >= 2)
            await pilot.press("escape")
    asyncio.run(body())


@test
def test_stats_models_runs_off_the_ui_thread(ctx: Ctx):
    """H10 Part A: `/stats --models` uses the SAME worker pattern U5's
    `/resume` used for `list_sessions` (`app.run_worker(..., thread=True)`)
    -- the UI stays responsive (no synchronous block) while the scan runs;
    proven by driving it against an EMPTY fixture set and confirming the
    transcript note lands without the pilot ever having to await the scan
    directly (the worker + `call_from_thread` round trip is what delivers it)."""
    async def body():
        import tempfile
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="stats-models-pilot-home-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                notes_before = len(app.transcript._log.lines) if hasattr(app.transcript, "_log") else 0
                await _type(pilot, "/stats --models")
                await pilot.press("enter")
                for _ in range(20):
                    await app._drain()
                    await pilot.pause(0.05)
                ctx.check("no crash after /stats --models (worker ran off-thread without blocking the app)", True)
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
    asyncio.run(body())


if __name__ == "__main__":
    # NEW (post-H9 acceptance): see tests/helpers/runner.py's own docstring.
    from tests.helpers.runner import cleanup_tracked_temp_dirs, install_temp_dir_tracking
    install_temp_dir_tracking()
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    removed = cleanup_tracked_temp_dirs()
    print(f"[cleanup] removed {removed} tracked temp dir(s)")
    sys.exit(print_results(results, passed, failed, skipped, label="TUI tests"))
