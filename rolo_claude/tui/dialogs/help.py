"""rolo_claude.tui.dialogs.help -- F1 (D-TUI: "Help"). Static key reference
plus the live slash-command list from the registry (`help_rows()`, U0).
"""

from __future__ import annotations

from typing import Optional

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

_KEY_ROWS = [
    ("Enter", "Submit"),
    ("\\ + Enter, Ctrl+J, Alt+Enter", "Insert a newline"),
    ("Esc", "Interrupt the running turn / dismiss a card / deny"),
    ("Ctrl+C (x2)", "Interrupt, then quit"),
    ("Ctrl+D", "Quit (on an empty prompt)"),
    ("Shift+Tab", "Cycle permission mode: default -> acceptEdits -> plan -> auto"),
    ("Ctrl+L", "Clear the transcript view"),
    ("Ctrl+O", "Toggle verbose (expand thinking / tool cards)"),
    ("Ctrl+R", "Search prompt history"),
    ("PgUp / PgDn", "Scroll the transcript"),
    ("Tab", "Accept the highlighted / completion"),
    ("F1", "This help"),
    ("/ then text", "Slash command (Tab completes)"),
    ("@ then text", "Path completion"),
]


class HelpDialog(ModalScreen):
    BINDINGS = [Binding("escape,f1,q", "cancel", "Close", show=False)]
    DEFAULT_CSS = """
    HelpDialog { align: center middle; }
    HelpDialog > VerticalScroll { width: 80%; height: 80%; border: round $primary;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, registry: Optional[object] = None) -> None:
        super().__init__()
        self.registry = registry

    def compose(self):
        with VerticalScroll():
            yield Static("Keys", classes="dialog-title")
            width = max(len(k) for k, _ in _KEY_ROWS)
            for key, desc in _KEY_ROWS:
                yield Static(f"  {key.ljust(width)}   {desc}", markup=False)
            yield Static("")
            yield Static("Commands", classes="dialog-title")
            rows = self.registry.help_rows() if self.registry is not None else []
            if rows:
                cwidth = max(len(inv) for inv, _ in rows)
                for inv, desc in rows:
                    yield Static(f"  {inv.ljust(cwidth)}   {desc or ''}", markup=False)
            else:
                yield Static("  (no commands discovered)")

    def action_cancel(self) -> None:
        self.dismiss(None)
