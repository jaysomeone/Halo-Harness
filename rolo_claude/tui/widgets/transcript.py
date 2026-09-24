"""rolo_claude.tui.widgets.transcript -- the scrolling transcript and its
plain (non-card) content widgets: `UserMessage`, `AssistantText` (streamed
via `Markdown.get_stream()`), `ThinkingBlock` (collapsed by default),
`SystemNote`, `FoldedHistory`, and the `Transcript(VerticalScroll)`
container that owns mounting/streaming/folding for all of them plus the
card widgets from `cards.py`/`diffview.py` (mounted through the same
`_mount_tracked` so folding counts everything uniformly).
"""

from __future__ import annotations

from typing import Optional

from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Markdown, Static

from rolo_claude.tui.keys import FOLD_AFTER_WIDGETS


class UserMessage(Static):
    """One user turn -- rendered once, never streamed. `markup=False`: a
    user's own text (or a model's tool result echoed nowhere near here) must
    never be parsed as Rich markup -- a literal `[red]` in a prompt must
    show up literally."""

    def __init__(self, text: str) -> None:
        super().__init__(f"❯ {text}", markup=False, classes="user-message")


class SystemNote(Static):
    """Slash-command output, toasts-as-transcript-lines, errors, and
    interrupted/notification lines."""

    def __init__(self, text: str, *, kind: str = "note") -> None:
        super().__init__(text, markup=False, classes=f"system-note system-note-{kind}")


class FoldedHistory(Static):
    def __init__(self, count: int) -> None:
        super().__init__(
            f"⋯ {count} earlier lines folded (use /export to save the full transcript)",
            markup=False, classes="folded-history",
        )


class ThinkingBlock(Static):
    """Dim, collapsed by default: "✻ Thinking… <last line>".
    Ctrl+O (app-level, `BridgeApp.action_toggle_verbose`) or a click expands
    to the full accumulated text."""

    expanded = reactive(False)

    def __init__(self) -> None:
        super().__init__("", markup=False, classes="thinking-block")
        self.text = ""

    def append(self, delta: str) -> None:
        self.text += delta
        self._refresh_display()

    def set_expanded(self, value: bool) -> None:
        self.expanded = value

    def watch_expanded(self, _value: bool) -> None:
        self._refresh_display()

    def _refresh_display(self) -> None:
        stripped = self.text.strip()
        if self.expanded:
            self.update(f"✻ Thinking\n{self.text}")
            return
        last_line = stripped.splitlines()[-1] if stripped else ""
        self.update((f"✻ Thinking… {last_line}")[:200])

    def on_click(self) -> None:
        self.expanded = not self.expanded


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
    text/thinking blocks are additionally keyed by `(turn, message_seq,
    kind, index)` so `agent/loop.py`'s per-content-block `index` (which
    repeats across separate model calls within one turn) never collides
    across messages."""

    def __init__(self, **kwargs) -> None:
        super().__init__(id="transcript", **kwargs)
        self._history: "list" = []  # every tracked widget, oldest first
        self._blocks: dict = {}  # (turn, seq, kind, index) -> AssistantText|ThinkingBlock
        self._message_seq: dict = {}  # turn -> current message sequence number
        self.tool_cards: dict = {}  # tool_use_id -> ToolCard
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

    def is_at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 1

    async def _mount_tracked(self, widget, *, before=None) -> None:
        was_at_bottom = self.is_at_bottom()
        if before is not None:
            await self.mount(widget, before=before)
        else:
            await self.mount(widget)
        self._history.append(widget)
        if was_at_bottom:
            self.scroll_end(animate=False)
            self.new_since_scroll = 0
        else:
            self.new_since_scroll += 1
        await self._fold_if_needed()

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

    def begin_message(self, turn: int) -> int:
        """Call once per `message_start` -- returns this message's sequence
        number within `turn` (used to key its text/thinking blocks)."""
        seq = self._message_seq.get(turn, 0) + 1
        self._message_seq[turn] = seq
        return seq

    async def append_text(self, turn: int, index: int, text: str) -> None:
        seq = self._message_seq.get(turn, 1)
        key = (turn, seq, "text", index)
        widget = self._blocks.get(key)
        if widget is None:
            widget = AssistantText()
            await self._mount_tracked(widget)
            widget.start_stream()
            self._blocks[key] = widget
        await widget.append_delta(text)

    async def append_thinking(self, turn: int, index: int, text: str) -> None:
        seq = self._message_seq.get(turn, 1)
        key = (turn, seq, "thinking", index)
        widget = self._blocks.get(key)
        if widget is None:
            widget = ThinkingBlock()
            # review/U5 must-do: a ThinkingBlock renders ABOVE this
            # message's answer text regardless of event ARRIVAL order --
            # OpenAI-dialect reasoning can stream its text delta before
            # its own reasoning delta, and the old unconditional append
            # put the thinking block wherever it happened to arrive
            # (below the answer, in that case). If a text widget for this
            # SAME message already exists, mount right before it;
            # otherwise (the common, correctly-ordered case) this is
            # exactly the old behaviour.
            await self._mount_tracked(widget, before=self._first_text_widget_for(turn, seq))
            self._blocks[key] = widget
        widget.append(text)

    def _first_text_widget_for(self, turn: int, seq: int):
        for (t, s, kind, _idx), widget in self._blocks.items():
            if t == turn and s == seq and kind == "text":
                return widget
        return None

    async def finish_open_streams(self) -> None:
        """Stop every still-open `AssistantText` stream (called on
        `turn_done`/`message_end` boundaries, and on interrupt, so a
        `MarkdownStream` never leaks a running asyncio task past its
        widget's useful life) and harvest its final text into `plain_log`.
        Safe to call more than once per message (idempotent via `_blocks`
        being fully cleared here)."""
        for widget in self._blocks.values():
            if isinstance(widget, AssistantText):
                await widget.finish()
                if widget.raw_text.strip():
                    self.plain_log.append(widget.raw_text)
            elif isinstance(widget, ThinkingBlock) and widget.text.strip():
                self.plain_log.append(f"[thinking] {widget.text}")
        self._blocks = {}

    # ---- tool cards (mounted by app.py, tracked here for folding) --------

    async def mount_tool_card(self, card) -> None:
        self.tool_cards[card.tool_use_id] = card
        await self._mount_tracked(card)

    async def mount_widget(self, widget) -> None:
        """Generic hook for permission/question/plan cards -- tracked for
        folding exactly like everything else."""
        await self._mount_tracked(widget)
