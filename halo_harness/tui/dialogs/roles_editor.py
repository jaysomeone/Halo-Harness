"""halo_harness.tui.dialogs.roles_editor -- `/roles edit <name>` (Halo
2.0.2 brief A.5): one row per role (every built-in `roles.ROLE_NAMES` plus
any custom name the template already defines), each showing its current
model/effort. Enter on a row opens the SAME `ModelPicker` `/model` uses to
pick that row's model; an inline effort `Input` then appears, pre-filled
with the row's current effort (or blank), validated against the harness's
own accepted effort words. `ctrl+s` saves back to the named template
(`roles.save_role_template`) and dismisses with `True`; Escape cancels
with no write, dismissing with `False`. `tui/slash.py::_handle_roles`
builds this with the template's already-loaded `roles` dict and
`controller.list_models()`'s own catalog -- never loads either itself, so
it stays unit-testable with a plain fake `models` list.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


class RolesEditor(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=True),
    ]
    DEFAULT_CSS = """
    RolesEditor { align: center middle; }
    RolesEditor > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    RolesEditor OptionList { height: 1fr; }
    RolesEditor #roles-effort-input { display: none; }
    """

    def __init__(self, template_name: str, roles: dict, models: list, *, description: str = "") -> None:
        super().__init__()
        self.template_name = template_name
        self.roles = dict(roles)
        self.models = models
        self.description = description
        self._pending_role = None
        self._pending_model = None

    def _all_role_names(self) -> list:
        from halo_harness.roles import ROLE_NAMES
        names = list(ROLE_NAMES)
        for name in self.roles:
            if name not in names:
                names.append(name)
        return names

    def compose(self):
        with Vertical():
            title = f"Role template: {self.template_name}"
            if self.description:
                title += f" -- {self.description}"
            yield Static(title, classes="dialog-title")
            yield Static("Enter: pick a model for the highlighted role  |  ctrl+s: save  |  Esc: cancel",
                          classes="dialog-subtitle")
            yield OptionList(id="roles-list")
            yield Input(placeholder="", id="roles-effort-input")
            yield Static("", id="roles-hint")

    def on_mount(self) -> None:
        self._refresh_list()

    def _row_text(self, name: str) -> str:
        from halo_harness.roles import role_value_parts
        model, effort = role_value_parts(self.roles.get(name))
        if not model:
            return f"{name}: (unset -- session model)"
        return f"{name}: {model}" + (f" ({effort})" if effort else "")

    def _refresh_list(self) -> None:
        option_list = self.query_one("#roles-list", OptionList)
        highlighted = option_list.highlighted
        option_list.clear_options()
        for name in self._all_role_names():
            option_list.add_option(Option(self._row_text(name), id=name))
        if highlighted is not None and highlighted < len(option_list.options):
            option_list.highlighted = highlighted

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        role_name = str(event.option_id)
        from halo_harness.roles import role_value_parts
        from halo_harness.tui.dialogs.model_picker import ModelPicker
        current_model, _effort = role_value_parts(self.roles.get(role_name))
        self.app.push_screen(ModelPicker(self.models, current=current_model or ""),
                              lambda ref: self._model_picked(role_name, ref))

    def _model_picked(self, role_name: str, ref) -> None:
        if not ref:
            return
        from halo_harness.roles import role_value_parts
        self._pending_role, self._pending_model = role_name, ref
        _model, effort = role_value_parts(self.roles.get(role_name))
        effort_input = self.query_one("#roles-effort-input", Input)
        effort_input.placeholder = f"Effort for {role_name} (Enter for none), model={ref}"
        effort_input.value = effort or ""
        effort_input.styles.display = "block"
        effort_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._pending_role is None:
            return
        effort = event.value.strip()
        from halo_harness.providers.profiles import EFFORT_LEVELS
        hint = self.query_one("#roles-hint", Static)
        if effort and effort not in EFFORT_LEVELS:
            hint.update(f"Not a recognized effort level: {effort!r} (expected one of {', '.join(EFFORT_LEVELS)}, "
                        f"or blank for the route's own default)")
            return
        self.roles[self._pending_role] = ({"model": self._pending_model, "effort": effort} if effort
                                           else self._pending_model)
        self._pending_role = self._pending_model = None
        event.input.value = ""
        event.input.styles.display = "none"
        hint.update("")
        self._refresh_list()
        self.query_one("#roles-list", OptionList).focus()

    def action_save(self) -> None:
        from halo_harness.roles import save_role_template
        ok, problems = save_role_template(self.template_name, {"description": self.description, "roles": self.roles})
        if not ok:
            self.query_one("#roles-hint", Static).update("Could not save: " + "; ".join(problems))
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
