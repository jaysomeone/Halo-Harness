"""rolo_claude.tui.widgets.input -- `PromptInput(TextArea)` (auto-growing
1-8 lines, paste placeholders, `\\`+Enter/Ctrl+J/Alt+Enter newline, history
Up/Down) and `CompletionPopup` (the `/`+`@` overlay list). Both are dumb
widgets: PromptInput posts `Submitted`/`CompletionQuery` messages and
exposes `.pasted`/history state; `app.py` owns the registry/filesystem
lookups and feeds results back via `show_completions`.
"""

from __future__ import annotations


from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import OptionList, TextArea

from rolo_claude.tui.keys import PASTE_PLACEHOLDER_MIN_LINES, PROMPT_MAX_LINES, PROMPT_MIN_LINES


class CompletionPopup(OptionList):
    """Overlay list for `/` and `@` completion -- hidden (`display=False`)
    until `app.py` has candidates to show."""

    def __init__(self) -> None:
        super().__init__(id="completion-popup")
        self.display = False
        self.can_focus = False  # PromptInput keeps focus; app.py drives this list by index

    def show(self, items: "list[str]") -> None:
        self.clear_options()
        for item in items:
            self.add_option(item)
        self.display = bool(items)
        if items:
            self.highlighted = 0

    def hide(self) -> None:
        self.display = False
        self.clear_options()


class PromptInput(TextArea):
    """`tab_behavior="focus"` is overridden entirely -- Tab always means
    "accept completion" here, never focus-navigation (this app has nowhere
    else useful to Tab to while composing a prompt)."""

    class Submitted(Message):
        def __init__(self, text: str, pasted: dict) -> None:
            super().__init__()
            self.text = text
            self.pasted = dict(pasted)

    class CompletionQuery(Message):
        def __init__(self, kind: str, token: str) -> None:
            super().__init__()
            self.kind = kind
            self.token = token

    class CompletionDismissed(Message):
        pass

    class HistoryNav(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    BINDINGS = [
        Binding("tab", "accept_completion", "Complete", show=False),
        Binding("ctrl+j,alt+enter", "insert_newline", "Newline", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus",
                          id="prompt-input", **kwargs)
        self.pasted: dict = {}
        self._paste_counter = 0

    # ---- submit / newline --------------------------------------------

    async def _on_key(self, event: events.Key) -> None:
        if self.read_only:
            await super()._on_key(event)
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            row, col = self.cursor_location
            line = self.document.get_line(row)
            if col > 0 and line[col - 1] == "\\":
                self.delete((row, col - 1), (row, col))
                self.insert("\n")
            else:
                text = self.text
                self.post_message(self.Submitted(text, self.pasted))
            self._auto_grow()
            return
        await super()._on_key(event)
        self._auto_grow()

    def action_insert_newline(self) -> None:
        self.insert("\n")
        self._auto_grow()

    def clear_submitted(self) -> None:
        self.text = ""
        self.pasted = {}
        self._paste_counter = 0
        self._auto_grow()

    # ---- auto-grow 1-8 lines -------------------------------------------

    def _auto_grow(self) -> None:
        # review/U5 must-do: count WRAPPED display rows, not logical
        # (newline-delimited) lines -- a single 300-character line at 80
        # columns is ~4 wrapped rows and used to report "1 of 5" (barely
        # taller than empty) because `document.line_count` only counts
        # `\n`s. `TextArea.wrapped_document.height` is Textual's own
        # wrap-aware row count, kept live as the document/width change;
        # `wrap_width` is 0 (nothing computed yet) for exactly one frame
        # around initial mount, before any resize has happened, so this
        # falls back to the logical count then rather than raising.
        try:
            row_count = self.wrapped_document.height
        except Exception:
            row_count = self.document.line_count
        lines = max(PROMPT_MIN_LINES, min(PROMPT_MAX_LINES, row_count))
        self.styles.height = lines

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._auto_grow()
        self._maybe_query_completion()

    # ---- paste placeholder ------------------------------------------------

    async def _on_paste(self, event: events.Paste) -> None:
        if self.read_only:
            return
        # Textual dispatches a private `_on_<name>` handler at EVERY class
        # in the MRO that defines one, INDEPENDENTLY (message_pump.py's
        # `_get_dispatch_methods` walks `self.__class__.__mro__` and calls
        # each level's own definition -- it does NOT rely on `super()`
        # chaining). Calling `super()._on_paste(...)` from here would
        # therefore run `TextArea._on_paste` TWICE: once explicitly, right
        # now, and once more automatically and independently right after
        # this method returns (observed: the ORIGINAL, un-substituted text
        # got inserted a second time). The fix is to never call `super()`
        # here at all -- mutate `event.text` in place (Paste.text is a
        # plain mutable attribute) so the framework's OWN later, automatic
        # call to `TextArea._on_paste` performs the (correct, one-time)
        # insertion itself, using our substituted text.
        event.stop()  # never let it also bubble to the Screen/App
        text = event.text
        line_count = text.count("\n") + 1
        if line_count >= PASTE_PLACEHOLDER_MIN_LINES:
            self._paste_counter += 1
            n = self._paste_counter
            self.pasted[n] = text
            event.text = f"[Pasted text #{n} +{line_count} lines]"
        # `_auto_grow`/`_maybe_query_completion` run from `on_text_area_
        # changed` once the (still-pending) real insertion actually happens.

    # ---- / and @ completion ------------------------------------------

    def _maybe_query_completion(self) -> None:
        from rolo_claude.tui.completion import current_token

        row, col = self.cursor_location
        line = self.document.get_line(row)
        kind, _start, token = current_token(line, col)
        if kind and row == 0:
            self.post_message(self.CompletionQuery(kind, token))
        else:
            self.post_message(self.CompletionDismissed())

    def action_accept_completion(self) -> None:
        self.post_message(self.CompletionQuery("accept", ""))

    def replace_current_token(self, kind: str, replacement: str) -> None:
        """Replace the `/`- or `@`-prefixed token under the cursor (on the
        first line only, matching `_maybe_query_completion`) with
        `replacement` -- called by app.py after the user accepts a
        completion candidate."""
        from rolo_claude.tui.completion import current_token

        row, col = self.cursor_location
        line = self.document.get_line(row)
        _kind, start, _token = current_token(line, col)
        prefix_char = "/" if kind == "slash" else "@"
        self.replace(f"{prefix_char}{replacement}", (row, start), (row, col))
        self._auto_grow()

    # ---- history Up/Down (only at the first/last line -- see app.py) -----

    def action_cursor_up(self, select: bool = False) -> None:
        row, _col = self.cursor_location
        if row == 0 and not select:
            self.post_message(self.HistoryNav(-1))
            return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        row, _col = self.cursor_location
        if row == self.document.line_count - 1 and not select:
            self.post_message(self.HistoryNav(1))
            return
        super().action_cursor_down(select)

    # ---- PgUp/PgDn scroll the transcript, never the (usually one-line)
    # prompt (D-TUI: "PgUp/PgDn ... scroll") -------------------------------

    def action_cursor_page_up(self) -> None:
        transcript = getattr(self.app, "transcript", None)
        if transcript is not None:
            transcript.scroll_page_up()
        else:
            super().action_cursor_page_up()

    def action_cursor_page_down(self) -> None:
        transcript = getattr(self.app, "transcript", None)
        if transcript is not None:
            transcript.scroll_page_down()
        else:
            super().action_cursor_page_down()
