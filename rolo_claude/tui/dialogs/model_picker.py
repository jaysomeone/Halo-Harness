"""rolo_claude.tui.dialogs.model_picker -- `/model` with no argument (D-TUI:
"ModelPicker (filter + ref/context/price)"). `models` is whatever
`Controller.list_models()` returned: `[{ref, context, output, price_in,
price_out, provider}, ...]`. Dismisses with the chosen `ref` string, or
`None` if cancelled.
"""

from __future__ import annotations

import difflib

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


def _fmt_price(v) -> str:
    if v is None:
        return "?"
    try:
        return f"${float(v) * 1_000_000:.2f}/M"
    except (TypeError, ValueError):
        return "?"


def _row_text(m: dict) -> str:
    ctx = m.get("context")
    ctx_str = f"{ctx // 1000}k" if isinstance(ctx, int) else "?"
    return f"{m['ref']:<42} ctx={ctx_str:<6} out={m.get('output') or '?':<8} in={_fmt_price(m.get('price_in'))} out={_fmt_price(m.get('price_out'))}"


class ModelPicker(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    ModelPicker > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    ModelPicker Input { margin-bottom: 1; }
    ModelPicker OptionList { height: 1fr; }
    """

    def __init__(self, models: "list", *, current: str = "") -> None:
        super().__init__()
        # `Controller.list_models()` returns dicts; `FakeController`'s own
        # (tests/test_fake_controller.py-pinned) shape is a bare list of ref
        # strings -- normalize both to the dict shape this dialog renders.
        self.models = [m if isinstance(m, dict) else {"ref": m, "context": None, "output": None,
                                                        "price_in": None, "price_out": None, "provider": "?"}
                       for m in models]
        self.current = current
        self._filtered = models

    def compose(self):
        with Vertical():
            yield Static(f"Select a model (current: {self.current or '?'})", classes="dialog-title")
            yield Input(placeholder="Filter models...", id="model-filter")
            yield OptionList(id="model-list")
            yield Static("", id="model-hint")

    def on_mount(self) -> None:
        self._refresh_list("")
        self.query_one("#model-filter", Input).focus()

    def _refresh_list(self, query: str) -> None:
        query_low = query.strip().lower()
        if query_low:
            self._filtered = [m for m in self.models if query_low in m["ref"].lower()]
        else:
            self._filtered = self.models
        option_list = self.query_one("#model-list", OptionList)
        option_list.clear_options()
        hint = self.query_one("#model-hint", Static)
        if self._filtered:
            for m in self._filtered:
                option_list.add_option(Option(_row_text(m), id=m["ref"]))
            hint.update("")
        else:
            refs = [m["ref"] for m in self.models]
            near = difflib.get_close_matches(query, refs, n=5, cutoff=0.4)
            hint.update(f"No exact match. Did you mean: {', '.join(near)}" if near else "No matching models.")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_list(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._filtered:
            self.dismiss(self._filtered[0]["ref"])
        else:
            self.dismiss(event.value.strip() or None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option_id))

    def action_cancel(self) -> None:
        self.dismiss(None)
