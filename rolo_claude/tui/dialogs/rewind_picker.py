"""rolo_claude.tui.dialogs.rewind_picker -- `/rewind` with no argument (U5
scope B): pick a shadow-repo step to restore to. `steps` is
`ShadowStore.list_steps()`'s own shape (oldest first); shown newest first.
Dismisses with the chosen step's `id`, or `None` if cancelled.
"""

from __future__ import annotations

import time

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class RewindPicker(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    RewindPicker { align: center middle; }
    RewindPicker > Vertical { width: 85%; height: 70%; border: round $primary; background: $surface; padding: 1 2; }
    RewindPicker OptionList { height: 1fr; }
    """

    def __init__(self, steps: "list[dict]") -> None:
        super().__init__()
        self.steps = list(reversed(steps))

    def compose(self):
        with Vertical():
            yield Static("Rewind to a step (Esc to cancel)", classes="dialog-title")
            option_list = OptionList()
            if not self.steps:
                option_list.add_option(Option("No recorded steps yet for this session.", disabled=True))
            for s in self.steps:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.get("ts", 0) or 0))
                files = ", ".join(s.get("files") or [])[:60]
                label = f"{when}  {s.get('id', '')}  {s.get('label', '')}  [{files}]"
                option_list.add_option(Option(label, id=s.get("id")))
            yield option_list

    def on_mount(self) -> None:
        # 1.0.1 hotfix addendum 7: highlights the first (newest) step up
        # front, same convention model_picker.py/session_picker.py/
        # palette.py now use -- `highlighted` otherwise starts at None.
        if self.steps:
            self.query_one(OptionList).action_first()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
