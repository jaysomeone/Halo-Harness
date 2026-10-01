"""rolo_claude.tui.dialogs.palette -- `Ctrl+P` command palette (U5 scope A,
adapted from OpenCode's own `ctrl+p`): one fuzzy-filterable list over slash
commands, skills (already a `source == "skill"` subset of the registry),
recent files (top-level `cwd` listing, reusing `tui/completion.py`'s own
walk), and sessions (`Controller.list_sessions()`). `app.py` assembles
`items` (this dialog does no filesystem/controller I/O itself, matching
every other dialog in this package) and reads the dismissed item's `kind`
to decide what to do with it.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from rolo_claude.tui.dialogs.listnav import NavInput

_KIND_GLYPH = {"command": "/", "skill": "✦", "file": "\U0001f4c4", "session": "\U0001f552"}


def _score(query: str, item: dict) -> "tuple[int, int]":
    """Lower sorts first. `(0, position)` for a prefix match on the label,
    `(1, position)` for a substring match anywhere in label/detail, else
    `(2, 0)` (excluded by the caller -- see `filter_items`)."""
    label = item.get("label", "").lower()
    if label.startswith(query):
        return (0, 0)
    pos = label.find(query)
    if pos >= 0:
        return (1, pos)
    detail = item.get("detail", "").lower()
    pos = detail.find(query)
    if pos >= 0:
        return (1, pos + len(label))
    return (2, 0)


def filter_items(items: "list[dict]", query: str) -> "list[dict]":
    """Pure fuzzy-ish filter (prefix > substring, alphabetical within a
    tier) -- kept free of textual so it's unit-testable directly."""
    query = query.strip().lower()
    if not query:
        return list(items)
    scored = [(item, _score(query, item)) for item in items]
    scored = [(item, s) for item, s in scored if s[0] < 2]
    scored.sort(key=lambda pair: (pair[1][0], pair[1][1], pair[0].get("label", "")))
    return [item for item, _ in scored]


class CommandPalette(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    CommandPalette { align: center middle; }
    CommandPalette > Vertical { width: 90%; height: 80%; border: round $primary;
        background: $surface; padding: 1 2; }
    CommandPalette Input { margin-bottom: 1; }
    CommandPalette OptionList { height: 1fr; }
    """

    def __init__(self, items: "list[dict]") -> None:
        super().__init__()
        # item: {"kind": "command"|"skill"|"file"|"session", "label": str,
        # "detail": str, "value": str}
        self.items = items
        self._filtered = items

    def compose(self):
        with Vertical():
            yield Static("Command palette (slash commands, skills, files, sessions)", classes="dialog-title")
            yield NavInput(placeholder="Type to filter...", id="palette-filter", option_list_id="palette-list")
            yield OptionList(id="palette-list")

    def on_mount(self) -> None:
        self._refresh("")
        self.query_one("#palette-filter", Input).focus()

    def _refresh(self, query: str) -> None:
        self._filtered = filter_items(self.items, query)
        option_list = self.query_one("#palette-list", OptionList)
        option_list.clear_options()
        for i, item in enumerate(self._filtered):
            glyph = _KIND_GLYPH.get(item.get("kind", ""), "•")
            detail = item.get("detail", "")
            label = f"{glyph} {item.get('label', '')}" + (f"  — {detail}" if detail else "")
            option_list.add_option(Option(label, id=str(i)))
        # 1.0.1 hotfix addendum 7: see model_picker.py's matching comment.
        if self._filtered:
            option_list.action_first()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        if self._filtered:
            self.dismiss(self._filtered[0])
        else:
            self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        try:
            idx = int(event.option_id)
        except (TypeError, ValueError):
            self.dismiss(None)
            return
        self.dismiss(self._filtered[idx] if 0 <= idx < len(self._filtered) else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
