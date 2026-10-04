"""halo_harness.tui.dialogs.model_picker -- `/model` with no argument (D-TUI:
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

from halo_harness.model_display import ROW_HEADER, format_model_row
from halo_harness.tui.dialogs.listnav import NavInput


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
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        # Halo 2.0.3 round 3 (brief item 5): set a role for the
        # HIGHLIGHTED model without editing JSON/leaving this dialog.
        Binding("u", "set_role", "Set role", show=True),
    ]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    ModelPicker > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    ModelPicker Input { margin-bottom: 1; }
    ModelPicker OptionList { height: 1fr; }
    """

    def __init__(self, models: "list", *, current: str = "", last_used: str = "") -> None:
        super().__init__()
        # `Controller.list_models()` returns dicts; `FakeController`'s own
        # (tests/test_fake_controller.py-pinned) shape is a bare list of ref
        # strings -- normalize both to the dict shape this dialog renders.
        # H15 item 21.2: a `{"hint": "..."}` entry (no "ref" at all -- a
        # detected-but-disabled provider's dim notice) is split out here so
        # it never reaches the filterable/selectable `self.models` list.
        self.hints = [m.get("hint", "") for m in models if isinstance(m, dict) and "hint" in m]
        self.models = [m if isinstance(m, dict) else {"ref": m, "provider": "?"}
                       for m in models if not (isinstance(m, dict) and "hint" in m)]
        self.current = current
        # 2.0.1 W3a ("launch with the last session's model and effort"):
        # the ref `launch_state.resolve_last_model` would pick on the NEXT
        # launch -- "" (the default) marks no row at all, same as today.
        self.last_used = last_used
        self._filtered = self.models

    def compose(self):
        with Vertical():
            yield Static(f"Select a model (current: {self.current or '?'})", classes="dialog-title")
            yield Static("Enter: select  |  u: set a role for the highlighted model  |  Esc: cancel",
                          classes="dialog-subtitle")
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
                    row_text = Text(format_model_row(m), no_wrap=True, overflow="ellipsis")
                    if self.last_used and m["ref"] == self.last_used:
                        row_text.append("  (last used)", style="dim italic")
                    option_list.add_option(Option(row_text, id=m["ref"]))
            # 1.0.1 hotfix addendum 7: highlights the first SELECTABLE row
            # up front (`action_first` skips a disabled group-header, unlike
            # a bare `.highlighted = 0`) -- otherwise `highlighted` starts
            # at None and "Down twice" only reaches the SECOND row, not the
            # third, contradicting the documented "Down, Down, Enter -> the
            # third entry" behavior.
            option_list.action_first()
            hint.update(self._hint_text())
        else:
            refs = [m["ref"] for m in self.models]
            near = difflib.get_close_matches(query, refs, n=5, cutoff=0.4)
            no_match = f"No exact match. Did you mean: {', '.join(near)}" if near else "No matching models."
            extra = self._hint_text()
            hint.update(f"{no_match}\n{extra}" if extra else no_match)

    def _hint_text(self) -> str:
        """H15 item 21.2: one dim line per detected-but-disabled provider,
        always shown (filter or not) -- never counted as a selectable row."""
        return "\n".join(f"  {h}" for h in self.hints) if self.hints else ""

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

    def action_set_role(self) -> None:
        """Halo 2.0.3 round 3 (brief item 5): `u` on the highlighted row
        opens `RoleAssignPicker` for that model's own ref -- never
        dismisses THIS dialog (the user may want to assign several roles
        in one browse); `RoleAssignPicker` itself writes `roles.<name>`
        via `roles.assign_role` (the SAME config-table mechanism `/roles`
        already reads, no second one) and reports the result in
        `#model-hint`."""
        option_list = self.query_one("#model-list", OptionList)
        highlighted = option_list.highlighted
        if highlighted is None or highlighted >= len(self._filtered):
            return
        ref = self._filtered[highlighted]["ref"]
        self.app.push_screen(RoleAssignPicker(ref, main_ref=self.current),
                              lambda role_name: self._role_assigned(ref, role_name))

    def _role_assigned(self, ref: str, role_name) -> None:
        hint = self.query_one("#model-hint", Static)
        if not role_name:
            hint.update("")
            return
        hint.update(f"Set role {role_name!r} to {ref!r}.")
        if role_name == "orchestrator" and ref.startswith("ol:"):
            # brief: "choosing a non-session-capable local model as main
            # prints the plain consequence sentence and proceeds" -- the
            # assignment above ALREADY happened (proceeds immediately);
            # the catalog read behind the consequence sentence is a real
            # network call, so it runs off the UI thread and the hint is
            # amended a moment later rather than ever blocking this dialog.
            self.app.run_worker(lambda: self._consequence_worker(ref), thread=True,
                                 name="role-consequence", group="role-consequence")

    def _consequence_worker(self, ref: str) -> None:
        from halo_harness.roles import main_role_consequence_note
        note = main_role_consequence_note(ref, catalog_capabilities=_catalog_capabilities_for(ref))
        if note:
            self.app.call_from_thread(self._append_hint, note)

    def _append_hint(self, note: str) -> None:
        hint = self.query_one("#model-hint", Static)
        current = hint.renderable
        hint.update(f"{current}\n{note}" if current else note)


def _catalog_capabilities_for(model_ref: str):
    """Best-effort, OFF the UI thread (see `ModelPicker._consequence_
    worker`, the only caller): a dim/offline host, or a model not yet in
    the catalog, must never turn `u` into a hang or a traceback -- see
    `roles.main_role_consequence_note`'s own "benefit of the doubt" note
    for what `None` here means to that function."""
    if not model_ref.startswith("ol:"):
        return None
    try:
        from halo_harness.model import parse_model_ref
        from halo_harness.providers.ollama import get_catalog, resolve_ollama_host
        ref = parse_model_ref(model_ref)
        host = resolve_ollama_host(ref.host)
        if host is None:
            return None
        for row in get_catalog(host).get("models") or []:
            if isinstance(row, dict) and (row.get("model") == ref.model or row.get("name") == ref.model):
                return row.get("capabilities")
    except Exception:
        pass
    return None


class RoleAssignPicker(ModalScreen):
    """Halo 2.0.3 round 3 (brief item 5): `u` on `ModelPicker`'s
    highlighted row -- a short list of `roles.known_role_names()`,
    pre-selecting `roles.default_role_for_ref(model_ref)` (`small` for
    an `ol:` ref, never main by default). Enter writes `roles.<chosen>`
    to `model_ref` (`roles.assign_role` -- cheap, a local config write,
    safe to do right here on the UI thread) and dismisses with the
    chosen role name; Escape dismisses with `None` (no write). The
    brief's own "choosing a non-session-capable local model as main
    prints the plain consequence sentence and proceeds" is `ModelPicker`'s
    job, not this dialog's -- that sentence needs a real catalog read,
    which never belongs on this (or any) Textual event handler; see
    `ModelPicker._role_assigned`/`_consequence_worker`."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    RoleAssignPicker { align: center middle; }
    RoleAssignPicker > Vertical { width: 50%; height: 60%; border: round $primary; background: $surface; padding: 1 2; }
    RoleAssignPicker OptionList { height: 1fr; }
    """

    def __init__(self, model_ref: str, *, main_ref: str = "") -> None:
        super().__init__()
        self.model_ref = model_ref
        # Round 5b part 2 (brief item 3): the session's CURRENT model --
        # needed only for the "(same as main: fits beside it: no)" caption
        # below; `""` (a caller that omits it, or no session is running
        # yet) just means the caption never has anything to show.
        self.main_ref = main_ref

    def compose(self):
        with Vertical():
            yield Static(f"Set a role for {self.model_ref}", classes="dialog-title")
            yield Static("Enter: set  |  Esc: cancel", classes="dialog-subtitle")
            yield OptionList(id="role-assign-list")

    def on_mount(self) -> None:
        from halo_harness.roles import default_role_for_ref, known_role_names
        option_list = self.query_one("#role-assign-list", OptionList)
        default = default_role_for_ref(self.model_ref)
        names = known_role_names()
        for name in names:
            option_list.add_option(Option(f"{name}{'  (default)' if name == default else ''}", id=name))
        if default in names:
            option_list.highlighted = names.index(default)
        option_list.focus()
        from halo_harness.roles import VRAM_AWARE_ROLE_NAMES
        if self.main_ref and self.main_ref != self.model_ref and default in VRAM_AWARE_ROLE_NAMES:
            # Round 5b part 2: a real GPU-memory/`/api/ps` read, off the UI
            # thread (the SAME `run_worker(thread=True)` pattern every
            # other live probe in this codebase uses) and short -- the
            # dialog is already fully usable before this resolves; the
            # caption just appears a moment later, or never if the probe
            # never finishes before the screen closes (`query_one`'s own
            # `except Exception: pass` below degrades silently).
            self.run_worker(self._vram_reason_worker, thread=True, name="role-assign-vram-reason")

    def _vram_reason_worker(self) -> None:
        from halo_harness.model import parse_model_ref
        from halo_harness.roles import default_role_for_ref, vram_aware_override
        try:
            default = default_role_for_ref(self.model_ref)
            main = parse_model_ref(self.main_ref)
            _value, reason = vram_aware_override(default, self.model_ref, main_ref=main)
        except Exception:
            reason = None
        if reason:
            self.app.call_from_thread(self._apply_vram_reason, default, reason)

    def _apply_vram_reason(self, role_name: str, reason: str) -> None:
        try:
            option_list = self.query_one("#role-assign-list", OptionList)
            option_list.replace_option_prompt(role_name, f"{role_name}  (default)  {reason}")
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """`roles.assign_role` is a local config write (no network) --
        safe to call right here; dismisses with the plain role-name
        string either way (a write failure is vanishingly rare -- a
        syntactically bad role name can't reach this list at all, since
        every option id comes from `known_role_names()` -- so this
        never needs its own error-reporting shape)."""
        role_name = str(event.option_id)
        from halo_harness.roles import assign_role
        assign_role(role_name, self.model_ref)
        self.dismiss(role_name)

    def action_cancel(self) -> None:
        self.dismiss(None)
