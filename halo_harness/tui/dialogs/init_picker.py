"""halo_harness.tui.dialogs.init_picker -- 1.0.1 hotfix 5: `halo init`'s
own interactive model picker (a small standalone Textual `App`, since a
picker offered from a plain CLI command has no host `BridgeApp`/screen stack
to push onto -- `init_cli.py` runs this synchronously via `run_init_picker`,
in a real terminal only; a piped/non-tty run never reaches this module at
all, see `init_cli.py`'s own numbered-list fallback for that case).

Same keys as every other list dialog (`tui/dialogs/listnav.py`'s `NavInput`:
Up/Down/PageUp/PageDown/Home/End move the highlight, Enter confirms, typing
filters, Esc cancels) and the same grouped-with-a-header-per-group rendering
`model_picker.py` uses, reusing that module's own (Textual-free) `_grouped`
helper so the two never drift apart on grouping rules.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from halo_harness.model_display import ROW_HEADER, format_model_row
from halo_harness.tui.dialogs.listnav import NavInput


class InitPickerApp(App):
    """`entries`: `Controller.list_models()`-shaped dicts (see
    `init_cli.py::_chat_capable_dbx_entries`) -- `{"ref": "dbx:<name>",
    "group": "<family>", "context_tokens", "max_output_tokens",
    "price_in_per_m", "price_out_per_m", "detail"}`. `.chosen` (read by the
    caller AFTER `.run()` returns) is the picked `ref`, or `None` on
    Esc/no pick."""

    TITLE = "halo init -- pick a default model"
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    Screen { align: center middle; }
    #init-picker-body { width: 90%; height: 80%; border: round $primary; padding: 1 2; }
    #init-picker-list { height: 1fr; }
    NavInput { margin-bottom: 1; }
    """

    def __init__(self, entries: "list[dict]") -> None:
        super().__init__()
        self.entries = entries
        self._filtered = entries
        self.chosen: "str | None" = None

    def compose(self) -> ComposeResult:
        with Vertical(id="init-picker-body"):
            yield Static(f"Pick a default model ({len(self.entries)} chat-capable endpoint(s) cached) "
                         f"-- Enter to confirm, Esc to keep the preset default", classes="dialog-title")
            yield Static(ROW_HEADER, classes="dialog-subtitle")
            yield NavInput(placeholder="Filter...", id="init-picker-filter", option_list_id="init-picker-list")
            yield OptionList(id="init-picker-list")

    def on_mount(self) -> None:
        self._refresh("")
        self.query_one(NavInput).focus()

    def _refresh(self, query: str) -> None:
        from halo_harness.tui.dialogs.model_picker import _grouped
        query_low = query.strip().lower()
        self._filtered = ([e for e in self.entries
                            if query_low in e["ref"].lower() or query_low in (e.get("group") or "").lower()]
                           if query_low else self.entries)
        option_list = self.query_one("#init-picker-list", OptionList)
        option_list.clear_options()
        for group, members in _grouped(self._filtered):
            if group:
                option_list.add_option(Option(Text(f"── {group} ──", style="bold dim"),
                                               disabled=True))
            for e in members:
                option_list.add_option(Option(Text(format_model_row(e), no_wrap=True, overflow="ellipsis"),
                                               id=e["ref"]))
        if self._filtered:
            option_list.action_first()

    def on_input_changed(self, event: NavInput.Changed) -> None:
        self._refresh(event.value)

    def on_input_submitted(self, event: NavInput.Submitted) -> None:
        self.chosen = self._filtered[0]["ref"] if self._filtered else None
        self.exit()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.chosen = str(event.option_id)
        self.exit()

    def action_cancel(self) -> None:
        self.chosen = None
        self.exit()


def run_init_picker(entries: "list[dict]") -> "str | None":
    """Runs the picker as a standalone full-screen app; returns the chosen
    `ref`, or `None` if nothing was picked (Esc, or the app exits some other
    way -- the caller keeps whatever default it already had either way)."""
    app = InitPickerApp(entries)
    app.run()
    return app.chosen


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 13: a minimal Up/Down/Enter/Esc picker for a SHORT, unfiltered
# list of plain (ref, label) choices -- `init`'s own "select a provider to
# set up" and "set up another provider?" steps have only a handful of rows,
# too few to need NavInput's filter-typing (or model_display's ctx/price
# columns) at all.
# ---------------------------------------------------------------------------

class SimpleListPickerApp(App):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    Screen { align: center middle; }
    #simple-picker-body { width: 70%; height: auto; max-height: 80%; border: round $primary; padding: 1 2; }
    #simple-picker-list { height: auto; max-height: 20; }
    """

    def __init__(self, title: str, items: "list[tuple[str, str]]", *, initial_ref: "str | None" = None) -> None:
        """`items`: `[(ref, label), ...]`. `initial_ref`, when it matches
        one of `items`' own refs, is where the highlight starts -- any
        other value (including None) just leaves it at the first row."""
        super().__init__()
        self.title_text = title
        self.items = items
        self.initial_ref = initial_ref
        self.chosen: "str | None" = None

    def compose(self) -> ComposeResult:
        with Vertical(id="simple-picker-body"):
            yield Static(self.title_text, classes="dialog-title")
            option_list = OptionList(*[Option(label, id=ref) for ref, label in self.items], id="simple-picker-list")
            yield option_list

    def on_mount(self) -> None:
        option_list = self.query_one("#simple-picker-list", OptionList)
        option_list.focus()
        refs = [ref for ref, _label in self.items]
        start = refs.index(self.initial_ref) if self.initial_ref in refs else None
        if start is not None:
            option_list.highlighted = start
        else:
            option_list.action_first()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.chosen = str(event.option_id)
        self.exit()

    def action_cancel(self) -> None:
        self.chosen = None
        self.exit()


def run_simple_picker(title: str, items: "list[tuple[str, str]]", *, initial_ref: "str | None" = None) -> "str | None":
    """Runs `SimpleListPickerApp` as a standalone full-screen app; returns
    the chosen `ref`, or `None` on Esc/cancel."""
    app = SimpleListPickerApp(title, items, initial_ref=initial_ref)
    app.run()
    return app.chosen
