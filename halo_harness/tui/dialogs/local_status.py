"""halo_harness.tui.dialogs.local_status -- `/local` with no arguments
(Halo 2.0.3 round 5, brief item 4): the same read-only-status-dialog shape
`/ollama`'s `OllamaStatus` already uses. `rows` is a list of `providers.
local_models.LocalModelRow`, already computed OFF the UI thread by
`tui/slash.py::_handle_local` (every source here can touch the network);
`refresh`, when given, is a 0-arg callable returning a fresh `rows` list
-- `r` re-runs it, also off the UI thread via `app.run_worker`, and ALSO
probes manual Hugging Face local-server entries (bare open never does --
see `providers.local_models.build_local_view`'s own docstring).
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static


class LocalStatus(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=True),
    ]
    DEFAULT_CSS = """
    LocalStatus { align: center middle; }
    LocalStatus > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    LocalStatus RichLog { height: 1fr; }
    """

    def __init__(self, rows: list, *, refresh=None) -> None:
        super().__init__()
        self.rows = rows
        self._refresh = refresh

    def compose(self):
        with Vertical():
            yield Static("Local models", classes="dialog-title")
            yield Static("r: refresh (also probes manual Hugging Face servers)  |  Esc: close",
                         classes="dialog-subtitle")
            yield RichLog(id="local-log", wrap=True, highlight=False, markup=False, auto_scroll=False)

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        from halo_harness.providers.local_models import format_local_view
        log_widget = self.query_one("#local-log", RichLog)
        log_widget.clear()
        log_widget.write(format_local_view(self.rows))

    def action_refresh(self) -> None:
        if self._refresh is None:
            return
        self.app.run_worker(self._refresh_worker, thread=True, name="local-refresh", group="local-refresh")

    def _refresh_worker(self) -> None:
        try:
            fresh = self._refresh()
        except Exception:
            fresh = self.rows
        self.app.call_from_thread(self._apply_refresh, fresh)

    def _apply_refresh(self, rows: list) -> None:
        self.rows = rows
        self._render()

    def action_cancel(self) -> None:
        self.dismiss(None)
