"""halo_harness.tui.dialogs.ollama_status -- `/ollama` (Halo 2.0.3 round 3,
brief item 4): the same read-only-status-dialog shape `/mcp`'s McpStatus
and `/tasks`'s TranscriptViewer already use. `analyses` is a list of
`providers.ollama_panel.HostAnalysis`, already computed OFF the UI thread
by `tui/slash.py::_handle_ollama` (host reads are real network calls --
never done from inside a Textual event handler); `refresh`, when given, is
a 0-arg callable returning a fresh `analyses` list -- `r` re-runs it, also
off the UI thread via `app.run_worker`, never blocking the TUI.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static


class OllamaStatus(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=True),
    ]
    DEFAULT_CSS = """
    OllamaStatus { align: center middle; }
    OllamaStatus > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    OllamaStatus RichLog { height: 1fr; }
    """

    def __init__(self, analyses: list, *, refresh=None) -> None:
        super().__init__()
        self.analyses = analyses
        self._refresh = refresh

    def compose(self):
        with Vertical():
            yield Static("Ollama hosts", classes="dialog-title")
            yield Static("r: refresh  |  Esc: close", classes="dialog-subtitle")
            yield RichLog(id="ollama-log", wrap=True, highlight=False, markup=False, auto_scroll=False)

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        from halo_harness.providers.ollama_panel import format_host_analysis
        log_widget = self.query_one("#ollama-log", RichLog)
        log_widget.clear()
        if not self.analyses:
            log_widget.write("No Ollama hosts configured.")
            return
        for i, a in enumerate(self.analyses):
            if i:
                log_widget.write("")
            log_widget.write(format_host_analysis(a))

    def action_refresh(self) -> None:
        if self._refresh is None:
            return
        self.app.run_worker(self._refresh_worker, thread=True, name="ollama-refresh", group="ollama-refresh")

    def _refresh_worker(self) -> None:
        try:
            fresh = self._refresh()
        except Exception:
            fresh = self.analyses
        self.app.call_from_thread(self._apply_refresh, fresh)

    def _apply_refresh(self, analyses: list) -> None:
        self.analyses = analyses
        self._render()

    def action_cancel(self) -> None:
        self.dismiss(None)
