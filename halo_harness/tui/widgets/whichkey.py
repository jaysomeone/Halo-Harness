"""halo_harness.tui.widgets.whichkey -- the which-key overlay (U5 scope A):
shown the instant a chord prefix (e.g. Ctrl+X) is pressed, listing every
live continuation keystroke and the action it runs, until the next
keystroke -- or `tui.keys.CHORD_TIMEOUT_S` -- resolves or cancels it. A
plain `Static`, positioned like `CompletionPopup`: hidden (`display=False`)
until `app.py` has a pending chord to show.
"""

from __future__ import annotations

from textual.widgets import Static


class WhichKeyOverlay(Static):
    def __init__(self) -> None:
        super().__init__("", markup=False, id="which-key-overlay", classes="which-key-overlay")
        self.display = False

    def show_for(self, prefix: str, continuations: "dict[str, object]") -> None:
        from halo_harness.tui.keys import format_which_key

        body = format_which_key(continuations)
        text = f"{prefix} …" if not body else f"{prefix} …\n{body}"
        self.update(text)
        self.display = True

    def hide(self) -> None:
        self.display = False
        self.update("")
