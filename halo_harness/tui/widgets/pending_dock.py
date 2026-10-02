"""halo_harness.tui.widgets.pending_dock -- `PendingDock` (Halo 2.0.1 W3a,
"PendingDock (nothing that waits for the user may be off-screen)"):
a container between the `Transcript` and the input row, hidden when empty,
holding exactly the ACTIVE permission/question/plan card. It is a NORMAL
(non-docked) flow child placed right after `Transcript` in `BridgeApp.
compose()` -- `Transcript` itself is `height: 1fr` (grows to fill whatever
space is left), so this dock (height: auto, 0 when hidden) always ends up
sitting directly above the prompt row regardless of how tall the
transcript's own content is, with no absolute positioning of its own
needed. `BridgeApp._pending_queue` (finding 16's own queue, keyed by each
card's `request_id`) is what actually feeds it one card at a time, in
arrival order -- this widget itself knows nothing about the queue, only
how to show/hide exactly one card.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import VerticalScroll


class PendingDock(VerticalScroll):
    """Capped at 12 lines with its own internal scroll (a long diff/
    question list scrolls INSIDE the dock rather than pushing the prompt
    row off-screen); `o` opens the current card's full content in the same
    `PagerScreen` a `ToolCard`'s own `o` already uses -- reached via
    Textual's normal focus-chain binding fallback (the focused CARD's own
    bindings, e.g. PermissionCard's 1/2/3/4, are checked first and always
    win; `o` only ever reaches this dock because none of the three pending
    card types bind it themselves)."""

    BINDINGS = [Binding("o", "open_pager", "Pager", show=False)]

    DEFAULT_CSS = """
    PendingDock {
        height: auto;
        max-height: 12;
        width: 100%;
        background: $surface;
        border-top: solid $warning;
        display: none;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.card = None
        self.display = False

    async def show_card(self, card) -> None:
        """Mounts `card` as this dock's one and only child and makes the
        dock itself visible. Defensive `remove_children()` first -- the
        queue (`BridgeApp`) only ever calls this once the previous card
        has already been cleared, so there should never actually be one,
        but a stray leftover must never silently stack two cards in here."""
        await self.remove_children()
        self.card = card
        await self.mount(card)
        self.display = True

    async def clear(self) -> None:
        await self.remove_children()
        self.card = None
        self.display = False

    def action_open_pager(self) -> None:
        if self.card is None:
            return
        from halo_harness.tui.widgets.cards import PagerScreen
        title = getattr(self.card, "summary", None) or type(self.card).__name__
        body = (getattr(self.card, "plan_text", None) or getattr(self.card, "reason", None)
                or getattr(self.card, "summary", None) or "(no further detail)")
        self.app.push_screen(PagerScreen(str(title), str(body)))
