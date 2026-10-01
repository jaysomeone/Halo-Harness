"""halo_harness.tui.dialogs.permissions -- `/permissions` (D-TUI:
"PermissionsDialog (rules by source, add rule)"). `rules` is
`Controller.list_permission_rules()`'s shape: `[{action, source, rule},
...]`. Adding a rule here always teaches the SESSION (in-memory) --
Claude-Code-shared "always" rules are added from the `PermissionCard`
itself (`3`), which is the point at which a real suggested rule exists;
this dialog is for reviewing what's active and adding an ad hoc one.
"""

from __future__ import annotations

from typing import Callable, Optional

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


class PermissionsDialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]
    DEFAULT_CSS = """
    PermissionsDialog { align: center middle; }
    PermissionsDialog > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    PermissionsDialog OptionList { height: 1fr; }
    """

    def __init__(self, rules: "list[dict]", *, mode: str = "default",
                 add_rule: Optional[Callable] = None) -> None:
        super().__init__()
        self.rules = rules
        self.mode = mode
        self._add_rule = add_rule

    def compose(self):
        with Vertical():
            yield Static(f"Permission mode: {self.mode}", classes="dialog-title")
            option_list = OptionList()
            if not self.rules:
                option_list.add_option(Option("No rules configured.", disabled=True))
            for r in sorted(self.rules, key=lambda r: (r.get("action", ""), r.get("source", ""))):
                option_list.add_option(Option(f"[{r.get('action')}] {r.get('rule')}  ({r.get('source')})"))
            yield option_list
            yield Input(placeholder="Add a session rule, e.g. Bash(git *)  (Enter to add)", id="add-rule")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text and self._add_rule is not None:
            self._add_rule(text)
            self.rules = self.rules + [{"action": "allow", "source": "session", "rule": text}]
            event.input.value = ""
            self._refresh_list()

    def _refresh_list(self) -> None:
        option_list = self.query_one(OptionList)
        option_list.clear_options()
        for r in sorted(self.rules, key=lambda r: (r.get("action", ""), r.get("source", ""))):
            option_list.add_option(Option(f"[{r.get('action')}] {r.get('rule')}  ({r.get('source')})"))

    def action_cancel(self) -> None:
        self.dismiss(None)
