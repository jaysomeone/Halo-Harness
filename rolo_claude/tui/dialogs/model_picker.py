"""rolo_claude.tui.dialogs.model_picker -- `/model` with no argument (D-TUI:
"ModelPicker (filter + ref/context/price)"). `models` is whatever
`Controller.list_models()` returned: `[{ref, context_tokens,
max_output_tokens, price_in_per_m, price_out_per_m, provider}, ...]`.
Dismisses with the chosen `ref` string, or `None` if cancelled.

1.0.1 hotfix addendum 7/8: the filter `Input` is a `NavInput` (Up/Down/
PageUp/PageDown/Home/End move the `OptionList` highlight, Enter selects it --
see `tui/dialogs/listnav.py`'s own docstring for why Textual's plain `Input`
needed this at all), and rows are grouped by `Controller.list_models()`'s own
`group` tag with one header per group instead of an inline `[group]` suffix,
each row rendered as a single ellipsized line (never wrapped, which used to
break the column alignment on a long ref/path).

1.0.1 hotfix 12: every row -- OpenRouter, cc:, Databricks alike -- now shows
context/output/price columns through the ONE shared `model_display.
format_model_row` (never a per-provider "show path=/dbu= INSTEAD of prices"
special case); a Databricks row's family/path still shows, as a bracketed
`detail` tag after the price columns instead of replacing them.
"""

from __future__ import annotations

import difflib

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rolo_claude.model_display import ROW_HEADER, format_model_row
from rolo_claude.tui.dialogs.listnav import NavInput


def _grouped(models: "list[dict]") -> "list[tuple[str, list[dict]]]":
    """Stable-groups `models` by their own `group` tag (ungrouped rows --
    OpenRouter, aliases, the synthesized current-model row -- share one
    untitled, header-less bucket) -- preserves each group's FIRST-SEEN
    order rather than assuming same-group rows already sit contiguously in
    the incoming list (`Controller.list_models()`'s own Databricks section
    is endpoint-NAME-sorted, which does not always keep one family
    together)."""
    order: "list[str]" = []
    buckets: "dict[str, list[dict]]" = {}
    for m in models:
        key = m.get("group") or ""
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(m)
    return [(key, buckets[key]) for key in order]


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
        self.models = [m if isinstance(m, dict) else {"ref": m, "provider": "?"} for m in models]
        self.current = current
        self._filtered = models

    def compose(self):
        with Vertical():
            yield Static(f"Select a model (current: {self.current or '?'})", classes="dialog-title")
            yield Static(ROW_HEADER, classes="dialog-subtitle")
            yield NavInput(placeholder="Filter models...", id="model-filter", option_list_id="model-list")
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
            for group, members in _grouped(self._filtered):
                if group:
                    option_list.add_option(Option(Text(f"── {group} ──", style="bold dim"),
                                                   disabled=True))
                for m in members:
                    option_list.add_option(
                        Option(Text(format_model_row(m), no_wrap=True, overflow="ellipsis"), id=m["ref"]))
            # 1.0.1 hotfix addendum 7: highlights the first SELECTABLE row
            # up front (`action_first` skips a disabled group-header, unlike
            # a bare `.highlighted = 0`) -- otherwise `highlighted` starts
            # at None and "Down twice" only reaches the SECOND row, not the
            # third, contradicting the documented "Down, Down, Enter -> the
            # third entry" behavior.
            option_list.action_first()
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
