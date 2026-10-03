"""halo_harness.tui.widgets.transcript -- the scrolling transcript and its
plain (non-card) content widgets: `UserMessage`, `AssistantText` (streamed
via `Markdown.get_stream()`), `ThinkingBlock` (collapsed by default),
`SystemNote`, `FoldedHistory`, `IntroLine` (the 2.0.0 launch intro's own
typewriter line), and the `Transcript(VerticalScroll)` container that owns
mounting/streaming/folding for all of them plus the card widgets from
`cards.py`/`diffview.py` (mounted through the same `_mount_tracked` so
folding counts everything uniformly).
"""

from __future__ import annotations

import textwrap
import time
from typing import Optional

from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Markdown, Static

from halo_harness.model_display import format_elapsed_seconds, format_live_token_count
from halo_harness.tui.keys import FOLD_AFTER_WIDGETS


class UserMessage(Static):
    """One user turn -- rendered once, never streamed. `markup=False`: a
    user's own text (or a model's tool result echoed nowhere near here) must
    never be parsed as Rich markup -- a literal `[red]` in a prompt must
    show up literally."""

    def __init__(self, text: str) -> None:
        super().__init__(f"❯ {text}", markup=False, classes="user-message")
        # W4c item 1: the SOURCE text a copy ever reads -- never the
        # displayed `"❯ {text}"` (re-deriving it by slicing the rendered
        # string would just be re-reading our own chrome back out again).
        self.raw_text = text

    def copy_text(self) -> str:
        return self.raw_text


class SystemNote(Static):
    """Slash-command output, toasts-as-transcript-lines, errors, and
    interrupted/notification lines."""

    def __init__(self, text: str, *, kind: str = "note") -> None:
        super().__init__(text, markup=False, classes=f"system-note system-note-{kind}")
        self.raw_text = text

    def copy_text(self) -> str:
        return self.raw_text

    def set_text(self, text: str) -> None:
        """W4c item 1: the ONE way to change this widget's text after
        construction -- keeps `raw_text` (what a copy reads) and the
        actual render in sync. `app.py::clear_pending_card` rewrites a
        pending-ask marker to its decision line through this, never
        through a bare `.update()` that would leave `copy_text()` still
        answering with the stale "permission needed ..." wording after
        the ask was already resolved."""
        self.raw_text = text
        self.update(text)


class IntroLine(Static):
    """2.0.0 Launch intro (rolo, 2026-09-30): `I am just a copy, of a copy,
    of a copy... halo <version>` typed out character by character, like
    someone typing it, the first time a fresh interactive session mounts
    (see `BridgeApp.on_mount`/`tui/launch.py`'s own `show_intro` gating --
    this widget itself has no opinion on WHEN it should appear, only how it
    types once mounted).

    A plain, self-rescheduling `Textual.Widget.set_timer` drives the reveal
    -- no thread, no `asyncio.sleep` loop -- one more character per tick,
    a block cursor appended while typing. The per-character delay is a
    LOOKED-UP class attribute (not inlined into the scheduling call) so a
    test can shrink `BASE_DELAY_S`/`PAUSE_DELAY_S`/`ELLIPSIS_PAUSE_DELAY_S`
    before mounting instead of waiting out the real ~2s animation. `skip()`
    (any keypress, or a submitted prompt, per the brief) reveals the rest
    instantly and is idempotent -- safe to call after the line is already
    done.

    `_reveal_one`/`skip` each call `self.update(...)` DIRECTLY rather than
    through a shared `_render()` helper -- empirically load-bearing on the
    installed Textual (8.2.8): routing a self-rescheduled timer's `update()`
    through an intermediary method reliably corrupted a LATER widget's own
    layout cache (`visual.get_height()` on `None`) at app-shutdown time,
    reproduced in isolation outside this whole app; inlining the two calls
    (there are only ever two: one per tick, one on completion/skip) avoids
    it entirely. Keep this inlined if this class is ever touched again."""

    BASE_DELAY_S = 0.035     # "about 35 ms per character"
    PAUSE_DELAY_S = 0.15     # slightly longer pause after a comma
    ELLIPSIS_PAUSE_DELAY_S = 0.22  # slightly longer pause after "..."
    CURSOR = "█"        # block cursor, shown only while typing

    def __init__(self, full_text: str) -> None:
        super().__init__("", markup=False, classes="intro-line")
        self.full_text = full_text
        self._shown = 0
        self.done = False
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_timer(self._delay_before_next_char(), self._reveal_one)

    def _delay_before_next_char(self) -> float:
        """The pause BEFORE revealing the character at `self._shown`,
        based on what was just revealed (index `self._shown - 1`) -- a
        comma, or the line's own ellipsis having just completed."""
        if self._shown == 0:
            return self.BASE_DELAY_S
        if self.full_text[max(0, self._shown - 3): self._shown] == "...":
            return self.ELLIPSIS_PAUSE_DELAY_S
        if self.full_text[self._shown - 1] == ",":
            return self.PAUSE_DELAY_S
        return self.BASE_DELAY_S

    def _reveal_one(self) -> None:
        self._shown += 1
        if self._shown < len(self.full_text):
            self.update(self.full_text[: self._shown] + self.CURSOR)
            self._timer = self.set_timer(self._delay_before_next_char(), self._reveal_one)
        else:
            self.done = True
            self.update(self.full_text[: self._shown])  # cursor gone -- the line is complete

    def skip(self) -> None:
        """Any keypress, or a submitted prompt, during the intro: reveal
        the rest instantly (the brief: "no waiting") -- a no-op once the
        line is already done, so callers never need to check `done` first."""
        if self.done:
            return
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._shown = len(self.full_text)
        self.done = True
        self.update(self.full_text)

    def copy_text(self) -> str:
        """W4c item 1: the full line regardless of how much has actually
        typed out yet -- never the partial `self._shown` prefix (plus the
        block cursor glyph, which is not content)."""
        return self.full_text


class FoldedHistory(Static):
    def __init__(self, count: int) -> None:
        self.message = f"⋯ {count} earlier lines folded (use /export to save the full transcript)"
        super().__init__(self.message, markup=False, classes="folded-history")

    def copy_text(self) -> str:
        return self.message


class ThinkingBlock(Static):
    """Halo 2.0.1 W2b (HALO-2.0.1-liveness-tips-brief.md Part A1/A2): the
    ONE live status line for a single model call -- "something visible
    changes every second while a turn runs" -- extended in place from the
    original "collapsed reasoning preview" widget rather than adding a
    parallel widget tree (phase line A1 and the reasoning preview A2 are
    literally the same widget, collapsed vs. expanded).

    Lifecycle (driven by `Transcript.begin_phase_line`/`phase_headers`/
    `phase_first_token`/`begin_waiting_line`/`finish_phase_line`, called
    from `tui/dispatch.py`'s own `phase`/`message_end` handlers):

      sending -> headers -> reasoning|writing -> done (collapses to a
      "Thought for Ns" summary) or REMOVED (message_end's own "or is
      removed when there was none") -- `waiting` is its own short-lived
      state between a tool result and the next call, always a FRESH
      widget (the previous call's own line already finalized at its own
      message_end).

    A bare `ThinkingBlock(phase_state="reasoning")` with no phase events
    at all (how `append_thinking` always worked before this brief, and how
    `Transcript.append_thinking` still falls back when no `phase(request_
    sent)` ever ran for this call -- a bare scripted test, or a dialect
    this harness hasn't wired phase events for yet) behaves exactly as
    the pre-2.0.1 widget did: content shows the moment it arrives, no
    "sending"/"no tokens yet" wait.

    `NO_DATA_THRESHOLD_S`/`NO_DATA_REFRESH_S` are class attributes (same
    seam convention as `IntroLine.BASE_DELAY_S`) so a test can shrink the
    real 30s/10s to something it can actually wait out."""

    expanded = reactive(False)

    NO_DATA_THRESHOLD_S = 30.0
    NO_DATA_REFRESH_S = 10.0
    # A model call with no reasoning/text/tool activity at all for this
    # long is still "sending"/"headers" -- not a realistic wait, just a
    # safety cap so `_elapsed()` never prints an absurd number if a test
    # (or a real, genuinely very slow route) leaves one hanging.
    _STATES_WITH_NO_DATA_SUFFIX = frozenset({"sending", "headers", "waiting"})

    def __init__(self, *, model_label: Optional[str] = None, phase_state: str = "sending") -> None:
        super().__init__("", markup=False, classes="thinking-block")
        self.model_label = model_label
        self.phase_state = phase_state
        self.reasoning_text = ""
        self.written_chars = 0
        self.ttfb_ms: Optional[float] = None
        now = time.monotonic()
        self.started_at = now
        self.last_activity_at = now
        self.final_elapsed = 0.0
        self._last_rendered: Optional[str] = None
        # Back-compat: `action_toggle_verbose` iterates `transcript._blocks.
        # values()` and `finish_open_streams` checks `widget.text.strip()`
        # for the plain-text scrollback -- both pre-date this brief and
        # read `.text` directly; kept as a live alias of `reasoning_text`
        # rather than touching either call site.
        self._refresh_display()

    @property
    def text(self) -> str:
        return self.reasoning_text

    def copy_text(self) -> str:
        """W4c item 1: `reasoning_text` itself (never the rendered
        `✻ Thinking… (Ns · N tokens)` header/preview `_refresh_display`
        builds -- that header is computed fresh for display and never
        stored, so there is nothing to strip here to begin with)."""
        return self.reasoning_text

    def had_reasoning(self) -> bool:
        return bool(self.reasoning_text.strip())

    # ---- phase-event transitions (called by Transcript, never by dispatch
    # directly -- see that module's own begin_phase_line/phase_headers/
    # phase_first_token/begin_waiting_line/finish_phase_line) -------------

    def restart(self, model_label: Optional[str]) -> None:
        """A `waiting` line reused as the NEXT call's own `sending` state
        (Transcript.begin_phase_line's own "waiting -> request_sent is one
        continuous line, never a flicker of remove-then-mount" choice)."""
        self.model_label = model_label
        self.phase_state = "sending"
        self.reasoning_text = ""
        self.written_chars = 0
        self.ttfb_ms = None
        now = time.monotonic()
        self.started_at = now
        self.last_activity_at = now
        self._refresh_display()

    def enter_headers(self, ttfb_ms: Optional[float]) -> None:
        self.phase_state = "headers"
        self.ttfb_ms = ttfb_ms
        self.last_activity_at = time.monotonic()
        self._refresh_display()

    def enter_first_token(self, kind: Optional[str]) -> None:
        self.phase_state = "reasoning" if kind == "reasoning" else "writing"
        self.last_activity_at = time.monotonic()
        self._refresh_display()

    def enter_reasoning(self) -> None:
        self.phase_state = "reasoning"
        self._refresh_display()

    def enter_writing(self, delta_chars: int = 0) -> None:
        self.phase_state = "writing"
        self.written_chars += max(0, delta_chars)
        self.last_activity_at = time.monotonic()
        self._refresh_display()

    def enter_done(self) -> None:
        self.final_elapsed = self._elapsed()
        self.phase_state = "done"
        self._refresh_display()

    def tick(self) -> None:
        """Called every drain tick (`Transcript.tick_phase_lines`, from
        `BridgeApp._drain`) for every STILL-LIVE phase line, whether or not
        any new event arrived this tick -- A1: "updated in place at least
        once a second ... never only on chunk arrival". A no-op render-
        wise unless the computed text actually changed (the elapsed/no-
        data text only changes once a whole second ticks over, even though
        this is called up to 30x/s)."""
        self._refresh_display()

    # ---- reasoning-preview content (Transcript.append_thinking/append_text
    # feed deltas in; append_thinking also calls enter_reasoning/append) ---

    def append(self, delta: str) -> None:
        self.reasoning_text += delta
        self.last_activity_at = time.monotonic()
        self._refresh_display()

    def set_expanded(self, value: bool) -> None:
        self.expanded = value

    def watch_expanded(self, _value: bool) -> None:
        self._refresh_display()

    def on_click(self) -> None:
        self.expanded = not self.expanded

    # ---- rendering ------------------------------------------------------

    def _elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def _reasoning_tokens(self) -> int:
        return len(self.reasoning_text) // 4

    def _no_data_suffix(self) -> str:
        if self.phase_state not in self._STATES_WITH_NO_DATA_SUFFIX:
            return ""
        stall = time.monotonic() - self.last_activity_at
        threshold, refresh = self.NO_DATA_THRESHOLD_S, self.NO_DATA_REFRESH_S
        if stall < threshold or refresh <= 0:
            return ""
        shown = int(stall // refresh) * refresh
        shown_str = f"{shown:g}"
        return f" · no data for {shown_str} s, Esc interrupts, typing steers"

    def _preview_lines(self, n: int = 3) -> str:
        width = max(20, (self.size.width or 76) - 4)
        wrapped: "list[str]" = []
        for para in self.reasoning_text.splitlines() or [""]:
            wrapped.extend(textwrap.wrap(para, width=width) or [""])
        return "\n".join(wrapped[-n:])

    def _refresh_display(self) -> None:
        if self.phase_state == "sending":
            label = f" to {self.model_label}" if self.model_label else ""
            text = f"✻ Sending request{label}…"
        elif self.phase_state == "headers":
            text = f"✻ Thinking… ({format_elapsed_seconds(self._elapsed())}, no tokens yet){self._no_data_suffix()}"
        elif self.phase_state == "reasoning":
            header = (f"✻ Thinking… ({format_elapsed_seconds(self._elapsed())} · "
                      f"{format_live_token_count(self._reasoning_tokens())} reasoning tokens)")
            # A2: expanded (the last 3 wrapped lines) WHILE actively
            # streaming, regardless of Ctrl+O -- or whenever Ctrl+O/verbose
            # is on (so toggling it mid-stream still does something, even
            # though streaming is already auto-expanded).
            text = f"{header}\n{self._preview_lines()}" if self.reasoning_text.strip() else header
        elif self.phase_state == "writing":
            text = (f"✻ Writing… ({format_elapsed_seconds(self._elapsed())} · "
                    f"{format_live_token_count(self.written_chars // 4)} tokens){self._no_data_suffix()}")
        elif self.phase_state == "waiting":
            text = f"✻ Waiting for model… ({format_elapsed_seconds(self._elapsed())}){self._no_data_suffix()}"
        else:  # "done"
            summary = (f"✻ Thought for {format_elapsed_seconds(self.final_elapsed)} "
                      f"({format_live_token_count(self._reasoning_tokens())} tokens)")
            # A1: "Ctrl+O ... expands the full reasoning text under the
            # summary" -- the WHOLE text now (streaming is over), not the
            # 3-line preview.
            text = f"{summary}\n{self.reasoning_text}" if self.expanded and self.reasoning_text.strip() else summary
        if text == self._last_rendered:
            return
        self._last_rendered = text
        self.update(text)


class AssistantText(Markdown):
    """One assistant text block, streamed incrementally via
    `Markdown.get_stream()` (Textual coalesces/queues fragments internally
    at its own rate; our own `_drain` coalescing in `tui/events.py` is a
    second, independent layer -- one per network delta batch, this one per
    widget-render tick)."""

    def __init__(self) -> None:
        super().__init__("")
        self._stream = None
        self.raw_text = ""

    def copy_text(self) -> str:
        """W4c item 1: the markdown SOURCE this streamed, never Textual's
        own rendered `Markdown` output (which would mean re-reading the
        widget's rendered content, exactly what this round moves away
        from)."""
        return self.raw_text

    def start_stream(self) -> None:
        # `Markdown.get_stream` is a classmethod taking the widget as an
        # explicit arg (`Markdown.get_stream(self)`) -- NOT an ordinary
        # bound method (`self.get_stream()` raises: "missing 1 required
        # positional argument: 'markdown'").
        self._stream = Markdown.get_stream(self)

    async def append_delta(self, delta: str) -> None:
        """Named to avoid shadowing `Markdown.append` (a REAL Textual API
        `MarkdownStream._run` calls internally to flush queued fragments --
        overriding it here would recurse into `_stream.write` forever)."""
        self.raw_text += delta
        if self._stream is not None:
            await self._stream.write(delta)
        else:
            await self.update(self.raw_text)

    async def finish(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None


class Transcript(VerticalScroll):
    """The scrolling history pane. Every mounted child (plain text, cards,
    diff views) goes through `_mount_tracked` so folding (D-TUI/U5: "old
    turns folded after 300 widgets") counts them all uniformly; streamed
    text/thinking blocks are additionally keyed by `(agent_id, turn,
    message_seq, kind, index)` so `agent/loop.py`'s per-content-block
    `index` (which repeats across separate model calls within one turn)
    never collides across messages.

    H9 whole-tree review finding 11: `agent_id` (None for the main
    session, a sub-agent's own id for a child) is now part of every one of
    those keys -- before this, TWO PARALLEL children (which share the
    PARENT's own turn NUMBER -- a child is never "turn 1 of the parent's
    turn 1", it's simply turn 1 of its OWN, entirely separate Session)
    streamed their text deltas into the exact SAME `AssistantText` widget
    as each other and as the parent (verified via a Textual pilot: two
    children's deltas interleaved into one widget, "childA-0 childB-0
    childA-1 …"). Every child's own streamed block now gets the
    `assistant-text-child`/`thinking-block-child` CSS class and a small
    `[sub-agent <id>] ` prefix on its first delta, so it reads as its own
    distinct, grouped block instead of silently merging into the main
    session's own transcript flow."""

    def __init__(self, **kwargs) -> None:
        super().__init__(id="transcript", **kwargs)
        self._history: "list" = []  # every tracked widget, oldest first
        self._blocks: dict = {}  # (agent_id, turn, seq, kind, index) -> AssistantText|ThinkingBlock
        self._message_seq: dict = {}  # (agent_id, turn) -> current message sequence number
        self.tool_cards: dict = {}  # tool_use_id -> ToolCard
        # Halo 2.0.1 W2b (liveness-tips-brief Part A5): agent_id -> the
        # live SubAgentCard summarizing that sub-agent's own run -- see
        # `tui/dispatch.py`'s `subagent_start`/`subagent_end`/`phase`/
        # `tool_use_ready` handlers, the only writers.
        self.subagent_cards: dict = {}
        # Halo 2.0.1 W2b (liveness-tips-brief Part A1): (agent_id, turn) ->
        # the CURRENTLY LIVE ThinkingBlock acting as that call's phase line
        # -- present only between `begin_phase_line`/`begin_waiting_line`
        # and the matching `finish_phase_line`; see `tui/dispatch.py`'s own
        # `phase`/`message_end` handlers, the only callers of all four.
        self._phase_lines: dict = {}
        self.new_since_scroll = 0
        self.folded_count = 0
        # Plain-text scrollback (D-TUI: printed on exit when settings `tui
        # != "fullscreen"`) -- survives folding, unlike the widget tree.
        self.plain_log: "list[str]" = []
        # U5 scope C: every subagent_start/subagent_end note, in mount
        # order -- `scroll_to_next_subagent` (child-session navigation
        # keys) jumps between them. A SEPARATE list from `_history`
        # because folding may evict the widget objects themselves; a
        # dead (unmounted) widget is skipped defensively there.
        self.subagent_marks: "list" = []

    # ---- generic mount/fold plumbing --------------------------------------

    def on_mount(self) -> None:
        # 1.0.1 hotfix 16: `anchor()` (Textual 8.2.8, widget.py:800) keeps
        # this container pinned to the bottom as ANY child grows -- a
        # streamed AssistantText's own `append_delta`/`MarkdownStream.write`
        # reflow included, not just a fresh mount -- until the user scrolls
        # up, which Textual's own `scroll_y` watcher auto-releases (any
        # ordinary scroll call defaults `release_anchor=True`); scrolling
        # back to the bottom auto-reacquires it, both with zero code here.
        # Before this fix, `_mount_tracked` below only ever called a
        # one-shot `scroll_end()` at MOUNT time -- a long streamed answer
        # mounts once, one line tall, then grows for hundreds of deltas
        # with nothing re-scrolling after, so the view stayed wherever the
        # user's own prompt was (rolo's report: "auto scroll does not
        # work... I have to scroll down to see the new answers").
        self.anchor()

    def is_at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 1

    def is_following(self) -> bool:
        """Whether new content is currently expected to auto-scroll into
        view. There is no public accessor for Textual's own internal
        "anchor released" flag, so this is inferred instead: with `anchor()`
        engaged (see `on_mount`), the compositor recomputes `scroll_y` to
        the live bottom on every layout pass for as long as we're actually
        following -- so "at the bottom right now" and "currently following"
        are the same fact, without reaching into a private attribute."""
        return self.is_at_bottom()

    async def _mount_tracked(self, widget, *, before=None) -> None:
        was_following = self.is_following()
        if before is not None:
            await self.mount(widget, before=before)
        else:
            await self.mount(widget)
        self._history.append(widget)
        if was_following:
            # No explicit scroll_end() needed any more -- the anchor from
            # on_mount keeps this pinned to the new bottom automatically.
            self.new_since_scroll = 0
        else:
            self.new_since_scroll += 1
        await self._fold_if_needed()

    def note_growth(self) -> None:
        """1.0.1 hotfix 16: called after content grows on an ALREADY-
        mounted block (a streamed delta, a tool card expanding) -- the
        anchor keeps the view scrolled correctly either way, but the "N
        new" counter (`_mount_tracked`'s own bump) previously only ever
        fired on a block's FIRST mount, never on the dozens/hundreds of
        deltas that follow while the user has scrolled away."""
        if not self.is_following():
            self.new_since_scroll += 1

    def mark_seen(self) -> None:
        if self.is_at_bottom():
            self.new_since_scroll = 0

    async def clear_view(self) -> None:
        """`/clear` and Ctrl+L (D-TUI): remove every mounted widget and
        reset all bookkeeping. View-only -- callers are responsible for any
        wording about whether the underlying session context also reset."""
        await self.remove_children()
        self._history = []
        self._blocks = {}
        self._message_seq = {}
        self.tool_cards = {}
        self.subagent_cards = {}
        self._phase_lines = {}
        self.new_since_scroll = 0
        self.folded_count = 0

    async def _fold_if_needed(self) -> None:
        if len(self._history) <= FOLD_AFTER_WIDGETS:
            return
        batch = FOLD_AFTER_WIDGETS // 2
        victims = self._history[:batch]
        self._history = self._history[batch:]
        victim_ids = {id(w) for w in victims}
        self._blocks = {k: w for k, w in self._blocks.items() if id(w) not in victim_ids}
        self.tool_cards = {k: w for k, w in self.tool_cards.items() if id(w) not in victim_ids}
        self.subagent_cards = {k: w for k, w in self.subagent_cards.items() if id(w) not in victim_ids}
        self._phase_lines = {k: w for k, w in self._phase_lines.items() if id(w) not in victim_ids}
        for w in victims:
            await w.remove()
        self.folded_count += len(victims)
        placeholder = FoldedHistory(self.folded_count)
        await self.mount(placeholder, before=0)

    # ---- plain content ------------------------------------------------

    async def add_user(self, text: str) -> None:
        await self._mount_tracked(UserMessage(text))
        self.plain_log.append(f"> {text}")

    async def add_note(self, text: str, *, kind: str = "note") -> "SystemNote":
        widget = SystemNote(text, kind=kind)
        await self._mount_tracked(widget)
        if kind not in ("todos",):  # a todo list is a transient status render, not worth replaying
            self.plain_log.append(text)
        return widget

    def scroll_to_next_subagent(self, direction: int) -> None:
        """U5 scope C: child-session navigation keys -- jump to the next
        (`direction=1`) or previous (`direction=-1`) sub-agent start/end
        marker. A no-op if none exist yet, or if folding has evicted every
        marker still in `subagent_marks` (each is dropped from the DOM
        without being removed from this list -- `is_mounted` filters
        those out defensively rather than scrolling to a dead widget)."""
        marks = [w for w in self.subagent_marks if w.is_mounted]
        if not marks:
            return
        y = self.scroll_y
        ahead = [w for w in marks if w.region.y > y + 1]
        behind = [w for w in marks if w.region.y < y - 1]
        if direction > 0:
            target = min(ahead, key=lambda w: w.region.y) if ahead else marks[0]
        else:
            target = max(behind, key=lambda w: w.region.y) if behind else marks[-1]
        self.scroll_to_widget(target, animate=False)

    def begin_message(self, turn: int, agent_id: "str | None" = None) -> int:
        """Call once per `message_start` -- returns this message's sequence
        number within `(agent_id, turn)` (used to key its text/thinking
        blocks). `agent_id=None` is the main session; a sub-agent's own
        turn numbering is entirely independent (finding 11), so the SAME
        `(agent_id, turn)` pair is what actually disambiguates it, not
        `turn` alone."""
        mkey = (agent_id, turn)
        seq = self._message_seq.get(mkey, 0) + 1
        self._message_seq[mkey] = seq
        return seq

    # ---- Halo 2.0.1 W2b: phase line lifecycle (liveness-tips-brief Part A1)
    # -- called from tui/dispatch.py's own `phase`/`message_end` handlers,
    # never from anywhere else. See ThinkingBlock's own class docstring for
    # the full state diagram these four drive it through. -------------------

    async def begin_phase_line(self, turn: int, *, agent_id: "str | None" = None,
                                model_label: "str | None" = None) -> None:
        """`phase(state="request_sent")`: mount a fresh line, UNLESS the
        previous call's own line is still showing `waiting` (the gap
        between a tool result and this next call) -- reusing it there
        makes "waiting for model" -> "sending" one continuous line instead
        of a remove-then-mount flicker with nothing in between."""
        key = (agent_id, turn)
        existing = self._phase_lines.get(key)
        if existing is not None and existing.phase_state == "waiting":
            existing.restart(model_label)
            return
        if existing is not None:
            # Defensive: a line from an EARLIER call at this same key that
            # was never finalized (shouldn't happen given the real event
            # contract's own ordering) -- finalize it first rather than
            # silently leaking a second untracked widget under one key.
            await self.finish_phase_line(turn, agent_id=agent_id)
        widget = ThinkingBlock(model_label=model_label, phase_state="sending")
        if agent_id is not None:
            widget.add_class("thinking-block-child")
        await self._mount_tracked(widget)
        self._phase_lines[key] = widget

    def phase_headers(self, turn: int, *, agent_id: "str | None" = None, ttfb_ms=None) -> None:
        widget = self._phase_lines.get((agent_id, turn))
        if widget is not None:
            widget.enter_headers(ttfb_ms)

    def phase_first_token(self, turn: int, *, agent_id: "str | None" = None, kind=None) -> None:
        widget = self._phase_lines.get((agent_id, turn))
        if widget is not None:
            widget.enter_first_token(kind)

    async def begin_waiting_line(self, turn: int, *, agent_id: "str | None" = None) -> None:
        """`phase(state="waiting_for_model")`: always a FRESH line -- the
        call that just finished already had its own line finalized by
        `finish_phase_line` at its own `message_end` (defensively finalized
        here too, if that somehow hasn't happened yet)."""
        key = (agent_id, turn)
        if key in self._phase_lines:
            await self.finish_phase_line(turn, agent_id=agent_id)
        widget = ThinkingBlock(phase_state="waiting")
        if agent_id is not None:
            widget.add_class("thinking-block-child")
        await self._mount_tracked(widget)
        self._phase_lines[key] = widget

    async def finish_phase_line(self, turn: int, *, agent_id: "str | None" = None) -> None:
        """`message_end`: "collapses to a Thought-for summary... or is
        removed when there was none" -- a no-op if no line is live for this
        (agent_id, turn) at all (a plain-text-only call with no phase
        events wired, or already finalized)."""
        widget = self._phase_lines.pop((agent_id, turn), None)
        if widget is None:
            return
        if widget.had_reasoning():
            widget.enter_done()
        else:
            await widget.remove()
            self._history = [w for w in self._history if w is not widget]

    async def finish_all_phase_lines(self, *, agent_id: "str | None" = None) -> None:
        """finding 7 (W6a): `turn_done` (main session or a sub-agent's) is
        a definitive "nothing more is coming" signal for this `agent_id`
        -- unlike `message_end`, it can arrive with NO phase line ever
        finalized first at all (a call that ends in an error or an Esc
        skips `message_end` entirely), leaving the line live and ticking
        ("Thinking... (2 m, no tokens yet) ... no data for 120 s") long
        after the status bar already went idle. Finalizes (or removes)
        EVERY line still live under `agent_id`, not just one at a single
        turn number, since a stale line from an earlier turn under the
        same key would never otherwise get cleaned up either."""
        for line_agent_id, turn in [k for k in self._phase_lines if k[0] == agent_id]:
            await self.finish_phase_line(turn, agent_id=line_agent_id)

    def tick_phase_lines(self) -> None:
        """Called every drain tick (`BridgeApp._drain`) regardless of
        whether any new event arrived -- A1: the elapsed/no-data text is
        driven by the TIMER, not chunk arrival."""
        for widget in list(self._phase_lines.values()):
            widget.tick()

    async def append_text(self, turn: int, index: int, text: str, agent_id: "str | None" = None) -> None:
        phase_widget = self._phase_lines.get((agent_id, turn))
        if phase_widget is not None:
            phase_widget.enter_writing(len(text))
        seq = self._message_seq.get((agent_id, turn), 1)
        key = (agent_id, turn, seq, "text", index)
        widget = self._blocks.get(key)
        if widget is None:
            widget = AssistantText()
            if agent_id is not None:
                # finding 11: "render children in their own grouped
                # block" -- a distinct CSS class plus a one-time prefix on
                # this block's very first delta, so a child's own text
                # reads as clearly its own rather than silently blending
                # into the main session's transcript flow.
                widget.add_class("assistant-text-child")
                text = f"[sub-agent {agent_id}] " + text
            await self._mount_tracked(widget)
            widget.start_stream()
            self._blocks[key] = widget
        else:
            self.note_growth()
        await widget.append_delta(text)

    async def append_thinking(self, turn: int, index: int, text: str, agent_id: "str | None" = None) -> None:
        seq = self._message_seq.get((agent_id, turn), 1)
        key = (agent_id, turn, seq, "thinking", index)
        widget = self._blocks.get(key)
        if widget is None:
            # Halo 2.0.1 W2b: a `phase(request_sent/headers/first_token)`
            # sequence for this call already mounted a live phase line
            # (ThinkingBlock) BEFORE any content ever arrived -- reuse it
            # (transition to "reasoning") rather than mounting a second,
            # redundant widget. No phase line active (a bare scripted test,
            # or a dialect this harness hasn't wired phase events for) ->
            # exactly the pre-2.0.1 fallback: a fresh widget, content shown
            # the moment it arrives.
            phase_widget = self._phase_lines.get((agent_id, turn))
            if phase_widget is not None:
                widget = phase_widget
                widget.enter_reasoning()
            else:
                widget = ThinkingBlock(phase_state="reasoning")
                # review/U5 must-do: a ThinkingBlock renders ABOVE this
                # message's answer text regardless of event ARRIVAL order --
                # OpenAI-dialect reasoning can stream its text delta before
                # its own reasoning delta, and the old unconditional append
                # put the thinking block wherever it happened to arrive
                # (below the answer, in that case). If a text widget for this
                # SAME message already exists, mount right before it;
                # otherwise (the common, correctly-ordered case) this is
                # exactly the old behaviour.
                await self._mount_tracked(widget, before=self._first_text_widget_for(agent_id, turn, seq))
                self._phase_lines[(agent_id, turn)] = widget
            if agent_id is not None:
                widget.add_class("thinking-block-child")
            self._blocks[key] = widget
        else:
            self.note_growth()
        widget.append(text)

    def _first_text_widget_for(self, agent_id: "str | None", turn: int, seq: int):
        for (a, t, s, kind, _idx), widget in self._blocks.items():
            if a == agent_id and t == turn and s == seq and kind == "text":
                return widget
        return None

    async def finish_open_streams(self, agent_id: "str | None" = None) -> None:
        """Stop every still-open `AssistantText` stream BELONGING TO
        `agent_id` (called on `turn_done`/`message_end` boundaries, and on
        interrupt, so a `MarkdownStream` never leaks a running asyncio task
        past its widget's useful life) and harvest its final text into
        `plain_log`. Safe to call more than once per message (idempotent --
        each finished key is popped as it's handled).

        finding 11: scoped to ONE `agent_id` -- before this, a CHILD's own
        `message_end`/`turn_done` closed EVERY open stream, including the
        PARENT's own still-streaming answer (verified: a child's
        `message_end` set the status bar to idle while the parent was
        still actively generating) -- the parent's turn_done fires this
        with `agent_id=None`, and each child's own fires it with that
        child's id, never touching each other's still-open blocks."""
        done_keys = [k for k in self._blocks if k[0] == agent_id]
        for key in done_keys:
            widget = self._blocks.pop(key)
            if isinstance(widget, AssistantText):
                await widget.finish()
                if widget.raw_text.strip():
                    self.plain_log.append(widget.raw_text)
            elif isinstance(widget, ThinkingBlock) and widget.text.strip():
                self.plain_log.append(f"[thinking] {widget.text}")

    # ---- tool cards (mounted by app.py, tracked here for folding) --------

    async def mount_tool_card(self, card) -> None:
        self.tool_cards[card.tool_use_id] = card
        await self._mount_tracked(card)

    def tick_tool_cards(self) -> None:
        """A4: every drain tick, regardless of new events -- same "driven
        by the timer, not chunk arrival" rule as `tick_phase_lines`."""
        for card in self.tool_cards.values():
            card.tick()

    # ---- sub-agent summary cards (A5) ------------------------------------

    async def mount_subagent_card(self, card) -> None:
        self.subagent_cards[card.agent_id] = card
        await self._mount_tracked(card)
        self.subagent_marks.append(card)

    def tick_subagent_cards(self) -> None:
        for card in self.subagent_cards.values():
            card.tick()

    async def mount_widget(self, widget) -> None:
        """Generic hook for permission/question/plan cards -- tracked for
        folding exactly like everything else."""
        await self._mount_tracked(widget)

    def widgets_in_order(self) -> list:
        """W4c item 1/2: every currently-tracked widget, oldest (top) first
        -- the read-only view `tui/app.py`'s copy actions need (a multi-
        widget transcript selection, `Y`'s "whole current turn", `/copy`'s
        "last reply"/"last tool output" search) without reaching into
        `_history` directly from outside this class. A plain list copy --
        callers never mutate the transcript's own bookkeeping through it."""
        return list(self._history)
