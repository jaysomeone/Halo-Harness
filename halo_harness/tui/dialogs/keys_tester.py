"""halo_harness.tui.dialogs.keys_tester -- `/keys` (Halo 2.0.2 round C,
the macOS/VS Code terminal brief): shows the exact key NAME halo's own
Textual app receives for each press, so a user whose terminal silently
swallows a shortcut before halo ever sees it (VS Code's integrated
terminal on macOS being the owner's own reported case) can tell whether
halo received it at all, with no debugger. Esc leaves.

Deliberately has NO `on_key`/`_on_key` override of its own: `tui/app.py`'s
own `BridgeApp._on_key` already runs on EVERY key, before any binding
resolution, and is documented at length there as the one safe place to
observe a raw key without taking on an opinion about its own chord-prefix/
self-heal logic. This dialog is simply handed each key through that
existing path (`halo_keys_tester_receive` below, duck-typed -- `_on_key`
checks for the method with a bare `getattr`, so neither module imports
the other) -- a key bound to a `priority=True` app-level action (Ctrl+E,
Ctrl+X, Ctrl+End) still shows up here too, even though its own action
ALSO still fires: both are real, useful signal (the key reached halo, AND
which binding it triggered).
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class KeysTesterDialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]
    DEFAULT_CSS = """
    KeysTesterDialog { align: center middle; }
    KeysTesterDialog > Vertical { width: 60%; height: auto; border: round $primary;
        background: $surface; padding: 1 2; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    def compose(self):
        with Vertical():
            yield Static("Key tester", classes="dialog-title")
            yield Static("Press any key -- the name halo receives for it shows up below. "
                         "Esc leaves.", markup=False)
            yield Static("(no key pressed yet)", id="keys-tester-last", markup=False)

    def halo_keys_tester_receive(self, key: str) -> None:
        """Called by `BridgeApp._on_key` for every key while this dialog
        is the active screen (see this module's own docstring on why
        there's no `on_key` override here instead). Esc is deliberately
        NOT special-cased here -- the ordinary `BINDINGS` entry above
        already closes the dialog on it, the same way every other modal
        in this app handles Esc; special-casing it here too would just
        risk a harmless but pointless double-dismiss."""
        self._count += 1
        try:
            self.query_one("#keys-tester-last", Static).update(
                f"Last key: {key!r}  ({self._count} this session)")
        except Exception:
            pass  # the widget isn't mounted yet/any more -- nothing to update

    def action_cancel(self) -> None:
        self.dismiss(None)
