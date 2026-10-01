"""halo_harness.tui.dialogs.session_picker -- `/resume` (`--resume` list,
D-TUI). `sessions` is `Controller.list_sessions()`'s shape:
`[{id, cwd, mtime, summary, title, model, cost_usd, turns}, ...]` (U5:
"title/age/cost/turns" -- `model` and everything after `title` are
additive; a plain `{id, cwd, mtime, summary}` FakeController-era dict still
renders fine, just with blank/zero values via `.get()`). Dismisses with the
chosen session id, or `None` if cancelled.

H13 Part C ("/resume search"): a live text filter, same shape as `tui/
dialogs/palette.py`'s own `Input` + `on_input_changed` pattern -- ranking
itself lives in `halo_harness.agent.sessions.filter_sessions` (shared with
the CLI's own `--resume <text>` unique-match/ambiguous-picker logic, so
typing a word here and passing the same word to `--resume` agree on what
counts as a match) rather than being reimplemented here.
"""

from __future__ import annotations

import time

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from halo_harness.agent.sessions import filter_sessions
from halo_harness.tui.dialogs.listnav import NavInput


def _age(mtime: float) -> str:
    seconds = max(0.0, time.time() - (mtime or 0))
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _row(s: dict) -> str:
    title = s.get("title") or s.get("summary") or "(no summary)"
    cost = s.get("cost_usd")
    cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "$?"
    turns = s.get("turns", "?")
    return (f"{_age(s.get('mtime', 0)):>4} ago  {s.get('id', '')[:12]}  "
            f"{turns} turn(s)  {cost_str}  {title}")


class SessionPicker(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    SessionPicker > Vertical { width: 80%; height: 70%; border: round $primary; background: $surface; padding: 1 2; }
    SessionPicker Input { margin-bottom: 1; }
    SessionPicker OptionList { height: 1fr; }
    """

    def __init__(self, sessions: "list[dict]", *, initial_query: str = "") -> None:
        super().__init__()
        self.sessions = sessions
        # H13 Part C: `/resume <text>` (tui/slash.py) and an ambiguous
        # `--resume <text>` at startup (tui/bootstrap.py) both open this
        # ALREADY filtered, so the user sees the narrowed list immediately
        # instead of having to retype what they just asked for.
        self._initial_query = initial_query or ""
        self._filtered = filter_sessions(sessions, self._initial_query) if self._initial_query else list(sessions)

    def compose(self):
        with Vertical():
            yield Static("Resume a previous session (Esc to cancel)", classes="dialog-title")
            yield NavInput(value=self._initial_query, placeholder="Type to filter by title, prompt, cwd or model...",
                           id="resume-filter", option_list_id="resume-list")
            yield OptionList(id="resume-list")

    def on_mount(self) -> None:
        self._refresh(self._initial_query)
        self.query_one("#resume-filter", Input).focus()

    def _refresh(self, query: str) -> None:
        self._filtered = filter_sessions(self.sessions, query)
        option_list = self.query_one("#resume-list", OptionList)
        option_list.clear_options()
        if not self._filtered:
            option_list.add_option(Option(
                "No previous sessions for this directory." if not self.sessions else "No sessions match.",
                disabled=True))
            return
        for s in self._filtered:
            option_list.add_option(Option(_row(s), id=s.get("id")))
        # 1.0.1 hotfix addendum 7: see model_picker.py's matching comment --
        # highlights the first row up front so "Down twice" lands on the
        # third, not the second.
        option_list.action_first()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        if self._filtered:
            self.dismiss(self._filtered[0].get("id"))
        else:
            self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
