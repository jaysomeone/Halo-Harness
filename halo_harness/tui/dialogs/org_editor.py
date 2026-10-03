"""halo_harness.tui.dialogs.org_editor -- `/org edit <name>` (Halo 2.0.2
round 2, brief B): the roles editor's own form family, extended for a
TREE of positions instead of a flat role table. A tree view on the left
(`#org-tree-list`, one row per position indented by its depth from the
root -- an orphan not yet linked under the root is listed separately,
never silently dropped) and the SELECTED position's own fields on the
right: title, role-or-model (free text, or `ctrl+p` opens the SAME
`ModelPicker` `/model`/`RolesEditor` use), effort, instructions, and
`reports` (a comma-separated list of other titles). `ctrl+n` adds a new,
unlinked position; `ctrl+d` deletes the selected one (pruning it from
every other position's own `reports` so nothing dangles); `ctrl+s` saves
(refusing, with the reasons shown, if `orgs.validate_org` rejects the
result -- e.g. the just-added position is still unlinked) and dismisses
with `True`; `Escape` cancels with no write, dismissing with `False`.
`tui/slash.py::_handle_org` builds this with the org's already-loaded (or
freshly-started) dict and `controller.list_models()`'s own catalog --
never loads either itself, so this stays unit-testable with a plain fake
`models` list.
"""

from __future__ import annotations

import copy

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option


def _ordered_titles(org: dict) -> "list[tuple[str, int]]":
    """One `(title, depth)` per position, root-first in tree order --
    display/ordering only (never raises, safe against a transiently
    invalid mid-edit org with no single root or a dangling `reports`);
    the real structural checks live in `orgs.validate_org`. A position
    already shown is never expanded a second time even if another
    position's own `reports` also names it (release-flow's own `Fixer`
    -> `Tester` shape), which also makes this cycle-safe for free."""
    from halo_harness.orgs import root_position
    positions = org.get("positions") or []
    by_title = {p["title"]: p for p in positions if isinstance(p, dict) and p.get("title")}
    root = root_position(org)
    out: list = []
    visited: set = set()

    def _walk(title: str, depth: int) -> None:
        if title in visited or title not in by_title:
            return
        visited.add(title)
        out.append((title, depth))
        for child in (by_title[title].get("reports") or []):
            _walk(child, depth + 1)

    if root is not None:
        _walk(root["title"], 0)
    for title in by_title:
        if title not in visited:
            out.append((title, 0))
    return out


class OrgEditor(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("ctrl+n", "add_position", "Add position", show=True),
        Binding("ctrl+d", "delete_position", "Delete position", show=True),
        Binding("ctrl+p", "pick_model", "Pick model", show=True),
    ]
    DEFAULT_CSS = """
    OrgEditor { align: center middle; }
    OrgEditor > Horizontal { width: 96%; height: 86%; border: round $primary; background: $surface; }
    OrgEditor #org-tree-pane { width: 36%; padding: 1 2; border-right: solid $primary-darken-1; }
    OrgEditor #org-fields-pane { width: 64%; padding: 1 2; }
    OrgEditor #org-tree-list { height: 1fr; }
    OrgEditor #org-field-instructions { height: 8; }
    """

    def __init__(self, name: str, org: dict, models: list) -> None:
        super().__init__()
        # NOT `self.name` -- that shadows Textual's own read-only
        # `DOMNode.name` property (`RolesEditor` avoids the same trap
        # with its own `template_name`).
        self.org_name = name
        self.org = copy.deepcopy(org)
        self.org.setdefault("positions", [])
        self.models = models
        self._current_title: "str | None" = None

    def _position(self, title: str) -> "dict | None":
        for p in self.org["positions"]:
            if isinstance(p, dict) and p.get("title") == title:
                return p
        return None

    def compose(self):
        with Horizontal():
            with Vertical(id="org-tree-pane"):
                yield Static(f"Organization: {self.org_name}", classes="dialog-title")
                yield Static("Enter: edit position  |  ^N add  ^D delete  |  ^S save  |  Esc cancel",
                              classes="dialog-subtitle")
                yield OptionList(id="org-tree-list")
            with Vertical(id="org-fields-pane"):
                yield Static("", id="org-field-heading")
                yield Static("Title")
                yield Input(id="org-field-title")
                yield Static("Role or model (^P to pick a model)")
                yield Input(id="org-field-role")
                yield Static("Effort")
                yield Input(id="org-field-effort")
                yield Static("Instructions")
                yield TextArea(id="org-field-instructions")
                yield Static("Reports to (comma-separated titles it may delegate to)")
                yield Input(id="org-field-reports")
                yield Static("", id="org-hint")

    def on_mount(self) -> None:
        self._refresh_tree()
        ordered = _ordered_titles(self.org)
        if ordered:
            self._load_position(ordered[0][0])
        self.query_one("#org-tree-list", OptionList).focus()

    def _refresh_tree(self) -> None:
        option_list = self.query_one("#org-tree-list", OptionList)
        highlighted = option_list.highlighted
        option_list.clear_options()
        for title, depth in _ordered_titles(self.org):
            option_list.add_option(Option(("  " * depth) + title, id=title))
        if highlighted is not None and highlighted < len(option_list.options):
            option_list.highlighted = highlighted

    def _commit_current_fields(self) -> None:
        """Writes the right-hand fields back into the CURRENTLY loaded
        position before switching rows or saving -- a rename updates
        every OTHER position's own `reports` entry too, so renaming a
        position never silently dangles the links pointing at it."""
        if self._current_title is None:
            return
        position = self._position(self._current_title)
        if position is None:
            return
        new_title = self.query_one("#org-field-title", Input).value.strip() or self._current_title
        role_or_model = self.query_one("#org-field-role", Input).value.strip()
        effort = self.query_one("#org-field-effort", Input).value.strip()
        instructions = self.query_one("#org-field-instructions", TextArea).text
        reports = [t.strip() for t in self.query_one("#org-field-reports", Input).value.split(",") if t.strip()]
        position.pop("role", None)
        position.pop("model", None)
        if role_or_model:
            from halo_harness.roles import is_role_name_syntax
            position["role" if is_role_name_syntax(role_or_model) else "model"] = role_or_model
        position["effort"] = effort or None
        position["instructions"] = instructions
        position["reports"] = reports
        if new_title != self._current_title:
            position["title"] = new_title
            for other in self.org["positions"]:
                if other is position:
                    continue
                other["reports"] = [new_title if t == self._current_title else t for t in (other.get("reports") or [])]
            self._current_title = new_title

    def _load_position(self, title: str) -> None:
        position = self._position(title)
        if position is None:
            return
        self._current_title = title
        self.query_one("#org-field-heading", Static).update(f"Editing: {title}")
        self.query_one("#org-field-title", Input).value = title
        self.query_one("#org-field-role", Input).value = position.get("role") or position.get("model") or ""
        self.query_one("#org-field-effort", Input).value = position.get("effort") or ""
        self.query_one("#org-field-instructions", TextArea).text = position.get("instructions") or ""
        self.query_one("#org-field-reports", Input).value = ", ".join(position.get("reports") or [])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._commit_current_fields()
        self._refresh_tree()
        self._load_position(str(event.option_id))

    def action_pick_model(self) -> None:
        from halo_harness.tui.dialogs.model_picker import ModelPicker
        current = self.query_one("#org-field-role", Input).value
        self.app.push_screen(ModelPicker(self.models, current=current), self._model_picked)

    def _model_picked(self, ref) -> None:
        if not ref:
            return
        self.query_one("#org-field-role", Input).value = ref

    def action_add_position(self) -> None:
        self._commit_current_fields()
        existing = {p["title"] for p in self.org["positions"] if isinstance(p, dict)}
        n = 1
        while f"New Position {n}" in existing:
            n += 1
        title = f"New Position {n}"
        self.org["positions"].append({"title": title, "reports": [], "instructions": ""})
        self._refresh_tree()
        self._load_position(title)

    def action_delete_position(self) -> None:
        if self._current_title is None or len(self.org["positions"]) <= 1:
            self.query_one("#org-hint", Static).update("Can't delete the last remaining position.")
            return
        gone = self._current_title
        self.org["positions"] = [p for p in self.org["positions"] if p.get("title") != gone]
        for p in self.org["positions"]:
            p["reports"] = [t for t in (p.get("reports") or []) if t != gone]
        self._current_title = None
        self._refresh_tree()
        ordered = _ordered_titles(self.org)
        if ordered:
            self._load_position(ordered[0][0])

    def action_save(self) -> None:
        from halo_harness.orgs import save_org, validate_org
        self._commit_current_fields()
        problems = validate_org(self.org)
        if problems:
            self.query_one("#org-hint", Static).update("Could not save: " + "; ".join(problems))
            return
        ok, problems = save_org(self.org_name, self.org)
        if not ok:
            self.query_one("#org-hint", Static).update("Could not save: " + "; ".join(problems))
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
