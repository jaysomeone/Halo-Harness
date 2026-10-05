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
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option


class RolesEditor(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=True),
    ]
    DEFAULT_CSS = """
    RolesEditor { align: center middle; }
    RolesEditor > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    RolesEditor #roles-list { height: 1fr; }
    RolesEditor #roles-template-picker { height: 5; margin-bottom: 1; }
    RolesEditor #roles-effort-input { display: none; }
    RolesEditor #roles-save-as-input { display: none; }
    RolesEditor #roles-save-as-row { height: 3; margin-top: 1; }
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
        from textual.containers import Horizontal
        with Vertical():
            title = f"Role template: {self.template_name}"
            if self.description:
                title += f" -- {self.description}"
            yield Static(title, classes="dialog-title")
            yield Static("Enter: pick a model for the highlighted role  |  ctrl+s: save  |  Esc: cancel",
                          classes="dialog-subtitle")
            # Halo 2.0.2 round 7 (init wizard brief, item 2): "the roles
            # editor gains the template picker at its top" -- choosing one
            # REPLACES the form's current roles with that template's own
            # (`_apply_template`), it does not merge; ctrl+s still saves
            # to THIS SAME `template_name`, never the one picked from here.
            yield Static("Start from a template (replaces the roles below):", classes="dialog-subtitle")
            yield OptionList(id="roles-template-picker")
            yield OptionList(id="roles-list")
            yield Input(placeholder="", id="roles-effort-input")
            with Horizontal(id="roles-save-as-row"):
                yield Button("Save as template...", id="roles-save-as-button")
            yield Input(placeholder="New template name, Enter to save a copy", id="roles-save-as-input")
            yield Static("", id="roles-hint")

    def on_mount(self) -> None:
        self._refresh_list()
        self._refresh_template_picker()
        # The template picker sits ABOVE #roles-list in DOM order, so
        # Textual's own "focus the first focusable widget" default would
        # otherwise land there instead -- #roles-list is the form's own
        # main focus (Enter there opens the model picker for the
        # highlighted ROLE), unchanged from before this picker existed.
        self.query_one("#roles-list", OptionList).focus()

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

    def _refresh_template_picker(self) -> None:
        # Deliberately NEVER calls `ensure_builtin_role_presets()` here --
        # this round-1 form (any `/roles edit <name>`) must not manufacture
        # the three round-7 preset files as a side effect of simply being
        # OPENED; that belongs to the wizard's own Roles step, the actual
        # "setting up with presets" entry point. This picker shows
        # whatever already exists, same as it would before presets existed.
        from halo_harness.roles import list_role_templates
        picker = self.query_one("#roles-template-picker", OptionList)
        picker.clear_options()
        for name in list_role_templates():
            picker.add_option(Option(name, id=name))
        if not picker.option_count:
            picker.add_option(Option("(no saved templates yet)", disabled=True))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "roles-template-picker":
            if event.option_id:
                self._apply_template(str(event.option_id))
            return
        role_name = str(event.option_id)
        from halo_harness.roles import role_value_parts
        from halo_harness.tui.dialogs.model_picker import ModelPicker
        current_model, _effort = role_value_parts(self.roles.get(role_name))
        self.app.push_screen(ModelPicker(self.models, current=current_model or ""),
                              lambda ref: self._model_picked(role_name, ref))

    def _apply_template(self, name: str) -> None:
        """Round 7, item 2: "choose a template, the form prefills" --
        REPLACES `self.roles` with the template's own (never merges), so
        the form genuinely reflects just that template once picked; the
        description only fills in when this editor started with none, so
        a template already named here is never silently overwritten."""
        from halo_harness.roles import load_role_template
        template = load_role_template(name)
        if template is None:
            self.query_one("#roles-hint", Static).update(f"No such template: {name!r}")
            return
        self.roles = dict(template["roles"])
        if not self.description:
            self.description = template.get("description") or ""
        self._refresh_list()
        self.query_one("#roles-hint", Static).update(f"Loaded template {name!r} into the form -- ctrl+s "
                                                       f"saves it as {self.template_name!r}.")

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "roles-save-as-button":
            save_as_input = self.query_one("#roles-save-as-input", Input)
            save_as_input.styles.display = "block"
            save_as_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "roles-save-as-input":
            self._save_as(event.value.strip())
            return
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

    def _save_as(self, name: str) -> None:
        """Round 7, item 2: "Save as template..." -- a COPY of the
        form's current roles under a NEW name, distinct from ctrl+s
        (which always saves to `self.template_name`); the input hides
        again either way, and a successful save refreshes the picker so
        the new name shows up immediately."""
        input_widget = self.query_one("#roles-save-as-input", Input)
        hint = self.query_one("#roles-hint", Static)
        if not name:
            hint.update("Save as template: enter a name first.")
            return
        from halo_harness.roles import save_role_template
        ok, problems = save_role_template(name, {"description": self.description, "roles": self.roles})
        input_widget.value = ""
        input_widget.styles.display = "none"
        if not ok:
            hint.update("Could not save: " + "; ".join(problems))
            return
        hint.update(f"Saved a copy as template {name!r}.")
        self._refresh_template_picker()
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
