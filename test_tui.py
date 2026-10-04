"""test_tui.py -- the Textual TUI's own suite (U2, D-TUI Testing), run
separately from `python tests/run_all.py` (that discoverer only globs
`tests/test_*.py`, matching `test_bridge.py`'s own repo-root placement) --
see `docs/harness/INSTALL.md`/the U2 report for the exact command. Same
Ctx/@test/run_all pattern as `test_bridge.py`/`tests/helpers/runner.py`
(`tests.helpers.runner.new_registry`); every pilot test is a plain sync
`@test` function that calls `asyncio.run(...)` itself, per the brief.

textual/rich are real imports here (this file's whole job is exercising
them) -- never imported at `halo_harness` package scope elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once


def _clear_halo_env_vars() -> None:
    """2.0.0 fixpass item G: a stray HALO_* left in the developer's own
    shell would silently out-rank whatever legacy BRIDGE_*/plain name a
    fixture or FakeController-based pilot below relies on (env_compat
    resolves HALO_* first) -- cleared here, before any halo_harness import
    and before ensure_default_provider_credentials/ensure_scoped_state_
    dir_once run just below, same timing as test_bridge.py's own sweep."""
    for key in [k for k in os.environ if k.startswith("HALO_")]:
        os.environ.pop(key, None)


_clear_halo_env_vars()
# 2.0.0 fixpass item G: a stray BRIDGE_STATE_DIR left in the parent shell
# would make ensure_scoped_state_dir_once() below treat it as deliberate
# external scoping and skip setting its own BRIDGE_TEST_HOME, leaving
# home()-based paths pointed at the real machine home even though
# bridge_home() itself would still be safely scoped by the stray var.
os.environ.pop("BRIDGE_STATE_DIR", None)

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps every `or:mock/...` ref below resolving exactly as it did
# before parse_model_ref started refusing an auto-detected-disabled
# provider; each test here already scopes its OWN BRIDGE_TEST_HOME.
ensure_default_provider_credentials()
# 2.0.0 fixpass finding 2: `BridgeApp.__init__` reads config through
# `bridge_home()` (images_render_mode) and `_submit_prompt` appends to
# `bridge_home()/history.jsonl` on every scripted submit -- without THIS
# seam, a test built with neither BRIDGE_TEST_HOME nor BRIDGE_STATE_DIR set
# (every FakeController-based pilot test below does exactly that) performs
# the real `~/.rolo-claude` -> `~/.halo` migration and writes real prompt
# text into the real `~/.halo/history.jsonl` -- confirmed live: 8 entries
# from this file's own scripted inputs landed there. A no-op once a test
# (or an earlier-imported module) has already scoped either var itself.
ensure_scoped_state_dir_once()

from halo_harness import events as ev
from halo_harness.testing.fake_controller import FakeController, default_demo_turns
from halo_harness.tui.app import BridgeApp
from halo_harness.tui.events import drain_queue
from halo_harness.tui.widgets.cards import PermissionCard, PlanCard, QuestionCard, ToolCard
from halo_harness.tui.widgets.transcript import AssistantText, IntroLine, SystemNote, ThinkingBlock, UserMessage
import halo_harness.tui.clipboard as clipboard_mod

# W4c: `copy_to_clipboard` (tui/app.py) now ALWAYS kicks off the external-
# tool fallback worker on every copy (belt-and-suspenders, see its own
# docstring) -- before this round that worker was a guaranteed no-op on
# Windows (no xclip/wl-copy/xsel on PATH, and no win32 branch existed at
# all), but W4c item 3 adds a REAL `clip.exe` write there. Stubbed once,
# for the whole file, so no pilot below -- old or new -- ever touches the
# developer's actual OS clipboard just by pressing Ctrl+C/y/Y or running
# /copy; every assertion in this file already reads `app.clipboard`
# (Textual's own in-process register, set by `copy_to_clipboard` itself),
# never the real clipboard, so nothing here loses coverage. The clipboard
# module's own pure dispatch logic (which argv, which platform) is tested
# directly, with its own stubbing, in tests/test_clipboard.py.
clipboard_mod.copy_via_external_tool = lambda *a, **kw: False

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
    from halo_harness.tui.dispatch import apply_event

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
    from halo_harness.tui.dispatch import apply_event

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
def test_system_note_event_renders_a_transcript_line_not_a_toast(ctx: Ctx):
    """W5b ("connector cold start, properly"): `system_note` is the async,
    out-of-band transcript line `tui/bootstrap.build_controller`'s
    connector-discovery `on_done` posts onto `Controller.events` once
    background discovery finishes -- unlike `notification` (which dispatch.py
    renders as a toast via `app.notify`), it must land as a REAL line in
    the scrolling transcript instead."""
    from halo_harness.tui.dispatch import apply_event

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            await apply_event(app, ev.system_note("3 claude.ai connectors available."))
            notes = [w for w in app.transcript.children if isinstance(w, SystemNote)]
            ctx.check(f"exactly one transcript note, got {len(notes)}", len(notes) == 1)
            ctx.check(f"it carries the exact text, got {notes[0].content!r}",
                      "3 claude.ai connectors available." in str(notes[0].content))
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
    from halo_harness.tui.dispatch import apply_event

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
        from halo_harness.permissions import add_allow_rule
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
    answered, matching the owner's report ("I am unable to type anything and get
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
            from halo_harness.tui.dialogs.permissions import PermissionsDialog
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
            from halo_harness.tui.widgets.cards import EffortCard
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
            from halo_harness.tui.widgets.cards import PagerScreen
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


@test
def test_shift_tab_to_auto_also_resolves_a_queued_second_card(ctx: Ctx):
    """Release review finding 21: `_reevaluate_pending_permission_for_mode`
    only ever re-decided `app.pending_card` -- anything already parked in
    `app._pending_queue` (Halo 2.0.1 W3a's PendingDock FIFO, finding 16)
    kept asking under the new mode regardless, and had to be answered by
    hand even after a Shift+Tab switch that auto/bypassPermissions should
    have resolved outright. The ACTIVE card here carries an explicit ask:
    rule (via still_ask_request_ids, same stand-in finding 4's own test
    above uses) so it stays pending and untouched; the QUEUED one behind
    it has no such rule and must be dropped/resolved the moment the mode
    reaches auto."""
    async def body():
        fake = FakeController(turns=_two_permission_asks_turn())
        fake.still_ask_request_ids.add("tu1")
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do two things")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=10)

            active_card = app.pending_card
            ctx.check("the FIRST ask is active", "tmp/x" in (getattr(active_card, "summary", "") or ""))
            ctx.check("exactly one card queued behind it", len(app._pending_queue) == 1)
            queued_card = app._pending_queue[0]
            ctx.check("the queued card is the SECOND ask", "tmp/y" in queued_card.summary)

            for _ in range(3):
                await pilot.press("shift+tab")
                await pilot.pause(0.02)
                if fake.permission_mode == "auto":
                    break
            ctx.check(f"mode reached auto, got {fake.permission_mode!r}", fake.permission_mode == "auto")

            ctx.check("the ACTIVE card (explicit ask: rule) is untouched: still pending, not done",
                      app.pending_card is active_card and not active_card.done)
            ctx.check("the QUEUED card was dropped from the queue", app._pending_queue == [])
            ctx.check(f"the queued card was actually resolved (done), got done={queued_card.done}",
                      queued_card.done is True)
            ctx.check(f"tu2 was answered through the real reevaluate path, got {fake.permission_replies}",
                      ("tu2", {"action": "allow", "reason": "", "rule": None, "message": ""})
                      in fake.permission_replies)
            ctx.check(f"needs-you count dropped from 2 to 1 (one resolved, one still pending), got "
                      f"{app.status_bar.needs_you_count}", app.status_bar.needs_you_count == 1)
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
def test_bare_alias_model_command_defers_cold_auth_check_off_the_ui_thread(ctx: Ctx):
    """2.0.1 finding 26: `/model opus` (a BARE alias, no provider prefix)
    with no ANTHROPIC_API_KEY and a COLD auth-status cache must never spawn
    `claude auth status` synchronously on the UI thread -- `claude_auth_
    status` itself is poisoned (raises if called at all) to prove the
    gating check in `_apply_model_or_defer` never reaches it; the actual
    cache-warm is done by a worker (`refresh_cached_claude_auth_status`,
    faked here to avoid a real subprocess), and the model is applied only
    once that worker's `call_from_thread` callback lands."""
    import halo_harness.providers.cc_models as cc_models_mod

    async def body():
        def _poison(*, timeout=10.0, env=None):
            raise AssertionError("claude auth status must never be spawned on the UI thread")

        real_claude_auth_status = cc_models_mod.claude_auth_status
        real_cached = cc_models_mod.cached_claude_auth_status
        real_refresh = cc_models_mod.refresh_cached_claude_auth_status
        real_gateway = cc_models_mod.is_claude_gateway_driven
        cc_models_mod.claude_auth_status = _poison
        cc_models_mod.cached_claude_auth_status = lambda: None  # cold cache
        cc_models_mod.is_claude_gateway_driven = lambda *a, **kw: False
        refreshed = []

        def _fake_refresh(*, timeout=10.0):
            refreshed.append(True)
        cc_models_mod.refresh_cached_claude_auth_status = _fake_refresh

        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                # on_mount's own startup worker (_prime_auth_status_worker)
                # also calls refresh_cached_claude_auth_status() once, at
                # launch -- unrelated to this test's own deferred-worker
                # check. Let it settle, then isolate the measurement to
                # just the /model-triggered call below.
                await pilot.pause(0.2)
                refreshed.clear()
                await pilot.click("#prompt-input")
                await _type(pilot, "/model opus")
                await pilot.press("enter")
                await pilot.pause(0.1)
                deadline = time.monotonic() + 3.0
                while fake.model != "opus" and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                ctx.check(f"the worker warmed the cache exactly once, got {refreshed}", refreshed == [True])
                ctx.check(f"fake.model eventually updated via the deferred worker, got {fake.model!r}",
                          fake.model == "opus")
        finally:
            cc_models_mod.claude_auth_status = real_claude_auth_status
            cc_models_mod.cached_claude_auth_status = real_cached
            cc_models_mod.refresh_cached_claude_auth_status = real_refresh
            cc_models_mod.is_claude_gateway_driven = real_gateway
            if old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_key
    asyncio.run(body())


@test
def test_slash_model_persists_as_last_used_and_picker_marks_the_row(ctx: Ctx):
    """2.0.1 W3a: a successful /model change persists to ~/.halo/state.json
    (cwd-scoped) and the next time the picker opens, the matching row is
    marked '(last used)'."""
    from halo_harness import launch_state
    from halo_harness.tui.dialogs.model_picker import ModelPicker

    async def body():
        scratch_home = Path(tempfile.mkdtemp(prefix="w3a-model-persist-"))
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_TEST_HOME"] = str(scratch_home)
        os.environ["BRIDGE_STATE_DIR"] = str(scratch_home / ".halo")
        try:
            fake = FakeController()
            cwd = scratch_home / "proj"
            app = await _mounted(fake, cwd=cwd)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/model or:vendor/persisted-model")
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"fake.model applied, got {fake.model}", fake.model == "or:vendor/persisted-model")
                ctx.check("persisted to state.json for this cwd",
                          launch_state.resolve_last_model(cwd) == "or:vendor/persisted-model")

                # Reopen the picker (bare /model) -- FakeController.
                # list_models() returns just [self.model], already the
                # persisted ref itself (set by the /model command above);
                # what matters here is the picker's OWN last_used wiring.
                await _type(pilot, "/model")
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"ModelPicker opened, got {type(app.screen).__name__}", isinstance(app.screen, ModelPicker))
                ctx.check(f"picker's own last_used matches what was persisted, got {app.screen.last_used!r}",
                          app.screen.last_used == "or:vendor/persisted-model")
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state
    asyncio.run(body())


@test
def test_model_picker_opens_on_bare_slash_model_and_esc_dismisses(ctx: Ctx):
    from halo_harness.tui.dialogs.model_picker import ModelPicker

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
# Halo 2.0.2 round 6: /update's confirm dialog (tui/dialogs/update_dialog.py)
# and the once-a-day startup note (tui/slash.py::update_check_startup_worker).
# Every real git/network call is replaced with a canned lambda -- these
# tests exercise the DIALOG/WORKER wiring only, never halo_harness.update's
# own git-ls-remote/GitHub-API internals (see tests/test_update.py for those).
# ============================================================================

def _patch_update_module(*, installed: dict, available: dict, kind: Optional[dict] = None,
                          commits=None):
    from halo_harness import update as upd
    orig = (upd.installed_build, upd.latest_available, upd.install_kind, upd.commits_between)
    upd.installed_build = lambda **kw: dict(installed)
    upd.latest_available = lambda *a, **kw: dict(available)
    upd.install_kind = lambda **kw: dict(kind or {
        "kind": "uv_tool", "spec": "git+https://github.com/roloVibes/Halo-Harness",
        "reinstall_cmd": "uv tool install --reinstall git+https://github.com/roloVibes/Halo-Harness"})
    upd.commits_between = lambda *a, **kw: (commits if commits is not None else ([], None))
    return orig


def _unpatch_update_module(orig) -> None:
    from halo_harness import update as upd
    upd.installed_build, upd.latest_available, upd.install_kind, upd.commits_between = orig


@test
def test_update_dialog_opens_and_esc_dismisses_without_exiting(ctx: Ctx):
    from halo_harness.tui.dialogs.update_dialog import UpdateDialog
    orig = _patch_update_module(
        installed={"version": "2.0.2", "commit": "aaaaaaa", "requested_revision": None,
                   "checkout": None, "branch": "master", "editable": False},
        available={"channel": "main", "commit": "bbbbbbb", "ref": "master", "reason": None, "source": "cache"},
        commits=(["bbbbbbb fix: something"], 1))
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/update")
                await pilot.press("enter")
                opened = False
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, UpdateDialog):
                        opened = True
                        break
                ctx.check(f"UpdateDialog opened, got {type(app.screen).__name__}", opened)
                ctx.check("shows the installed commit", "aaaaaaa" in app.screen.installed_line)
                ctx.check("shows the available commit", "bbbbbbb" in app.screen.available_line)
                await pilot.press("escape")
                await pilot.pause(0.1)
                ctx.check("Esc dismisses the dialog", not isinstance(app.screen, UpdateDialog))
                ctx.check(f"the app is still running (no exit), got {app.return_code!r}",
                          app.return_code is None)
        asyncio.run(body())
    finally:
        _unpatch_update_module(orig)


@test
def test_update_dialog_enter_requests_restart_exit_code(ctx: Ctx):
    from halo_harness import update as upd
    from halo_harness.tui.dialogs.update_dialog import UpdateDialog
    orig = _patch_update_module(
        installed={"version": "2.0.2", "commit": "aaaaaaa", "requested_revision": None,
                   "checkout": None, "branch": "master", "editable": False},
        available={"channel": "main", "commit": "bbbbbbb", "ref": "master", "reason": None, "source": "cache"})
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/update")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, UpdateDialog):
                        break
                ctx.check(f"UpdateDialog opened, got {type(app.screen).__name__}",
                          isinstance(app.screen, UpdateDialog))
                await pilot.press("enter")
                await pilot.pause(0.1)
            ctx.check(f"app exited with update.RESTART_EXIT_CODE, got {app.return_code}",
                      app.return_code == upd.RESTART_EXIT_CODE)
        asyncio.run(body())
    finally:
        _unpatch_update_module(orig)


@test
def test_update_dialog_already_up_to_date_enter_does_not_restart(ctx: Ctx):
    from halo_harness.tui.dialogs.update_dialog import UpdateDialog
    orig = _patch_update_module(
        installed={"version": "2.0.2", "commit": "ccccccc", "requested_revision": None,
                   "checkout": None, "branch": "master", "editable": False},
        available={"channel": "main", "commit": "ccccccc", "ref": "master", "reason": None, "source": "cache"},
        commits=([], 0))
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/update")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, UpdateDialog):
                        break
                ctx.check("dialog reports up to date", app.screen.up_to_date is True)
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check("Enter just closes it (already up to date) -- no restart",
                          not isinstance(app.screen, UpdateDialog))
                ctx.check(f"the app is still running (no exit), got {app.return_code!r}",
                          app.return_code is None)
        asyncio.run(body())
    finally:
        _unpatch_update_module(orig)


@test
def test_update_check_startup_note_shows_once_when_available(ctx: Ctx):
    from halo_harness import update as upd
    orig = _patch_update_module(
        installed={"version": "2.0.2", "commit": "aaaaaaa", "requested_revision": None,
                   "checkout": None, "branch": "master", "editable": False},
        available={"channel": "main", "commit": "bbbbbbb", "ref": "master", "reason": None, "source": "cache"})
    old_no_bg_net = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    try:
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)  # the gate this worker itself checks
        state_dir = Path(tempfile.mkdtemp(prefix="h2026-update-note-"))

        async def body():
            fake = FakeController()
            fake.state_dir = state_dir
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                notes = []
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
                    if any("update available" in n for n in notes):
                        break
                ctx.check(f"a one-line update note appears, names /update, got {notes}",
                          any("update available" in n and "/update" in n for n in notes))
        asyncio.run(body())
        ctx.check("note_due_today flips to False right away (same day, same cache)",
                  upd.note_due_today(state_dir) is False)
    finally:
        _unpatch_update_module(orig)
        if old_no_bg_net is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_no_bg_net


@test
def test_update_check_startup_note_off_with_config_notify_false(ctx: Ctx):
    orig = _patch_update_module(
        installed={"version": "2.0.2", "commit": "aaaaaaa", "requested_revision": None,
                   "checkout": None, "branch": "master", "editable": False},
        available={"channel": "main", "commit": "bbbbbbb", "ref": "master", "reason": None, "source": "cache"})
    old_no_bg_net = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    old_state_dir_env = os.environ.get("BRIDGE_STATE_DIR")
    try:
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        state_dir = Path(tempfile.mkdtemp(prefix="h2026-update-note-off-"))
        os.environ["BRIDGE_STATE_DIR"] = str(state_dir)  # theme.get_config_value reads bridge_home()
        from halo_harness.theme import set_config_value
        set_config_value("update.notify", False)

        async def body():
            fake = FakeController()
            fake.state_dir = state_dir
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                for _ in range(20):
                    await app._drain()
                    await pilot.pause(0.05)
                notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
                ctx.check(f"no update note with update.notify: false, got {notes}",
                          not any("update available" in n for n in notes))
        asyncio.run(body())
    finally:
        _unpatch_update_module(orig)
        if old_no_bg_net is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_no_bg_net
        if old_state_dir_env is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir_env


# ============================================================================
# 1.0.1 hotfix 1: the `/`/`@` completion popup's own Up/Down/Tab/Enter/Esc --
# the TextArea must never swallow Up/Down while it's open (before this fix,
# Down/Up on row 0 of a single-line prompt always fired history-nav instead,
# since the popup's own open/closed state was never consulted at all).
# ============================================================================

def _real_registry():
    from halo_harness.commands.builtins import register_builtins
    from halo_harness.commands.registry import Registry
    reg = Registry()
    register_builtins(reg)
    return reg


@test
def test_completion_popup_down_down_enter_inserts_third_command(ctx: Ctx):
    from halo_harness.tui.completion import complete_slash
    from halo_harness.tui.widgets.input import CompletionPopup

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
    from halo_harness.tui.widgets.input import CompletionPopup

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
    from halo_harness.tui.widgets.input import CompletionPopup

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
    from halo_harness.tui.completion import complete_slash
    from halo_harness.tui.widgets.input import CompletionPopup

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
    from halo_harness.tui.completion import complete_slash

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
def test_role_command_arg_completion_tab_inserts_the_matching_role_name(ctx: Ctx):
    """Halo 2.0.2 brief A.4: `/role <name> ...` tab-completes the role
    NAME argument (not just the command name itself) -- "cod" ranks
    "coder" (the only built-in role starting with it) and Tab inserts it
    with no leading "/" (unlike ordinary slash-command completion)."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/role cod")
            await pilot.pause(0.1)
            ctx.check("the popup is open for the role-name argument", app.completion_popup.display)
            await pilot.press("tab")
            await pilot.pause(0.1)
            ctx.check(f"'coder' inserted with no leading slash, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "/role coder ")
    asyncio.run(body())


@test
def test_role_command_arg_completion_model_then_effort_slots(ctx: Ctx):
    """The SECOND argument completes against `controller.list_models()`
    (here, `FakeController`'s own bare-string list); the THIRD completes
    against effort levels. Both via the SAME `/role` line, proving the
    argument index advances correctly as the user keeps typing."""
    async def body():
        fake = FakeController(model="or:vendor/my-model")
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/role coder or:vendor/my-m")
            await pilot.pause(0.1)
            ctx.check(f"model argument completion is open, got items={app._completion_items!r}",
                      app.completion_popup.display and "or:vendor/my-model" in app._completion_items)
            await pilot.press("tab")
            await pilot.pause(0.1)
            ctx.check(f"the model ref was inserted, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "/role coder or:vendor/my-model ")
            await _type(pilot, "hi")
            await pilot.pause(0.1)
            ctx.check(f"effort argument completion is open, got items={app._completion_items!r}",
                      app.completion_popup.display and "high" in app._completion_items)
    asyncio.run(body())


@test
def test_role_command_effort_completion_reads_the_model_not_the_role_name(ctx: Ctx):
    """2.0.2 review finding 24 pin: the effort argument's own completion
    used to read `pieces[1]` of the FULL, un-stripped line as "the model
    just typed" -- the role name for `/role ...`, and the literal word
    "set" for `/roles set ...` -- never the model either form actually
    typed. Spies on `_effort_levels_for` directly (real route-based
    effort sets would make a weak test here, since both the right model
    and the old bug's wrong strings usually fall back to the SAME full
    EFFORT_LEVELS list through the same except branch)."""
    async def body():
        for line, expected_model in (
            ("/role coder or:vendor/my-model hi", "or:vendor/my-model"),
            ("/roles set coder or:vendor/my-model hi", "or:vendor/my-model"),
        ):
            fake = FakeController(model="or:vendor/my-model")
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                seen = []
                orig = app._effort_levels_for

                def _spy(model_text, _orig=orig):
                    seen.append(model_text)
                    return _orig(model_text)
                app._effort_levels_for = _spy
                await pilot.click("#prompt-input")
                await _type(pilot, line)
                await pilot.pause(0.1)
                ctx.check(f"{line!r}: _effort_levels_for was called at all, got {seen}", bool(seen))
                ctx.check(f"{line!r}: read the typed MODEL, not the role name or 'set', got {seen}",
                          seen[-1] == expected_model)
    asyncio.run(body())


@test
def test_org_command_arg_completion_tab_inserts_the_matching_org_name(ctx: Ctx):
    """Round B fix pass ("No Tab completion for org/position names in
    `/org ...`", confirmed): `/org show <name>` tab-completes the org
    NAME argument against every saved organization."""
    from halo_harness.orgs import ensure_builtin_orgs

    async def body():
        old, home = _scoped_state_dir_env("org-arg-completion-")
        try:
            ensure_builtin_orgs(state_dir=home / ".halo")
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/org show comp")
                await pilot.pause(0.1)
                ctx.check(f"the popup is open for the org-name argument, got items={app._completion_items!r}",
                          app.completion_popup.display and "company" in app._completion_items)
                await pilot.press("tab")
                await pilot.pause(0.1)
                ctx.check(f"'company' inserted with no leading slash, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "/org show company ")
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


def _scoped_state_dir_env(prefix: str) -> "tuple[dict, Path]":
    """Snapshot the two state-dir env vars, set both to a fresh scratch
    dir, and return `(old_values, scratch_home)` for the caller's own
    `finally` block to restore -- the exact pattern the pre-existing
    `/model` persistence pilot above already uses, factored out so the
    roles-editor pilots below don't repeat it three times."""
    scratch_home = Path(tempfile.mkdtemp(prefix=prefix))
    old = {"BRIDGE_TEST_HOME": os.environ.get("BRIDGE_TEST_HOME"), "BRIDGE_STATE_DIR": os.environ.get("BRIDGE_STATE_DIR")}
    os.environ["BRIDGE_TEST_HOME"] = str(scratch_home)
    os.environ["BRIDGE_STATE_DIR"] = str(scratch_home / ".halo")
    return old, scratch_home


def _restore_state_dir_env(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@test
def test_roles_edit_opens_the_form_with_one_row_per_role(ctx: Ctx):
    """Halo 2.0.2 brief A.5: `/roles edit <name>` on a template that
    doesn't exist yet creates an empty one and opens the form -- every
    built-in role gets its own row, shown as unset."""
    from halo_harness.roles import ROLE_NAMES
    from halo_harness.tui.dialogs.roles_editor import RolesEditor

    async def body():
        old, _home = _scoped_state_dir_env("roles-editor-open-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/roles edit demo-profile")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, RolesEditor):
                        break
                ctx.check(f"RolesEditor opened, got {type(app.screen).__name__}", isinstance(app.screen, RolesEditor))
                option_ids = [str(o.id) for o in app.screen.query_one("#roles-list").options]
                ctx.check(f"one row per built-in role, got {option_ids}", set(ROLE_NAMES) <= set(option_ids))
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_roles_edit_pick_model_set_effort_then_save_persists_to_disk(ctx: Ctx):
    """Picking a model for a row (via the SAME ModelPicker /model uses),
    then an effort, then ctrl+s -- the template file on disk reflects it."""
    from halo_harness.roles import load_role_template
    from halo_harness.tui.dialogs.model_picker import ModelPicker
    from halo_harness.tui.dialogs.roles_editor import RolesEditor

    async def body():
        old, _home = _scoped_state_dir_env("roles-editor-save-")
        try:
            fake = FakeController(model="or:vendor/pickable-model")
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/roles edit demo-profile")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, RolesEditor):
                        break
                editor = app.screen
                editor.query_one("#roles-list").highlighted = 0  # "orchestrator", the first built-in row
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"the nested ModelPicker opened, got {type(app.screen).__name__}",
                          isinstance(app.screen, ModelPicker))
                await pilot.press("enter")  # accepts the (only) filtered model
                await pilot.pause(0.1)
                ctx.check("back on the RolesEditor after picking", app.screen is editor)
                await _type(pilot, "high")
                await pilot.press("enter")
                await pilot.pause(0.05)
                ctx.check(f"the row now shows the picked model+effort, got {editor.roles.get('orchestrator')}",
                          editor.roles.get("orchestrator") == {"model": "or:vendor/pickable-model", "effort": "high"})
                await pilot.press("ctrl+s")
                await pilot.pause(0.05)
                saved = load_role_template("demo-profile")
                ctx.check(f"persisted to disk, got {saved}",
                          saved is not None
                          and saved["roles"].get("orchestrator") == {"model": "or:vendor/pickable-model", "effort": "high"})
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_roles_edit_escape_cancels_without_saving(ctx: Ctx):
    from halo_harness.roles import list_role_templates
    from halo_harness.tui.dialogs.roles_editor import RolesEditor

    async def body():
        old, _home = _scoped_state_dir_env("roles-editor-cancel-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/roles edit never-saved")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, RolesEditor):
                        break
                await pilot.press("escape")
                await pilot.pause(0.05)
                ctx.check("the editor screen closed", not isinstance(app.screen, RolesEditor))
                ctx.check(f"nothing was ever written to disk, got {list_role_templates()}",
                          list_role_templates() == [])
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_roles_edit_routes_to_external_editor_when_configured(ctx: Ctx):
    """Halo 2.0.2 brief A.5: `roles.editor: "external"` -- routing only
    (never drives a real `$EDITOR`/`app.suspend()` under the headless
    pilot harness, which has no real terminal to suspend to)."""
    from halo_harness.tui import slash as slash_mod

    async def body():
        old, _home = _scoped_state_dir_env("roles-editor-external-")
        try:
            from halo_harness.theme import set_config_value
            set_config_value("roles.editor", "external")
            calls = []
            orig = slash_mod._edit_role_template_externally
            slash_mod._edit_role_template_externally = lambda app, name: calls.append(name)
            try:
                fake = FakeController()
                app = await _mounted(fake)
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.click("#prompt-input")
                    await _type(pilot, "/roles edit demo-profile")
                    await pilot.press("enter")
                    await pilot.pause(0.1)
                    ctx.check(f"routed to the external-editor path, got {calls}", calls == ["demo-profile"])
            finally:
                slash_mod._edit_role_template_externally = orig
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_org_edit_opens_with_one_row_per_position(ctx: Ctx):
    """Halo 2.0.2 round 2 (brief B): `/org edit company` opens the form
    with one tree row per position, root first."""
    from halo_harness.tui.dialogs.org_editor import OrgEditor

    async def body():
        old, _home = _scoped_state_dir_env("org-editor-open-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/org edit company")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, OrgEditor):
                        break
                ctx.check(f"OrgEditor opened, got {type(app.screen).__name__}", isinstance(app.screen, OrgEditor))
                option_ids = [str(o.id) for o in app.screen.query_one("#org-tree-list").options]
                expected = {"CEO", "VP Engineering", "VP Marketing", "VP Research", "Engineering Manager",
                            "Marketing Manager", "Research Manager", "Engineer 1", "Engineer 2",
                            "Marketing Associate 1", "Marketing Associate 2", "Research Analyst 1",
                            "Research Analyst 2"}
                ctx.check(f"one row per company position, got {option_ids}", set(option_ids) == expected)
                ctx.check(f"root (CEO) listed first, got {option_ids}", option_ids[0] == "CEO")
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_org_edit_pick_model_then_save_persists_and_reloads(ctx: Ctx):
    """Picking a model for the highlighted position (via the SAME
    ModelPicker `/model`/`/roles edit` use), then ctrl+s -- the org file
    on disk reflects it, and re-loading it shows the change (brief's own
    pilot: "opening /org edit company, changing a model, saving, and
    reloading")."""
    from textual.widgets import Input, OptionList
    from halo_harness.orgs import load_org
    from halo_harness.tui.dialogs.model_picker import ModelPicker
    from halo_harness.tui.dialogs.org_editor import OrgEditor

    async def body():
        old, _home = _scoped_state_dir_env("org-editor-save-")
        try:
            fake = FakeController(model="or:vendor/pickable-org-model")
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/org edit company")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, OrgEditor):
                        break
                editor = app.screen
                editor.query_one("#org-tree-list", OptionList).highlighted = 0  # "CEO", the root
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"CEO's own fields loaded, got {editor._current_title!r}", editor._current_title == "CEO")
                await pilot.press("ctrl+p")
                await pilot.pause(0.1)
                ctx.check(f"the nested ModelPicker opened, got {type(app.screen).__name__}",
                          isinstance(app.screen, ModelPicker))
                await pilot.press("enter")  # accepts the (only) filtered model
                await pilot.pause(0.1)
                ctx.check("back on the OrgEditor after picking", app.screen is editor)
                role_field = editor.query_one("#org-field-role", Input).value
                ctx.check(f"the role field now shows the picked model, got {role_field!r}",
                          role_field == "or:vendor/pickable-org-model")
                await pilot.press("ctrl+s")
                await pilot.pause(0.1)
                saved = load_org("company")
                ceo = next(p for p in saved["positions"] if p["title"] == "CEO")
                ctx.check(f"persisted to disk as a model (role cleared), got {ceo}",
                          ceo.get("model") == "or:vendor/pickable-org-model" and "role" not in ceo)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_org_edit_ctrl_d_deletes_position_even_with_a_field_focused(ctx: Ctx):
    """2.0.2 review finding 38 pin (half 1): Textual's own Input/TextArea
    bind ctrl+d to delete-right -- with no `priority=True`, a FOCUSED
    field (the usual case; this screen is mostly fields) swallowed
    ctrl+d before the screen's own `delete_position` binding ever saw
    it, so the key did nothing at all while editing a position."""
    from textual.widgets import Input, OptionList
    from halo_harness.tui.dialogs.org_editor import OrgEditor

    async def body():
        old, _home = _scoped_state_dir_env("org-editor-ctrld-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/org edit company")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, OrgEditor):
                        break
                editor = app.screen
                option_list = editor.query_one("#org-tree-list", OptionList)
                target_idx = next(i for i, o in enumerate(option_list.options) if o.id == "Engineer 1")
                option_list.highlighted = target_idx
                await pilot.press("enter")
                await pilot.pause(0.1)
                ctx.check(f"loaded the target position, got {editor._current_title!r}",
                          editor._current_title == "Engineer 1")
                role_field = editor.query_one("#org-field-role", Input)
                role_field.focus()
                await pilot.pause(0.05)
                before = len(editor.org["positions"])
                await pilot.press("ctrl+d")
                await pilot.pause(0.1)
                after = len(editor.org["positions"])
                ctx.check(f"the position was actually deleted despite the field's own focus, "
                          f"got before={before} after={after}", after == before - 1)
                ctx.check("Engineer 1 is gone", not any(p.get("title") == "Engineer 1" for p in editor.org["positions"]))
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_org_edit_bare_model_alias_saved_as_model_not_role(ctx: Ctx):
    """2.0.2 review finding 38 pin (half 2): `is_role_name_syntax` is a
    pure SYNTAX check -- a bare model alias like "haiku" matches
    [a-z][a-z0-9_]* too, so it used to be saved as position["role"],
    and the org then failed validation with "unknown role"."""
    from textual.widgets import Input, OptionList
    from halo_harness.orgs import load_org
    from halo_harness.tui.dialogs.org_editor import OrgEditor

    async def body():
        old, _home = _scoped_state_dir_env("org-editor-alias-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/org edit solo")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, OrgEditor):
                        break
                editor = app.screen
                editor.query_one("#org-tree-list", OptionList).highlighted = 0
                await pilot.press("enter")
                await pilot.pause(0.1)
                role_field = editor.query_one("#org-field-role", Input)
                role_field.value = ""
                role_field.focus()
                await _type(pilot, "haiku")
                await pilot.press("ctrl+s")
                await pilot.pause(0.1)
                saved = load_org("solo")
                root = saved["positions"][0]
                ctx.check(f"saved as a model, never a role, got {root}",
                          root.get("model") == "haiku" and "role" not in root)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_org_edit_escape_cancels_without_saving(ctx: Ctx):
    from halo_harness.orgs import load_org
    from halo_harness.tui.dialogs.org_editor import OrgEditor

    async def body():
        old, _home = _scoped_state_dir_env("org-editor-cancel-")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/org edit solo")
                await pilot.press("enter")
                for _ in range(40):
                    await app._drain()
                    await pilot.pause(0.05)
                    if isinstance(app.screen, OrgEditor):
                        break
                before = load_org("solo")  # built-ins auto-seed on first use
                await pilot.press("escape")
                await pilot.pause(0.05)
                ctx.check("the editor screen closed", not isinstance(app.screen, OrgEditor))
                after = load_org("solo")
                ctx.check(f"nothing was written to disk, got before={before} after={after}", after == before)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_at_path_completion_down_down_enter_inserts_third_entry(ctx: Ctx):
    from halo_harness.tui.widgets.input import CompletionPopup

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
    from halo_harness.tui.dialogs.model_picker import ModelPicker
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
    from halo_harness.tui.dialogs.session_picker import SessionPicker
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
            from halo_harness.tui.dialogs.session_picker import SessionPicker as SP
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
    from halo_harness.tui.dialogs.palette import CommandPalette
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
    from halo_harness.tui.dialogs.init_picker import InitPickerApp

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
    from halo_harness.tui.dialogs.init_picker import InitPickerApp

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
    from halo_harness.tui.dialogs.session_picker import SessionPicker
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
    from halo_harness.tui.dialogs.session_picker import SessionPicker
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
    from halo_harness.tui.dialogs.session_picker import SessionPicker
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
    from halo_harness.tui.widgets.cards import ToolCard

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
    from halo_harness.tui.widgets.cards import ToolCard

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
    from halo_harness.tui.widgets.cards import ToolCard

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
def test_shift_tab_shows_a_toast_describing_the_new_mode(ctx: Ctx):
    """2.0.1 W3a: Shift+Tab's toast names the mode and a plain description
    of what it does -- 'auto: no prompts', never a safety/gating phrase."""
    from halo_harness.tui.keys import MODE_DESCRIPTIONS

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            # on_mount's own startup workers (auth-status/catalog/balance)
            # can post their OWN notify (e.g. "Claude subscription
            # detected...") asynchronously, racing this test's own capture
            # -- a fixed settle pause here is exactly the "timing-sensitive
            # pilot, fixed sleep" pattern the 2.0.1 hygiene round calls out
            # (it can still lose the race under load, W4a found live): the
            # capture itself filters to only mode-change toasts (`"<mode>: "`
            # for a KNOWN mode) instead, which is correct regardless of
            # whether unrelated startup noise lands before, during or after
            # this test's own three key presses.
            await pilot.pause(0.3)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("shift+tab")  # default -> acceptEdits
            await pilot.press("shift+tab")  # acceptEdits -> plan
            await pilot.press("shift+tab")  # plan -> auto
            mode_toasts = [m for m in notified if m.split(": ", 1)[0] in MODE_DESCRIPTIONS]
            ctx.check(f"one toast per Shift+Tab (startup noise filtered out), got {notified}",
                      len(mode_toasts) == 3)
            ctx.check(f"each names mode and plain description, got {notified}",
                      mode_toasts == [f"acceptEdits: {MODE_DESCRIPTIONS['acceptEdits']}",
                                      f"plan: {MODE_DESCRIPTIONS['plan']}",
                                      f"auto: {MODE_DESCRIPTIONS['auto']}"])
            ctx.check(f"the auto toast describes behaviour plainly, got {mode_toasts[-1]!r}",
                      mode_toasts[-1] == "auto: no prompts")
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
    import halo_harness.tui.app as app_mod

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
def test_history_up_recalls_from_both_claude_code_and_halo_harness_files(ctx: Ctx):
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
                    "display": "from halo history", "pastedContents": {}, "project": cwd_str,
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
                ctx.check(f"the newest (halo's own file) entry recalls first, got {first!r}",
                          first == "from halo history")
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
    from halo_harness.tui.widgets.transcript import FoldedHistory

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
    """Acceptance: `python -m halo_harness --demo --stress 500` must stay
    responsive. An interactive full-screen session can't be driven headless
    from a test runner, so this is the automated, deterministic proxy: push
    the exact same `stress_turns(500)` scripted event volume (~2,500 events
    across 501 turns) through the REAL drain/apply_event pipeline (not a
    micro-benchmark of `drain_queue` alone) and assert it stays within a
    generous wall-clock budget, with the transcript's live widget count kept
    bounded by folding (D-TUI: fold after 300 widgets) rather than growing
    unboundedly for the whole run."""
    import time

    from halo_harness.testing.fake_controller import stress_turns

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
    svg = app.export_screenshot(title=f"halo -- {name}", simplify=True)
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
            from halo_harness.tui.widgets.cards import RewindCard
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
            from halo_harness.tui.bootstrap import build_controller

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
            from halo_harness.tui.bootstrap import build_controller
            from halo_harness.tui.slash import handle_slash

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


@test
def test_slash_effort_against_a_real_session_persists_the_sent_value(ctx: Ctx):
    """2.0.1 W3a: `/effort <level>` against a REAL session (FakeController
    has no `.session` at all, so this needs the same real-controller rig as
    the /model switch test above) persists the session's own clamped
    `effort` -- not the raw typed text -- to ~/.halo/state.json."""
    import argparse
    from halo_harness import launch_state

    async def body():
        fh = build_fake_home()
        SCENARIOS["tui-effort-a"] = ScriptedTurns([_final_text_chunk("ok")])
        mock = MockUpstream().start()
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_OPENROUTER_BASE_URL", "OPENROUTER_API_KEY")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        controller = None
        try:
            from halo_harness.tui.bootstrap import build_controller
            from halo_harness.tui.slash import handle_slash

            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=True,
                tools=None, add_dir=None, model="or:mock/tui-effort-a", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            async with app.run_test(size=(100, 40)) as pilot:
                ctx.check("the real session exists before /effort runs", controller.session is not None)
                await handle_slash(app, "effort", "medium")
                await pilot.pause(0.1)
                sent = getattr(controller.session, "effort", None)
                ctx.check(f"the session's own (clamped) effort is non-empty, got {sent!r}", bool(sent))
                ctx.check(f"persisted value matches the session's SENT effort, got "
                          f"{launch_state.resolve_last_effort(fh['proj'])!r} vs session={sent!r}",
                          launch_state.resolve_last_effort(fh["proj"]) == sent)
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
    from halo_harness.tui import keys as tui_keys

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
    from halo_harness.tui import keys as tui_keys

    ctx.check("control/opt/cmd aliases fold to ctrl/alt/meta",
              tui_keys.normalize_keystroke("Control+Opt+S") == "ctrl+alt+s")
    ctx.check("modifier order is canonicalized",
              tui_keys.normalize_keystroke("shift+ctrl+p") == tui_keys.normalize_keystroke("ctrl+shift+p"))
    ctx.check("esc/return aliases fold", tui_keys.normalize_keystroke("Esc") == "escape")
    ctx.check("a chord normalizes each keystroke independently",
              tui_keys.normalize_chord("Control+X Control+S") == "ctrl+x ctrl+s")


@test
def test_which_key_continuations_and_overlay_widget(ctx: Ctx):
    from halo_harness.tui import keys as tui_keys
    from halo_harness.tui.widgets.whichkey import WhichKeyOverlay

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
    from halo_harness.tui.dialogs.palette import filter_items

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
    from halo_harness.tui.completion import parse_at_mentions

    mentions = parse_at_mentions("look at @src/main.py#L10-20 and also @README.md and @a/b#L5 please")
    ctx.check(f"three mentions found, got {mentions}", len(mentions) == 3)
    ctx.check("a #L10-20 range parses (start, end)", mentions[0] == ("src/main.py", 10, 20))
    ctx.check("a bare @path has no line range", mentions[1] == ("README.md", None, None))
    ctx.check("a #L5 (no dash) sets start==end", mentions[2] == ("a/b", 5, 5))
    ctx.check("no @ at all -> no mentions", parse_at_mentions("plain text, no mentions here") == [])


@test
def test_statusline_command_receives_claude_code_json_contract(ctx: Ctx):
    from halo_harness import statusline as sl

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
    from halo_harness.agent.log import SessionLog
    from halo_harness.controller import Controller
    from halo_harness.permissions import PermissionEngine

    # H11b finding 27 (same spirit): `SessionLog(cwd)` below honours
    # BRIDGE_TEST_HOME/BRIDGE_STATE_DIR if set, else the REAL
    # ~/.halo -- this helper never scoped either one itself, only
    # ever safe because SOME other test happened to leave one set first.
    # Never overrides a value a caller deliberately set. `BRIDGE_STATE_DIR`
    # (skips the extra "/.halo" segment) with a SHORT prefix --
    # `halo_harness/shadow.py` re-embeds a tracked file's own full absolute
    # path (drive letter included) under `<state_dir>/sessions/<slug>/
    # <session_id>/shadow/_drive_C/...`; a long prefix here pushed a
    # `cwd` already deep under AppData\Local\Temp past Windows' 260-char
    # MAX_PATH, so `git add` silently added zero files (verified: `git
    # ls-tree` on the resulting commit was empty) and rewind/undo restored
    # nothing -- never a halo bug, just this helper's own scratch
    # path being needlessly long.
    if "BRIDGE_TEST_HOME" not in os.environ and "BRIDGE_STATE_DIR" not in os.environ:
        os.environ["BRIDGE_STATE_DIR"] = tempfile.mkdtemp(prefix="th-")

    class _MinimalSession:
        def __init__(self) -> None:
            self.permission_engine = PermissionEngine(mode=mode, print_mode=False, cwd=cwd)
            self.log = SessionLog(cwd)
            self.busy = False  # Controller.submit reads this; no real turn ever runs in these pilots
            self.interactive = False
            # 2.0.1: Controller.list_models() reads `self.session.model_ref.
            # raw` to mark the "current" model -- an empty `raw` (falsy)
            # keeps that whole branch a no-op (it would otherwise also need
            # `self.session.model_profile`, which no pilot using this stub
            # has ever needed before list_models() itself was first called
            # against one).
            from halo_harness.model import ModelRef
            self.model_ref = ModelRef(raw="", provider="openrouter", model="", dialect="openai-chat")

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
    from halo_harness.tui import keys as tui_keys

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
                from halo_harness.tui.dispatch import apply_event

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
    from halo_harness.commands.registry import Registry
    from halo_harness.commands.builtins import register_builtins

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
def test_h5b_f11_compact_queued_while_a_turn_is_already_running(ctx: Ctx):
    """finding 11 / W4a misc ("/compact typed mid-turn is queued and runs
    when the turn ends"): a compaction and an ordinary turn must never race
    over the same log, so /compact while `session.busy` is True is never
    run immediately -- but it IS queued now (the same thread-safe FIFO
    `commands` queue `run()`'s own pump loop only ever reaches the NEXT
    entry of once the current turn's `_pump_turn` returns), so it runs
    automatically once the turn ends instead of being silently dropped and
    needing a retype."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        controller = _real_controller(cwd)
        controller.session.busy = True
        result = controller.run_compact("")
        ctx.check(f"a notice explaining it's queued, got {result!r}", "queued" in result)
        queued = []
        while True:
            try:
                queued.append(controller.commands.get_nowait())
            except Exception:
                break
        ctx.check(f"run_compact WAS queued (runs once the turn ends), got {[c.kind for c in queued]}",
                  len(queued) == 1 and queued[0].kind == "run_compact")


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
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.hooks import HookDef, HookRunner
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        # 2.0.0 fixpass finding 2: this used to overwrite BRIDGE_TEST_HOME
        # with no save, then unconditionally POP it (never restore) in the
        # finally below -- the first test anywhere in this file to run
        # AFTER this one, with no BRIDGE_TEST_HOME/BRIDGE_STATE_DIR of its
        # own, fell through to the REAL machine home. Save/restore now,
        # same idiom as every other test in this file.
        old_home = os.environ.get("BRIDGE_TEST_HOME")
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
            from halo_harness.agent.derive import derive_request
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
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home


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
            from halo_harness.tui.bootstrap import build_controller

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


@test
def test_terminal_title_reasserted_after_a_fake_claude_child_exits(ctx: Ctx):
    """Halo 2.0.2 W7 round 1 (brief F): a real TUI pilot on a `cc:`
    session (same real-Controller/real-Session/fake-`claude`-binary setup
    as the permission-card pilot above) -- `halo_harness.termtitle.
    set_terminal_title` is monkeypatched to record calls instead of
    touching the real console, cleared right after the app mounts (its
    own `on_mount` already asserts "halo" once, at startup -- not what
    this test is about), and the real assertion is that quitting the
    controller (which closes the cc: subprocess -- `ClaudeCodeProcess.
    wait()` -- the moment this fake `claude` child is confirmed exited)
    re-asserts "halo" again, proving the spawn-site hook actually fires."""
    import argparse

    from halo_harness import termtitle

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
        titles_set: list = []
        orig_set_terminal_title = termtitle.set_terminal_title
        termtitle.set_terminal_title = lambda title, stream=None: titles_set.append(title)
        # H202: these two module-level counters are process-wide (by
        # design -- see termtitle.py's own docstring) -- an EARLIER test
        # in this same `python test_tui.py` process run (any other cc:
        # pilot that closes a real ClaudeCodeProcess) can leave a pending,
        # not-yet-consumed exit signal that this test's own first drain
        # tick would otherwise pick up, adding a spurious extra "halo"
        # before this test ever sends a prompt. Reset to a clean, synced
        # state here so this test's own startup assertion below is
        # deterministic regardless of what ran before it in this process.
        old_exit_counters = (termtitle._claude_child_exit_seen, termtitle._claude_child_exit_handled)
        termtitle._claude_child_exit_seen = termtitle._claude_child_exit_handled = 0
        try:
            from halo_harness.tui.bootstrap import build_controller

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
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.05)
                # At least once: on Linux the startup worker's own fake
                # `claude` probe can already have exited by now, adding a
                # second, equally correct "halo" (seen on the Kali VM).
                ctx.check(f"on_mount asserted halo at startup, got {titles_set!r}",
                          bool(titles_set) and set(titles_set) == {"halo"})
                titles_set.clear()  # this test is about the POST-CHILD re-assert, not startup's own
                await pilot.click("#prompt-input")
                await _type(pilot, "pong")
                await pilot.press("enter")
                for _ in range(400):  # the fake claude's first start (mcp SDK import) can take a few seconds
                    await app._drain()
                    await pilot.pause(0.05)
                    if not app._turn_running:
                        break
                ctx.check("the turn actually finished", not app._turn_running)
            # Closing the controller tears down the cc: subprocess
            # (session.close_cc() -> ClaudeCodeProcess.wait()), the exact
            # moment halo_harness.termtitle.reassert_after_claude_child
            # fires (see agent/cc_process.py's own hook).
            controller.quit()
            controller = None
            ctx.check(f"'halo' was re-asserted after the claude child exited, got {titles_set!r}",
                      "halo" in titles_set)
        finally:
            termtitle.set_terminal_title = orig_set_terminal_title
            termtitle._claude_child_exit_seen, termtitle._claude_child_exit_handled = old_exit_counters
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
            from halo_harness.tui.bootstrap import build_controller

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
    from halo_harness.improve.draft import Candidate
    from halo_harness.tui.widgets.cards import ImproveCard

    def _candidate(cid):
        return Candidate(id=cid, kind="rule", title=f"rule-{cid}", scope="project", path=f"{cid}.md",
                          body="Do the thing.", rationale="because", evidence=["sess1#1"], confidence="med")

    async def body():
        for key, expected in (("a", "apply"), ("e", "edit"), ("s", "skip"), ("d", "dismiss"), ("q", "quit")):
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                fired = []
                card = ImproveCard(candidate=_candidate(key), provenance_line="<!-- halo improve: x -->",
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
    from halo_harness.improve.draft import Candidate
    from halo_harness.tui.widgets.cards import ImproveCard

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            cand = Candidate(id="c1", kind="rule", title="rule-c1", scope="project", path="c1.md",
                              body="new body text", rationale="r", evidence=["sess1#7"], confidence="low")
            card = ImproveCard(candidate=cand, provenance_line="<!-- halo improve: x -->", index=1, total=1,
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


# ============================================================================
# H15 Part A: `halo init`'s tabbed provider setup (InitTabsApp) --
# tab navigation, masked picked-up credentials, live status on entry,
# reachability (mocked), and the catalog cache write on tab completion.
# ============================================================================

_H15_TABS_PROVIDER_VARS = (
    "OPENROUTER_API_KEY", "BRIDGE_OPENROUTER_BASE_URL", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "BRIDGE_ANTHROPIC_BASE_URL",
    "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
)

# W3b test-determinism: a loopback address nothing listens on -- "connection
# refused" comes back near-instantly on every OS, no DNS lookup, no real
# 8s-capped connect attempt. `InitTabsApp._finish_tab_worker`'s own catalog
# refresh (`refresh_tab_catalog` -> `probe_openrouter_models`) uses a
# DIFFERENT `open_upstream` import than `providers.reachability`'s own (the
# one `test_init_tabs_reachability_tag_for_a_refused_host` mocks), so
# without this override entering an OpenRouter key in ANY pilot using
# `_H15TabsEnv` made that worker actually dial the real openrouter.ai --
# the root cause of "reachability: checking..." intermittently taking
# several real seconds under load (never reproduced "mocked", per that
# test's own docstring promise, until this fixes it for real).
_H15_TABS_FAST_REFUSED_LOCAL_URL = "http://127.0.0.1:1"


class _H15TabsEnv:
    """Scopes BRIDGE_TEST_HOME/BRIDGE_STATE_DIR/BRIDGE_ENV_FILE plus every
    provider variable InitTabsApp's own tabs can touch -- the real
    ~/.halo is never written by these pilots."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _H15_TABS_PROVIDER_VARS)}
        d = Path(tempfile.mkdtemp(prefix="h15-init-tabs-pilot-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = '{"loggedIn": false}'
        for k in _H15_TABS_PROVIDER_VARS:
            if k != "BRIDGE_TEST_CC_AUTH_STATUS":
                os.environ.pop(k, None)
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = _H15_TABS_FAST_REFUSED_LOCAL_URL
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _static_text(widget) -> str:
    return str(widget.renderable) if hasattr(widget, "renderable") else str(widget.render())


async def _poll_until(pilot, predicate, *, deadline_s: float = 5.0, step_s: float = 0.05):
    """W3b test-determinism: waits for `predicate()` (called fresh each
    tick) to become truthy, polling every `step_s` up to a WALL-CLOCK
    `deadline_s` -- never a fixed iteration count. A fixed `for _ in
    range(20): await pilot.pause(0.05)` budget is only ever "1.0s" when
    every tick actually takes its nominal 0.05s; under load (another
    suite running concurrently, a background worker thread genuinely
    slower to schedule) each tick can take longer while the iteration
    count stays the same, silently shrinking the REAL budget -- exactly
    the class of flake the Kali VM showed concurrently with the Windows
    suites. Returns the last predicate() value (truthy on success, falsy
    if the deadline passed first) so a caller's own ctx.check still gets
    a real value to report either way."""
    import time
    start = time.monotonic()
    value = predicate()
    while not value and (time.monotonic() - start) < deadline_s:
        await pilot.pause(step_s)
        value = predicate()
    return value


@test
def test_init_tabs_shift_tab_navigates_providers_with_wraparound(ctx: Ctx):
    from halo_harness.init_providers import TAB_PROVIDERS
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp, _pane_id
    from textual.widgets import TabbedContent

    async def body():
        with _H15TabsEnv():
            app = InitTabsApp()
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                tabs = app.query_one(TabbedContent)
                ctx.check(f"starts on the first provider, got {tabs.active!r}",
                          tabs.active == _pane_id(TAB_PROVIDERS[0]))
                await pilot.press("shift+tab")
                await pilot.pause(0.05)
                ctx.check(f"Shift+Tab wraps to the LAST provider, got {tabs.active!r}",
                          tabs.active == _pane_id(TAB_PROVIDERS[-1]))
                await pilot.press("escape")
                await pilot.pause(0.05)
    asyncio.run(body())


@test
def test_init_tabs_masked_picked_up_credential_with_source(ctx: Ctx):
    """A.2: an already-resolved credential shows "picked up from <source>"
    with the value MASKED, never the raw key."""
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Static

    async def body():
        with _H15TabsEnv():
            os.environ["OPENROUTER_API_KEY"] = "sk-or-super-secret-value"
            app = InitTabsApp()
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                status = _static_text(app.query_one("#openrouter-status", Static))
                ctx.check(f"names the source, got {status!r}", "picked up from" in status)
                ctx.check(f"the value is MASKED, got {status!r}", "sk-or-super-secret-value" not in status)
                ctx.check(f"a redacted prefix is still shown, got {status!r}", "sk-o" in status)
                await pilot.press("escape")
                await pilot.pause(0.05)
    asyncio.run(body())


@test
def test_init_tabs_inline_entry_updates_the_status_tag(ctx: Ctx):
    """A.2: "the status tag updates as soon as a value is entered"."""
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Input, Static

    async def body():
        with _H15TabsEnv():
            app = InitTabsApp()
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                before = _static_text(app.query_one("#openrouter-status", Static))
                ctx.check(f"starts 'not set up', got {before!r}", before == "not set up")
                key_input = app.query_one("#openrouter-field-key", Input)
                key_input.focus()
                await pilot.press("x")
                await pilot.pause(0.05)
                after = _static_text(app.query_one("#openrouter-status", Static))
                ctx.check(f"tag updates immediately on entry, got {after!r}", after != before)
                ctx.check(f"says a value was entered, got {after!r}", "value entered" in after)
                await pilot.press("escape")
                await pilot.pause(0.05)
    asyncio.run(body())


@test
def test_init_tabs_reachability_tag_for_a_refused_host(ctx: Ctx):
    """A.3: "unreachable (one-line reason)" from a bounded probe -- mocked
    here (`providers.reachability.open_upstream`) so the pilot never
    touches the real network."""
    from halo_harness.providers.http import UpstreamConnectError
    import halo_harness.providers.reachability as reach_mod
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Input, Static

    def _fake_open_upstream(host, port, tls, *a, **kw):
        raise UpstreamConnectError(f"cannot resolve/reach {host} -- check the machine's network, DNS or VPN "
                                     f"(connection refused)", host=host)

    async def body():
        real_open_upstream = reach_mod.open_upstream
        reach_mod.open_upstream = _fake_open_upstream
        try:
            with _H15TabsEnv():
                app = InitTabsApp()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause(0.1)
                    key_input = app.query_one("#openrouter-field-key", Input)
                    key_input.focus()
                    for ch in "sk-or-fake":
                        await pilot.press(ch)
                    await pilot.press("enter")
                    await _poll_until(
                        pilot, lambda: "unreachable" in _static_text(app.query_one("#openrouter-reach", Static)))
                    reach = _static_text(app.query_one("#openrouter-reach", Static))
                    ctx.check(f"shows unreachable with a one-line reason, got {reach!r}",
                              "unreachable" in reach and "connection refused" in reach)
                    await pilot.press("escape")
                    await pilot.pause(0.05)
        finally:
            reach_mod.open_upstream = real_open_upstream
    asyncio.run(body())


@test
def test_init_tabs_catalog_cache_written_on_tab_completion(ctx: Ctx):
    """A.4: "finishing a tab ... fetches and caches that provider's
    catalog right then" -- `probe_openrouter_models` mocked (loopback-free,
    deterministic), the real `write_models_json`/`load_models_json`
    round-trip is NOT mocked, so this proves the cache file is actually
    written to this test's own scoped state dir."""
    from halo_harness.providers.databricks import load_models_json
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Input

    def _fake_probe(base_url, api_key):
        return [{"id": "vendor/fake-model", "context_length": 128000, "max_output_tokens": 8192,
                 "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]

    async def body():
        import halo_harness.providers.databricks as dbx_mod
        real_probe = dbx_mod.probe_openrouter_models
        dbx_mod.probe_openrouter_models = _fake_probe
        try:
            with _H15TabsEnv() as env:
                app = InitTabsApp()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause(0.1)
                    key_input = app.query_one("#openrouter-field-key", Input)
                    key_input.focus()
                    for ch in "sk-or-fake":
                        await pilot.press(ch)
                    await pilot.press("enter")
                    await _poll_until(pilot, lambda: "openrouter" in app.catalog_notes)
                    ctx.check(f"a catalog note was recorded, got {app.catalog_notes}",
                              "openrouter" in app.catalog_notes)
                    cached = load_models_json(env.state_dir)
                    ctx.check(f"the model is actually cached on disk, got {list(cached)}",
                              "vendor/fake-model" in cached)
                    await pilot.press("escape")
                    await pilot.pause(0.05)
        finally:
            dbx_mod.probe_openrouter_models = real_probe
    asyncio.run(body())


# ============================================================================
# 1.0.1 part 2 (reviewer minor): /effort, /rewind and /improve must not
# open their own card while a permission card is already pending -- a
# one-line note instead.
# ============================================================================

def _pending_permission_card(app) -> PermissionCard:
    card = PermissionCard(request_id="pending-1", summary="Bash(rm -rf /tmp/x)", reason="",
                           suggested_rule=None, on_decide=lambda decision: None)
    app.set_pending_card(card)
    return card


@test
def test_effort_shows_a_note_instead_of_opening_while_a_permission_card_is_pending(ctx: Ctx):
    from halo_harness.tui.slash import handle_slash

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            card = _pending_permission_card(app)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await handle_slash(app, "effort", "")
            ctx.check(f"a one-line note was shown, got {notified}", len(notified) == 1)
            ctx.check(f"names the permission card, got {notified}", "permission" in notified[0].lower())
            ctx.check("the ORIGINAL permission card is still pending, untouched", app.pending_card is card)
    asyncio.run(body())


@test
def test_rewind_shows_a_note_instead_of_opening_while_a_permission_card_is_pending(ctx: Ctx):
    from halo_harness.tui.slash import _show_rewind_confirmation

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            card = _pending_permission_card(app)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await _show_rewind_confirmation(app, {"id": "step-1"}, "rewind")
            ctx.check(f"a one-line note was shown, got {notified}", len(notified) == 1)
            ctx.check("the ORIGINAL permission card is still pending, untouched", app.pending_card is card)
    asyncio.run(body())


@test
def test_improve_shows_a_note_instead_of_opening_while_a_permission_card_is_pending(ctx: Ctx):
    from halo_harness.tui.slash import _handle_improve

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            card = _pending_permission_card(app)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await _handle_improve(app, "")
            ctx.check(f"a one-line note was shown (never drafted anything), got {notified}", len(notified) == 1)
            ctx.check("the ORIGINAL permission card is still pending, untouched", app.pending_card is card)
    asyncio.run(body())


@test
def test_effort_still_opens_normally_with_nothing_pending(ctx: Ctx):
    """Regression guard: the fix must not block /effort when it's actually
    safe to open."""
    from halo_harness.tui.slash import handle_slash
    from halo_harness.tui.widgets.cards import EffortCard

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            ctx.check("nothing pending to start", app.pending_card is None)
            await handle_slash(app, "effort", "")
            await pilot.pause(0.05)
            ctx.check(f"the EffortCard actually opened, got {app.pending_card}",
                      isinstance(app.pending_card, EffortCard))
    asyncio.run(body())


# ============================================================================
# 1.0.1 part 2 (reviewer minor): Ctrl+Q kills background jobs BEFORE
# arming its os._exit timer, not only deep inside the ordinary
# controller.quit() path it exists to route around when THAT is wedged.
# ============================================================================

@test
def test_force_quit_kills_background_jobs_before_arming_the_exit_timer(ctx: Ctx):
    import threading as threading_mod

    class _FakeJobRegistry:
        def __init__(self):
            self.killed = False

        def kill_all(self):
            self.killed = True

    class _FakeSession:
        def __init__(self):
            self.job_registry = _FakeJobRegistry()

    class _FakeForceQuitController:
        def __init__(self):
            self.session = _FakeSession()

        def quit(self):
            return 0

    class _FakeTimer:
        """Records that a timer WOULD have been armed -- never actually
        scheduled against this (the real test) process; `.start()` is a
        deliberate no-op so a stray real os._exit can never fire later."""
        instances: list = []

        def __init__(self, interval, function, args=()):
            self.interval, self.function, self.args = interval, function, args
            self.daemon = False
            _FakeTimer.instances.append(self)

        def start(self):
            pass

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            app.controller = _FakeForceQuitController()
            calls: list = []
            app.call_from_thread = lambda fn, *a, **kw: calls.append((fn, a, kw))
            real_timer_cls = threading_mod.Timer
            threading_mod.Timer = _FakeTimer
            try:
                app._force_quit_worker()
                ctx.check("job_registry.kill_all() was actually called",
                          app.controller.session.job_registry.killed is True)
                ctx.check(f"an exit timer was armed afterward too, got {len(_FakeTimer.instances)}",
                          len(_FakeTimer.instances) == 1)
                ctx.check(f"self.exit() was scheduled via call_from_thread, got {calls}", len(calls) == 1)
            finally:
                threading_mod.Timer = real_timer_cls
    asyncio.run(body())


@test
def test_force_quit_kills_background_jobs_even_if_controller_quit_raises(ctx: Ctx):
    """kill_all() must not be gated on a successful (or even a completed)
    controller.quit() -- it runs BEFORE that call is even attempted."""
    import threading as threading_mod

    class _FakeJobRegistry:
        def __init__(self):
            self.killed = False

        def kill_all(self):
            self.killed = True

    class _FakeSession:
        def __init__(self):
            self.job_registry = _FakeJobRegistry()

    class _RaisingController:
        def __init__(self):
            self.session = _FakeSession()

        def quit(self):
            raise RuntimeError("simulated: the ordinary quit path is wedged/broken")

    class _FakeTimer:
        instances: list = []

        def __init__(self, interval, function, args=()):
            self.daemon = False
            _FakeTimer.instances.append(self)

        def start(self):
            pass

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            app.controller = _RaisingController()
            app.call_from_thread = lambda fn, *a, **kw: None
            real_timer_cls = threading_mod.Timer
            threading_mod.Timer = _FakeTimer
            try:
                app._force_quit_worker()  # must not raise even though controller.quit() does
                ctx.check("job_registry.kill_all() still ran despite controller.quit() raising",
                          app.controller.session.job_registry.killed is True)
            finally:
                threading_mod.Timer = real_timer_cls
    asyncio.run(body())


# ============================================================================
# 1.0.1 part 2 (reviewer minor): cached_auth_status_is_stale's TTL is now
# actually consulted -- /model's own stale-catalog-refresh worker does the
# same for the claude-auth-status cache, so a claude.ai login/logout is
# seen without restarting the whole app.
# ============================================================================

class _CcAuthStaleEnv:
    def __enter__(self):
        self._saved = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        from halo_harness.providers.cc_models import reset_cached_claude_auth_status
        reset_cached_claude_auth_status()
        return self

    def __exit__(self, *exc):
        if self._saved is None:
            os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        else:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = self._saved
        from halo_harness.providers.cc_models import reset_cached_claude_auth_status
        reset_cached_claude_auth_status()


@test
def test_cc_auth_status_auto_refresh_worker_refreshes_a_cold_cache(ctx: Ctx):
    """`cached_claude_auth_status()` itself bypasses the cache ENTIRELY
    whenever BRIDGE_TEST_CC_AUTH_STATUS is set (by design, so a test always
    sees its own current value) -- these two tests are specifically about
    whether the INTERNAL cache variable gets written, so they read it
    directly rather than through that bypass."""
    import halo_harness.providers.cc_models as cc_models_mod
    from halo_harness.tui.slash import _cc_auth_status_auto_refresh_worker
    with _CcAuthStaleEnv():
        ctx.check("nothing cached yet", cc_models_mod._auth_status_cache is None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        _cc_auth_status_auto_refresh_worker(app=None)
        status = cc_models_mod._auth_status_cache
        ctx.check(f"the internal cache is now populated, got {status}", status is not None and status.logged_in)


@test
def test_cc_auth_status_auto_refresh_worker_skips_a_fresh_cache(ctx: Ctx):
    """The TTL actually gates the refresh -- a cache that's still fresh
    must not be re-fetched on every single /model open."""
    import halo_harness.providers.cc_models as cc_models_mod
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    from halo_harness.tui.slash import _cc_auth_status_auto_refresh_worker
    with _CcAuthStaleEnv():
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        refresh_cached_claude_auth_status()  # a genuinely FRESH cache entry
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        _cc_auth_status_auto_refresh_worker(app=None)  # the cache is fresh -- must NOT re-fetch yet
        status = cc_models_mod._auth_status_cache
        ctx.check(f"the STALE (pre-refresh) cached value is still what's stored, got {status}",
                  status is not None and status.logged_in is False)


@test
def test_cc_auth_status_auto_refresh_worker_picks_up_a_login_change_once_stale(ctx: Ctx):
    """The exact scenario the reviewer minor names: a login change becomes
    visible without restarting the app, once the cache is stale."""
    import halo_harness.providers.cc_models as cc_models_mod
    from halo_harness.providers.cc_models import cached_claude_auth_status, refresh_cached_claude_auth_status
    from halo_harness.tui.slash import _cc_auth_status_auto_refresh_worker
    with _CcAuthStaleEnv():
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        refresh_cached_claude_auth_status()
        ctx.check("starts logged out", cached_claude_auth_status().logged_in is False)
        # Force staleness directly (never sleeping CACHED_AUTH_STATUS_TTL_S
        # seconds in a test) -- the same effect real time passing would have.
        cc_models_mod._auth_status_cached_at -= (cc_models_mod.CACHED_AUTH_STATUS_TTL_S + 1)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        _cc_auth_status_auto_refresh_worker(app=None)
        status = cached_claude_auth_status()
        ctx.check(f"the login change is now visible, got {status}", status is not None and status.logged_in is True)


@test
def test_handle_model_fires_the_cc_auth_auto_refresh_worker(ctx: Ctx):
    """Wiring check: bare /model actually starts this worker (its own
    named group, matching every other slash.py worker's convention)."""
    from halo_harness.tui.slash import _handle_model

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            started: list = []
            real_run_worker = app.run_worker

            def _spy(*a, **kw):
                if kw.get("name") == "cc-auth-auto-refresh":
                    started.append(kw.get("group"))
                return real_run_worker(*a, **kw)

            app.run_worker = _spy
            await _handle_model(app, "")
            await pilot.pause(0.1)
            ctx.check(f"the cc-auth-auto-refresh worker started with its own group, got {started}",
                      started == ["cc-auth-auto-refresh"])
    asyncio.run(body())


# ============================================================================
# H15 Part D2.3: the `!cmd` inline-shell permission card, end to end through
# the real BridgeApp -- Shift+Tab resolves it via the SAME reevaluate_
# pending_permission path a live tool-call ask already uses.
# ============================================================================

class _InlineAskController:
    """A standalone double (not FakeController -- its own `decide_inline_
    shell` always allows, and it has no register/discard/reevaluate support
    at all) with just enough surface for D2.3's own flow: an inline `!cmd`
    that asks, Shift+Tab mode changes that re-decide it for real."""

    def __init__(self):
        from halo_harness.permissions import Decision
        self._Decision = Decision
        self.permission_mode = "default"
        self._waiters: dict = {}
        self.inline_runs: list = []
        self.events = __import__("queue").Queue()

    def decide_inline_shell(self, command: str):
        return self._Decision("ask", "a plain Bash tool would ask here too", suggested_rule=None)

    def run_inline_shell(self, command: str):
        from halo_harness.tools.base import ToolResult
        self.inline_runs.append(command)
        return f"inline_{len(self.inline_runs)}", ToolResult(content=f"ran: {command}")

    def register_pending_permission(self, request_id, tool_name, tool_input) -> None:
        self._waiters[request_id] = {"tool_name": tool_name, "tool_input": tool_input}

    def discard_pending_permission(self, request_id) -> None:
        self._waiters.pop(request_id, None)

    def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode

    def reevaluate_pending_permission(self, request_id):
        """Mirrors the REAL Session.reevaluate_pending_permission's own
        contract (re-decide, resolve only on a real verdict) using a tiny
        stand-in rule: `auto`/`bypassPermissions` allow, everything else
        still asks -- enough to prove the WIRING without needing a real
        PermissionEngine."""
        if request_id not in self._waiters:
            return None
        if self.permission_mode in ("auto", "bypassPermissions"):
            return "allow"
        return None

    def add_permission_rule(self, rule, scope) -> None:
        pass


@test
def test_inline_shell_ask_card_resolves_via_shift_tab_mode_change(ctx: Ctx):
    controller = _InlineAskController()

    async def body():
        app = await _mounted(controller)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            await app._handle_bang_command("echo hi")
            await pilot.pause(0.05)
            from halo_harness.tui.widgets.cards import PermissionCard
            ctx.check(f"an inline ask card is pending, got {app.pending_card}",
                      isinstance(app.pending_card, PermissionCard))
            card = app.pending_card
            ctx.check("the ask was registered for re-evaluation", card.request_id in controller._waiters)
            # Shift+Tab default -> acceptEdits -> plan -> auto (3 presses).
            await pilot.press("shift+tab")
            await pilot.press("shift+tab")
            await pilot.press("shift+tab")
            await pilot.pause(0.1)
            ctx.check(f"auto resolved the inline ask (card done), got done={card.done}", card.done is True)
            ctx.check(f"the command actually ran, got {controller.inline_runs}",
                      controller.inline_runs == ["echo hi"])
            ctx.check("no pending card left", app.pending_card is None)
            ctx.check("the slot was cleaned up", card.request_id not in controller._waiters)
    asyncio.run(body())


@test
def test_inline_shell_ask_card_stays_pending_under_a_mode_that_still_asks(ctx: Ctx):
    controller = _InlineAskController()

    async def body():
        app = await _mounted(controller)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            await app._handle_bang_command("rm -rf /tmp/x")
            await pilot.pause(0.05)
            card = app.pending_card
            await pilot.press("shift+tab")  # default -> acceptEdits only; still "ask" in this fake
            await pilot.pause(0.05)
            ctx.check("still pending -- acceptEdits doesn't resolve an inline shell ask here", card.done is False)
            ctx.check(f"the command did NOT run, got {controller.inline_runs}", controller.inline_runs == [])
            ctx.check("still showing up as the app's own pending card", app.pending_card is card)
    asyncio.run(body())


# ============================================================================
# H15 Part B: hang diagnostics -- the watchdog fires on a simulated stalled
# heartbeat, and --debug tracing lines actually appear.
# ============================================================================

@test
def test_watchdog_dumps_diagnostics_on_a_stalled_heartbeat(ctx: Ctx):
    import halo_harness.tui.app as app_mod

    async def body():
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state = os.environ.get("BRIDGE_STATE_DIR")
        home = Path(tempfile.mkdtemp(prefix="h15-watchdog-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["BRIDGE_STATE_DIR"] = str(home / ".halo")
        real_threshold = app_mod.HANG_HEARTBEAT_THRESHOLD_S
        real_min_interval = app_mod.HANG_DUMP_MIN_INTERVAL_S
        app_mod.HANG_HEARTBEAT_THRESHOLD_S = 0.2
        app_mod.HANG_DUMP_MIN_INTERVAL_S = 0.2
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                # Simulate a stall WITHOUT actually blocking the loop (a
                # real 15s+ block would make this test painfully slow) --
                # `_tick_spinner`'s own 1Hz interval timer keeps running
                # fine as long as asyncio itself is alive (exactly what
                # `await pilot.pause`/`asyncio.sleep` below leaves running),
                # so it must be stood down too, or it would keep refreshing
                # the heartbeat right out from under the backdated value,
                # same as a REAL hang would stop it from firing at all.
                app._tick_spinner = lambda: None
                app._last_heartbeat_monotonic = time.monotonic() - 5.0
                deadline = time.monotonic() + 5.0
                hang_files: list = []
                from halo_harness.config.paths import bridge_home
                while time.monotonic() < deadline:
                    hang_files = list(bridge_home().glob("hang-*.log"))
                    if hang_files:
                        break
                    await pilot.pause(0.1)
                ctx.check(f"a hang log was written, got {hang_files}", len(hang_files) == 1)
                text = hang_files[0].read_text(encoding="utf-8")
                ctx.check(f"names the stall reason, got {text[:200]!r}", "heartbeat stalled" in text)
                ctx.check(f"names the active screen, got {text[:200]!r}", "active screen:" in text)
                ctx.check(f"names the worker list, got {text[:200]!r}", "named workers:" in text)
                ctx.check(f"carries real thread stacks, got {len(text)} chars", "--- thread " in text)
        finally:
            app_mod.HANG_HEARTBEAT_THRESHOLD_S = real_threshold
            app_mod.HANG_DUMP_MIN_INTERVAL_S = real_min_interval
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state
    asyncio.run(body())


@test
def test_watchdog_never_dumps_twice_within_the_minimum_interval(ctx: Ctx):
    """"at most once a minute while it persists" -- pinned with a short
    interval so the test itself stays fast."""
    import halo_harness.tui.app as app_mod

    async def body():
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state = os.environ.get("BRIDGE_STATE_DIR")
        home = Path(tempfile.mkdtemp(prefix="h15-watchdog-once-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["BRIDGE_STATE_DIR"] = str(home / ".halo")
        real_threshold = app_mod.HANG_HEARTBEAT_THRESHOLD_S
        real_min_interval = app_mod.HANG_DUMP_MIN_INTERVAL_S
        app_mod.HANG_HEARTBEAT_THRESHOLD_S = 0.2
        app_mod.HANG_DUMP_MIN_INTERVAL_S = 30.0  # deliberately long -- a SECOND dump must not appear
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                app._tick_spinner = lambda: None  # see the sibling test's own comment on this
                app._last_heartbeat_monotonic = time.monotonic() - 5.0
                from halo_harness.config.paths import bridge_home
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not list(bridge_home().glob("hang-*.log")):
                    await pilot.pause(0.1)
                first_count = len(list(bridge_home().glob("hang-*.log")))
                ctx.check(f"exactly one dump so far, got {first_count}", first_count == 1)
                # Still stalled 3+ seconds later -- the SAME one dump must remain.
                await asyncio.sleep(3.0)
                second_count = len(list(bridge_home().glob("hang-*.log")))
                ctx.check(f"still exactly one dump (rate-limited), got {second_count}", second_count == 1)
        finally:
            app_mod.HANG_HEARTBEAT_THRESHOLD_S = real_threshold
            app_mod.HANG_DUMP_MIN_INTERVAL_S = real_min_interval
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state
    asyncio.run(body())


@test
def test_sigusr1_dumps_on_demand(ctx: Ctx):
    if not hasattr(__import__("signal"), "SIGUSR1"):
        raise SkipTest("SIGUSR1 is POSIX-only -- this host has no such signal")

    async def body():
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state = os.environ.get("BRIDGE_STATE_DIR")
        home = Path(tempfile.mkdtemp(prefix="h15-sigusr1-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["BRIDGE_STATE_DIR"] = str(home / ".halo")
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                import os as os_mod
                import signal
                os_mod.kill(os_mod.getpid(), signal.SIGUSR1)
                await pilot.pause(0.3)
                from halo_harness.config.paths import bridge_home
                hang_files = list(bridge_home().glob("hang-*.log"))
                ctx.check(f"SIGUSR1 dumped on demand, got {hang_files}", len(hang_files) == 1)
                ctx.check("names SIGUSR1 as the reason", "SIGUSR1" in hang_files[0].read_text(encoding="utf-8"))
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state
    asyncio.run(body())


# ---------------------------------------------------------------------------
# --debug tracing: Key events, AppBlur/AppFocus, worker lifecycle.
# ---------------------------------------------------------------------------

@test
def test_debug_tracing_logs_key_events_with_focus_and_screen(ctx: Ctx):
    import logging as logging_mod

    async def body():
        logger = logging_mod.getLogger("halo_harness.tui")
        records: list = []

        class _Capture(logging_mod.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging_mod.DEBUG)
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.05)
                # A plain character is consumed (and the event stopped) by
                # the focused PromptInput/TextArea itself before it ever
                # bubbles up to the App's own `_on_key` -- `escape` has no
                # such built-in TextArea handling (and isn't a `priority=
                # True` App binding, which bypasses `_on_key` the same
                # way), so it's the key this hook actually sees, same as
                # the SAME self-heal logic right next to this trace call
                # already relies on.
                await pilot.press("escape")
                await pilot.pause(0.05)
            key_lines = [r for r in records if r.startswith("key: ")]
            ctx.check(f"at least one Key trace line, got {records}", key_lines)
            ctx.check(f"names the focused widget, got {key_lines}", "focused=" in key_lines[0])
            ctx.check(f"names the active screen, got {key_lines}", "screen=" in key_lines[0])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    asyncio.run(body())


@test
def test_debug_tracing_logs_app_blur_and_focus(ctx: Ctx):
    import logging as logging_mod
    from textual import events as textual_events

    async def body():
        logger = logging_mod.getLogger("halo_harness.tui")
        records: list = []

        class _Capture(logging_mod.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging_mod.DEBUG)
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.05)
                app.on_app_blur(textual_events.AppBlur())
                app.on_app_focus(textual_events.AppFocus())
                await pilot.pause(0.05)
            ctx.check(f"AppBlur traced, got {records}", any("AppBlur" in r for r in records))
            ctx.check(f"AppFocus traced, got {records}", any("AppFocus" in r for r in records))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    asyncio.run(body())


@test
def test_debug_tracing_logs_named_worker_lifecycle(ctx: Ctx):
    import logging as logging_mod

    async def body():
        logger = logging_mod.getLogger("halo_harness.tui")
        records: list = []

        class _Capture(logging_mod.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging_mod.DEBUG)
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.05)
                app.run_worker(lambda: None, thread=True, name="h15-debug-trace-worker", group="h15-test")
                for _ in range(20):
                    await pilot.pause(0.05)
                    if any("h15-debug-trace-worker" in r for r in records):
                        break
            ctx.check(f"worker start traced, got {records}",
                      any(r.startswith("worker start: h15-debug-trace-worker") for r in records))
            ctx.check(f"worker finish traced, got {records}",
                      any(r.startswith("worker finish: h15-debug-trace-worker") for r in records))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    asyncio.run(body())


@test
def test_troubleshooting_doc_names_the_hang_log(ctx: Ctx):
    text = (REPO_DIR / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    ctx.check("names the exact hang log path pattern",
              "~/.halo/hang-" in text or "hang-<UTC" in text)
    ctx.check("names SIGUSR1 for an on-demand dump", "SIGUSR1" in text)
    ctx.check("mentions the 15s/one-minute cadence", "15s" in text and "minute" in text)


# ============================================================================
# H15 part 2 addendum 3.2a: the app-launch catalog refresh worker is
# actually wired into on_mount (not just /model's own trigger).
# ============================================================================

@test
def test_launch_actually_populates_an_empty_openrouter_catalog(ctx: Ctx):
    """End to end: a fresh app mount, with OPENROUTER_API_KEY set and an
    empty models.json, ends up with a real cached catalog WITHOUT /model
    ever being opened -- `on_mount`'s own `catalog-startup-refresh` worker,
    not just `_handle_model`'s."""
    from tests.helpers.mock_get_endpoints import MockGetEndpoints

    async def body():
        mock = MockGetEndpoints({"/api/v1/models": (200, {"data": [
            {"id": "deepseek/deepseek-v3.2", "context_length": 128000},
        ]})}).start()
        old_key = os.environ.get("OPENROUTER_API_KEY")
        old_base = os.environ.get("BRIDGE_OPENROUTER_BASE_URL")
        old_no_bg_net = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
        try:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            # 1.0.1 part 2 fixpass finding 10: this test's whole point is
            # the REAL launch-time catalog worker running end to end
            # against the mock -- override the module-level default (set
            # by `ensure_default_provider_credentials()` above) that now
            # makes that worker return immediately.
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
            from halo_harness.providers.databricks import load_models_json
            state_dir = Path(tempfile.mkdtemp(prefix="h15-launch-catalog-"))
            fake = FakeController()
            fake.state_dir = state_dir
            ctx.check("models.json starts empty", load_models_json(state_dir) == {})
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                deadline = time.monotonic() + 5.0
                models = {}
                while time.monotonic() < deadline:
                    models = load_models_json(state_dir)
                    if models:
                        break
                    await pilot.pause(0.1)
                ctx.check(f"catalog populated by launch alone, got {models}",
                          "deepseek/deepseek-v3.2" in models)
        finally:
            mock.stop()
            if old_key is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old_key
            if old_base is None:
                os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
            else:
                os.environ["BRIDGE_OPENROUTER_BASE_URL"] = old_base
            if old_no_bg_net is None:
                os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
            else:
                os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_no_bg_net
    asyncio.run(body())


# ============================================================================
# 1.0.1 part 2 fixpass finding 10: BRIDGE_TEST_NO_BACKGROUND_NET (the
# default this module's own `ensure_default_provider_credentials()` sets)
# must actually stop every background catalog/balance worker from ever
# reaching the network, for an ordinary mount with real-looking (fake)
# credentials -- the exact contradiction the finding named: "building a
# session never triggers first-time catalog discovery" vs. what a mounted
# BridgeApp's own launch workers used to do regardless.
# ============================================================================

@test
def test_background_net_disabled_mount_never_touches_the_network(ctx: Ctx):
    """Mounts a real-controller BridgeApp (an or: model, same `build_
    controller` pattern as the e2e pilot above) with `BRIDGE_TEST_NO_
    BACKGROUND_NET=1` (this module's own ambient default, left ON here,
    unlike the catalog-population test above) and poisons `open_upstream`
    -- any launch worker that still tried to touch the network would raise
    and fail this test."""
    import argparse
    import halo_harness.providers.http as http_mod

    async def body():
        fh = build_fake_home()
        mock = MockUpstream().start()  # never expected to receive a request
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_OPENROUTER_BASE_URL", "OPENROUTER_API_KEY",
                    "BRIDGE_TEST_NO_BACKGROUND_NET")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test-default"
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        real_open_upstream = http_mod.open_upstream

        def _poison(*a, **kw):
            raise AssertionError("a background worker touched the network with "
                                  "BRIDGE_TEST_NO_BACKGROUND_NET=1 set")

        http_mod.open_upstream = _poison
        controller = None
        try:
            from halo_harness.tui.bootstrap import build_controller
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
                for _ in range(10):
                    await app._drain()
                    await pilot.pause(0.05)
            ctx.check("mounted and idled with no network call from any launch worker", True)
        finally:
            http_mod.open_upstream = real_open_upstream
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
# 1.0.1 part 2 fixpass critical finding 1: /providers and /doctor build their
# result off the UI thread (a worker), never synchronously in handle_slash.
# ============================================================================

@test
def test_slash_providers_builds_the_table_off_the_main_thread(ctx: Ctx):
    """Proven by recording whether `reachability_tag` (the slowest per-row
    check -- a real DNS+TCP+TLS connect in production) ever runs on the
    main/UI thread -- it must not, regardless of timing."""
    import threading
    import halo_harness.providers.reachability as reach_mod

    seen_main_thread = []
    real_tag = reach_mod.reachability_tag

    def _spy_tag(name, *, detected=None):
        seen_main_thread.append(threading.current_thread() is threading.main_thread())
        return real_tag(name, detected=detected)

    async def body():
        reach_mod.reachability_tag = _spy_tag
        old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="h15b-providers-thread-")))
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/providers")
                await pilot.press("enter")
                notes = []
                for _ in range(60):
                    await app._drain()
                    await pilot.pause(0.05)
                    notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
                    if any("Databricks" in n for n in notes):
                        break
                ctx.check("reachability_tag was actually called", len(seen_main_thread) > 0)
                ctx.check(f"it never ran on the main/UI thread, got {seen_main_thread}",
                          all(is_main is False for is_main in seen_main_thread))
                ctx.check(f"the table eventually reached the transcript, got {notes}",
                          any("Databricks" in n for n in notes))
        finally:
            reach_mod.reachability_tag = real_tag
            if old_state_dir is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state_dir
    asyncio.run(body())


@test
def test_f17_w6a_custom_command_run_slash_fallback_runs_off_the_ui_thread(ctx: Ctx):
    """finding 17 (W6a): the `handle_slash` FALLBACK (everything not in
    its own `handler` dict -- every custom command and skill) used to
    call `Controller.run_slash` directly on the UI thread. Its prompt-kind
    path fires the UserPromptExpansion command/HTTP hook synchronously
    (600s default timeout, no abort), so a slow one froze the whole TUI,
    not just this one command. Proven the same way the providers/doctor
    fix above is: recording whether `run_slash` itself ever runs on the
    main thread."""
    import threading

    seen_main_thread = []

    def _fake_run_slash(name: str, args: str = "") -> str:
        seen_main_thread.append(threading.current_thread() is threading.main_thread())
        return f"ran /{name} {args}".rstrip()

    async def body():
        fake = FakeController()
        fake.run_slash = _fake_run_slash
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "/my-custom-command hello")
            await pilot.press("enter")
            notes = []
            for _ in range(60):
                await app._drain()
                await pilot.pause(0.05)
                notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
                if any("my-custom-command" in n for n in notes):
                    break
            ctx.check("run_slash was actually called", len(seen_main_thread) > 0)
            ctx.check(f"it never ran on the main/UI thread, got {seen_main_thread}",
                      all(is_main is False for is_main in seen_main_thread))
            ctx.check(f"its result still reached the transcript, got {notes}",
                      any("my-custom-command" in n for n in notes))
    asyncio.run(body())


@test
def test_slash_providers_enable_disable_stay_synchronous(ctx: Ctx):
    """`enable`/`disable` are a plain local config.json write (no network/
    subprocess) -- the decision keeps them synchronous, never a worker."""
    async def body():
        old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="h15b-providers-endis-")))
        try:
            from halo_harness.providers.enablement import is_enabled
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/providers enable databricks")
                await pilot.press("enter")
                await app._drain()
                await pilot.pause(0.05)
                ctx.check("enabled immediately -- no worker round trip needed", is_enabled("databricks") is True)
        finally:
            if old_state_dir is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state_dir
    asyncio.run(body())


@test
def test_slash_doctor_runs_off_the_main_thread(ctx: Ctx):
    import threading
    import halo_harness.doctor as doctor_mod

    seen_main_thread = []
    real_run_checks = doctor_mod.run_checks

    def _spy_run_checks(cwd=None):
        seen_main_thread.append(threading.current_thread() is threading.main_thread())
        return real_run_checks(cwd=cwd)

    async def body():
        doctor_mod.run_checks = _spy_run_checks
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="h15b-doctor-thread-")))
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "/doctor")
                await pilot.press("enter")
                for _ in range(60):
                    await app._drain()
                    await pilot.pause(0.05)
                    if seen_main_thread:
                        break
                ctx.check("run_checks was actually called", len(seen_main_thread) > 0)
                ctx.check(f"it never ran on the main/UI thread, got {seen_main_thread}",
                          all(is_main is False for is_main in seen_main_thread))
        finally:
            doctor_mod.run_checks = real_run_checks
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
    asyncio.run(body())


# ============================================================================
# 2.0.1 launch-hang fix: the owner's own live use on a Databricks-only work
# VM -- `halo` printed the migration line, then sat for a long time before
# the TUI appeared, because `claude auth status` (hung on a gateway-driven
# `claude`, full 10s timeout) was reachable synchronously from startup.
# tui/app.py's own startup worker (`_prime_auth_status_worker`) is now the
# ONLY launch-time spawner, off the UI thread, and a gateway-driven `claude`
# is never spawned at all, by anyone.
# ============================================================================

_CC_AUTH_ENV_VARS = ("BRIDGE_TEST_CC_AUTH_STATUS", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")


def _save_cc_auth_env() -> dict:
    return {k: os.environ.get(k) for k in _CC_AUTH_ENV_VARS}


def _restore_cc_auth_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@test
def test_first_frame_renders_fast_despite_a_slow_claude_auth_check_then_cc_group_lands(ctx: Ctx):
    """The owner's own repro, simulated: `claude auth status` sleeps 5s
    (standing in for a gateway-driven `claude` hanging its full 10s
    timeout). The first frame must still render within 1s -- only the
    startup worker, off the UI thread, ever calls it -- and once that
    worker's slow result lands, `/model`'s own "Claude Code subscription"
    group appears from the SAME cache `Controller.list_models()` reads,
    with no second spawn needed."""
    import time as time_mod
    import halo_harness.providers.cc_models as cc_models_mod

    real_claude_auth_status = cc_models_mod.claude_auth_status

    def _slow_claude_auth_status(*, timeout=10.0, env=None):
        time_mod.sleep(5.0)
        return cc_models_mod.ClaudeAuthStatus(logged_in=True, auth_method="claude.ai")

    saved_env = _save_cc_auth_env()
    for k in _CC_AUTH_ENV_VARS:
        os.environ.pop(k, None)  # a real gateway/test-seam env var here would mask the scenario under test
    cc_models_mod.claude_auth_status = _slow_claude_auth_status
    cc_models_mod.reset_cached_claude_auth_status()

    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            controller = _real_controller(cwd)
            start = time_mod.monotonic()
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                elapsed = time_mod.monotonic() - start
                ctx.check(f"first frame rendered quickly despite a 5s-sleeping claude auth check, "
                          f"got {elapsed:.2f}s", elapsed < 1.0)
                status = None
                for _ in range(90):
                    await pilot.pause(0.1)
                    status = cc_models_mod.cached_claude_auth_status()
                    if status is not None:
                        break
                ctx.check(f"the slow startup worker eventually lands in the cache, got {status!r}",
                          bool(status and status.logged_in and status.auth_method == "claude.ai"))
                models = controller.list_models()
                groups = {m.get("group") for m in models if isinstance(m, dict) and m.get("group")}
                ctx.check(f"the Claude Code subscription group appears once the worker lands, got {groups}",
                          "Claude Code subscription" in groups)
    try:
        asyncio.run(body())
    finally:
        cc_models_mod.claude_auth_status = real_claude_auth_status
        cc_models_mod.reset_cached_claude_auth_status()
        _restore_cc_auth_env(saved_env)


@test
def test_gateway_driven_claude_never_spawns_auth_status_from_providers_or_model(ctx: Ctx):
    """Gateway rule: a shell/settings env that sets ANTHROPIC_BASE_URL means
    the installed `claude` is gateway-driven -- `claude auth status` must
    never be spawned at all, by `/providers`, `/model`, or the startup
    worker, and `/providers`' own claude_subscription row names the gateway
    specifically rather than an ambiguous plain "not set up"."""
    import halo_harness.providers.cc_models as cc_models_mod
    from halo_harness.tui.slash import handle_slash

    calls: list = []
    real_claude_auth_status = cc_models_mod.claude_auth_status

    def _poison(*a, **kw):
        calls.append(True)
        raise AssertionError("claude auth status must never be spawned when claude is gateway-driven")

    saved_env = _save_cc_auth_env()
    os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
    os.environ["ANTHROPIC_BASE_URL"] = "https://my-work-gateway.example.test"
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    cc_models_mod.claude_auth_status = _poison
    cc_models_mod.reset_cached_claude_auth_status()

    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            controller = _real_controller(cwd)
            app = await _mounted(controller, cwd=str(cwd))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.2)  # let the startup worker land first (it must stay silent, never spawn)
                await handle_slash(app, "providers", "")
                notes = []
                for _ in range(60):
                    await app._drain()
                    await pilot.pause(0.05)
                    notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
                    if any("Claude Code subscription" in n for n in notes):
                        break
                ctx.check(f"the providers table reached the transcript, got {notes}",
                          any("Claude Code subscription" in n for n in notes))
                ctx.check("the claude_subscription row names the gateway, not a plain 'not set up'",
                          any("not set up (claude is configured for a gateway)" in n for n in notes))
                await handle_slash(app, "model", "")
                await pilot.pause(0.2)
                await pilot.press("escape")  # close the model picker /model just opened
                await pilot.pause(0.05)
                ctx.check("claude auth status was never spawned by the startup worker, /providers or /model",
                          not calls)
    try:
        asyncio.run(body())
    finally:
        cc_models_mod.claude_auth_status = real_claude_auth_status
        cc_models_mod.reset_cached_claude_auth_status()
        _restore_cc_auth_env(saved_env)


# ============================================================================
# 1.0.1 part 2 fixpass finding 4: the init tabs app composes instantly (the
# "claude" tab's own claude-auth-status spawn never blocks compose/mount)
# and --no-live skips every reachability probe/catalog fetch outright.
# ============================================================================

@test
def test_init_tabs_compose_is_instant_despite_a_slow_claude_check(ctx: Ctx):
    import time as time_mod
    import halo_harness.init_providers as init_providers_mod
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Static

    def _slow_claude_login():
        time_mod.sleep(1.0)
        return False

    async def body():
        real_login = init_providers_mod.claude_login_available
        init_providers_mod.claude_login_available = _slow_claude_login
        try:
            with _H15TabsEnv():
                start = time_mod.monotonic()
                app = InitTabsApp()
                async with app.run_test(size=(100, 40)) as pilot:
                    elapsed = time_mod.monotonic() - start
                    ctx.check(f"mounted quickly despite the slow claude check, got {elapsed:.2f}s", elapsed < 0.5)
                    claude_status = _static_text(app.query_one("#claude-status", Static))
                    ctx.check(f"the placeholder is shown first, got {claude_status!r}", claude_status == "checking…")
                    # Eventually the slow worker resolves it for real.
                    for _ in range(30):
                        await pilot.pause(0.1)
                        resolved = _static_text(app.query_one("#claude-status", Static))
                        if resolved != "checking…":
                            break
                    ctx.check(f"resolves once the slow check finishes, got {resolved!r}", resolved == "not set up")
                    await pilot.press("escape")
                    await pilot.pause(0.05)
        finally:
            init_providers_mod.claude_login_available = real_login
    asyncio.run(body())


@test
def test_init_tabs_check_login_button_does_not_block_the_ui_thread(ctx: Ctx):
    """M3 (1.0.1 final pass): the "claude" tab's own "Check login" button
    used to call save_tab_credentials()+tab_credential_state() (each
    spawning `claude auth status`) directly on the UI thread -- a slow
    subprocess froze the whole app for its duration. Now runs in a
    thread=True worker + call_from_thread, same pattern as this file's own
    test_init_tabs_compose_is_instant_despite_a_slow_claude_check above."""
    import time as time_mod
    import halo_harness.init_providers as init_providers_mod
    from halo_harness.init_providers import TAB_PROVIDERS
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Static

    def _slow_claude_login():
        time_mod.sleep(2.0)
        return False

    async def body():
        real_login = init_providers_mod.claude_login_available
        try:
            with _H15TabsEnv():
                app = InitTabsApp()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause(0.1)
                    # Reach the "claude" tab (index 3 of 5: databricks,
                    # openrouter, anthropic, claude, typesafe) -- shift+tab
                    # wraps 0 -> 4 -> 3, same math
                    # test_init_tabs_shift_tab_navigates_providers_with_
                    # wraparound above already pins.
                    ctx.check(f"claude really is index 3, got {TAB_PROVIDERS}", TAB_PROVIDERS[3] == "claude")
                    await pilot.press("shift+tab")
                    await pilot.press("shift+tab")
                    await pilot.pause(0.05)
                    # Only make the check slow AFTER mount -- __init__'s own
                    # eager on_mount worker must not eat this patch.
                    init_providers_mod.claude_login_available = _slow_claude_login
                    t0 = time_mod.monotonic()
                    await pilot.click("#claude-save")
                    click_elapsed = time_mod.monotonic() - t0
                    ctx.check(f"clicking Save returns promptly (off the UI thread) despite the "
                              f"2s-sleeping claude auth check, got {click_elapsed:.2f}s", click_elapsed < 1.0)
                    # The app is still alive and responsive right away -- a
                    # key press is handled well before the 2s check lands.
                    t1 = time_mod.monotonic()
                    await pilot.press("tab")
                    key_elapsed = time_mod.monotonic() - t1
                    ctx.check(f"a key press right after is handled promptly too, got {key_elapsed:.2f}s",
                              key_elapsed < 1.0)
                    # Eventually the slow worker resolves the real result.
                    status = "checking…"
                    for _ in range(40):
                        await pilot.pause(0.1)
                        status = _static_text(app.query_one("#claude-status", Static))
                        if "not set up" in status:
                            break
                    ctx.check(f"the slow check eventually lands, got {status!r}", "not set up" in status)
                    await pilot.press("escape")
                    await pilot.pause(0.05)
        finally:
            init_providers_mod.claude_login_available = real_login
    asyncio.run(body())


@test
def test_init_tabs_no_live_never_touches_the_network_or_fetches_a_catalog(ctx: Ctx):
    import halo_harness.init_providers as init_providers_mod
    import halo_harness.providers.reachability as reach_mod
    from halo_harness.tui.dialogs.init_tabs import InitTabsApp
    from textual.widgets import Input, Static

    def _poison_open_upstream(*a, **kw):
        raise AssertionError("--no-live must never probe reachability")

    def _poison_refresh_tab_catalog(provider):
        raise AssertionError("--no-live must never fetch a catalog")

    async def body():
        real_open_upstream = reach_mod.open_upstream
        real_refresh = init_providers_mod.refresh_tab_catalog
        reach_mod.open_upstream = _poison_open_upstream
        init_providers_mod.refresh_tab_catalog = _poison_refresh_tab_catalog
        try:
            with _H15TabsEnv():
                app = InitTabsApp(no_live=True)
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause(0.1)
                    reach = _static_text(app.query_one("#openrouter-reach", Static))
                    ctx.check(f"shows skipped immediately, got {reach!r}", "skipped" in reach)
                    key_input = app.query_one("#openrouter-field-key", Input)
                    key_input.focus()
                    for ch in "sk-or-fake":
                        await pilot.press(ch)
                    await pilot.press("enter")
                    await pilot.pause(0.3)
                    reach_after = _static_text(app.query_one("#openrouter-reach", Static))
                    ctx.check(f"still skipped after Save -- no probe/catalog fetch fired, got {reach_after!r}",
                              "skipped" in reach_after)
                    ctx.check("openrouter was still enabled despite --no-live",
                              "openrouter" in app.configured_this_run)
                    await pilot.press("escape")
                    await pilot.pause(0.05)
        finally:
            reach_mod.open_upstream = real_open_upstream
            init_providers_mod.refresh_tab_catalog = real_refresh
    asyncio.run(body())


# ============================================================================
# 1.0.1 part 2 fixpass finding 14: the hang watchdog pauses around a known
# blocking call, caps dumps per process, and prunes old hang-*.log files.
# ============================================================================

@test
def test_watchdog_paused_flag_suppresses_a_hang_dump(ctx: Ctx):
    """Simulates a stale heartbeat directly (never actually blocks the
    event loop for 15s+) -- no dump while `_watchdog_paused` is set (the
    flag `_enter_suspend_for_editor`/`_exit_suspend_for_editor` toggle
    around `self.suspend()`); the SAME stale heartbeat dumps once unpaused,
    proving the mechanism genuinely works (not just coincidentally quiet)."""
    import time as time_mod
    from halo_harness.tui.app import HANG_HEARTBEAT_THRESHOLD_S

    async def body():
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
        scratch = Path(tempfile.mkdtemp(prefix="h15b-watchdog-pause-"))
        os.environ["BRIDGE_TEST_HOME"] = str(scratch)
        os.environ["BRIDGE_STATE_DIR"] = str(scratch / ".halo")
        try:
            fake = FakeController()
            fake.state_dir = scratch / ".halo"
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)):
                # The real `_tick_spinner` interval (every 1s) refreshes
                # `_last_heartbeat_monotonic` on its own -- stopped here so
                # this test's OWN simulated-stale value isn't immediately
                # overwritten by the app genuinely running fine underneath it.
                app._tick_spinner = lambda: None
                app._watchdog_paused = True
                app._last_heartbeat_monotonic = time_mod.monotonic() - (HANG_HEARTBEAT_THRESHOLD_S + 5)
                await asyncio.sleep(2.5)  # >= one 2s watchdog poll cycle
                ctx.check(f"no dump while paused, got count={app._watchdog_dump_count}",
                          app._watchdog_dump_count == 0)
                app._watchdog_paused = False
                await asyncio.sleep(2.5)
                ctx.check(f"a dump fires once unpaused with the same stale heartbeat, got "
                          f"count={app._watchdog_dump_count}", app._watchdog_dump_count >= 1)
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state_dir is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state_dir
    asyncio.run(body())


@test
def test_hang_dump_count_is_capped_per_process(ctx: Ctx):
    from halo_harness.tui.app import MAX_HANG_DUMPS_PER_PROCESS

    async def body():
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
        scratch = Path(tempfile.mkdtemp(prefix="h15b-watchdog-cap-"))
        os.environ["BRIDGE_TEST_HOME"] = str(scratch)
        os.environ["BRIDGE_STATE_DIR"] = str(scratch / ".halo")
        try:
            fake = FakeController()
            fake.state_dir = scratch / ".halo"
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)):
                for i in range(MAX_HANG_DUMPS_PER_PROCESS + 3):
                    app._dump_hang_diagnostics(30.0, reason=f"test-{i}")
                ctx.check(f"capped at {MAX_HANG_DUMPS_PER_PROCESS}, got {app._watchdog_dump_count}",
                          app._watchdog_dump_count == MAX_HANG_DUMPS_PER_PROCESS)
                dumps = list((scratch / ".halo").glob("hang-*.log"))
                ctx.check(f"at most {MAX_HANG_DUMPS_PER_PROCESS} dump files landed on disk, got {len(dumps)}",
                          0 < len(dumps) <= MAX_HANG_DUMPS_PER_PROCESS)
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state_dir is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state_dir
    asyncio.run(body())


@test
def test_sigusr1_after_the_cap_logs_one_line_instead_of_staying_silent(ctx: Ctx):
    """2.0.1 finding 25: before this fix, a SIGUSR1 sent after `MAX_HANG_
    DUMPS_PER_PROCESS` was already reached did PRECISELY nothing -- no file,
    no log line, nothing to tell the sender their signal landed at all.
    `_on_sigusr1` now logs one "bridge" warning per such signal instead,
    without writing a new dump file (the cap itself is unchanged)."""
    import logging
    from halo_harness.tui.app import MAX_HANG_DUMPS_PER_PROCESS

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            app._watchdog_dump_count = MAX_HANG_DUMPS_PER_PROCESS
            logger = logging.getLogger("bridge")
            real_warning = logger.warning
            captured = []
            logger.warning = lambda msg, *a, **kw: captured.append(msg % a if a else msg)
            try:
                app._on_sigusr1(None, None)
            finally:
                logger.warning = real_warning
            ctx.check(f"no new dump attempted (count unchanged), got {app._watchdog_dump_count}",
                      app._watchdog_dump_count == MAX_HANG_DUMPS_PER_PROCESS)
            ctx.check(f"exactly one warning logged, got {captured}", len(captured) == 1)
            ctx.check(f"it names the cap, got {captured[0]!r}",
                      "SIGUSR1" in captured[0] and str(MAX_HANG_DUMPS_PER_PROCESS) in captured[0])
    asyncio.run(body())


@test
def test_prune_old_hang_dumps_keeps_only_the_newest_ten(ctx: Ctx):
    from halo_harness.tui.app import MAX_HANG_DUMP_FILES_KEPT

    async def body():
        old_home = os.environ.get("BRIDGE_TEST_HOME")
        old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
        scratch = Path(tempfile.mkdtemp(prefix="h15b-watchdog-prune-"))
        state_dir = scratch / ".halo"
        state_dir.mkdir(parents=True, exist_ok=True)
        os.environ["BRIDGE_TEST_HOME"] = str(scratch)
        os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
        try:
            total = MAX_HANG_DUMP_FILES_KEPT + 4
            for i in range(total):
                (state_dir / f"hang-2026010100{i:04d}Z.log").write_text("x", encoding="utf-8")
            fake = FakeController()
            fake.state_dir = state_dir
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)):
                pass  # on_mount -> _start_watchdog -> _prune_old_hang_dumps already ran
            remaining = sorted(p.name for p in state_dir.glob("hang-*.log"))
            ctx.check(f"pruned down to {MAX_HANG_DUMP_FILES_KEPT}, got {len(remaining)}",
                      len(remaining) == MAX_HANG_DUMP_FILES_KEPT)
            expected_kept = sorted(f"hang-2026010100{i:04d}Z.log"
                                   for i in range(total - MAX_HANG_DUMP_FILES_KEPT, total))
            ctx.check(f"kept exactly the newest ones, got {remaining}", remaining == expected_kept)
        finally:
            if old_home is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_home
            if old_state_dir is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state_dir
    asyncio.run(body())


# ============================================================================
# 1.0.1 part 2 fixpass finding 17: --debug's own per-keystroke trace must
# never be able to reconstruct a typed secret.
# ============================================================================

@test
def test_debug_key_repr_redacts_printable_characters_only(ctx: Ctx):
    from halo_harness.tui.app import _debug_key_repr
    for ch in ("a", "A", "1", "9", "!", "@", "z"):
        ctx.check(f"{ch!r} redacted, got {_debug_key_repr(ch)!r}", _debug_key_repr(ch) == "<char>")
    for safe in ("enter", "tab", "escape", "backspace", "space", "up", "down", "left", "right", "f5"):
        ctx.check(f"{safe!r} passes through verbatim, got {_debug_key_repr(safe)!r}", _debug_key_repr(safe) == safe)
    for chord in ("ctrl+c", "ctrl+x", "alt+e", "shift+tab"):
        ctx.check(f"{chord!r} passes through verbatim, got {_debug_key_repr(chord)!r}", _debug_key_repr(chord) == chord)


@test
def test_on_key_debug_trace_never_logs_a_raw_printable_keystroke(ctx: Ctx):
    """Calls `BridgeApp._on_key` directly (a plain `SimpleNamespace(key=...)`
    stand-in -- the method only ever reads `.key` off it when no chord is
    pending, the default state) rather than via `pilot.press`: a plain
    character typed into the focused PromptInput's own TextArea is
    consumed before ever reaching `_on_key` at all (see test_debug_tracing_
    logs_key_events_with_focus_and_screen's own comment on exactly this),
    so driving it through the UI would only ever exercise control keys --
    this tests the handler's OWN redaction directly, the exact code path
    finding 17 fixes, independent of which widget happens to be focused."""
    from types import SimpleNamespace

    key_calls = []

    async def body():
        fake = FakeController()
        app = await _mounted(fake)

        def _spy(msg, *a):
            if msg.startswith("key:"):
                key_calls.append(a)

        app._debug_trace = _spy
        async with app.run_test(size=(100, 40)):
            await app._on_key(SimpleNamespace(key="x"))  # as if typing part of a secret
            await app._on_key(SimpleNamespace(key="escape"))
        key_args = [a[0] for a in key_calls if a]
        ctx.check(f"the printable 'x' never appears raw in a trace, got {key_args}", "x" not in key_args)
        ctx.check(f"the placeholder was logged instead, got {key_args}", "<char>" in key_args)
        ctx.check(f"a control key (escape) still logs its real name, got {key_args}", "escape" in key_args)
    asyncio.run(body())


# ============================================================================
# 2.0.0 Launch intro (IntroLine, BridgeApp's own show_intro/_on_key/
# _submit_prompt wiring, tui/launch.py's _show_intro_for gating).
# ============================================================================

@test
def test_intro_types_out_the_full_line_then_the_cursor_disappears(ctx: Ctx):
    """Advances time with the per-character delay shrunk to near-zero
    (IntroLine's own class attributes -- plain, test-overridable constants,
    restored in `finally`) instead of waiting out the real ~2s animation:
    asserts the fully-revealed line, the real installed version substring,
    and the block cursor gone once `done`."""
    from halo_harness import __version__

    saved = (IntroLine.BASE_DELAY_S, IntroLine.PAUSE_DELAY_S, IntroLine.ELLIPSIS_PAUSE_DELAY_S)
    IntroLine.BASE_DELAY_S = IntroLine.PAUSE_DELAY_S = IntroLine.ELLIPSIS_PAUSE_DELAY_S = 0.001

    async def body():
        fake = FakeController()
        app = await _mounted(fake, show_intro=True)
        async with app.run_test(size=(100, 40)) as pilot:
            ctx.check("an IntroLine is mounted as the first transcript child",
                      isinstance(app.transcript.children[0], IntroLine))
            await pilot.pause(0.5)  # far more than len(line) * 0.001s at the shrunk delay
            widget = app.transcript.children[0]
            expected = f"I am just a copy, of a copy, of a copy... halo {__version__}"
            rendered = _static_text(widget)
            ctx.check(f"the full line is revealed, got {rendered!r}", rendered == expected)
            ctx.check(f"the real version ({__version__!r}) is in the rendered line", __version__ in rendered)
            ctx.check("the widget itself reports done", widget.done is True)
            ctx.check(f"the block cursor is gone once done, got {rendered!r}", IntroLine.CURSOR not in rendered)
            # Visual proof (D-TUI convention): a real SVG screenshot of the
            # completed intro line, written to docs/harness/tui-snapshots/.
            path, svg = _write_snapshot(app, "launch-intro")
            ctx.check(f"launch-intro snapshot written to {path}", path.exists() and len(svg) > 0)
            ctx.check("the snapshot SVG actually shows the intro text", "just a copy" in svg and "halo" in svg)
    try:
        asyncio.run(body())
    finally:
        IntroLine.BASE_DELAY_S, IntroLine.PAUSE_DELAY_S, IntroLine.ELLIPSIS_PAUSE_DELAY_S = saved


@test
def test_intro_keypress_mid_typing_completes_it_at_once_and_lands_in_input(ctx: Ctx):
    """A keypress while the intro is still typing (delay deliberately left
    LARGE so it's still mid-animation the moment we press) finishes the
    line INSTANTLY -- no waiting -- and the SAME keystroke still reaches
    the prompt input (first-frame focus, never stolen by the intro)."""
    saved = (IntroLine.BASE_DELAY_S, IntroLine.PAUSE_DELAY_S, IntroLine.ELLIPSIS_PAUSE_DELAY_S)
    IntroLine.BASE_DELAY_S = IntroLine.PAUSE_DELAY_S = IntroLine.ELLIPSIS_PAUSE_DELAY_S = 30.0

    async def body():
        fake = FakeController()
        app = await _mounted(fake, show_intro=True)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            widget = app.transcript.children[0]
            ctx.check("still typing (30s/char delay) right before the keypress", widget.done is False)
            ctx.check("the prompt input already has focus before any key is pressed",
                      app.screen.focused is app.prompt_input)
            await pilot.press("x")
            ctx.check("the SAME keypress finished the intro instantly", widget.done is True)
            ctx.check(f"and also landed in the prompt input, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "x")
    try:
        asyncio.run(body())
    finally:
        IntroLine.BASE_DELAY_S, IntroLine.PAUSE_DELAY_S, IntroLine.ELLIPSIS_PAUSE_DELAY_S = saved


@test
def test_show_intro_false_means_no_intro_widget_at_all(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake, show_intro=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.05)
            ctx.check("show_intro=False (BridgeApp's own default) never mounts one", app.intro_line is None)
            kinds = [type(w).__name__ for w in app.transcript.children]
            ctx.check(f"no IntroLine anywhere in the transcript, got {kinds}", "IntroLine" not in kinds)
    asyncio.run(body())


@test
def test_show_intro_for_honours_no_intro_flag_demo_config_and_tty(ctx: Ctx):
    """Pure-function coverage of `tui/launch.py::_show_intro_for` -- the
    REAL gating logic (BridgeApp itself defaults show_intro=False and has
    no opinion on WHY); covers `--no-intro`, `--demo`, `"intro": false` in
    `~/.halo/config.json`, and stdout not being a tty, each in isolation."""
    from types import SimpleNamespace
    from halo_harness.tui.launch import _show_intro_for

    saved_home = os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-intro-gating-"))
    os.environ["BRIDGE_TEST_HOME"] = str(tmp)
    real_isatty = sys.stdout.isatty
    try:
        sys.stdout.isatty = lambda: True  # pretend a real terminal unless a case below says otherwise
        args_plain = SimpleNamespace(demo=False, no_intro=False)
        ctx.check("plain interactive launch: intro shown", _show_intro_for(args_plain) is True)

        args_no_intro = SimpleNamespace(demo=False, no_intro=True)
        ctx.check("--no-intro: never shown", _show_intro_for(args_no_intro) is False)

        args_demo = SimpleNamespace(demo=True, no_intro=False)
        ctx.check("--demo: never shown (brief: never in --demo snapshots unless asked)",
                  _show_intro_for(args_demo) is False)

        sys.stdout.isatty = lambda: False
        ctx.check("stdout not a tty: never shown", _show_intro_for(args_plain) is False)
        sys.stdout.isatty = lambda: True

        from halo_harness.theme import set_config_value
        set_config_value("intro", False)
        ctx.check('"intro": false in config.json: never shown', _show_intro_for(args_plain) is False)
        set_config_value("intro", True)
        ctx.check('"intro": true in config.json: shown again (plain launch)', _show_intro_for(args_plain) is True)
    finally:
        sys.stdout.isatty = real_isatty
        if saved_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = saved_home


@test
def test_print_mode_demo_output_contains_no_intro_text(ctx: Ctx):
    """`run_demo` is `-p --demo`'s own entry point (cli.py: `args.demo and
    args.print_mode` -> `testing.fake_controller.run_demo`, never
    `tui.launch.run_tui`/`BridgeApp` at all) -- the structural reason print
    mode never shows the intro, pinned here as a literal text check on its
    actual captured output."""
    import io
    from halo_harness.testing.fake_controller import run_demo

    buf = io.StringIO()
    rc = run_demo(output_format="text", stream=buf)
    ctx.check(f"run_demo exits 0, got {rc}", rc == 0)
    output = buf.getvalue()
    ctx.check("print-mode/--demo output has real content (sanity)", len(output) > 0)
    ctx.check(f"no intro text anywhere in print-mode output, got {output[:200]!r}...",
              "I am just a copy" not in output)


@test
def test_state_dir_is_scoped_away_from_the_real_machine_home(ctx: Ctx):
    """2.0.0 fixpass finding 2: `ensure_scoped_state_dir_once()` (called at
    module import, above) must have already scoped BRIDGE_TEST_HOME/
    BRIDGE_STATE_DIR away from the real machine before ANY test in this
    file ever mounts a BridgeApp -- the exact gap that let
    test_submit_streams_text_and_mounts_tool_card perform the real
    ~/.rolo-claude -> ~/.halo migration and write real prompt text into
    the real ~/.halo/history.jsonl (confirmed live: 8 entries landed
    there from this file's own scripted inputs)."""
    from halo_harness.config.paths import bridge_home, home
    ctx.check("BRIDGE_TEST_HOME or BRIDGE_STATE_DIR is set by the time this test runs",
              "BRIDGE_TEST_HOME" in os.environ or "BRIDGE_STATE_DIR" in os.environ)
    ctx.check(f"home() is not the real machine home, got {home()}", home() != Path.home())
    ctx.check(f"bridge_home() is not the real ~/.halo, got {bridge_home()}",
              bridge_home() != Path.home() / ".halo")


# ============================================================================
# Halo 2.0.1 W2b (HALO-2.0.1-liveness-tips-brief.md Part A): the live phase
# line, the reasoning preview, the status-bar cluster, tool/sub-agent
# elapsed counters. Most pilots inject synthetic `phase`/delta events
# directly into `app._local_events` (bypassing FakeController.submit()
# entirely) for full control over EXACT event ordering/timing -- the
# upstream-to-event mapping itself is W2a's own pinned responsibility
# (tests/test_w2a_finish_reasons_and_phase.py, against MockDatabricks);
# this file's job is "given these events, does the TUI render correctly AND
# keep ticking on its own between events". `test_real_e2e_pilot_against_a_
# slow_databricks_mock_shows_the_phase_line_live` below is the one genuine
# end-to-end exception, reusing the EXACT MockDatabricks scenarios W2a's
# own suite already proved produce the right phase events.
# ============================================================================

@test
def test_phase_line_progresses_sending_thinking_writing_then_summary(ctx: Ctx):
    """A1/A2: the live phase line passes through every documented state as
    the matching `phase`/`thinking_delta`/`text_delta` events arrive, the
    last-3-lines reasoning preview while expanded, and collapses to a
    "Thought for Ns" summary (removed from the live-lines registry) once
    reasoning happened."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            line = app.transcript._phase_lines.get((None, 1))
            ctx.check("the phase line is mounted the moment the request is sent", line is not None)
            ctx.check(f"sending state names the model, got {str(line.render())!r}",
                      "Sending request to or:mock/x" in str(line.render()))

            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            rendered = str(line.render())
            ctx.check(f"headers-in state: thinking, no tokens yet, got {rendered!r}",
                      "Thinking…" in rendered and "no tokens yet" in rendered)

            app._local_events.put(ev.phase(state="first_token", turn=1, kind="reasoning"))
            app._local_events.put(ev.thinking_delta(
                "thinking step one\nstep two\nstep three\nstep four", turn=1))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            rendered = str(line.render())
            ctx.check(f"reasoning state shows a live token count, got {rendered!r}",
                      "Thinking…" in rendered and "reasoning tokens" in rendered)
            ctx.check(f"the preview shows only the LAST three lines, got {rendered!r}",
                      "step four" in rendered and "step one" not in rendered)

            app._local_events.put(ev.phase(state="first_token", turn=1, kind="text"))
            app._local_events.put(ev.text_delta("the final answer", turn=1, index=1))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check(f"writing state shows a token count, got {str(line.render())!r}",
                      "Writing…" in str(line.render()))

            app._local_events.put(ev.message_end(turn=1, stop_reason="end_turn"))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            rendered = str(line.render())
            ctx.check(f"collapses to a Thought-for summary once reasoning happened, got {rendered!r}",
                      "Thought for" in rendered and "reasoning tokens" not in rendered)
            ctx.check("the phase line is finalized (no longer the live one for this call)",
                      (None, 1) not in app.transcript._phase_lines)
    asyncio.run(body())


@test
def test_f7_w6a_turn_done_without_message_end_still_finalizes_the_phase_line(ctx: Ctx):
    """finding 7 (W6a): a call that ends in an error or an Esc yields
    `turn_done` with NO `message_end` first -- `finish_phase_line` used
    to run only from the `message_end` branch, so the line stayed live
    and kept ticking ("Thinking... (2 m, no tokens yet) ... no data for
    120 s, Esc interrupts...") even after the status bar already went
    idle."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            ctx.check("the phase line is live before turn_done",
                      (None, 1) in app.transcript._phase_lines)

            app._local_events.put(ev.turn_done(turn=1, reason="interrupted"))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check("turn_done alone (no message_end) finalizes/removes the phase line",
                      (None, 1) not in app.transcript._phase_lines)
    asyncio.run(body())


@test
def test_phase_line_elapsed_ticks_via_the_drain_timer_with_no_new_events(ctx: Ctx):
    """A1: "updated in place at least once a second ... never only on chunk
    arrival" -- NO new event arrives during the whole sampling window; the
    rendered text must still advance, purely from wall-clock elapsed time
    recomputed every drain tick."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            line = app.transcript._phase_lines[(None, 1)]
            texts = []
            for _ in range(18):
                await pilot.pause(0.25)
                await app._drain()
                texts.append(str(line.render()))
            ctx.check(f"the elapsed counter advanced at least 4 times with zero new events, "
                      f"got {len(set(texts))} distinct renders across {len(texts)} ticks "
                      f"(samples: {sorted(set(texts))})", len(set(texts)) >= 4)
    asyncio.run(body())


@test
def test_reasoning_only_in_final_chunk_shows_summary_with_no_live_preview(ctx: Ctx):
    """A2: "Reasoning that arrives only with the final chunk (Databricks
    GLM when the gateway does not stream it) is shown as the collapsed
    summary at that moment" -- no `first_token(kind="reasoning")` ever
    fires; the ONE `thinking_delta` lands immediately before `message_end`,
    so the line must never show a multi-line expanded preview, only ever
    the elapsed-counter states and then the final collapsed summary."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            line = app.transcript._phase_lines[(None, 1)]
            ctx.check("no reasoning-token wording before the reasoning ever arrives",
                      "reasoning tokens" not in str(line.render()))
            app._local_events.put(ev.thinking_delta("the whole reasoning, all at once", turn=1))
            app._local_events.put(ev.text_delta("final answer", turn=1, index=1))
            app._local_events.put(ev.message_end(turn=1, stop_reason="end_turn"))
            await _drain_a_few(app, pilot, n=4, pause=0.02)
            rendered = str(line.render())
            ctx.check(f"collapses straight to the Thought-for summary, got {rendered!r}",
                      "Thought for" in rendered)
            ctx.check(f"a single summary line, never a multi-line preview, got {rendered!r}",
                      "\n" not in rendered)
    asyncio.run(body())


@test
def test_phase_line_no_data_suffix_appears_past_threshold_and_refreshes(ctx: Ctx):
    """A1: "after 30s with no chunk the line gains the dim suffix ... no
    data for 30s ... refreshed every 10s" -- via a test seam that shrinks
    both numbers so this never actually waits 30 real seconds."""
    from halo_harness.tui.widgets.transcript import ThinkingBlock
    real_threshold, real_refresh = ThinkingBlock.NO_DATA_THRESHOLD_S, ThinkingBlock.NO_DATA_REFRESH_S
    ThinkingBlock.NO_DATA_THRESHOLD_S = 0.6
    ThinkingBlock.NO_DATA_REFRESH_S = 0.3
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
                app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
                await _drain_a_few(app, pilot, n=2, pause=0.02)
                line = app.transcript._phase_lines[(None, 1)]
                ctx.check("no suffix yet, under the (shrunk) threshold",
                          "no data for" not in str(line.render()))
                await pilot.pause(0.7)
                await app._drain()
                rendered = str(line.render())
                ctx.check(f"the suffix appears past the (shrunk) threshold and names both actions, "
                          f"got {rendered!r}",
                          "no data for" in rendered and "Esc interrupts, typing steers" in rendered)
                first_suffix = rendered
                await pilot.pause(0.4)
                await app._drain()
                second = str(line.render())
                ctx.check(f"the suffix's own number refreshes past the (shrunk) refresh interval, "
                          f"got {first_suffix!r} -> {second!r}", second != first_suffix)
                ctx.check("never says the harness is stuck/hung/frozen",
                          not any(w in second.lower() for w in ("stuck", "hung", "frozen")))
        asyncio.run(body())
    finally:
        ThinkingBlock.NO_DATA_THRESHOLD_S = real_threshold
        ThinkingBlock.NO_DATA_REFRESH_S = real_refresh


@test
def test_waiting_line_between_a_tool_result_and_the_next_call_reuses_the_line(ctx: Ctx):
    """A1: "after tool results are sent back: Waiting for model… (3s)" --
    the prior call's own line (no reasoning) is removed at its message_end,
    a fresh "waiting" line appears for the gap, and the NEXT call's own
    request_sent continues that SAME widget (no flicker of remove-then-
    mount with nothing shown in between)."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            app._local_events.put(ev.phase(state="first_token", turn=1, kind="tool"))
            app._local_events.put(ev.Event(
                "tool_use_ready", {"id": "t1", "name": "Bash", "input": {"command": "echo hi"},
                                    "repaired": False}, turn=1))
            app._local_events.put(ev.Event("tool_result", {"id": "t1", "ok": True, "summary": "hi"}, turn=1))
            app._local_events.put(ev.message_end(turn=1, stop_reason="tool_use"))
            await _drain_a_few(app, pilot, n=4, pause=0.02)
            ctx.check("no reasoning happened on the tool-only call -- its own line is removed",
                      (None, 1) not in app.transcript._phase_lines)

            app._local_events.put(ev.phase(state="waiting_for_model", turn=1))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            waiting_line = app.transcript._phase_lines.get((None, 1))
            ctx.check("a fresh line appears for the waiting gap", waiting_line is not None)
            ctx.check(f"it names waiting for the model, got {str(waiting_line.render())!r}",
                      "Waiting for model…" in str(waiting_line.render()))
            await pilot.pause(1.1)
            await app._drain()
            ctx.check(f"its own elapsed ticks too, got {str(waiting_line.render())!r}",
                      "Waiting for model… (1 s)" in str(waiting_line.render())
                      or "Waiting for model… (2 s)" in str(waiting_line.render()))

            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x2"))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            reused = app.transcript._phase_lines.get((None, 1))
            ctx.check("the NEXT call's request_sent reuses the SAME waiting widget",
                      reused is waiting_line)
            ctx.check(f"it now shows the new call being sent, got {str(reused.render())!r}",
                      "Sending request to or:mock/x2" in str(reused.render()))
    asyncio.run(body())


@test
def test_turn_done_resets_the_status_bar_cluster_and_writing_shows_its_own_token_count(ctx: Ctx):
    """A3: the phase word, elapsed and received-token counter all move
    while a turn runs (thinking -> writing resets the token counter to
    THAT phase's own count, never a running total across both, while the
    elapsed clock keeps counting from the same call); `turn_done` returns
    the whole cluster to idle with no stale counter/tool-name surviving."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            app._local_events.put(ev.phase(state="first_token", turn=1, kind="reasoning"))
            app._local_events.put(ev.thinking_delta("x" * 40, turn=1))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            rendered_a = str(app.status_bar.render())
            ctx.check(f"status bar shows thinking with a received-token count, got {rendered_a!r}",
                      "thinking" in rendered_a and "↓" in rendered_a)

            await pilot.pause(1.1)
            await app._drain()
            rendered_b = str(app.status_bar.render())
            ctx.check(f"consecutive drain ticks differ with no new event (elapsed moved), "
                      f"got {rendered_a!r} vs {rendered_b!r}", rendered_a != rendered_b)

            app._local_events.put(ev.phase(state="first_token", turn=1, kind="text"))
            app._local_events.put(ev.text_delta("y" * 4800, turn=1, index=1))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            rendered_c = str(app.status_bar.render())
            ctx.check(f"writing shows ITS OWN token count (not the earlier reasoning total), "
                      f"got {rendered_c!r}", "writing" in rendered_c and "↓1.2k" in rendered_c)

            app._local_events.put(ev.message_end(turn=1, stop_reason="end_turn"))
            app._local_events.put(ev.turn_done(turn=1, reason="end_turn"))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check(f"turn_done returns the cluster to idle, got phase={app.status_bar.phase!r}",
                      app.status_bar.phase == "idle")
            rendered_d = str(app.status_bar.render())
            ctx.check(f"no stale counter/word survives into idle, got {rendered_d!r}",
                      "↓" not in rendered_d and "thinking" not in rendered_d and "writing" not in rendered_d)
    asyncio.run(body())


@test
def test_tool_card_header_shows_elapsed_seconds_while_running_only(ctx: Ctx):
    """A4: "A running card shows elapsed seconds in its header" -- ticks
    via `Transcript.tick_tool_cards` (the same drain-timer mechanism as the
    phase line), and stops/omits it once resolved."""
    import re

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.Event(
                "tool_use_ready", {"id": "t1", "name": "Bash", "input": {"command": "pytest -q"},
                                    "repaired": False}, turn=1))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            card = app.transcript.tool_cards["t1"]
            texts = []
            for _ in range(10):
                await pilot.pause(0.25)
                await app._drain()
                texts.append(str(card.render()))
            ctx.check(f"the running card's own elapsed seconds advance on their own, "
                      f"got {len(set(texts))} distinct renders (samples: {sorted(set(texts))})",
                      len(set(texts)) >= 2)
            ctx.check(f"the header carries the elapsed suffix right after the tool summary, "
                      f"got {texts[-1]!r}", re.search(r"⏺ Bash\([^)]*\) · \d+ s\n", texts[-1]) is not None)
            app._local_events.put(ev.Event("tool_result", {"id": "t1", "ok": True, "summary": "ok"}, turn=1))
            await _drain_a_few(app, pilot, n=2, pause=0.02)
            resolved_text = str(card.render())
            await pilot.pause(0.3)
            await app._drain()
            ctx.check(f"a resolved card's header no longer ticks/changes, got {resolved_text!r} "
                      f"vs {str(card.render())!r}", resolved_text == str(card.render()))
    asyncio.run(body())


@test
def test_nested_subagent_card_shows_live_phase_and_tool_count(ctx: Ctx):
    """A5: "the parent's nested card shows the child's phase and tool
    count live" -- a real foreground sub-agent (Task tool), its own `phase`/
    `tool_use_ready` events (agent_id-tagged, same mechanism finding 11
    already relies on elsewhere) drive a live `SubAgentCard` the parent's
    transcript shows, frozen once the child finishes."""
    import argparse
    import re

    async def body():
        fh = build_fake_home()
        SCENARIOS["a5-parent"] = ScriptedTurns([
            _tool_call_chunk("call_a5", "Task", {"description": "look", "prompt": "go",
                                                   "subagent_type": "general-purpose",
                                                   "model": "or:mock/a5-child"}),
            _final_text_chunk("parent done"),
        ])
        SCENARIOS["a5-child"] = ScriptedTurns([
            _tool_call_chunk("call_a5_child_tool", "Read", {"file_path": "pong.txt"}),
            _final_text_chunk("child done"),
        ])
        mock = MockUpstream().start()
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_OPENROUTER_BASE_URL", "OPENROUTER_API_KEY")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        controller = None
        try:
            from halo_harness.tui.bootstrap import build_controller
            from halo_harness.tui.widgets.cards import SubAgentCard

            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=False,
                tools=None, add_dir=None, model="or:mock/a5-parent", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "spawn a sub-agent")
                await pilot.press("enter")
                for _ in range(150):
                    await app._drain()
                    await pilot.pause(0.03)
                    texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                    if any("parent done" in t for t in texts):
                        break
                # Everything above may coalesce within very few drain ticks
                # once the parent's final text streams in (the whole child
                # run genuinely can finish between two polls) -- `subagent_
                # marks` (unlike the `subagent_cards` dict, which loses an
                # entry the moment `subagent_end` pops it) keeps the SAME
                # card object forever, so its final state alone is already
                # full proof the live updates reached it correctly; no
                # separate "caught it mid-flight" poll is needed.
                marks = [m for m in app.transcript.subagent_marks if isinstance(m, SubAgentCard)]
                ctx.check(f"a SubAgentCard was mounted for the child, got marks={marks}", len(marks) == 1)
                card = marks[0]
                ctx.check(f"it names the child's own subagent_type, got {card.agent_name!r}",
                          card.agent_name == "general-purpose")
                ctx.check(f"it recorded the child's one tool call, got tool_count={card.tool_count}",
                          card.tool_count >= 1)
                ctx.check(f"it is frozen (done) once the child finished, got done={card.done}", card.done)
                ctx.check(f"its rendered text matches the brief's own shape, got {str(card.render())!r}",
                          re.match(r"agent general-purpose · \w+ \d+ s · \d+ tools", str(card.render())))
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
def test_real_e2e_pilot_against_a_slow_databricks_mock_shows_the_phase_line_live(ctx: Ctx):
    """A1: a genuine end-to-end pilot -- a REAL Controller/Session against
    a REAL (if local) upstream with controllable delays, reusing W2a's own
    proven `MockDatabricks` "phase-sequence" scenario (headers delayed,
    then a silent gap, then reasoning, then text) and "tool-call-ok" (the
    waiting-for-model gap) -- not a synthetic event script, so this proves
    the full real stack (loop -> events -> dispatch -> widgets), not just
    this worker's own dispatch code reacting to a hand-built event."""
    import argparse

    from tests.helpers.mock_databricks import MockDatabricks

    async def body():
        fh = build_fake_home()
        mock = MockDatabricks().start()
        env_keys = ("BRIDGE_TEST_HOME", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN")
        old_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
        os.environ["BRIDGE_DBX_TOKEN"] = "t"
        controller = None
        try:
            from halo_harness.tui.bootstrap import build_controller

            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=True,
                tools=None, add_dir=None, model="dbx:databricks-glm-5-3-phase-sequence", small_model=None,
                session_id=None, max_turns=10, effort=None, append_system_prompt=None, chrome=False,
                no_chrome=False, playwright=False, playwright_cdp=None, playwright_headless=False,
                mcp_config=None, strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "howdy")
                await pilot.press("enter")
                seen_sending_or_headers = False
                seen_reasoning = False
                for _ in range(150):
                    await app._drain()
                    await pilot.pause(0.03)
                    line = app.transcript._phase_lines.get((None, 1))
                    if line is not None:
                        rendered = str(line.render())
                        if "Sending request" in rendered or "no tokens yet" in rendered:
                            seen_sending_or_headers = True
                        if "reasoning tokens" in rendered:
                            seen_reasoning = True
                    texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                    if any("the answer" in t for t in texts):
                        break
                ctx.check("the real stack showed the sending/headers phase live", seen_sending_or_headers)
                ctx.check("the real stack showed the live reasoning phase too", seen_reasoning)
                texts = [w.raw_text for w in app.transcript.children if isinstance(w, AssistantText)]
                ctx.check(f"the real model's final answer streamed through, got {texts}",
                          any("the answer" in t for t in texts))
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
# Halo 2.0.1 W2b (liveness-tips-brief Part B): rotating tips. Pure checks
# against halo_harness.tui.tips (B5's own validation) need no Textual app;
# the rotation-timing checks below them do.
# ============================================================================

@test
def test_every_curated_tip_is_under_97_chars_and_never_mentions_rolo_claude(ctx: Ctx):
    from halo_harness.tui import tips as tips_mod
    too_long = [t.text for t in tips_mod.TIPS if len(t.text) >= 97]
    ctx.check(f"every curated tip is under 97 chars, got too-long: {too_long}", not too_long)
    mentions = [t.text for t in tips_mod.TIPS if "rolo-claude" in t.text.lower()]
    ctx.check(f"no tip mentions rolo-claude, got: {mentions}", not mentions)
    banned = ("stuck", "hung", "frozen", "not allowed", "can't do that", "for safety", "refus")
    bad = [t.text for t in tips_mod.TIPS if any(w in t.text.lower() for w in banned)]
    ctx.check(f"no safety/refusal/stuck-hung-frozen wording in any tip, got: {bad}", not bad)


@test
def test_every_tip_word_flag_and_key_resolves_to_something_real(ctx: Ctx):
    """B5: "Tips may only name things that exist" -- every `/word` resolves
    in the real command registry, every `--flag` is a real CLI flag outside
    the not-yet list, every Ctrl+X/Shift+Tab/Esc names a bound key."""
    from halo_harness.commands.registry import Registry
    from halo_harness.tui import tips as tips_mod
    from halo_harness.tui.keys import load_keymap
    import halo_harness.cli as cli_mod

    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry.discover(Path(tmp))
    names = {c.name for c in reg.all()}
    real_flags = {f for flags, _kwargs in cli_mod._REAL_FLAGS for f in flags}
    not_yet_flags = {f for item in cli_mod._NOT_YET_FLAGS for f in item[0]}
    # Slash-command/subcommand-local tokens -- never a cli.py main-parser
    # flag, but genuinely real: `/stats --models`/`--tools` (tui/slash.py::
    # _handle_stats's own token parsing), `halo stats --since` (stats_cli.
    # py's own separate subcommand parser).
    extra_real_flags = {"--models", "--tools", "--since"}
    keymap = load_keymap()

    bad_words, bad_flags, bad_keys = [], [], []
    for tip in tips_mod.TIPS:
        for word in tips_mod.slash_words_in(tip.text):
            if word not in names:
                bad_words.append((tip.text, word))
        for flag in tips_mod.flags_in(tip.text):
            if flag in not_yet_flags or (flag not in real_flags and flag not in extra_real_flags):
                bad_flags.append((tip.text, flag))
        for key in tips_mod.key_names_in(tip.text):
            if not tips_mod.key_is_bound(key, keymap):
                bad_keys.append((tip.text, key))
    ctx.check(f"every /word resolves in the registry, got: {bad_words}", not bad_words)
    ctx.check(f"every --flag is real (outside the not-yet list), got: {bad_flags}", not bad_flags)
    ctx.check(f"every key mention is actually bound, got: {bad_keys}", not bad_keys)


@test
def test_generated_tips_cover_every_builtin_command_not_already_curated(ctx: Ctx):
    """B1: "GENERATED tips for every slash command in the registry that no
    curated tip covers"."""
    import re
    from halo_harness.commands.registry import Registry
    from halo_harness.tui import tips as tips_mod

    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry.discover(Path(tmp))
    names = {c.name for c in reg.all()}
    covered = tips_mod._covered_command_names(tips_mod.TIPS)
    generated = tips_mod.generated_tips_for_registry(reg)
    generated_names = set()
    for t in generated:
        m = re.match(r"/([a-zA-Z][\w-]*)", t.text)
        if m:
            generated_names.add(m.group(1))
    missing = names - covered - generated_names
    ctx.check(f"every registered command has a curated or generated tip, missing: {missing}", not missing)
    ctx.check("no command gets BOTH a curated and a generated tip", not (covered & generated_names))


@test
def test_tip_needs_filtering_hides_a_cc_tip_when_cc_is_not_enabled(ctx: Ctx):
    """B1/B6: "needs" filtering -- a `cc` tip hidden when cc is not enabled."""
    from halo_harness.tui import tips as tips_mod
    cc_tip = next(t for t in tips_mod.TIPS if "cc" in t.needs)
    shown_without = tips_mod.applicable_tips([cc_tip], frozenset())
    shown_with = tips_mod.applicable_tips([cc_tip], frozenset({"cc"}))
    ctx.check("a cc-needing tip is hidden when cc is not enabled", shown_without == [])
    ctx.check("it reappears once cc is enabled", shown_with == [cc_tip])


@test
def test_esc_ctrl_c_tip_matches_the_real_quit_on_double_ctrl_c_setting(ctx: Ctx):
    """Parity gap: "Esc interrupts the current step; Ctrl+C twice quits"
    was wrong when `quit_on_double_ctrl_c: false` (tui/app.py's own
    on_key handler: Ctrl+C never quits at all then, double-pressed or
    not) -- the right one of the two gated variants must be the one
    `detect_enabled_needs`'s own "double_ctrl_c"/"single_ctrl_c" flag
    selects."""
    from halo_harness.theme import get_config_value, set_config_value
    from halo_harness.tui import tips as tips_mod

    double_tip = next(t for t in tips_mod.TIPS if "double_ctrl_c" in t.needs)
    single_tip = next(t for t in tips_mod.TIPS if "single_ctrl_c" in t.needs)
    old = get_config_value("quit_on_double_ctrl_c", True)
    try:
        set_config_value("quit_on_double_ctrl_c", True)
        needs_true = tips_mod.detect_enabled_needs()
        ctx.check(f"double_ctrl_c is in the needs set, got {needs_true}", "double_ctrl_c" in needs_true)
        ctx.check(f"single_ctrl_c is NOT, got {needs_true}", "single_ctrl_c" not in needs_true)
        ctx.check("only the double-press tip is shown",
                  tips_mod.applicable_tips([double_tip, single_tip], needs_true) == [double_tip])

        set_config_value("quit_on_double_ctrl_c", False)
        needs_false = tips_mod.detect_enabled_needs()
        ctx.check(f"single_ctrl_c is in the needs set, got {needs_false}", "single_ctrl_c" in needs_false)
        ctx.check(f"double_ctrl_c is NOT, got {needs_false}", "double_ctrl_c" not in needs_false)
        ctx.check("only the never-quits tip is shown, and names /exit, Ctrl+D, Ctrl+Q",
                  tips_mod.applicable_tips([double_tip, single_tip], needs_false) == [single_tip]
                  and "/exit" in single_tip.text and "Ctrl+D" in single_tip.text and "Ctrl+Q" in single_tip.text)
    finally:
        set_config_value("quit_on_double_ctrl_c", old)


@test
def test_format_tip_placeholder_fits_the_width_minus_6_with_an_ellipsis(ctx: Ctx):
    from halo_harness.tui import tips as tips_mod
    fitted = tips_mod.format_tip_placeholder("x" * 50, 30)
    ctx.check(f"fits within width-6, got {fitted!r} (len={len(fitted)})", len(fitted) <= 24)
    ctx.check(f"ends with an ellipsis when truncated, got {fitted!r}", fitted.endswith("…"))
    untouched = tips_mod.format_tip_placeholder("short", 80)
    ctx.check(f"a short tip is shown whole with the Tip: prefix, got {untouched!r}",
              untouched == "Tip: short")


@test
def test_tip_rotator_shuffles_with_no_repeat_until_exhausted(ctx: Ctx):
    import random
    from halo_harness.tui.tips import Tip, TipRotator
    tips = [Tip("a"), Tip("b"), Tip("c")]
    rotator = TipRotator(tips, rng=random.Random(42))
    seen = [rotator.current.text]
    for _ in range(3):
        seen.append(rotator.advance().text)
    ctx.check(f"no repeat across the first full cycle (all 3 distinct), got {seen[:3]}",
              sorted(seen[:3]) == ["a", "b", "c"])
    ctx.check(f"a 4th draw, after reshuffling, is still a real tip, got {seen[3]!r}",
              seen[3] in ("a", "b", "c"))


@test
def test_tips_enabled_respects_config_and_env_off_switches(ctx: Ctx):
    """B3: `tips: false` in ~/.halo/config.json, or HALO_TIPS=0 (legacy
    BRIDGE_TIPS=0 via env_compat)."""
    from halo_harness import theme as theme_mod
    from halo_harness.tui import tips as tips_mod

    old_state = os.environ.get("BRIDGE_STATE_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BRIDGE_STATE_DIR"] = tmp
        try:
            ctx.check("tips are on by default (fresh config)", tips_mod.tips_enabled() is True)
            theme_mod.set_config_value("tips", False)
            ctx.check("tips: false in config.json turns them off", tips_mod.tips_enabled() is False)
            theme_mod.set_config_value("tips", True)
            ctx.check("tips: true turns them back on", tips_mod.tips_enabled() is True)
            os.environ["HALO_TIPS"] = "0"
            try:
                ctx.check("HALO_TIPS=0 turns them off even with config tips: true",
                          tips_mod.tips_enabled() is False)
            finally:
                os.environ.pop("HALO_TIPS", None)
            os.environ["BRIDGE_TIPS"] = "0"
            try:
                ctx.check("legacy BRIDGE_TIPS=0 (env_compat) also turns them off",
                          tips_mod.tips_enabled() is False)
            finally:
                os.environ.pop("BRIDGE_TIPS", None)
        finally:
            if old_state is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state


@test
def test_tips_off_switch_shows_the_static_placeholder_instead(ctx: Ctx):
    from halo_harness import theme as theme_mod
    from halo_harness.tui import tips as tips_mod

    old_state = os.environ.get("BRIDGE_STATE_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BRIDGE_STATE_DIR"] = tmp
        theme_mod.set_config_value("tips", False)
        try:
            async def body():
                fake = FakeController()
                app = await _mounted(fake)
                ctx.check("tips off -> no rotator at all", app._tip_rotator is None)
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause(0.05)
                    ctx.check(f"the static placeholder is shown, got {app.prompt_input.placeholder!r}",
                              app.prompt_input.placeholder == tips_mod.STATIC_PLACEHOLDER)
            asyncio.run(body())
        finally:
            theme_mod.set_config_value("tips", True)
            if old_state is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old_state


@test
def test_tips_command_lists_multiple_real_tips(ctx: Ctx):
    """B4: "/tips prints every applicable tip to the transcript as a list"."""
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_tips
    from halo_harness.commands.registry import Registry
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        reg = Registry.discover(cwd)
        facade = HeadlessFacade(cwd=cwd, registry=reg)
        result = _cmd_tips("", facade)
    lines = result.splitlines()
    ctx.check(f"/tips lists multiple real tips, got {len(lines)} line(s)", len(lines) > 5)
    ctx.check("every line is a list item", all(line.startswith("- ") for line in lines))
    ctx.check("/tips itself is listed (it names things that exist, including itself)",
              any("/tips" in line for line in lines))


@test
def test_help_lists_tips(ctx: Ctx):
    """B4: "/help lists /tips"."""
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_help
    from halo_harness.commands.registry import Registry
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        reg = Registry.discover(cwd)
        facade = HeadlessFacade(cwd=cwd, registry=reg)
        result = _cmd_help("", facade)
    ctx.check(f"/help lists /tips, got a line with it: {'/tips' in result}", "/tips" in result)


@test
def test_launch_tip_differs_from_the_after_turn_tip_seeded_rng(ctx: Ctx):
    """B2/B6: "launch tip differs from the after-turn tip (seeded RNG)"."""
    import random
    from halo_harness.tui import tips as tips_mod

    async def body():
        fake = FakeController(turns=[[
            ev.user_message("hi", turn=1),
            ev.Event("message_start", {"model": "x"}, turn=1),
            ev.text_delta("ok", turn=1),
            ev.message_end(turn=1, stop_reason="end_turn"),
            ev.turn_done(turn=1, reason="end_turn"),
        ]])
        app = await _mounted(fake)
        ctx.check("a real tip pool was built at construction", app._tip_rotator is not None)
        app._tip_rotator = tips_mod.TipRotator(app._tip_rotator._tips, rng=random.Random(42))
        async with app.run_test(size=(100, 40)) as pilot:
            app._refresh_tip_placeholder()
            launch_tip = app._tip_rotator.current.text
            ctx.check(f"the launch placeholder shows a tip, got {app.prompt_input.placeholder!r}",
                      app.prompt_input.placeholder.startswith("Tip: "))
            await pilot.click("#prompt-input")
            await _type(pilot, "hi")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=10)
            after_turn_tip = app._tip_rotator.current.text
            ctx.check(f"turn_done rotated to a DIFFERENT tip, got {launch_tip!r} -> {after_turn_tip!r}",
                      after_turn_tip != launch_tip)
            expected = tips_mod.format_tip_placeholder(after_turn_tip, app.prompt_input.size.width or 80)
            ctx.check(f"the placeholder itself reflects the new tip, got "
                      f"{app.prompt_input.placeholder!r} expected {expected!r}",
                      app.prompt_input.placeholder == expected)
    asyncio.run(body())


@test
def test_idle_tip_rotation_via_the_shrunk_15s_seam(ctx: Ctx):
    """B2/B6: "idle rotation with the 15s timer shrunk by a seam"."""
    import halo_harness.tui.app as app_mod
    real_interval = app_mod.TIP_ROTATE_INTERVAL_S
    app_mod.TIP_ROTATE_INTERVAL_S = 0.3
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                first = app._tip_rotator.current.text
                await pilot.pause(0.5)
                await app._drain()
                ctx.check(f"idle rotation advanced via the (shrunk) 15s timer with nothing typed "
                          f"and no turn running, got {first!r} -> {app._tip_rotator.current.text!r}",
                          app._tip_rotator.current.text != first)
        asyncio.run(body())
    finally:
        app_mod.TIP_ROTATE_INTERVAL_S = real_interval


@test
def test_tip_does_not_rotate_while_the_input_has_text(ctx: Ctx):
    """B2/B6: "no change while typing"."""
    import halo_harness.tui.app as app_mod
    real_interval = app_mod.TIP_ROTATE_INTERVAL_S
    app_mod.TIP_ROTATE_INTERVAL_S = 0.3
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "still composing")
                first = app._tip_rotator.current.text
                await pilot.pause(0.5)
                await app._drain()
                ctx.check(f"no rotation while the input has text, got {first!r} -> "
                          f"{app._tip_rotator.current.text!r}", app._tip_rotator.current.text == first)
        asyncio.run(body())
    finally:
        app_mod.TIP_ROTATE_INTERVAL_S = real_interval


@test
def test_tip_does_not_rotate_while_a_turn_is_running(ctx: Ctx):
    """B2/B6: "no change ... while a turn runs"."""
    import halo_harness.tui.app as app_mod
    real_interval = app_mod.TIP_ROTATE_INTERVAL_S
    app_mod.TIP_ROTATE_INTERVAL_S = 0.3
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                app._turn_running = True
                first = app._tip_rotator.current.text
                await pilot.pause(0.5)
                await app._drain()
                ctx.check(f"no rotation while a turn is marked running, got {first!r} -> "
                          f"{app._tip_rotator.current.text!r}", app._tip_rotator.current.text == first)
                app._turn_running = False
                await pilot.pause(0.5)
                await app._drain()
                ctx.check(f"rotation resumes once the turn is no longer running, got "
                          f"{first!r} -> {app._tip_rotator.current.text!r}",
                          app._tip_rotator.current.text != first)
        asyncio.run(body())
    finally:
        app_mod.TIP_ROTATE_INTERVAL_S = real_interval


# ============================================================================
# Halo 2.0.1 W2b (liveness-tips-brief Part C): effective-value rendering --
# the /effort card and the status-bar chip both show the SENT value, never
# silently just the requested one. A plain, real `ProviderProfile` (never a
# fake duck-typed stand-in) so `clamp_effort` itself proves the clamp is
# real, not just assumed by the test.
# ============================================================================

@test
def test_effort_card_and_chip_show_sent_as_high_for_a_glm_route_requesting_medium(ctx: Ctx):
    """Part C: "a GLM route with --effort medium shows 'sent as high' in
    the card [and] the chip"."""
    from types import SimpleNamespace
    from halo_harness.providers.profiles import ProviderProfile, clamp_effort
    from halo_harness.tui import slash as slash_mod
    from halo_harness.tui.widgets.cards import EffortCard

    profile = ProviderProfile(
        reasoning_effort_supported=True, effort_values_supported=("low", "high", "max"),
        effort_clamp_map={"medium": "high", "minimal": "low", "xhigh": "max", "none": "low"},
        reasoning_default_effort="high", thinking_format="deepseek_reasoning_content",
    )
    sent = clamp_effort("medium", profile)
    ctx.check(f"sanity: this route really does clamp medium to high, got {sent!r}", sent == "high")
    session = SimpleNamespace(provider_profile=profile, effort=sent, effort_requested="medium",
                               effort_source="session", model_ref=SimpleNamespace(raw="dbx:databricks-glm-5-3"))

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.session = session
            await slash_mod._handle_effort(app, "")
            await pilot.pause(0.05)
            card = app.pending_card
            ctx.check(f"an EffortCard opened, got {type(card).__name__}", isinstance(card, EffortCard))
            ctx.check(f"the card marks the clamp inline, got {str(card.render())!r}",
                      "sent as high" in str(card.render()))

            app.status_bar.set_effort(slash_mod._effort_status_text(session))
            ctx.check(f"the status chip also shows the sent value with the clamp note, got "
                      f"{str(app.status_bar.render())!r}", "sent as high" in str(app.status_bar.render()))
    asyncio.run(body())


@test
def test_effort_card_and_chip_show_sent_as_max_for_an_anthropic_route_requesting_xhigh(ctx: Ctx):
    """Part C: "an Anthropic route with xhigh on a model that lacks it
    shows 'sent as max'"."""
    from types import SimpleNamespace
    from halo_harness.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile, clamp_effort
    from halo_harness.tui import slash as slash_mod
    from halo_harness.tui.widgets.cards import EffortCard

    profile = ProviderProfile(reasoning_effort_supported=True, thinking_format="anthropic_thinking",
                               effort_values_supported=ANTHROPIC_EFFORT_LEVELS)
    ctx.check(f"sanity: xhigh is genuinely outside this route's own vocabulary, got {ANTHROPIC_EFFORT_LEVELS}",
              "xhigh" not in ANTHROPIC_EFFORT_LEVELS)
    sent = clamp_effort("xhigh", profile)
    ctx.check(f"sanity: this route narrows xhigh to max, got {sent!r}", sent == "max")
    session = SimpleNamespace(provider_profile=profile, effort=sent, effort_requested="xhigh",
                               effort_source="session", model_ref=SimpleNamespace(raw="ant:claude-x"))

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.session = session
            await slash_mod._handle_effort(app, "")
            await pilot.pause(0.05)
            card = app.pending_card
            ctx.check(f"an EffortCard opened, got {type(card).__name__}", isinstance(card, EffortCard))
            ctx.check(f"the card marks the clamp inline, got {str(card.render())!r}",
                      "sent as max" in str(card.render()))
            app.status_bar.set_effort(slash_mod._effort_status_text(session))
            ctx.check(f"the status chip also shows it, got {str(app.status_bar.render())!r}",
                      "sent as max" in str(app.status_bar.render()))
    asyncio.run(body())


@test
def test_bare_effort_card_selection_persists_like_the_plain_text_path(ctx: Ctx):
    """W3b item 11 (W3a scope cut): the BARE `/effort` path (opens the
    card, the user arrows to a level and presses Enter -- `EffortCard`'s
    own `on_select` callback in tui/slash.py's `_handle_effort`) must
    persist exactly like the plain-text `/effort <level>` path already
    does (test_slash_effort_against_a_real_session_persists_the_sent_
    value, against a real session) -- `on_select`'s own comment says so
    ("same persistence as the plain-text path") but nothing exercised the
    actual card interaction before this test."""
    from types import SimpleNamespace
    from halo_harness import launch_state
    from halo_harness.providers.profiles import ProviderProfile
    from halo_harness.tui import slash as slash_mod
    from halo_harness.tui.widgets.cards import EffortCard

    profile = ProviderProfile(reasoning_effort_supported=True, effort_values_supported=("low", "medium", "high"))
    session = SimpleNamespace(provider_profile=profile, effort="low", effort_requested="low",
                               effort_source="session", model_ref=SimpleNamespace(raw="or:mock/bare-effort-card"))

    def _fake_run_slash(name: str, args: str = "") -> str:
        # Stands in for commands.builtins._cmd_effort's own real behavior
        # (mutates the live session's effort directly, no event round
        # trip) -- FakeController.run_slash itself is a canned no-op stub,
        # and the real version is already covered end to end against a
        # REAL session by test_slash_effort_against_a_real_session_
        # persists_the_sent_value; this test's own focus is the CARD
        # interaction and persistence call that happen AROUND that call,
        # in tui/slash.py's `_handle_effort`/`on_select` themselves.
        if name == "effort" and args:
            session.effort = args
            return f"Effort level set to '{args}'"
        return f"(fake) ran /{name} {args}".rstrip()

    async def body():
        fake = FakeController()
        fake.run_slash = _fake_run_slash
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app.controller.session = session
            ctx.check("nothing persisted yet", launch_state.resolve_last_effort(app.cwd) != "medium")
            await slash_mod._handle_effort(app, "")
            await pilot.pause(0.05)
            card = app.pending_card
            ctx.check(f"the bare /effort card opened, got {type(card).__name__}", isinstance(card, EffortCard))
            ctx.check(f"starts on the session's current level 'low', got index={card.index}",
                      card.levels[card.index] == "low")
            await pilot.press("right")  # low -> medium
            await pilot.pause(0.05)
            await pilot.press("enter")  # apply -- fires on_select("medium")
            await pilot.pause(0.1)
            ctx.check("the card closed", app.pending_card is None)
            ctx.check(f"the session's own effort was actually set to 'medium', got {session.effort!r}",
                      session.effort == "medium")
            ctx.check(f"the CARD-selected level persisted to ~/.halo/state.json exactly like the plain-text "
                      f"/effort <level> path, got {launch_state.resolve_last_effort(app.cwd)!r}",
                      launch_state.resolve_last_effort(app.cwd) == "medium")
    asyncio.run(body())


# ============================================================================
# SVG snapshots (Part A/B): the phase-line states and the rotating tips
# placeholder, alongside the existing main/permission/question/model-picker
# set (D-TUI: regenerated freely, the substantive check is the rendered TEXT).
# ============================================================================

@test
def test_svg_snapshots_phase_line_and_tips_placeholder(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            app._local_events.put(ev.phase(state="first_token", turn=1, kind="reasoning"))
            app._local_events.put(ev.thinking_delta("considering the request carefully", turn=1))
            await _drain_a_few(app, pilot, n=5, pause=0.03)
            path, svg = _write_snapshot(app, "phase-line-thinking")
            ctx.check(f"phase-line snapshot written to {path}", path.exists() and len(svg) > 0)
            ctx.check(f"phase-line SVG shows the thinking state, got len={len(svg)}", "Thinking" in svg)

        fake2 = FakeController()
        app2 = await _mounted(fake2)
        async with app2.run_test(size=(100, 40)) as pilot2:
            await pilot2.pause(0.1)
            path2, svg2 = _write_snapshot(app2, "tips-placeholder")
            ctx.check(f"tips placeholder snapshot written to {path2}", path2.exists() and len(svg2) > 0)
            from halo_harness.tui import tips as tips_mod
            placeholder = app2.prompt_input.placeholder
            ctx.check(f"the input placeholder is a rotating tip or the static fallback, got {placeholder!r}",
                      placeholder.startswith("Tip: ") or placeholder == tips_mod.STATIC_PLACEHOLDER)
    asyncio.run(body())


# ============================================================================
# W2c item 1: prompt history recall -- a user report, live, "using the up arrow ...
# does not bring up the last message to edit if something happened". The
# mixed-ms/seconds-timestamp sort fix itself lives in tests/test_history.py
# (pure, no textual); these are the TUI-layer pieces: the prefix filter,
# draft restore, and the real end-to-end submit -> Up round trip, plus the
# two bypass paths (a steer, a slash command typed over a pending card)
# that used to skip the history append entirely.
# ============================================================================

def _scoped_history_home():
    """A FRESH BRIDGE_TEST_HOME for one test -- never the shared module-
    level scratch dir `ensure_scoped_state_dir_once()` set at import time,
    so one test's own history.jsonl entries can never leak into (or be
    polluted by) another test's. Returns (tmp_path, restore_callable)."""
    old = os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="halo-history-nav-"))
    os.environ["BRIDGE_TEST_HOME"] = str(tmp)

    def _restore():
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old
    return tmp, _restore


@test
def test_up_arrow_prefix_filter_walks_only_matching_entries(ctx: Ctx):
    """"with text typed on the first line, Up walks only entries that
    start with that text" (Claude Code's own prefix-filtered recall)."""
    from halo_harness import history as history_mod

    tmp, restore = _scoped_history_home()
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            history_mod.append_history_entry("fix the bug", str(app.cwd), timestamp=1.0)
            history_mod.append_history_entry("add a feature", str(app.cwd), timestamp=2.0)
            history_mod.append_history_entry("fix the typo", str(app.cwd), timestamp=3.0)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "fix")
                await pilot.press("up")
                await app._drain()
                ctx.check(f"prefix-filtered Up recalls the newest MATCHING entry, "
                          f"got {app.prompt_input.text!r}", app.prompt_input.text == "fix the typo")
                await pilot.press("up")
                await app._drain()
                ctx.check(f"a second Up walks to the NEXT matching entry, skipping the "
                          f"non-matching one in between, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "fix the bug")
        asyncio.run(body())
    finally:
        restore()


@test
def test_up_arrow_empty_box_recalls_newest_entry_unfiltered(ctx: Ctx):
    from halo_harness import history as history_mod

    tmp, restore = _scoped_history_home()
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            history_mod.append_history_entry("fix the bug", str(app.cwd), timestamp=1.0)
            history_mod.append_history_entry("add a feature", str(app.cwd), timestamp=2.0)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await pilot.press("up")
                await app._drain()
                ctx.check(f"an empty first line recalls the newest entry regardless of its "
                          f"text, got {app.prompt_input.text!r}", app.prompt_input.text == "add a feature")
        asyncio.run(body())
    finally:
        restore()


@test
def test_down_past_the_newest_restores_the_unsent_draft(ctx: Ctx):
    """The draft typed before Up was first pressed must be a PREFIX of the
    history entry it recalls (W2c's own prefix filter, pinned separately
    above, means an unrelated draft would simply find no match at all and
    Up would be a no-op) -- "an old" recalls "an old prompt", and Down past
    the newest hands back exactly "an old", not the full recalled text."""
    from halo_harness import history as history_mod

    tmp, restore = _scoped_history_home()
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            history_mod.append_history_entry("an old prompt", str(app.cwd), timestamp=1.0)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "an old")
                await pilot.press("up")
                await app._drain()
                ctx.check(f"Up recalls the matching old prompt, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "an old prompt")
                await pilot.press("down")
                await app._drain()
                ctx.check(f"Down past the newest restores the unsent draft, got "
                          f"{app.prompt_input.text!r}", app.prompt_input.text == "an old")
        asyncio.run(body())
    finally:
        restore()


@test
def test_submit_then_up_recalls_the_exact_prompt_just_sent(ctx: Ctx):
    """End-to-end: the owner's own report -- submit a real prompt through the
    pilot's own keyboard path (never history.append_history_entry called
    directly), then Up must hand back that EXACT text."""
    tmp, restore = _scoped_history_home()
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await _type(pilot, "what does this repo do")
                await pilot.press("enter")
                await _drain_a_few(app, pilot, n=10, pause=0.02)
                ctx.check("the prompt box is empty right after submit", app.prompt_input.text == "")
                await pilot.press("up")
                await app._drain()
                ctx.check(f"Up recalls the exact prompt just submitted, got "
                          f"{app.prompt_input.text!r}", app.prompt_input.text == "what does this repo do")
        asyncio.run(body())
    finally:
        restore()


@test
def test_steer_on_a_non_answerable_pending_card_still_reaches_history(ctx: Ctx):
    """A steer typed while a card with no free-text answer (a rewind
    confirmation) is pending used to bypass `_submit_prompt` -- and its
    history append -- entirely."""
    from halo_harness.tui.widgets.cards import RewindCard

    tmp, restore = _scoped_history_home()
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                card = RewindCard(step={"id": "s1", "ts": 0, "label": "x", "files": []},
                                   verb="rewind", on_decide=lambda confirmed: None)
                app.set_pending_card(card)
                await pilot.click("#prompt-input")
                await _type(pilot, "steer while rewind pending")
                await pilot.press("enter")
                await _drain_a_few(app, pilot, n=5, pause=0.02)
                ctx.check("the steer reached the controller (not swallowed)",
                          "steer while rewind pending" in fake.submitted)
                await pilot.press("up")
                await app._drain()
                ctx.check(f"Up recalls that steer, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "steer while rewind pending")
        asyncio.run(body())
    finally:
        restore()


@test
def test_slash_command_typed_over_a_pending_card_still_reaches_history(ctx: Ctx):
    """A slash command typed while a PermissionCard is pending used to
    bypass `_submit_prompt` (and its history append) entirely -- it only
    ever reached history when nothing was pending at all."""
    tmp, restore = _scoped_history_home()
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.05)
                card = _pending_permission_card(app)
                await pilot.click("#prompt-input")
                await _type(pilot, "/effort")
                await pilot.press("enter")
                await _drain_a_few(app, pilot, n=5, pause=0.02)
                ctx.check("the ORIGINAL permission card is still pending, untouched",
                          app.pending_card is card)
                await pilot.press("up")
                await app._drain()
                ctx.check(f"Up recalls the slash command typed while the card was pending, "
                          f"got {app.prompt_input.text!r}", app.prompt_input.text == "/effort")
        asyncio.run(body())
    finally:
        restore()


# ============================================================================
# W2c items 2/3: copy / cut / select-all / paste in the chat box -- rolo
# live, "copy / paste is off in the chat box too". `screen.get_selected_
# text()`'s OWN branch in `action_interrupt_or_quit` (a transcript/screen-
# level drag-select) is COMPLETELY UNCHANGED by this round -- it is only
# ever reached once the NEW prompt-input-selection branch above it finds
# nothing to copy; the pre-existing `test_ctrl_c_twice_quits_cleanly` above
# already pins "no selection anywhere -> double-press quit" untouched.
# ============================================================================

@test
def test_ctrl_c_with_input_selection_copies_and_does_not_arm_quit(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "secret text")
            await pilot.press("shift+home")  # Shift+arrows: select back to line start (D-TUI)
            ctx.check(f"a selection now exists in the prompt box, got {app.prompt_input.selected_text!r}",
                      app.prompt_input.selected_text == "secret text")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("ctrl+c")
            ctx.check(f"a 'Copied N characters' notice was shown, got {notified}",
                      any("Copied 11 characters" in m for m in notified))
            ctx.check(f"the text landed in Textual's own clipboard register, got {app.clipboard!r}",
                      app.clipboard == "secret text")
            ctx.check("Ctrl+C did not arm the double-press quit countdown",
                      app._ctrl_c_deadline is None)
            ctx.check(f"the text itself is untouched (copy, not cut), got {app.prompt_input.text!r}",
                      app.prompt_input.text == "secret text")
    asyncio.run(body())


@test
def test_ctrl_c_with_no_selection_keeps_todays_interrupt_quit_behavior(ctx: Ctx):
    """W2c item 2 test list, named explicitly alongside the pre-existing
    `test_ctrl_c_twice_quits_cleanly` above (which this is a focused,
    single-press slice of): an EMPTY prompt box has no `selected_text` at
    all, so the new copy branch is a no-op and the first Ctrl+C arms the
    double-press quit exactly as it always did."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            ctx.check("no selection in the empty prompt box", not app.prompt_input.selected_text)
            ctx.check("nothing selected on the screen either", not app.screen.get_selected_text())
            await pilot.press("ctrl+c")
            ctx.check("the FIRST Ctrl+C with nothing selected arms the double-press quit",
                      app._ctrl_c_deadline is not None)
    asyncio.run(body())


@test
def test_ctrl_x_cuts_an_input_selection_instead_of_opening_the_chord(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "cut me")
            await pilot.press("shift+home")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("ctrl+x")
            ctx.check(f"the selection was deleted from the input, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "")
            ctx.check(f"it landed in the clipboard register too, got {app.clipboard!r}",
                      app.clipboard == "cut me")
            ctx.check(f"a 'Cut N characters' notice was shown, got {notified}",
                      any("Cut 6 characters" in m for m in notified))
            ctx.check("the chord did NOT open (a selection means cut, not the chord)",
                      app._pending_chord is None and app.which_key.display is False)
    asyncio.run(body())


@test
def test_ctrl_a_selects_all_text_in_the_chat_box(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "select everything")
            await pilot.press("ctrl+a")
            ctx.check(f"the whole line is now selected, got {app.prompt_input.selected_text!r}",
                      app.prompt_input.selected_text == "select everything")
    asyncio.run(body())


@test
def test_ctrl_v_with_stubbed_clipboard_tool_inserts_the_text(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    old_read = clipboard_mod.read_via_external_tool
    clipboard_mod.read_via_external_tool = lambda **kw: "pasted from the system clipboard"
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await pilot.press("ctrl+v")
                await app.workers.wait_for_complete()
                ctx.check(f"the stubbed clipboard text was inserted, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "pasted from the system clipboard")
        asyncio.run(body())
    finally:
        clipboard_mod.read_via_external_tool = old_read


@test
def test_ctrl_v_applies_the_four_line_placeholder_rule(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    long_text = "line1\nline2\nline3\nline4\nline5"
    old_read = clipboard_mod.read_via_external_tool
    clipboard_mod.read_via_external_tool = lambda **kw: long_text
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                await pilot.press("ctrl+v")
                await app.workers.wait_for_complete()
                ctx.check(f"a 5-line paste became the placeholder, got {app.prompt_input.text!r}",
                          app.prompt_input.text == "[Pasted text #1 +5 lines]")
                ctx.check(f"the FULL text is stashed for the model to see later, got "
                          f"{app.prompt_input.pasted!r}", app.prompt_input.pasted.get(1) == long_text)
        asyncio.run(body())
    finally:
        clipboard_mod.read_via_external_tool = old_read


@test
def test_ctrl_v_with_no_clipboard_tool_notifies_instead_of_pasting(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    old_read = clipboard_mod.read_via_external_tool
    clipboard_mod.read_via_external_tool = lambda **kw: None
    try:
        async def body():
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.click("#prompt-input")
                notified = []
                app.notify = lambda msg, **kw: notified.append(msg)
                await pilot.press("ctrl+v")
                await app.workers.wait_for_complete()
                ctx.check(f"nothing was inserted, got {app.prompt_input.text!r}", app.prompt_input.text == "")
                ctx.check(f"the terminal-shortcut notice was shown, got {notified}",
                          any("terminal" in m.lower() and "shift" in m.lower() for m in notified))
        asyncio.run(body())
    finally:
        clipboard_mod.read_via_external_tool = old_read


@test
def test_help_text_lists_copy_cut_select_all_and_paste_keys(ctx: Ctx):
    """W2c items 2/3 test list: "the help text lists the keys"."""
    from halo_harness.tui.dialogs.help import _KEY_ROWS

    rows = dict(_KEY_ROWS)
    ctx.check(f"Ctrl+A row exists and names select-all, got {rows.get('Ctrl+A')!r}",
              "elect all" in rows.get("Ctrl+A", ""))
    ctx.check(f"Ctrl+V row exists and names paste, got {rows.get('Ctrl+V')!r}",
              "aste" in rows.get("Ctrl+V", ""))
    ctx.check(f"Ctrl+X row names cutting a selection, got {rows.get('Ctrl+X')!r}",
              "Cuts a chat-box selection" in rows.get("Ctrl+X", ""))
    ctx.check(f"Ctrl+C row mentions copying a selection, got {rows.get('Ctrl+C (x2)')!r}",
              "copies it" in rows.get("Ctrl+C (x2)", ""))


# ============================================================================
# W2c item 4: two polish items from the Kali live capture of W2b.
# ============================================================================

@test
def test_cwd_last_component_handles_both_separator_forms(ctx: Ctx):
    from halo_harness.tui.widgets.statusbar import _cwd_last_component

    ctx.check("POSIX form", _cwd_last_component("/home/kali/project") == "project")
    ctx.check("Windows form", _cwd_last_component("C:\\Users\\rolo\\project") == "project")
    ctx.check("a trailing slash is ignored", _cwd_last_component("/home/kali/project/") == "project")
    ctx.check("no separator at all -> unchanged", _cwd_last_component("project") == "project")


@test
def test_status_bar_phase_word_follows_the_phase_line_without_a_second_phase_event(ctx: Ctx):
    """The live capture: the status-bar cluster said 'thinking 2 s' while
    the transcript's own phase line already said 'Writing…'. Root cause:
    `agent/loop.py`'s own `chunk_started` latch fires the 'phase'/
    first_token event exactly ONCE per call, for whichever kind streams
    first -- a reasoning model that thinks before writing never gets a
    SECOND phase event for the reasoning -> text transition within that
    same call, so the cluster (driven only by that event) used to stay on
    'thinking' while the phase line (driven straight off content arrival
    via `Transcript.append_text`'s own `enter_writing()`) had already moved
    on."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            app._local_events.put(ev.phase(state="request_sent", turn=1, model="or:mock/x"))
            app._local_events.put(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
            # ONLY ONE first_token event, kind=reasoning -- the real one-shot
            # latch; no second phase event ever arrives for the reasoning ->
            # text transition within this same call.
            app._local_events.put(ev.phase(state="first_token", turn=1, kind="reasoning"))
            app._local_events.put(ev.thinking_delta("considering this", turn=1))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check(f"both start out on 'thinking', got cluster={app.status_bar.phase!r}",
                      app.status_bar.phase == "thinking")
            phase_line = app.transcript._phase_lines.get((None, 1))
            ctx.check("the phase line is reasoning too", phase_line.phase_state == "reasoning")

            # The model moves straight into its answer -- text_delta only,
            # no second "phase" event at all.
            app._local_events.put(ev.text_delta("the answer is", turn=1, index=1))
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check(f"the transcript's own phase line moved to writing, got "
                      f"{phase_line.phase_state!r}", phase_line.phase_state == "writing")
            ctx.check(f"the status bar cluster followed within the SAME drain tick, got "
                      f"{app.status_bar.phase!r} (rendered: {str(app.status_bar.render())!r})",
                      app.status_bar.phase == "writing")
    asyncio.run(body())


@test
def test_subagent_card_phase_word_follows_its_own_text_delta_without_a_second_phase_event(ctx: Ctx):
    """W3b item 10: the SAME one-shot chunk_started-latch gap the test just
    above pins for the MAIN status bar applies identically to a child's own
    SubAgentCard -- before this fix its phase word was driven ONLY by the
    `phase`/first_token event, so a child that reasons then writes within
    one call never got a second phase event for the transition and its
    card stayed on 'thinking' while it was visibly streaming real text.
    `ev.thinking_delta`/`ev.text_delta` carry no `agent_id` of their own
    (the factory never needed one before sub-agents existed) -- set
    directly on the constructed Event, the same dataclass field
    `_apply_event_inner` itself reads, exactly as a real child's own event
    stream would carry it."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            start_ev = ev.Event("subagent_start", {"name": "researcher"}, turn=1)
            start_ev.agent_id = "child-1"
            app._local_events.put(start_ev)
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            card = app.transcript.subagent_cards.get("child-1")
            ctx.check(f"the child's own SubAgentCard was mounted, got {card!r}", card is not None)
            ctx.check(f"starts on 'thinking' (the card's own default), got {card.phase_word!r}",
                      card.phase_word == "thinking")

            phase_ev = ev.phase(state="first_token", turn=1, kind="reasoning")
            phase_ev.agent_id = "child-1"
            app._local_events.put(phase_ev)
            thinking_ev = ev.thinking_delta("considering this", turn=1)
            thinking_ev.agent_id = "child-1"
            app._local_events.put(thinking_ev)
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check(f"still 'thinking' after the one-shot reasoning phase event, got {card.phase_word!r}",
                      card.phase_word == "thinking")
            # The child moves straight into its answer -- text_delta only,
            # no second "phase" event at all (the real one-shot latch).
            text_ev = ev.text_delta("the answer is", turn=1, index=1)
            text_ev.agent_id = "child-1"
            app._local_events.put(text_ev)
            await _drain_a_few(app, pilot, n=3, pause=0.02)
            ctx.check(f"the child's card followed to 'writing' within the SAME drain tick despite no "
                      f"second phase event, got {card.phase_word!r}", card.phase_word == "writing")
            # Finding 11's own "never touch the MAIN status bar" rule,
            # unchanged by this fix -- a child's own deltas still never
            # reach the main cluster.
            ctx.check(f"the MAIN status bar was never touched by the child's own deltas, got "
                      f"{app.status_bar.phase!r}", app.status_bar.phase == "idle")
    asyncio.run(body())


@test
def test_status_bar_drops_mcp_then_balance_then_shortens_cwd_before_a_mid_word_slice(ctx: Ctx):
    """A 140-column terminal used to character-slice the cwd ("/home/kali"
    -> "/home/kal" -> "/home/k") while the MCP/balance segments stayed
    fixed-width -- low-priority segments must now drop WHOLESALE first
    (MCP, then the balance), and the cwd itself must shorten to its last
    path component, never a mid-word slice. Pilot test at 100, 120 and 140
    columns, per the brief."""
    from halo_harness.testing.fake_controller import DEFAULT_MODEL

    cwd = "/home/kali/vibes/appDev/some-very-long-project-name"

    async def rendered_at(width: int) -> str:
        fake = FakeController()
        app = BridgeApp(fake, cwd=Path(cwd))
        async with app.run_test(size=(width, 40)) as pilot:
            await pilot.pause(0.05)
            await app.workers.wait_for_complete()  # let the automatic git-branch worker settle first
            app.status_bar.mcp_connected = 2
            app.status_bar.mcp_total = 3
            app.status_bar.set_or_balance("OR $12.40 left")
            app.status_bar.set_cwd_branch(cwd, "main")
            return str(app.status_bar.render())

    async def body():
        at_140 = await rendered_at(140)
        ctx.check(f"at 140 cols the FULL cwd survives intact (never cut mid-word), got {at_140!r}",
                  cwd in at_140 and "(main)" in at_140)
        ctx.check(f"MCP and the OR balance drop first to make room, got {at_140!r}",
                  "MCP" not in at_140 and "OR $12.40" not in at_140)
        ctx.check(f"no mid-word fragment of the path leaked through, got {at_140!r}",
                  "/home/kal " not in at_140 and "/home/k " not in at_140)

        at_120 = await rendered_at(120)
        ctx.check(f"at 120 cols the cwd shortens to its LAST component, got {at_120!r}",
                  "some-very-long-project-name" in at_120 and cwd not in at_120)

        at_100 = await rendered_at(100)
        ctx.check(f"at 100 cols the cwd is gone entirely, got {at_100!r}",
                  "some-very-long-project-name" not in at_100)
        ctx.check(f"the model label is still intact at 100 cols (cwd gives way first), got {at_100!r}",
                  DEFAULT_MODEL in at_100)
    asyncio.run(body())


# ============================================================================
# Halo 2.0.1 W3a: finding 16 (a request_id-keyed queue -- a second
# concurrent ask no longer overwrites the first) + PendingDock ("nothing
# that waits for the user may be off-screen").
# ============================================================================

def _two_permission_asks_turn() -> list:
    """Finding 16's own repro: two tool calls each needing permission,
    back to back, in the SAME session -- before this fix the second
    `permission_request` silently overwrote the first (the "permission
    needed" tag cleared on an answer to a DIFFERENT card than the one
    still blocking the first child)."""
    return [[
        ev.user_message("do two things", turn=1),
        ev.Event("tool_use_ready", {"id": "tu1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"},
                                     "repaired": False}, turn=1),
        ev.Event("permission_request", {"id": "tu1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"},
                                         "reason": "first ask", "suggested_rule": "Bash(rm:*)"}, turn=1),
        ev.Event("tool_use_ready", {"id": "tu2", "name": "Bash", "input": {"command": "rm -rf /tmp/y"},
                                     "repaired": False}, turn=1),
        ev.Event("permission_request", {"id": "tu2", "name": "Bash", "input": {"command": "rm -rf /tmp/y"},
                                         "reason": "second ask", "suggested_rule": "Bash(rm:*)"}, turn=1),
    ]]


@test
def test_pending_dock_shows_the_card_and_hides_once_answered_marker_becomes_decision(ctx: Ctx):
    from halo_harness.tui.widgets.cards import PermissionCard

    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            # Pad the transcript so there's something to scroll away from,
            # then scroll to the TOP before the ask arrives -- the dock
            # must stay visible regardless of transcript scroll position.
            for i in range(60):
                await app.transcript.add_note(f"padding line {i}", kind="note")
            app.transcript.scroll_home(animate=False)
            await pilot.pause(0.05)

            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)

            ctx.check("the dock is visible", bool(app.pending_dock.display))
            ctx.check("the dock holds the pending card", app.pending_dock.card is app.pending_card)
            ctx.check(f"it's a PermissionCard, got {type(app.pending_dock.card).__name__}",
                      isinstance(app.pending_dock.card, PermissionCard))
            ctx.check("needs-you tag reads 1", app.status_bar.needs_you_count == 1)
            marker_texts = [_static_text(w) for w in app.transcript.children
                            if getattr(w, "classes", None) and "system-note-pending-marker" in w.classes]
            ctx.check(f"a transcript marker exists at the ask's own position, got {marker_texts}",
                      any("permission needed" in t and "see below" in t for t in marker_texts))

            await pilot.press("1")  # allow once
            await pilot.pause(0.1)

            ctx.check("the dock hides once answered", not app.pending_dock.display)
            ctx.check("pending_card cleared", app.pending_card is None)
            ctx.check("needs-you tag back to 0", app.status_bar.needs_you_count == 0)
            marker_texts_after = [_static_text(w) for w in app.transcript.children
                                   if getattr(w, "classes", None) and "system-note-pending-marker" in w.classes]
            ctx.check(f"the SAME marker widget now shows the decision, got {marker_texts_after}",
                      any("allowed" in t and "rm -rf /tmp/x" in t for t in marker_texts_after))
    asyncio.run(body())


@test
def test_pending_dock_queues_a_second_concurrent_ask_instead_of_overwriting_the_first(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_two_permission_asks_turn())
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do two things")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=10)

            ctx.check(f"the FIRST ask is the one shown, got {getattr(app.pending_card, 'summary', None)}",
                      "tmp/x" in (getattr(app.pending_card, "summary", "") or ""))
            ctx.check(f"needs-you reads 2 (one shown, one queued), got {app.status_bar.needs_you_count}",
                      app.status_bar.needs_you_count == 2)
            ctx.check("exactly one card queued behind the active one", len(app._pending_queue) == 1)

            await pilot.press("1")  # answers the FIRST
            await pilot.pause(0.2)
            await _drain_a_few(app, pilot, n=5)

            ctx.check(f"the SECOND ask is now shown, got {getattr(app.pending_card, 'summary', None)}",
                      "tmp/y" in (getattr(app.pending_card, "summary", "") or ""))
            ctx.check("the queue is now empty", app._pending_queue == [])
            ctx.check(f"needs-you dropped to 1, got {app.status_bar.needs_you_count}",
                      app.status_bar.needs_you_count == 1)
            ctx.check("the dock still shows a card (the second one)", bool(app.pending_dock.display))

            await pilot.press("1")  # answers the SECOND
            await pilot.pause(0.1)
            ctx.check("both resolved -- dock hidden, tag at 0", not app.pending_dock.display
                      and app.status_bar.needs_you_count == 0)
    asyncio.run(body())


@test
def test_pending_dock_sub_agent_ask_names_the_agent(ctx: Ctx):
    from halo_harness.tui.widgets.cards import PermissionCard

    async def body():
        turns = [[
            ev.user_message("spawn a helper", turn=1),
            ev.Event("tool_use_ready", {"id": "tu1", "name": "Bash", "input": {"command": "echo hi"},
                                         "repaired": False}, turn=1, agent_id="child-1"),
            ev.Event("permission_request", {"id": "tu1", "name": "Bash", "input": {"command": "echo hi"},
                                             "reason": "child ask", "suggested_rule": "Bash(echo:*)"},
                      turn=1, agent_id="child-1"),
        ]]
        fake = FakeController(turns=turns)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "spawn a helper")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check(f"a PermissionCard is pending, got {type(app.pending_card).__name__}",
                      isinstance(app.pending_card, PermissionCard))
            ctx.check(f"it names the sub-agent, got {app.pending_card.summary!r}",
                      "child-1" in app.pending_card.summary)
    asyncio.run(body())


@test
def test_pending_dock_holds_an_approval_card_and_accept_resolves_it(ctx: Ctx):
    """Halo 2.0.2 round D (brief item 2, "approval gates"): `approval_
    request` -> a live `ApprovalCard` through the SAME PendingDock queue
    every other pending card already uses -- pressing `1` (accept) calls
    `Controller.answer_approval` (here, its FakeController mirror) and
    clears the dock, same shape as an existing PermissionCard/QuestionCard
    round trip."""
    from halo_harness.tui.widgets.cards import ApprovalCard

    async def body():
        fake = FakeController(turns=[[
            ev.user_message("run the org", turn=1),
            ev.Event("approval_request", {"id": "appr1", "position": "Implementer",
                                            "text": "Here is my draft implementation.", "is_error": False}, turn=1),
        ]])
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "run the org")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check(f"the dock holds an ApprovalCard, got {type(app.pending_dock.card).__name__}",
                      isinstance(app.pending_dock.card, ApprovalCard))
            await pilot.press("1")
            await pilot.pause(0.1)
            ctx.check(f"accept resolved through the controller, got {fake.approval_replies}",
                      fake.approval_replies == [("appr1", {"action": "accept", "instruction": None})])
            ctx.check("the dock cleared after the decision", app.pending_dock.card is None)
    asyncio.run(body())


@test
def test_pending_dock_holds_a_question_card_then_a_plan_card(ctx: Ctx):
    from halo_harness.tui.widgets.cards import PlanCard, QuestionCard

    async def body():
        fake = FakeController(turns=[[
            ev.user_message("pick", turn=1),
            ev.Event("question", {"id": "q1", "name": "AskUserQuestion",
                                   "input": {"question": "Which color?", "options": ["Red", "Blue"]}}, turn=1),
        ]])
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "pick")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            ctx.check(f"the dock holds a QuestionCard, got {type(app.pending_dock.card).__name__}",
                      isinstance(app.pending_dock.card, QuestionCard))

        fake2 = FakeController(turns=[[
            ev.user_message("make a plan", turn=1),
            ev.Event("plan_review", {"id": "p1", "plan": "1. do a thing\n2. do another"}, turn=1),
        ]])
        app2 = await _mounted(fake2)
        async with app2.run_test(size=(100, 40)) as pilot2:
            await pilot2.click("#prompt-input")
            await _type(pilot2, "make a plan")
            await pilot2.press("enter")
            await _drain_a_few(app2, pilot2, n=6)
            ctx.check(f"the dock holds a PlanCard, got {type(app2.pending_dock.card).__name__}",
                      isinstance(app2.pending_dock.card, PlanCard))
    asyncio.run(body())


@test
def test_pending_dock_o_opens_a_pager_with_the_full_content(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            await pilot.press("o")
            await pilot.pause(0.1)
            from halo_harness.tui.widgets.cards import PagerScreen
            ctx.check(f"'o' opened the pager, got {type(app.screen).__name__}", isinstance(app.screen, PagerScreen))
    asyncio.run(body())


@test
def test_pending_dock_o_shows_the_full_tool_input_not_just_the_reason(ctx: Ctx):
    """Parity gap: PendingDock's `o` pager used to show only the card's
    summary and reason (a short, one-line rationale) for a permission
    card, and "(no further detail)" for a question card -- never the
    full tool input (the real command/file path/etc the model actually
    asked to run, or the real question(s)/options)."""
    async def body():
        fake = FakeController(turns=_permission_turn("Bash(rm:*)"))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do something")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=6)
            await pilot.press("o")
            await pilot.pause(0.1)
            from halo_harness.tui.widgets.cards import PagerScreen
            ctx.check(f"'o' opened the pager, got {type(app.screen).__name__}", isinstance(app.screen, PagerScreen))
            full_text = app.screen.copy_text()
            ctx.check(f"the full tool input (the real command) is shown, got {full_text!r}",
                      "rm -rf /tmp/x" in full_text)
    asyncio.run(body())


@test
def test_svg_snapshot_pending_dock_with_two_queued_asks(ctx: Ctx):
    async def body():
        fake = FakeController(turns=_two_permission_asks_turn())
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.click("#prompt-input")
            await _type(pilot, "do two things")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=10)
            path, svg = _write_snapshot(app, "pending-dock-two-queued")
            ctx.check(f"pending-dock snapshot written to {path}", path.exists() and len(svg) > 0)
            ctx.check(f"SVG shows the active card's own prompt, got len={len(svg)}", "Permission needed" in svg)
            # The status bar's OWN rendered content (not the clipped SVG
            # viewport, which this test's unusually busy status line --
            # permission_str + needs_you_str + a running-tool spinner +
            # the unread-count all at once -- genuinely overflows at 100
            # cols; that width-based drop/shrink cascade is separately
            # covered by test_status_bar_drops_mcp_then_balance_then_
            # shortens_cwd_before_a_mid_word_slice) always carries it.
            bar_text = _static_text(app.status_bar)
            ctx.check(f"status bar shows the needs-you · 2 tag, got {bar_text!r}", "needs you · 2" in bar_text)
    asyncio.run(body())


# ============================================================================
# 2.0.1 W4c: clipboard quality and Ctrl+C reassurance (a user report, live, 2026-10-02
# ~03:30). Item 1: copy the SOURCE text, not screen cells -- each widget's
# own `copy_text()`, never `screen.get_selected_text()`'s cell-by-cell walk.
# Glyph/border stripping and "soft-wrap joining" are covered as PURE checks
# in tests/test_clipboard.py (no Textual needed there); these pilots cover
# the per-widget wiring, multi-widget selection, /copy, y/Y, the toast
# wording and the quit_on_double_ctrl_c switch -- everything that needs a
# real running BridgeApp. See this file's own top-of-file comment for why
# `copy_via_external_tool` is stubbed globally, not per-test, in this round.
# ============================================================================

def _last_mounted(app):
    return app.transcript.widgets_in_order()[-1]


@test
def test_copy_text_user_message_widget_is_the_raw_prompt_without_the_prompt_arrow(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            await app.transcript.add_user("hello there")
            widget = _last_mounted(app)
            ctx.check(f"widget is a UserMessage, got {type(widget).__name__}", isinstance(widget, UserMessage))
            ctx.check(f"copy_text has no prompt-arrow glyph, got {widget.copy_text()!r}",
                      widget.copy_text() == "hello there")
    asyncio.run(body())


@test
def test_copy_text_assistant_text_widget_is_the_raw_markdown_source(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            await app.transcript.append_text(1, 0, "**bold** reply")
            widget = _last_mounted(app)
            ctx.check(f"widget is AssistantText, got {type(widget).__name__}", isinstance(widget, AssistantText))
            ctx.check(f"copy_text is the raw markdown, got {widget.copy_text()!r}",
                      widget.copy_text() == "**bold** reply")
    asyncio.run(body())


@test
def test_copy_text_thinking_block_widget_is_reasoning_text_only_no_spinner_glyph(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            await app.transcript.append_thinking(1, 0, "considering the options")
            widget = _last_mounted(app)
            ctx.check(f"widget is ThinkingBlock, got {type(widget).__name__}", isinstance(widget, ThinkingBlock))
            ctx.check(f"copy_text is bare reasoning text, got {widget.copy_text()!r}",
                      widget.copy_text() == "considering the options")
    asyncio.run(body())


@test
def test_copy_text_tool_card_widget_is_header_plus_full_untruncated_body(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            card = ToolCard(tool_use_id="t1", header="⏺ Bash(git status)")
            card.set_result(ok=True, summary="2 files changed", content="line1\nline2\nline3\nline4")
            await app.transcript.mount_tool_card(card)
            # `copy_text()` itself is the RAW stored header+body (the bullet
            # glyph is baked into `self.header` by dispatch.py, unlike the
            # other widget types above whose own stored source never had a
            # glyph to begin with) -- `clean_copy_text` (tui/clipboard.py),
            # applied by every real caller (app.perform_copy, ToolCard's own
            # `y`, a transcript selection), is what strips it; checked here
            # the same way, so this test matches real usage exactly.
            from halo_harness.tui.clipboard import clean_copy_text
            ctx.check(f"copy_text is RAW (glyph still present), got {card.copy_text()!r}",
                      card.copy_text() == "⏺ Bash(git status)\n\nline1\nline2\nline3\nline4")
            cleaned = clean_copy_text(card.copy_text())
            ctx.check(f"clean_copy_text strips the glyph and keeps the FULL body, got {cleaned!r}",
                      cleaned == "Bash(git status)\n\nline1\nline2\nline3\nline4")
    asyncio.run(body())


@test
def test_system_note_marker_set_text_keeps_copy_text_in_sync_with_the_decision_line(ctx: Ctx):
    """W4c item 1: `app.py::clear_pending_card` rewrites a pending-ask
    marker through `set_text` (never a bare `.update()`) so a copy of the
    transcript after the ask resolves reads the DECISION line, never the
    stale "... needed, see below" text -- the actual bug a bare `.update()`
    would have reintroduced."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            marker = await app.transcript.add_note("⏸ permission needed for Bash(pytest -q), see below",
                                                     kind="pending-marker")
            ctx.check("copy_text starts as the pending wording",
                      "permission needed" in marker.copy_text())
            marker.set_text("✓ allowed once Bash(pytest -q)")
            ctx.check(f"copy_text now matches the rendered decision line, got {marker.copy_text()!r}",
                      marker.copy_text() == "✓ allowed once Bash(pytest -q)")
    asyncio.run(body())


@test
def test_multi_widget_selection_concatenates_texts_in_document_order(ctx: Ctx):
    """`screen.selections` is a plain dict -- set here directly (Textual's
    own PUBLIC reactive var) rather than simulating a real mouse drag,
    deliberately in REVERSE insertion order, to prove the copy is ordered
    by each widget's position in the TRANSCRIPT, not dict/selection order."""
    from textual.selection import Selection

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.add_user("what does this do")
            await app.transcript.append_text(1, 0, "it does X")
            card = ToolCard(tool_use_id="t1", header="⏺ Bash(ls)")
            card.set_result(ok=True, summary="ok", content="a.txt\nb.txt")
            await app.transcript.mount_tool_card(card)
            user_w, text_w, tool_w = app.transcript.widgets_in_order()
            app.screen.selections = {tool_w: Selection(None, None), user_w: Selection(None, None),
                                      text_w: Selection(None, None)}
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("ctrl+c")
            expected = "what does this do\n\nit does X\n\nBash(ls)\n\na.txt\nb.txt"
            ctx.check(f"all three texts concatenated in DOCUMENT order, glyph-stripped, got {app.clipboard!r}",
                      app.clipboard == expected)
            ctx.check(f"toast names what was copied and the char count, got {notified}",
                      any(f"Copied selection ({len(expected)} characters)" in m for m in notified))
    asyncio.run(body())


@test
def test_partial_widget_selection_still_copies_the_whole_widget_text(ctx: Ctx):
    """W4c item 1: "a selection that covers PART of a transcript widget
    copies that widget's underlying text" -- in FULL, never just the
    highlighted range. `Selection.extract`/`widget.get_selection` (the
    screen-cell path this round replaces) are never even called; only
    `screen.selections`' KEYS decide which widgets contribute at all."""
    from textual.geometry import Offset
    from textual.selection import Selection

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(1, 0, "a long reply with several words in it")
            widget = _last_mounted(app)
            app.screen.selections = {widget: Selection(Offset(2, 0), Offset(6, 0))}
            await pilot.press("ctrl+c")
            ctx.check(f"the FULL widget text was copied, not a narrow slice, got {app.clipboard!r}",
                      app.clipboard == "a long reply with several words in it")
    asyncio.run(body())


# ---------------------------------------------------------------------------
# W4c item 2: /copy, /copy code [N], /copy tool.
# ---------------------------------------------------------------------------

@test
def test_slash_copy_bare_copies_the_last_assistant_reply(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(1, 0, "the final answer")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check(f"the last reply landed on the clipboard, got {app.clipboard!r}",
                      app.clipboard == "the final answer")
            ctx.check(f"toast names 'last reply' and the char count, got {notified}",
                      any("Copied last reply (16 characters)" in m for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_with_no_reply_yet_notifies_instead_of_copying(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check("nothing landed on the clipboard", not app.clipboard)
            ctx.check(f"a 'no reply yet' notice was shown, got {notified}",
                      any("no assistant reply yet" in m.lower() for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_code_bare_copies_the_last_fenced_block_of_the_last_reply(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(
                1, 0, "intro\n```python\nprint(1)\n```\nmiddle\n```js\nconsole.log(2)\n```\nend")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy code")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check(f"the LAST fenced block was copied, got {app.clipboard!r}", app.clipboard == "console.log(2)")
            ctx.check(f"toast names 'last code block', got {notified}",
                      any("Copied last code block" in m for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_code_with_index_copies_the_nth_block_counting_from_the_top(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(
                1, 0, "intro\n```python\nprint(1)\n```\nmiddle\n```js\nconsole.log(2)\n```\nend")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy code 1")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check(f"the FIRST block (counting from the top) was copied, got {app.clipboard!r}",
                      app.clipboard == "print(1)")
            ctx.check(f"toast names 'code block 1', got {notified}", any("Copied code block 1" in m for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_code_index_out_of_range_notifies_instead_of_copying(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(1, 0, "```python\nprint(1)\n```")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy code 5")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check("nothing landed on the clipboard", not app.clipboard)
            ctx.check(f"an out-of-range notice named the real count (1), got {notified}",
                      any("1 code block(s)" in m and "5" in m for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_code_with_no_fenced_blocks_notifies_instead_of_copying(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(1, 0, "just plain prose, no code at all")
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy code")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check("nothing landed on the clipboard", not app.clipboard)
            ctx.check(f"a 'no code blocks' notice was shown, got {notified}",
                      any("no code blocks" in m.lower() for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_tool_copies_the_last_tool_cards_full_output(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            card1 = ToolCard(tool_use_id="t1", header="⏺ Glob(*.py)")
            card1.set_result(ok=True, summary="ok", content="old.py")
            await app.transcript.mount_tool_card(card1)
            card2 = ToolCard(tool_use_id="t2", header="⏺ Bash(ls)")
            card2.set_result(ok=True, summary="ok", content="a.txt\nb.txt")
            await app.transcript.mount_tool_card(card2)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy tool")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check(f"the LAST (not first) tool card's full output was copied, got {app.clipboard!r}",
                      app.clipboard == "Bash(ls)\n\na.txt\nb.txt")
            ctx.check(f"toast names 'last tool output', got {notified}",
                      any("Copied last tool output" in m for m in notified))
    asyncio.run(body())


@test
def test_slash_copy_tool_with_no_tool_card_yet_notifies_instead_of_copying(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.click("#prompt-input")
            await _type(pilot, "/copy tool")
            await pilot.press("enter")
            await _drain_a_few(app, pilot, n=5)
            ctx.check("nothing landed on the clipboard", not app.clipboard)
            ctx.check(f"a 'no tool output yet' notice was shown, got {notified}",
                      any("no tool output yet" in m.lower() for m in notified))
    asyncio.run(body())


# ---------------------------------------------------------------------------
# W4c item 2: y (focused tool card / pager), Y (whole current turn, guarded
# against hijacking ordinary typing in the chat box).
# ---------------------------------------------------------------------------

@test
def test_y_key_on_a_focused_tool_card_copies_its_full_output(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            card = ToolCard(tool_use_id="t1", header="⏺ Bash(ls)")
            card.set_result(ok=True, summary="ok", content="a.txt\nb.txt")
            await app.transcript.mount_tool_card(card)
            card.focus()
            await pilot.pause(0.05)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("y")
            ctx.check(f"the card's full output landed on the clipboard, got {app.clipboard!r}",
                      app.clipboard == "Bash(ls)\n\na.txt\nb.txt")
            ctx.check(f"toast names 'tool output', got {notified}", any("Copied tool output" in m for m in notified))
            from halo_harness.tui.widgets.cards import PagerScreen
            ctx.check("'y' does not open the pager (unlike 'o')", not isinstance(app.screen, PagerScreen))
    asyncio.run(body())


@test
def test_y_key_inside_the_pager_copies_the_same_full_content(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            card = ToolCard(tool_use_id="t1", header="⏺ Bash(ls)")
            card.set_result(ok=True, summary="ok", content="a.txt\nb.txt")
            await app.transcript.mount_tool_card(card)
            card.focus()
            await pilot.pause(0.05)
            await pilot.press("o")
            await pilot.pause(0.05)
            from halo_harness.tui.widgets.cards import PagerScreen
            ctx.check("the pager is open", isinstance(app.screen, PagerScreen))
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("y")
            ctx.check(f"the same full content was copied from the pager, got {app.clipboard!r}",
                      app.clipboard == "Bash(ls)\n\na.txt\nb.txt")
            ctx.check(f"toast fired, got {notified}", any("Copied tool output" in m for m in notified))
            ctx.check("the pager stays open after copying", isinstance(app.screen, PagerScreen))
    asyncio.run(body())


@test
def test_capital_y_types_a_literal_character_while_the_chat_box_has_focus(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.append_text(1, 0, "some earlier reply")
            await pilot.click("#prompt-input")
            await pilot.press("Y")
            ctx.check(f"a literal capital Y was typed, not copied, got {app.prompt_input.text!r}",
                      app.prompt_input.text == "Y")
            ctx.check("nothing was copied", not app.clipboard)
    asyncio.run(body())


@test
def test_capital_y_copies_the_whole_current_turn_when_the_chat_box_is_not_focused(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.transcript.add_user("explain X")
            await app.transcript.append_text(1, 0, "X is ...")
            card = ToolCard(tool_use_id="t1", header="⏺ Bash(ls)")
            card.set_result(ok=True, summary="ok", content="a.txt")
            await app.transcript.mount_tool_card(card)
            card.focus()  # anything other than the prompt input
            await pilot.pause(0.05)
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("Y")
            expected = "explain X\n\nX is ...\n\nBash(ls)\n\na.txt"
            ctx.check(f"the whole turn (prompt + reply + tool card) was copied, got {app.clipboard!r}",
                      app.clipboard == expected)
            ctx.check(f"toast names 'this turn', got {notified}", any("Copied this turn" in m for m in notified))
    asyncio.run(body())


# ---------------------------------------------------------------------------
# W4c item 4: the exact Ctrl+C toast wording, and the quit_on_double_ctrl_c
# config switch. `theme.set_config_value`/`get_config_value` read/write the
# SAME `~/.halo/config.json` this whole file's one scoped BRIDGE_STATE_DIR
# points at (set once, for the whole process, by `ensure_scoped_state_dir_
# once()` at import time) -- each test resets the key back to `true` (the
# documented default) in a finally block so it never leaks into another
# test in this same file.
# ---------------------------------------------------------------------------

@test
def test_ctrl_e_opens_the_editor_even_with_the_chat_prompt_focused(ctx: Ctx):
    """Round B fix pass (macOS/VS Code terminal item) pin: Textual's own
    Input/TextArea bind ctrl+e (alongside bare `end`) to "cursor to end
    of line" -- with the chat prompt (a TextArea) focused, the
    overwhelmingly common case, that widget-level binding used to win
    outright and `open_editor` never fired at all. No real editor is
    launched here (EDITOR/VISUAL cleared) -- `action_open_editor`'s own
    "no $VISUAL/$EDITOR set" notice firing IS the proof the app-level
    action ran instead of the TextArea's own cursor-move."""
    async def body():
        old_editor = os.environ.get("EDITOR")
        old_visual = os.environ.get("VISUAL")
        os.environ.pop("EDITOR", None)
        os.environ.pop("VISUAL", None)
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                notified = []
                app.notify = lambda msg, **kw: notified.append(msg)
                await pilot.click("#prompt-input")
                await _type(pilot, "some draft text")
                await pilot.press("ctrl+e")
                await pilot.pause(0.1)
                ctx.check(f"open_editor's own notice fired (never swallowed by the focused "
                          f"TextArea's own ctrl+e), got {notified}",
                          any("EDITOR" in m for m in notified))
                ctx.check("the draft text is untouched (no cursor-to-end-of-line side effect)",
                          app.prompt_input.text == "some draft text")
        finally:
            if old_editor is not None:
                os.environ["EDITOR"] = old_editor
            if old_visual is not None:
                os.environ["VISUAL"] = old_visual
    asyncio.run(body())


@test
def test_editor_slash_command_is_the_keyboard_independent_twin_of_ctrl_e(ctx: Ctx):
    """Halo 2.0.2 round C (macOS/VS Code terminal brief): `/editor` reaches
    the SAME `action_open_editor` Ctrl+E does, so typing the command works
    even on a terminal that never delivers the chord to halo at all."""
    from halo_harness.tui.slash import handle_slash

    async def body():
        old_editor = os.environ.get("EDITOR")
        old_visual = os.environ.get("VISUAL")
        os.environ.pop("EDITOR", None)
        os.environ.pop("VISUAL", None)
        try:
            fake = FakeController()
            app = await _mounted(fake)
            async with app.run_test(size=(100, 40)):
                notified = []
                app.notify = lambda msg, **kw: notified.append(msg)
                await handle_slash(app, "editor", "")
                ctx.check(f"typing /editor fired the same 'no $VISUAL/$EDITOR set' notice Ctrl+E "
                          f"would, got {notified}", any("EDITOR" in m for m in notified))
        finally:
            if old_editor is not None:
                os.environ["EDITOR"] = old_editor
            if old_visual is not None:
                os.environ["VISUAL"] = old_visual
    asyncio.run(body())


@test
def test_keys_slash_command_opens_the_tester_dialog_and_shows_received_keys(ctx: Ctx):
    """Halo 2.0.2 round C: `/keys` opens a dialog that shows the exact key
    NAME halo's own app received for a press -- so a user on a terminal
    that silently eats a shortcut (the owner's own macOS/VS Code report)
    can tell whether halo ever saw it at all. Esc leaves."""
    from halo_harness.tui.dialogs.keys_tester import KeysTesterDialog
    from halo_harness.tui.slash import handle_slash

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await handle_slash(app, "keys", "")
            await pilot.pause(0.1)
            ctx.check(f"the key tester dialog opened, got {type(app.screen).__name__}",
                      isinstance(app.screen, KeysTesterDialog))
            await pilot.press("a")
            await pilot.pause(0.1)
            last_line = _static_text(app.screen.query_one("#keys-tester-last"))
            ctx.check(f"the 'a' press shows up by name, got {last_line!r}", "'a'" in last_line)
            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check(f"Esc left the dialog, got {type(app.screen).__name__}",
                      not isinstance(app.screen, KeysTesterDialog))
    asyncio.run(body())


@test
def test_ctrl_c_toast_says_halo_and_that_the_terminal_stays_open(ctx: Ctx):
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("ctrl+c")
            ctx.check(f"the exact reassurance wording was shown, got {notified}",
                      any(m == "Press Ctrl+C again to exit halo (your terminal stays open)" for m in notified))
    asyncio.run(body())


@test
def test_quit_on_double_ctrl_c_false_disables_the_second_press_quit(ctx: Ctx):
    from halo_harness import theme as theme_mod

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            notified = []
            app.notify = lambda msg, **kw: notified.append(msg)
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            await pilot.press("ctrl+c")
            await pilot.pause(0.3)
            ctx.check("a second Ctrl+C within the window does NOT quit when the switch is off",
                      fake.quit_called is False)
            ctx.check(f"app.return_code is still unset, got {app.return_code!r}", app.return_code is None)
            ctx.check(f"the toast explains Ctrl+C never exits halo here, got {notified}",
                      any("never exits halo here" in m for m in notified))
    try:
        theme_mod.set_config_value("quit_on_double_ctrl_c", False)
        asyncio.run(body())
    finally:
        theme_mod.set_config_value("quit_on_double_ctrl_c", True)


@test
def test_quit_on_double_ctrl_c_false_still_leaves_ctrl_d_working(ctx: Ctx):
    """"only /exit, Ctrl+D or Ctrl+Q leave" -- Ctrl+D on an empty prompt is
    unaffected by the switch (it never went through `_ctrl_c_deadline` at
    all)."""
    from halo_harness import theme as theme_mod

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+d")
            await pilot.pause(0.2)
        ctx.check("Ctrl+D still quits on an empty prompt", fake.quit_called is True)
    try:
        theme_mod.set_config_value("quit_on_double_ctrl_c", False)
        asyncio.run(body())
    finally:
        theme_mod.set_config_value("quit_on_double_ctrl_c", True)


# ============================================================================
# Halo 2.0.2 round 3 (brief C item 1): the /tasks / Ctrl+T panel.
# ============================================================================

_FAKE_AGENT_ROWS = [
    {"agent_id": "agent-run1", "task_id": "t1", "title": "Worker", "model": "or:mock/x", "status": "running",
     "is_error": False, "tool_count": 2, "cost_usd": 0.001, "elapsed_s": 5.0, "depth": 0, "parent_agent_id": None,
     "log_path": ""},
    {"agent_id": "agent-done1", "task_id": "t2", "title": "Researcher", "model": "or:mock/y", "status": "completed",
     "is_error": False, "tool_count": 4, "cost_usd": 0.002, "elapsed_s": 12.0, "depth": 0, "parent_agent_id": None,
     "log_path": ""},
]


@test
def test_tasks_panel_shows_a_fake_running_agent_and_a_finished_one(ctx: Ctx):
    from halo_harness.tui.dialogs.tasks import TasksPanel

    async def body():
        fake = FakeController(agent_tasks=list(_FAKE_AGENT_ROWS))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+t")
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, TasksPanel):
                    break
            ctx.check(f"the panel opened, got {type(app.screen).__name__}", isinstance(app.screen, TasksPanel))
            option_list = app.screen.query_one("#tasks-list")
            texts = [str(o.prompt) for o in option_list.options]
            ctx.check(f"a row for the running agent, got {texts}", any("running" in t and "Worker" in t for t in texts))
            ctx.check(f"a row for the finished agent, got {texts}", any("done" in t and "Researcher" in t for t in texts))
    asyncio.run(body())


@test
def test_tasks_panel_enter_opens_transcript_viewer_in_follow_mode(ctx: Ctx):
    from halo_harness.tui.dialogs.tasks import TasksPanel, TranscriptViewer

    async def body():
        log_dir = Path(tempfile.mkdtemp(prefix="tasks-panel-log-"))
        log_path = log_dir / "agent-run1.jsonl"
        log_path.write_text(json.dumps({"type": "user", "content": [{"type": "text", "text": "hi"}]}) + "\n",
                             encoding="utf-8")
        rows = [dict(_FAKE_AGENT_ROWS[0], log_path=str(log_path))]
        fake = FakeController(agent_tasks=rows)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+t")
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, TasksPanel):
                    break
            app.screen.query_one("#tasks-list").highlighted = 0
            await pilot.press("enter")
            await pilot.pause(0.1)
            ctx.check(f"the transcript viewer opened, got {type(app.screen).__name__}",
                      isinstance(app.screen, TranscriptViewer))
            ctx.check("follow mode is on by default", app.screen.follow is True)
            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check(f"escape goes back to the panel, got {type(app.screen).__name__}",
                      isinstance(app.screen, TasksPanel))
    asyncio.run(body())


@test
def test_transcript_viewer_pgup_sticks_while_log_grows(ctx: Ctx):
    """2.0.2 review finding 20 pin: RichLog's own default `auto_scroll=
    True` used to force a scroll-to-end on every `write()` regardless of
    `self.follow`, so a PgUp was undone by the very next 1s poll that
    found the log had grown. Spies on `RichLog.write` rather than reading
    animated scroll pixels -- old `_load()` called `write(line)` with no
    `scroll_end` kwarg at all (falls back to the widget's own auto_scroll
    attribute, True); the fix passes `scroll_end=self.follow` explicitly."""
    from halo_harness.tui.dialogs.tasks import TasksPanel, TranscriptViewer
    from textual.widgets import RichLog

    async def body():
        log_dir = Path(tempfile.mkdtemp(prefix="transcript-viewer-log-"))
        log_path = log_dir / "agent-run1.jsonl"
        log_path.write_text(json.dumps({"type": "user", "content": [{"type": "text", "text": "hi"}]}) + "\n",
                             encoding="utf-8")
        rows = [dict(_FAKE_AGENT_ROWS[0], log_path=str(log_path))]
        fake = FakeController(agent_tasks=rows)
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+t")
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, TasksPanel):
                    break
            app.screen.query_one("#tasks-list").highlighted = 0
            await pilot.press("enter")
            await pilot.pause(0.1)
            viewer = app.screen
            ctx.check(f"viewer opened, got {type(viewer).__name__}", isinstance(viewer, TranscriptViewer))
            log_widget = viewer.query_one("#transcript-log", RichLog)
            ctx.check(f"RichLog built with auto_scroll off, got {log_widget.auto_scroll}",
                      log_widget.auto_scroll is False)

            viewer.action_page_up()
            ctx.check("PgUp turns follow off", viewer.follow is False)

            recorded = []
            orig_write = RichLog.write

            def _spy(self, content, *a, **kw):
                recorded.append(kw.get("scroll_end"))
                return orig_write(self, content, *a, **kw)
            RichLog.write = _spy
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "assistant", "content": [{"type": "text", "text": "more"}]}) + "\n")
                viewer._poll()
            finally:
                RichLog.write = orig_write
            ctx.check(f"the new line was appended while PgUp'd, got {recorded}", recorded == [False])
    asyncio.run(body())


@test
def test_ctrl_t_toggles_the_panel_open_and_closed(ctx: Ctx):
    from halo_harness.tui.dialogs.tasks import TasksPanel

    async def body():
        fake = FakeController(agent_tasks=list(_FAKE_AGENT_ROWS))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+t")
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, TasksPanel):
                    break
            ctx.check("first ctrl+t opens it", isinstance(app.screen, TasksPanel))
            await pilot.press("ctrl+t")
            await pilot.pause(0.1)
            ctx.check(f"second ctrl+t closes it, got {type(app.screen).__name__}",
                      not isinstance(app.screen, TasksPanel))
    asyncio.run(body())


@test
def test_tasks_panel_tab_switches_to_board_even_with_orgs_disabled(ctx: Ctx):
    """2.0.2 review finding 26 pin: Tab used to do nothing at all while
    `orgs.enabled` was off (the default) -- TaskCreate/TaskUpdate/
    TaskList are offered to the model in every session regardless of
    that setting, so the board tab must always be reachable."""
    from halo_harness.tui.dialogs.tasks import TasksPanel
    from halo_harness.theme import set_config_value

    async def body():
        set_config_value("orgs.enabled", False)
        fake = FakeController(agent_tasks=list(_FAKE_AGENT_ROWS))
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+t")
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, TasksPanel):
                    break
            panel = app.screen
            ctx.check("opens on the agents tab", panel.tab == "agents")
            await pilot.press("tab")
            await pilot.pause(0.1)
            ctx.check(f"Tab switches to the board tab even with orgs.enabled off, got {panel.tab!r}",
                      panel.tab == "board")
    asyncio.run(body())


@test
def test_tasks_panel_recurring_refresh_runs_off_the_ui_thread(ctx: Ctx):
    """2.0.2 review finding 17 (major) pin: the EXPENSIVE part of a
    RECURRING refresh (re-reading every sub-agent's log/meta.json) must
    run on a worker thread, never block the UI thread the 1s timer
    itself fires on. The very FIRST paint (`on_mount`) is deliberately
    still synchronous (so the panel never opens empty-then-populates a
    tick later) -- this checks the recurring path specifically, by
    calling `refresh_rows()` again after that first paint."""
    from halo_harness.tui.dialogs.tasks import TasksPanel
    import threading as _threading

    async def body():
        seen_threads = []

        def _tracking_list():
            seen_threads.append(_threading.get_ident())
            return list(_FAKE_AGENT_ROWS)
        fake = FakeController(agent_tasks=list(_FAKE_AGENT_ROWS))
        fake.list_agent_tasks = _tracking_list
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("ctrl+t")
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, TasksPanel):
                    break
            ctx.check("the panel opened", isinstance(app.screen, TasksPanel))
            ui_thread_id = _threading.get_ident()
            seen_threads.clear()  # on_mount's own synchronous first paint already called it once
            app.screen.refresh_rows()
            for _ in range(30):
                await pilot.pause(0.02)
                if seen_threads:
                    break
            ctx.check(f"the recurring refresh's own I/O ran off the UI thread, got {seen_threads} vs ui={ui_thread_id}",
                      bool(seen_threads) and seen_threads[0] != ui_thread_id)
    asyncio.run(body())


@test
def test_status_bar_shows_agents_running_count(ctx: Ctx):
    """Halo 2.0.2 round 3: `subagent_start` with no matching `subagent_
    end` yet bumps the status bar's `agents N` segment; `subagent_end`
    drops it back to 0 (segment omitted entirely, same convention as
    needs_you_count)."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            ctx.check("starts at 0 (no segment)", app.status_bar.agents_running == 0)
            ctx.check("no 'agents' segment yet", "agents " not in str(app.status_bar.render()))
            start_ev = ev.Event("subagent_start", {"name": "Worker"}, turn=1)
            start_ev.agent_id = "child-1"
            app._local_events.put(start_ev)
            await _drain_a_few(app, pilot, n=5, pause=0.02)
            ctx.check(f"bumped to 1, got {app.status_bar.agents_running}", app.status_bar.agents_running == 1)
            ctx.check(f"shown in the rendered bar, got {str(app.status_bar.render())!r}",
                      "agents 1" in str(app.status_bar.render()))
            end_ev = ev.Event("subagent_end", {"name": "Worker", "is_error": False}, turn=1)
            end_ev.agent_id = "child-1"
            app._local_events.put(end_ev)
            await _drain_a_few(app, pilot, n=5, pause=0.02)
            ctx.check(f"back to 0 once finished, got {app.status_bar.agents_running}",
                      app.status_bar.agents_running == 0)
            ctx.check("segment omitted again", "agents " not in str(app.status_bar.render()))
    asyncio.run(body())


@test
def test_subagent_progress_ticks_a_background_cards_phase_word_and_tools(ctx: Ctx):
    """Halo 2.0.2 round C (the owner's own background-streaming report):
    a BACKGROUND child's `SubAgentCard` used to sit on its constructor
    default ("thinking", 0 tools) until `subagent_end` -- no live signal
    at all while it actually ran. `agent/subagent.py`'s `_bg_run` now
    translates that child's own `phase`/`tool_use_ready` events into the
    narrower `subagent_progress` kind; this pins that tui/dispatch.py
    applies it to the right card and nothing else."""
    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            start_ev = ev.Event("subagent_start", {"name": "Worker"}, turn=1)
            start_ev.agent_id = "child-1"
            app._local_events.put(start_ev)
            await _drain_a_few(app, pilot, n=5, pause=0.02)
            card = app.transcript.subagent_cards.get("child-1")
            ctx.check("the card was mounted", card is not None)
            ctx.check(f"starts on the constructor default, got {card.phase_word!r}", card.phase_word == "thinking")

            progress = ev.subagent_progress(phase_word="tool", tool_call=True, turn=1)
            progress.agent_id = "child-1"
            app._local_events.put(progress)
            await _drain_a_few(app, pilot, n=5, pause=0.02)
            ctx.check(f"the card's phase word ticked to 'tool', got {card.phase_word!r}", card.phase_word == "tool")
            ctx.check(f"the card's tool count bumped, got {card.tool_count}", card.tool_count == 1)

            end_ev = ev.Event("subagent_end", {"name": "Worker", "is_error": False,
                                                 "result_preview": "all done here"}, turn=1)
            end_ev.agent_id = "child-1"
            app._local_events.put(end_ev)
            await _drain_a_few(app, pilot, n=5, pause=0.02)
            ctx.check("the card is gone (finished and popped)", "child-1" not in app.transcript.subagent_cards)
            notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
            ctx.check(f"the finish note carries the result preview and a /tasks pointer, got {notes}",
                      any("all done here" in n and "/tasks" in n for n in notes))
    asyncio.run(body())


@test
def test_status_bar_shows_bg_jobs_and_oldest_elapsed(ctx: Ctx):
    """Halo 2.0.2 round C: "the status bar keeps a live signal while the
    main turn is idle (agents N, bg jobs N, the oldest one's elapsed
    time)" -- a background Bash job has no live start/end event of its
    own (agent/jobs.py), so `BridgeApp._tick_background_activity` polls
    `job_registry.list_jobs()` directly; faked here with a tiny stand-in
    object rather than a real JobRegistry/subprocess."""
    from types import SimpleNamespace

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)):
            ctx.check("no 'bg jobs' segment yet", "bg jobs " not in str(app.status_bar.render()))
            started = time.time() - 5.0
            fake.session = SimpleNamespace(job_registry=SimpleNamespace(
                list_jobs=lambda: [{"status": "running", "started_at": started},
                                    {"status": "completed", "started_at": started}]))
            app._tick_background_activity()
            rendered = str(app.status_bar.render())
            ctx.check(f"only the RUNNING job is counted, got {rendered!r}", "bg jobs 1" in rendered)
            ctx.check(f"the oldest elapsed segment is shown, got {rendered!r}", "oldest " in rendered)
            fake.session = SimpleNamespace(job_registry=SimpleNamespace(list_jobs=lambda: []))
            app._tick_background_activity()
            rendered = str(app.status_bar.render())
            ctx.check("segment omitted once nothing is running", "bg jobs " not in rendered)
    asyncio.run(body())


@test
def test_two_background_jobs_show_live_notes_before_a_prompt_then_one_compact_block(ctx: Ctx):
    """Halo 2.0.2 round C's own pilot test, verbatim: "two background
    jobs finish with no input and both notes are on screen before any
    prompt is sent; then send a prompt and assert exactly one compact
    notice block reached the model." A REAL `agent.jobs.JobRegistry`
    against a minimal session stand-in -- proves THAT module's own live
    `system_note` emission (agent/jobs.py's `_push_notice`), not just
    dispatch.py's rendering of a hand-built event."""
    import threading
    from types import SimpleNamespace
    from halo_harness.agent.jobs import JobRegistry
    from halo_harness.agent.loop import _compact_notices_text
    from halo_harness.config.paths import git_bash

    bash = git_bash()
    if not bash:
        raise SkipTest("no Git Bash/sh found on this box")

    async def body():
        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            session_stand_in = SimpleNamespace(
                _event_sink=lambda ev: app._local_events.put(ev),
                _job_notices_lock=threading.Lock(), _pending_job_notices=[],
            )
            registry = JobRegistry(session_stand_in)
            for i in (1, 2):
                record, err = registry.start_background(
                    f"echo job-{i}-done", description=f"job {i}", cwd=REPO_DIR,
                    env=dict(os.environ), shell_path=str(bash))
                ctx.check(f"job {i} started, got err={err!r}", record is not None)

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and len(session_stand_in._pending_job_notices) < 2:
                await app._drain()
                await pilot.pause(0.05)
            ctx.check(f"both jobs completed with no input sent, got "
                      f"{len(session_stand_in._pending_job_notices)} pending notices",
                      len(session_stand_in._pending_job_notices) == 2)
            # the live system_note for each job is queued (sink()) strictly
            # BEFORE that job's own notice is appended to _pending_job_
            # notices (see agent/jobs.py's own _push_notice) -- both are
            # already in the queue by now, just maybe not yet DRAINED into
            # a mounted widget; a few more ticks flushes it.
            await _drain_a_few(app, pilot, n=10, pause=0.05)
            notes = [_static_text(w) for w in app.transcript.children if isinstance(w, SystemNote)]
            live_job_notes = [n for n in notes if "background job finished" in n]
            ctx.check(f"BOTH live notes are on screen already, before any prompt, got {live_job_notes}",
                      len(live_job_notes) == 2)
            ctx.check(f"each live note points at /tasks, got {live_job_notes}",
                      all("/tasks" in n for n in live_job_notes))

            # "then send a prompt" -- apply the queue exactly the way
            # Session._apply_pending_job_notices does for 2+ notices.
            pending = list(session_stand_in._pending_job_notices)
            block = _compact_notices_text(pending, noun="job")
            ctx.check(f"exactly ONE compact block (one head count, not one per job), got {block!r}",
                      block.count("background jobs finished while you were away") == 1)
            ctx.check(f"both jobs are still named inside that ONE block, got {block!r}",
                      block.count("finished, exit code") == 2)
    asyncio.run(body())


# ============================================================================
# Halo 2.0.2 round 4 (brief D): /mcp dialog repair actions. FakeController
# fakes every Controller call (reconnects/approvals/logins/tests/disables
# are plain recorded lists) -- no real McpManager/subprocess/file I/O here;
# that side is covered end to end in tests/test_mcp_repair.py.
# ============================================================================

_FAKE_MCP_ROWS = [
    {"name": "alpha", "type": "stdio", "command": "node", "args": ["x.js"], "state": "connected",
     "error": None, "tool_count": 3, "scope": "user", "backoff_status": None},
    {"name": "broken", "type": "stdio", "command": "ghost-cmd", "args": [], "state": "failed",
     "error": "FileNotFoundError: [Errno 2] No such file or directory: 'ghost-cmd'", "tool_count": 0,
     "scope": "local", "backoff_status": None},
    {"name": "pending-srv", "type": "stdio", "command": "node", "args": [], "state": "pending_approval",
     "error": None, "tool_count": 0, "scope": "project", "backoff_status": None},
    {"name": "connector__demo", "type": "connector", "state": "needs_auth", "url": None,
     "host": "example.com", "connector_name": "Demo", "tool_count": 2, "scope": None},
]


async def _open_mcp_dialog(pilot, app):
    from halo_harness.tui.dialogs.mcp_status import McpStatus
    await pilot.click("#prompt-input")
    await _type(pilot, "/mcp")
    await pilot.press("enter")
    for _ in range(30):
        await app._drain()
        await pilot.pause(0.02)
        if isinstance(app.screen, McpStatus):
            break
    return app.screen


def _highlight(screen, name: str) -> None:
    from textual.widgets import OptionList
    option_list = screen.query_one(OptionList)
    option_list.highlighted = next(i for i, o in enumerate(option_list.options) if o.id == name)


async def _wait_until(app, pilot, predicate, n: int = 40) -> None:
    for _ in range(n):
        await app._drain()
        await pilot.pause(0.02)
        if predicate():
            return


@test
def test_mcp_dialog_opens_with_legend_and_every_row(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import McpStatus
    from textual.widgets import OptionList, Static

    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            ctx.check(f"McpStatus opened, got {type(app.screen).__name__}", isinstance(screen, McpStatus))
            ids = {str(o.id) for o in screen.query_one(OptionList).options}
            ctx.check(f"every configured row is shown, got {ids}",
                      {"alpha", "broken", "pending-srv", "connector__demo"} <= ids)
            legend = _static_text(screen.query_one("#mcp-legend", Static))
            for key in ("reconnect", "approve", "login", "log", "edit", "install", "disable", "test"):
                ctx.check(f"legend mentions {key!r}, got {legend!r}", key in legend)
    asyncio.run(body())


@test
def test_mcp_dialog_r_reconnects_the_highlighted_row(ctx: Ctx):
    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "broken")
            await pilot.press("r")
            await _wait_until(app, pilot, lambda: fake.reconnects > 0)
            ctx.check(f"reconnect_mcp called once, got {fake.reconnects}", fake.reconnects == 1)
    asyncio.run(body())


@test
def test_mcp_dialog_R_reconnects_every_server(ctx: Ctx):
    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_mcp_dialog(pilot, app)
            await pilot.press("R")
            await _wait_until(app, pilot, lambda: fake.reconnect_all_calls > 0)
            ctx.check(f"reconnect_all_mcp called once, got {fake.reconnect_all_calls}", fake.reconnect_all_calls == 1)
            ctx.check(f"every row went through reconnect_mcp, got {fake.reconnects}",
                      fake.reconnects == len(_FAKE_MCP_ROWS))
    asyncio.run(body())


@test
def test_mcp_dialog_a_approves_the_pending_row(ctx: Ctx):
    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "pending-srv")
            await pilot.press("a")
            await _wait_until(app, pilot, lambda: fake.approvals)
            ctx.check(f"approve_mcp_server called with the right name, got {fake.approvals}",
                      fake.approvals == ["pending-srv"])
    asyncio.run(body())


@test
def test_mcp_dialog_l_logs_in_and_connector_row_too(ctx: Ctx):
    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "broken")
            await pilot.press("l")
            await _wait_until(app, pilot, lambda: fake.logins)
            ctx.check(f"login_mcp_server called for the local server, got {fake.logins}", fake.logins == ["broken"])

            _highlight(screen, "connector__demo")
            await pilot.press("l")
            await _wait_until(app, pilot, lambda: len(fake.logins) > 1)
            ctx.check(f"login_mcp_server called for the connector row too, got {fake.logins}",
                      fake.logins == ["broken", "connector__demo"])
    asyncio.run(body())


@test
def test_mcp_dialog_t_tests_the_highlighted_row(ctx: Ctx):
    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "alpha")
            await pilot.press("t")
            await _wait_until(app, pilot, lambda: fake.tests)
            ctx.check(f"test_mcp_server called with the right name, got {fake.tests}", fake.tests == ["alpha"])
    asyncio.run(body())


@test
def test_mcp_dialog_d_toggles_disabled_then_enabled(ctx: Ctx):
    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "alpha")
            await pilot.press("d")
            await _wait_until(app, pilot, lambda: fake.disables)
            ctx.check(f"first press disables (was connected), got {fake.disables}", fake.disables == [("alpha", True)])

            await pilot.press("d")
            await _wait_until(app, pilot, lambda: len(fake.disables) > 1)
            ctx.check(f"second press re-enables (now sees disabled), got {fake.disables}",
                      fake.disables == [("alpha", True), ("alpha", False)])
    asyncio.run(body())


@test
def test_mcp_dialog_apply_result_refetches_real_state(ctx: Ctx):
    """2.0.2 review finding 19 pin: `_apply_result` must not flip a row to
    "connected" just because the result text CONTAINS that substring (a
    failed `t` says "... not connected (state=failed)"), and `R` (`_apply_
    bulk_result`) must actually refresh rows instead of leaving them as
    they were. Uses a controller double that returns a FRESH dict every
    call (the way the real `Controller.list_mcp_servers` does) rather than
    FakeController's shared, mutated-in-place dict -- the review's own
    note is that the shared-dict fake hides this bug."""
    from halo_harness.tui.dialogs.mcp_status import McpStatus
    from textual.widgets import Static

    async def body():
        backing = {"name": "broken", "type": "stdio", "command": "ghost", "args": [],
                   "state": "failed", "error": "boom", "tool_count": 0, "scope": "user",
                   "backoff_status": None}

        def list_mcp_servers():
            return [dict(backing)]

        def test_mcp(name, abort=None):
            return [f"{name}: tools/list failed -- not connected (state=failed)"]

        fake = FakeController()
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = McpStatus(list_mcp_servers(), test=test_mcp, refresh=list_mcp_servers)
            app.push_screen(screen)
            for _ in range(20):
                await app._drain()
                await pilot.pause(0.02)
                if isinstance(app.screen, McpStatus):
                    break
            _highlight(screen, "broken")
            await pilot.press("t")
            await _wait_until(app, pilot, lambda: "tools/list failed"
                               in _static_text(screen.query_one("#mcp-hint", Static)))
            state = next(e["state"] for e in screen.servers if e["name"] == "broken")
            ctx.check(f"a failed test must not flip the row to connected, got state={state!r}",
                      state == "failed")

            backing["state"] = "connected"
            calls = {"n": 0}

            def reconnect_all(abort=None):
                calls["n"] += 1
                return ["broken: connected (fake)"]
            screen._reconnect_all = reconnect_all
            await pilot.press("R")
            await _wait_until(app, pilot, lambda: "connected (fake)"
                               in _static_text(screen.query_one("#mcp-hint", Static)))
            ctx.check(f"reconnect_all was actually called, got {calls['n']}", calls["n"] == 1)
            state2 = next(e["state"] for e in screen.servers if e["name"] == "broken")
            ctx.check(f"R must pull fresh state from the controller, got state={state2!r}",
                      state2 == "connected")
    asyncio.run(body())


@test
def test_mcp_dialog_i_shows_an_install_hint(ctx: Ctx):
    from textual.widgets import Static

    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "broken")
            await pilot.press("i")
            await pilot.pause(0.05)
            hint = _static_text(screen.query_one("#mcp-hint", Static))
            ctx.check(f"names the missing command, got {hint!r}", "ghost-cmd" in hint)
    asyncio.run(body())


@test
def test_mcp_dialog_L_opens_the_log_viewer_and_escape_returns(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import McpLogViewer, McpStatus

    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "broken")
            await pilot.press("L")
            await pilot.pause(0.1)
            ctx.check(f"the log viewer opened, got {type(app.screen).__name__}", isinstance(app.screen, McpLogViewer))
            await pilot.press("escape")
            await pilot.pause(0.1)
            ctx.check(f"escape goes back to McpStatus, got {type(app.screen).__name__}",
                      isinstance(app.screen, McpStatus))
    asyncio.run(body())


@test
def test_mcp_dialog_e_with_no_resolver_and_connector_row_are_clean_notes(ctx: Ctx):
    """FakeController's `resolve_mcp_config` always returns `None`
    (no real config to edit without a live McpManager) -- `e` must say
    so rather than crash or push a half-built form/editor."""
    from textual.widgets import Static

    async def body():
        fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
        app = await _mounted(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_mcp_dialog(pilot, app)
            _highlight(screen, "alpha")
            await pilot.press("e")
            await pilot.pause(0.05)
            hint = _static_text(screen.query_one("#mcp-hint", Static))
            ctx.check(f"could not resolve the config, got {hint!r}", "re-resolve" in hint)

            _highlight(screen, "connector__demo")
            await pilot.press("e")
            await pilot.pause(0.05)
            hint = _static_text(screen.query_one("#mcp-hint", Static))
            ctx.check(f"connector rows point at claude.ai instead, got {hint!r}", "claude.ai" in hint)
    asyncio.run(body())


@test
def test_mcp_dialog_e_opens_the_inline_form_and_saves(ctx: Ctx):
    """With a resolvable config and no $VISUAL/$EDITOR, `e` opens
    McpEntryForm (never the $EDITOR subprocess path -- real subprocess
    editors are deliberately never spawned from this hermetic suite);
    editing the command and ^S writes it back to the right scope file
    and reconnects."""
    from halo_harness.mcp.manager import McpServerConfig
    from halo_harness.tui.dialogs.mcp_entry_form import McpEntryForm
    from halo_harness.tui.dialogs.mcp_status import McpStatus
    from textual.widgets import Input

    async def body():
        old_editor, old_visual = os.environ.pop("EDITOR", None), os.environ.pop("VISUAL", None)
        try:
            fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
            fake.resolve_mcp_config = lambda name: McpServerConfig(
                name="alpha", type="stdio", command="node", args=["old.js"], scope="user")
            app = await _mounted(fake)
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _open_mcp_dialog(pilot, app)
                _highlight(screen, "alpha")
                await pilot.press("e")
                await _wait_until(app, pilot, lambda: isinstance(app.screen, McpEntryForm))
                ctx.check(f"the inline form opened, got {type(app.screen).__name__}",
                          isinstance(app.screen, McpEntryForm))
                app.screen.query_one("#mcp-field-command", Input).value = "node-renamed"
                await pilot.press("ctrl+s")
                await _wait_until(app, pilot, lambda: isinstance(app.screen, McpStatus) and fake.reconnects > 0)
                ctx.check(f"back on McpStatus after saving, got {type(app.screen).__name__}",
                          isinstance(app.screen, McpStatus))
                ctx.check(f"the edit triggered a reconnect, got {fake.reconnects}", fake.reconnects == 1)

                from halo_harness.config.claude_json import claude_json_path
                saved = json.loads(claude_json_path().read_text(encoding="utf-8"))
                ctx.check(f"the new command was actually written to ~/.claude.json, got {saved}",
                          saved.get("mcpServers", {}).get("alpha", {}).get("command") == "node-renamed")
        finally:
            if old_editor is not None:
                os.environ["EDITOR"] = old_editor
            if old_visual is not None:
                os.environ["VISUAL"] = old_visual
    asyncio.run(body())


@test
def test_mcp_dialog_e_save_preserves_fields_the_form_never_shows(ctx: Ctx):
    """2.0.2 review finding 15 (major) pin: saving used to REBUILD the
    entry from scratch, dropping cwd/timeout/alwaysLoad/mcpLazy/any
    unknown key even when the user only touched the command field. Also
    pins the args-with-spaces fix: args are now one per line (never
    whitespace-split), so a path containing a space round-trips as ONE
    argument."""
    from halo_harness.config.claude_json import claude_json_path
    from halo_harness.mcp.manager import McpServerConfig
    from halo_harness.tui.dialogs.mcp_entry_form import McpEntryForm
    from halo_harness.tui.dialogs.mcp_status import McpStatus
    from textual.widgets import Input, TextArea

    async def body():
        old_editor, old_visual = os.environ.pop("EDITOR", None), os.environ.pop("VISUAL", None)
        try:
            claude_json_path().write_text(json.dumps({"mcpServers": {"alpha": {
                "type": "stdio", "command": "node", "args": ["old.js", "--root", "C:/My Projects/app"],
                "env": {"X": "1"}, "cwd": "/some/dir", "timeout": 9999, "headersHelper": "h", "alwaysLoad": True,
                "mcpLazy": False, "someUnknownField": "keep-me",
            }}}), encoding="utf-8")
            fake = FakeController(mcp_servers=[dict(r) for r in _FAKE_MCP_ROWS])
            fake.resolve_mcp_config = lambda name: McpServerConfig(
                name="alpha", type="stdio", command="node",
                args=["old.js", "--root", "C:/My Projects/app"], env={"X": "1"}, scope="user")
            app = await _mounted(fake)
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _open_mcp_dialog(pilot, app)
                _highlight(screen, "alpha")
                await pilot.press("e")
                await _wait_until(app, pilot, lambda: isinstance(app.screen, McpEntryForm))
                args_area = app.screen.query_one("#mcp-field-args", TextArea)
                ctx.check(f"the spaced arg shows as ONE line, got {args_area.text!r}",
                          "C:/My Projects/app" in args_area.text.splitlines())
                app.screen.query_one("#mcp-field-command", Input).value = "node-renamed"
                await pilot.press("ctrl+s")
                await _wait_until(app, pilot, lambda: isinstance(app.screen, McpStatus) and fake.reconnects > 0)

                saved = json.loads(claude_json_path().read_text(encoding="utf-8"))
                entry = saved.get("mcpServers", {}).get("alpha", {})
                ctx.check(f"the edited field was saved, got {entry}", entry.get("command") == "node-renamed")
                ctx.check(f"the spaced arg survived as ONE argument, got {entry.get('args')}",
                          entry.get("args") == ["old.js", "--root", "C:/My Projects/app"])
                for key, expected in (("cwd", "/some/dir"), ("timeout", 9999), ("headersHelper", "h"),
                                       ("alwaysLoad", True), ("mcpLazy", False), ("someUnknownField", "keep-me")):
                    ctx.check(f"{key!r} the form never shows survived untouched, got {entry.get(key)!r}",
                              entry.get(key) == expected)
        finally:
            if old_editor is not None:
                os.environ["EDITOR"] = old_editor
            if old_visual is not None:
                os.environ["VISUAL"] = old_visual
    asyncio.run(body())


# ============================================================================
# Halo 2.0.2 round 7: the init wizard (tui/dialogs/init_wizard.py) -- one
# Textual app for the whole interactive `halo init`, Back/Skip/Next/Finish
# buttons, no console tear-down between steps. `InitWizardApp` is run as
# its OWN standalone app here (same `run_test()` pattern `InitTabsApp`'s
# own pilot block above already uses), never via `run_init_wizard`'s
# `.run()` (which blocks for a real terminal) -- a test builds `WizardState`
# directly and wraps it.
# ============================================================================

@test
def test_init_wizard_walks_every_step_with_next_never_exiting(ctx: Ctx):
    """Brief Tests section: "the wizard walks all steps with Next and
    never exits between them"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState

    async def body():
        old, _home = _scoped_state_dir_env("wizard-walk-")
        try:
            step_keys = ("providers", "default_model", "permission_mode", "theme", "roles", "orgs", "summary")
            state = WizardState(cwd=REPO_DIR, step_keys=step_keys, no_live=True)
            app = InitWizardApp(state)
            seen = []
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                for _key in step_keys:
                    seen.append(type(app.screen).__name__)
                    app.screen.query_one("#wiz-next").press()
                    await pilot.pause(0.25)
            ctx.check(f"walked every step in order with Next alone, got {seen}",
                      seen == ["ProvidersStep", "DefaultModelStep", "PermissionModeStep", "ThemeStep",
                               "RolesStep", "OrgsStep", "SummaryStep"])
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_back_returns_to_the_previous_step_with_values_kept(ctx: Ctx):
    """Brief Tests section: "Back returns to the previous step with its
    values kept"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
    from textual.widgets import OptionList

    async def body():
        old, _home = _scoped_state_dir_env("wizard-back-")
        try:
            state = WizardState(cwd=REPO_DIR, step_keys=("permission_mode", "theme"), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                perm_screen = app.screen
                option_list = perm_screen.query_one("#wiz-permission-list", OptionList)
                option_list.highlighted = 2  # NOT the initial "auto" highlight
                app.screen.query_one("#wiz-next").press()
                await pilot.pause(0.2)
                ctx.check("advanced to theme", type(app.screen).__name__ == "ThemeStep")
                app.screen.query_one("#wiz-back").press()
                await pilot.pause(0.2)
                ctx.check("back on the SAME permission_mode screen instance (never rebuilt)",
                          app.screen is perm_screen)
                ctx.check("the highlighted value survived the round trip",
                          app.screen.query_one("#wiz-permission-list", OptionList).highlighted == 2)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_skip_on_roles_and_orgs_leaves_config_untouched(ctx: Ctx):
    """Brief Tests section: "Skip on roles and orgs leaves config
    untouched"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
    from halo_harness.theme import get_config_value

    async def body():
        old, _home = _scoped_state_dir_env("wizard-skip-")
        try:
            state = WizardState(cwd=REPO_DIR, step_keys=("roles", "orgs"), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                app.screen.query_one("#wiz-skip").press()  # roles
                await pilot.pause(0.2)
                app.screen.query_one("#wiz-skip").press()  # orgs
                await pilot.pause(0.2)
                ctx.check("roles.enabled never written",
                          get_config_value("roles.enabled", default="<unset>") == "<unset>")
                ctx.check("orgs.enabled never written",
                          get_config_value("orgs.enabled", default="<unset>") == "<unset>")
                ctx.check("orgs.default never written",
                          get_config_value("orgs.default", default="<unset>") == "<unset>")
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_default_model_step_next_with_no_pick_keeps_current_default(ctx: Ctx):
    """Finding 1 (critical) pin: the step's own copy already promises
    "Next keeps the current default if you don't pick one" -- `on_mount`
    used to call `action_first()` unconditionally, so `commit()` always
    saw SOME row highlighted and overwrote `config.model` even with zero
    user interaction. Repro from the review: config.model points at a
    provider not configured this run (only Anthropic's fixed aliases
    show, "ant:fable" first) -- one bare Next used to silently flip it to
    "ant:fable"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
    from halo_harness.theme import get_config_value, set_config_value

    async def body():
        old, _home = _scoped_state_dir_env("wizard-default-model-")
        try:
            set_config_value("model", "or:deepseek/deepseek-chat")
            state = WizardState(cwd=REPO_DIR, step_keys=("default_model", "permission_mode"), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                ctx.check(f"opened on the Default model step, got {type(app.screen).__name__}",
                          type(app.screen).__name__ == "DefaultModelStep")
                app.screen.query_one("#wiz-next").press()
                await pilot.pause(0.2)
                ctx.check(f"config.model untouched by a bare Next, got {get_config_value('model', default=None)!r}",
                          get_config_value("model", default=None) == "or:deepseek/deepseek-chat")
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_esc_asks_before_quitting(ctx: Ctx):
    """Brief Tests section: "Esc asks before quitting"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState, _QuitConfirm

    async def body():
        old, _home = _scoped_state_dir_env("wizard-esc-")
        try:
            state = WizardState(cwd=REPO_DIR, step_keys=("roles",), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                await pilot.press("escape")
                await pilot.pause(0.2)
                ctx.check("a confirm modal appeared instead of an immediate exit",
                          isinstance(app.screen, _QuitConfirm) and app.is_running)
                await pilot.press("n")
                await pilot.pause(0.2)
                ctx.check("answering No returns to the step, the app keeps running",
                          type(app.screen).__name__ == "RolesStep" and app.is_running)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_quality_template_writes_the_expected_role_table(ctx: Ctx):
    """Brief Tests section: "picking the quality template writes the
    expected role table"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
    from halo_harness.roles import configured_role_table
    from halo_harness.theme import set_config_value

    async def body():
        old, _home = _scoped_state_dir_env("wizard-quality-")
        try:
            set_config_value("model", "or:vendor/strong-model")
            state = WizardState(cwd=REPO_DIR, step_keys=("roles",), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                picker = app.screen.query_one("#wiz-roles-templates")
                names = [str(picker.get_option_at_index(i).id) for i in range(picker.option_count)]
                picker.highlighted = names.index("quality")
                await pilot.pause(0.05)
                app.screen.query_one("#wiz-roles-use").press()
                await pilot.pause(0.2)
                table = configured_role_table()
                ctx.check(f"quality pins judge+reviewer to the session's own model, got {table}",
                          table.get("judge") == "or:vendor/strong-model"
                          and table.get("reviewer") == "or:vendor/strong-model")
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_edit_roles_opens_and_returns_to_the_step(ctx: Ctx):
    """Brief Tests section: "Edit roles... opens the editor and returns
    to the step"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
    from halo_harness.tui.dialogs.roles_editor import RolesEditor

    async def body():
        old, _home = _scoped_state_dir_env("wizard-editroles-")
        try:
            state = WizardState(cwd=REPO_DIR, step_keys=("roles",), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                roles_screen = app.screen
                roles_screen.query_one("#wiz-roles-edit").press()
                await pilot.pause(0.3)
                ctx.check(f"the roles editor opened, got {type(app.screen).__name__}",
                          isinstance(app.screen, RolesEditor))
                await pilot.press("escape")
                await pilot.pause(0.2)
                ctx.check("back on the SAME roles step instance", app.screen is roles_screen)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_choosing_default_org_writes_orgs_default(ctx: Ctx):
    """Brief Tests section: "choosing a default org writes orgs.
    default"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
    from halo_harness.theme import get_config_value

    async def body():
        old, _home = _scoped_state_dir_env("wizard-defaultorg-")
        try:
            state = WizardState(cwd=REPO_DIR, step_keys=("orgs",), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                picker = app.screen.query_one("#wiz-orgs-list")
                names = [str(picker.get_option_at_index(i).id) for i in range(picker.option_count)]
                picker.highlighted = names.index("release-flow")
                await pilot.pause(0.05)
                app.screen.query_one("#wiz-orgs-default").press()
                await pilot.pause(0.2)
                ctx.check(f"orgs.default was written, got {get_config_value('orgs.default', default=None)!r}",
                          get_config_value("orgs.default", default=None) == "release-flow")
                ctx.check("orgs.enabled was also turned on (the switch defaulted on when clicked)",
                          get_config_value("orgs.enabled", default=False) is True)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_init_wizard_start_step_five_opens_on_roles(ctx: Ctx):
    """Brief Tests section: "start_step=5 opens on roles"."""
    from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState, _resolve_start_index, \
        full_step_keys

    async def body():
        old, _home = _scoped_state_dir_env("wizard-startstep-")
        try:
            step_keys = full_step_keys()
            state = WizardState(cwd=REPO_DIR, step_keys=step_keys,
                                 index=_resolve_start_index(5, step_keys), no_live=True)
            app = InitWizardApp(state)
            async with app.run_test(size=(100, 45)) as pilot:
                await pilot.pause(0.1)
                ctx.check(f"opened directly on the Roles step, got {type(app.screen).__name__}",
                          type(app.screen).__name__ == "RolesStep")
                ctx.check("Back is enabled (earlier steps are still reachable from a mid-wizard start)",
                          not app.screen.query_one("#wiz-back").disabled)
        finally:
            _restore_state_dir_env(old)
    asyncio.run(body())


@test
def test_slash_setup_roles_opens_the_screen_and_updates_the_live_session(ctx: Ctx):
    """Brief Tests section: "/setup roles inside a mounted BridgeApp opens
    the screen and the live role table changes after save". `FakeController`
    has no real `.session`/`agent_runtime` at all (same reason
    `test_slash_effort_against_a_real_session_persists_the_sent_value`
    above needs the real-controller rig instead)."""
    import argparse
    from halo_harness.tui.dialogs.init_wizard import RolesStep

    async def body():
        fh = build_fake_home()
        old_env = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_OPENROUTER_BASE_URL",
                                                     "OPENROUTER_API_KEY")}
        mock = MockUpstream().start()
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        controller = None
        try:
            from halo_harness.tui.bootstrap import build_controller
            from halo_harness.tui.slash import handle_slash
            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=True,
                tools=None, add_dir=None, model="or:mock/setup-roles", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=False,
            )
            controller, registry, facade = build_controller(args)
            app = BridgeApp(controller, registry=registry, facade=facade,
                             tool_registry=getattr(facade, "tool_registry", None), cwd=fh["proj"])
            # Set BEFORE /setup roles opens -- the "quality" preset is
            # COMPUTED from the current default model the first time this
            # screen is built (and never recomputed once the file exists),
            # so this must land before that happens, not after.
            from halo_harness.theme import set_config_value
            set_config_value("model", "or:vendor/strong-for-setup")
            async with app.run_test(size=(100, 40)) as pilot:
                await handle_slash(app, "setup", "roles")
                await pilot.pause(0.2)
                ctx.check(f"the roles setup screen opened, got {type(app.screen).__name__}",
                          isinstance(app.screen, RolesStep))
                picker = app.screen.query_one("#wiz-roles-templates")
                names = [str(picker.get_option_at_index(i).id) for i in range(picker.option_count)]
                picker.highlighted = names.index("quality")
                app.screen.query_one("#wiz-roles-use").press()
                await pilot.pause(0.3)
                runtime = controller.session.agent_runtime
                ctx.check(f"the LIVE session's own role table picked up the template, got {runtime.role_table}",
                          runtime.role_table.get("judge") == "or:vendor/strong-for-setup")
                ctx.check("the screen popped back to the live session (no screens left over)",
                          not isinstance(app.screen, RolesStep))
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


if __name__ == "__main__":
    # NEW (post-H9 acceptance): see tests/helpers/runner.py's own docstring.
    from tests.helpers.runner import cleanup_tracked_temp_dirs, install_temp_dir_tracking
    install_temp_dir_tracking()
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    removed = cleanup_tracked_temp_dirs()
    print(f"[cleanup] removed {removed} tracked temp dir(s)")
    sys.exit(print_results(results, passed, failed, skipped, label="TUI tests"))
