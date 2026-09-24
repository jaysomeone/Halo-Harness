"""rolo_claude.tui.dialogs.session_picker -- `/resume` (`--resume` list,
D-TUI). `sessions` is `Controller.list_sessions()`'s shape:
`[{id, cwd, mtime, summary, title, cost_usd, turns}, ...]` (U5: "title/age/
cost/turns" -- the last three keys are additive; a plain
`{id, cwd, mtime, summary}` FakeController-era dict still renders fine,
just with blank/zero values via `.get()`). Dismisses with the chosen
session id, or `None` if cancelled.
"""

from __future__ import annotations

import time


def _age(mtime: float) -> str:
    seconds = max(0.0, time.time() - (mtime or 0))
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"

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
                title = s.get("title") or s.get("summary") or "(no summary)"
                cost = s.get("cost_usd")
                cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "$?"
                turns = s.get("turns", "?")
                label = (f"{_age(s.get('mtime', 0)):>4} ago  {s.get('id', '')[:12]}  "
                        f"{turns} turn(s)  {cost_str}  {title}")
                option_list.add_option(Option(label, id=s.get("id")))
            yield option_list

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
