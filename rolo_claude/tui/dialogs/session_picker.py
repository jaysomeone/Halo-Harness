"""rolo_claude.tui.dialogs.session_picker -- `/resume` (`--resume` list,
D-TUI). `sessions` is `Controller.list_sessions()`'s shape:
`[{id, cwd, mtime, summary}, ...]`. Dismisses with the chosen session id, or
`None` if cancelled.
"""

from __future__ import annotations

import time

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class SessionPicker(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    SessionPicker > Vertical { width: 80%; height: 70%; border: round $primary; background: $surface; padding: 1 2; }
    SessionPicker OptionList { height: 1fr; }
    """

    def __init__(self, sessions: "list[dict]") -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self):
        with Vertical():
            yield Static("Resume a previous session (Esc to cancel)", classes="dialog-title")
            option_list = OptionList()
            if not self.sessions:
                option_list.add_option(Option("No previous sessions for this directory.", disabled=True))
            for s in self.sessions:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("mtime", 0)))
                label = f"{when}  {s.get('id', '')[:12]}  {s.get('summary', '') or '(no summary)'}"
                option_list.add_option(Option(label, id=s.get("id")))
            yield option_list

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
