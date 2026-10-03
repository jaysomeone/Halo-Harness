"""halo_harness.tui.dialogs.update_dialog -- `/update`'s confirm dialog
(Halo 2.0.2 round 6): installed vs. available build, up to 15 commits
between them, the exact reinstall command, and the one real choice --
Enter applies it and restarts halo, Esc leaves everything exactly as it
was. Static display only; `tui/slash.py::_handle_update` builds every
string this receives off the UI thread first (a live git/network check).
"""

from __future__ import annotations

from typing import Optional

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class UpdateDialog(ModalScreen):
    BINDINGS = [
        Binding("enter", "confirm", "Update and restart", show=False),
        Binding("escape", "cancel", "Not now", show=False),
    ]
    DEFAULT_CSS = """
    UpdateDialog { align: center middle; }
    UpdateDialog > VerticalScroll { width: 84; max-height: 26; border: round $primary;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, installed_line: str, available_line: str, commit_lines: "Optional[list]" = None,
                 command: "Optional[str]" = None, *, up_to_date: bool = False) -> None:
        super().__init__()
        self.installed_line = installed_line
        self.available_line = available_line
        self.commit_lines = commit_lines or []
        self.command = command
        self.up_to_date = up_to_date

    def compose(self):
        with VerticalScroll():
            yield Static("Update halo", classes="dialog-title")
            yield Static(f"  installed: {self.installed_line}", markup=False)
            yield Static(f"  available: {self.available_line}", markup=False)
            if self.commit_lines:
                yield Static("")
                for line in self.commit_lines[:15]:
                    yield Static(f"  {line}", markup=False)
            yield Static("")
            yield Static(f"  command: {self.command or '(unknown)'}", markup=False)
            yield Static("")
            if self.up_to_date:
                yield Static("  Already up to date.  Esc: close")
            else:
                yield Static("  Enter: update and restart halo    Esc: not now")

    def action_confirm(self) -> None:
        self.dismiss(None if self.up_to_date else "update")

    def action_cancel(self) -> None:
        self.dismiss(None)
