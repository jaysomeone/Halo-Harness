"""halo_harness.tui.dialogs.local_status -- `/local` with no arguments
(Halo 2.0.3 round 5, brief item 4): the same read-only-status-dialog shape
`/ollama`'s `OllamaStatus` already uses. `rows` is a list of `providers.
local_models.LocalModelRow`, already computed OFF the UI thread by
`tui/slash.py::_handle_local` (every source here can touch the network);
`refresh`, when given, is a 0-arg callable returning a fresh `rows` list
-- `r` re-runs it, also off the UI thread via `app.run_worker`, and ALSO
probes manual Hugging Face local-server entries (bare open never does --
see `providers.local_models.build_local_view`'s own docstring).

Round 5c (brief item 3): the `s` key serves a file-backed model -- a small
prompt (`_ServePrompt`) asks for the exact path/name (this dialog's own
RichLog has no per-row selection to read a "highlighted" row from, unlike
the `OptionList`-based model picker), then starts it off the UI thread.
When the chosen runtime isn't already on this machine, the dialog never
offers the download-consent flow itself (that stays a deliberately
single-surface decision, `halo local serve`'s own `--yes`/stdin prompt in
a real terminal) -- it names the one-line CLI command to run instead.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static


class _ServePrompt(ModalScreen):
    """`s` on `LocalStatus` -- the one field this dialog needs that a
    plain RichLog row can't supply: which model to serve. Dismisses with
    the typed string, or `None` on Esc/Cancel."""
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    _ServePrompt { align: center middle; }
    _ServePrompt > Vertical { width: 70; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    _ServePrompt Horizontal { height: 3; align: right middle; margin-top: 1; }
    _ServePrompt Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Serve which model? (an exact path, or a name shown above)", classes="dialog-title")
            yield Input(placeholder="/path/to/model.gguf", id="serve-prompt-input")
            with Horizontal():
                yield Button("Cancel", id="serve-prompt-cancel")
                yield Button("Serve", id="serve-prompt-go", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "serve-prompt-go":
            self._go()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._go()

    def _go(self) -> None:
        value = self.query_one("#serve-prompt-input", Input).value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LocalStatus(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("s", "serve", "Serve a model", show=True),
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
            yield Static("r: refresh (also probes manual Hugging Face servers)  |  s: serve a model  |  "
                         "Esc: close", classes="dialog-subtitle")
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

    def action_serve(self) -> None:
        self.app.push_screen(_ServePrompt(), self._on_serve_prompt_result)

    def _on_serve_prompt_result(self, model) -> None:
        if not model:
            return
        self.app.run_worker(lambda: self._serve_worker(model), thread=True, name="local-serve", group="local-serve")

    def _serve_worker(self, model: str) -> None:
        from halo_harness.providers.local_use import resolve_local_file
        entry = resolve_local_file(model)
        if entry is None:
            self.app.call_from_thread(self._apply_serve_result, False,
                                       [f"no file-backed model found at or named {model!r}"])
            return
        from halo_harness.providers.local_runtime import find_runtime_binary, runtime_for_format
        runtime = runtime_for_format(entry.format)
        if runtime is not None and find_runtime_binary(runtime) is None:
            # Round 5c: the download-consent flow is deliberately a
            # single-surface decision (a real terminal) -- see this
            # module's own docstring.
            self.app.call_from_thread(self._apply_serve_result, False,
                                       [f"{runtime} isn't installed yet -- run `halo local serve {model}` in a "
                                        f"terminal, which will offer to fetch it."])
            return
        from halo_harness.providers.local_use import serve_local_model
        ok, lines = serve_local_model(model, confirm=lambda _q: False)
        self.app.call_from_thread(self._apply_serve_result, ok, lines)

    def _apply_serve_result(self, ok: bool, lines: list) -> None:
        text = "\n".join(lines)
        try:
            self.app.notify(text, severity="information" if ok else "error", timeout=10)
        except Exception:
            pass
        if ok and self._refresh is not None:
            self.action_refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)
