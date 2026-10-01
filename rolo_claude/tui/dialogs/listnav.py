"""rolo_claude.tui.dialogs.listnav -- 1.0.1 hotfix addendum 7: a shared
`Input` subclass for every dialog with an `Input` (filter box) + `OptionList`
(results) pair -- `ModelPicker`, `SessionPicker`, `CommandPalette`, and the
new `init`/`  /model` grouped picker (`rolo_claude.tui.dialogs.init_picker`).

Textual's own `Input` binds no key at all for Up/Down/PageUp/PageDown (they
silently do nothing while the Input has focus -- verified: this is exactly
why Up/Down in `/model` did nothing), and binds Home/End to text-cursor
movement, which a one-line filter box has little use for compared to "jump
to the first/last result" here. `NavInput` forwards all six to the sibling
`OptionList` instead, and Enter selects the HIGHLIGHTED row (the same
`OptionSelected` event each dialog's own `on_option_list_option_selected`
already handles) rather than submitting -- falling back to the Input's
ordinary `Submitted` event only when the list has nothing selectable (e.g.
filtered down to zero rows, or a disabled placeholder-only list), so each
dialog's own `on_input_submitted` fallback (first match / raw text) still
runs exactly as before in that case.
"""

from __future__ import annotations

from textual import events
from textual.widgets import Input, OptionList

_NAV_ACTIONS = {
    "up": "cursor_up", "down": "cursor_down",
    "pageup": "page_up", "pagedown": "page_down",
    "home": "first", "end": "last",
}


class NavInput(Input):
    """`option_list_id`: the CSS id of the sibling `OptionList` this Input's
    navigation keys should drive (looked up lazily via `self.screen`, so
    construction order between the two widgets never matters)."""

    def __init__(self, *args, option_list_id: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._option_list_id = option_list_id

    def _option_list(self) -> "OptionList | None":
        try:
            return self.screen.query_one(f"#{self._option_list_id}", OptionList)
        except Exception:
            return None

    async def _on_key(self, event: events.Key) -> None:
        option_list = self._option_list()
        if option_list is not None:
            action = _NAV_ACTIONS.get(event.key)
            if action is not None:
                event.stop()
                event.prevent_default()
                getattr(option_list, f"action_{action}")()
                return
            if event.key == "enter" and option_list.option_count and option_list.highlighted is not None:
                highlighted = option_list.get_option_at_index(option_list.highlighted)
                if not highlighted.disabled:
                    event.stop()
                    event.prevent_default()
                    option_list.action_select()
                    return
        await super()._on_key(event)
