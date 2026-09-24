"""rolo_claude.tui.app -- `BridgeApp`, the full-screen Textual UI (D-TUI
scope B). Composition: `Transcript` fills, `CompletionPopup` overlays, a
prompt row (glyph + `PromptInput`) and `StatusBar` dock the bottom. A 30 Hz
`_drain` timer pulls events from `controller.events` (a real `Controller`'s
queue, filled by its worker thread) AND `self._local_events` (fed
synchronously by `submit()` for a scripted `FakeController`, which returns
its events directly rather than through a queue) with an 8 ms budget,
coalesces them (`tui/events.py::drain_queue`), and applies each one
(`tui/dispatch.py::apply_event`) -- the ONLY code path that ever touches a
widget, so nothing here is ever called from another thread.
"""

from __future__ import annotations

import queue
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static

from rolo_claude.tui import theme as tui_theme
from rolo_claude.tui.dispatch import apply_event
from rolo_claude.tui.events import drain_queue
from rolo_claude.tui.keys import DOUBLE_CTRL_C_WINDOW_S, DRAIN_HZ, next_mode
from rolo_claude.tui.widgets.input import CompletionPopup, PromptInput
from rolo_claude.tui.widgets.statusbar import StatusBar
from rolo_claude.tui.widgets.transcript import Transcript

DEFAULT_PLACEHOLDER = 'Try "read README.md and summarise it"   (/ commands, @ files)'

_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #(\d+) \+\d+ lines\]")


def _expand_pasted(text: str, pasted: Optional[dict]) -> str:
    """review finding 4: `PromptInput` shows/logs `[Pasted text #n +N
    lines]` but the MODEL must see the real content -- expand every
    placeholder back to `pasted[n]` (the full text `_on_paste` stashed),
    leaving anything unmatched (a stale/unknown index) untouched rather
    than raising."""
    if not pasted or "[Pasted text #" not in text:
        return text

    def _sub(m: "re.Match") -> str:
        try:
            n = int(m.group(1))
        except ValueError:
            return m.group(0)
        return pasted.get(n, m.group(0))

    return _PASTE_PLACEHOLDER_RE.sub(_sub, text)


class BridgeApp(App):
    CSS_PATH = "styles.tcss"
    ENABLE_COMMAND_PALETTE = False
    TITLE = "rolo-claude"

    # `priority=True` on shift+tab/ctrl+d: both would otherwise be caught
    # first by a closer, non-priority binding from the DOM ancestor chain
    # before ever reaching the App -- `Screen` itself binds bare
    # `shift+tab` to `app.focus_previous` (Textual's default focus-cycle
    # key), and the focused `PromptInput` (a `TextArea`) inherits
    # `ctrl+d -> delete_right`. `action_quit_on_empty` restores the normal
    # forward-delete when the prompt isn't empty, so Ctrl+D only steals the
    # keypress when there's genuinely nothing to delete.
    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_quit", "Quit", priority=True, show=False),
        Binding("ctrl+d", "quit_on_empty", "Quit", priority=True, show=False),
        Binding("escape", "escape_pressed", "Interrupt", show=False),
        Binding("shift+tab", "cycle_mode", "Mode", priority=True, show=False),
        Binding("ctrl+l", "clear_view", "Clear", show=False),
        Binding("ctrl+o", "toggle_verbose", "Verbose", show=False),
        Binding("ctrl+r", "history_search", "History", show=False),
        Binding("f1", "show_help", "Help", show=False),
    ]

    def __init__(self, controller, *, registry=None, facade=None, tool_registry=None,
                 cwd: Optional[Path] = None, theme_name: Optional[str] = None,
                 tui_setting: Optional[str] = None, initial_prompt: Optional[str] = None) -> None:
        # `App.__init__` itself calls `get_css_variables()` (to build its
        # initial stylesheet) before returning -- `theme_name` must exist
        # on `self` BEFORE `super().__init__()` runs, not after.
        self.theme_name = theme_name or tui_theme.DEFAULT_THEME
        super().__init__()
        self.controller = controller
        self.registry = registry
        self.facade = facade
        self.tool_registry = tool_registry or getattr(facade, "tool_registry", None)
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.tui_setting = tui_setting
        self._initial_prompt = initial_prompt

        self._local_events: "queue.Queue" = queue.Queue()
        self.verbose = False
        self.pending_card = None
        self._borrowing_card = None
        self._ctrl_c_deadline: Optional[float] = None
        self._quitting = False
        self._history_cache: "list[str]" = []
        self._history_index = 0
        self._history_draft = ""
        self._completion_kind = ""
        self._completion_items: "list[str]" = []

    # ---- composition -------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript()
        yield CompletionPopup()
        with Horizontal(id="prompt-row"):
            yield Static("❯", id="prompt-glyph")
            yield PromptInput(placeholder=DEFAULT_PLACEHOLDER)
        yield StatusBar(cwd=str(self.cwd))

    def get_css_variables(self) -> dict:
        variables = dict(super().get_css_variables())
        variables.update(tui_theme.variables_for(self.theme_name))
        return variables

    async def on_mount(self) -> None:
        self.transcript = self.query_one(Transcript)
        self.completion_popup = self.query_one(CompletionPopup)
        self.prompt_input = self.query_one(PromptInput)
        self.status_bar = self.query_one(StatusBar)
        self.status_bar.set_cwd_branch(str(self.cwd), self._git_branch())
        starter = getattr(self.controller, "start", None)
        if callable(starter):
            starter()
        else:
            self.status_bar.apply_status({
                "model": getattr(self.controller, "model", None),
                "permission_mode": getattr(self.controller, "permission_mode", None),
            })
        self.set_focus(self.prompt_input)
        self.set_interval(1 / DRAIN_HZ, self._drain)
        self.set_interval(1.0, self._tick_spinner)
        self.set_interval(5.0, self._refresh_cwd_branch)
        if self._initial_prompt:
            await self._submit_prompt(self._initial_prompt, {})

    def _git_branch(self) -> str:
        try:
            result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(self.cwd),
                                     capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            # review finding 5: a hung/slow `git` (a huge repo, a network
            # filesystem, ...) raised `TimeoutExpired` straight through --
            # NOT an `OSError` subclass, so it used to kill the app.
            pass
        return ""

    def _refresh_cwd_branch(self) -> None:
        # review finding 5/"UX" must-do: `git` runs on a worker thread --
        # this used to block the UI thread (every 5s, plus once at
        # startup) for however long the subprocess took.
        self.run_worker(self._git_branch_worker, thread=True, exclusive=True, name="git-branch")

    def _git_branch_worker(self) -> None:
        branch = self._git_branch()
        self.call_from_thread(self.status_bar.set_cwd_branch, str(self.cwd), branch)

    def _tick_spinner(self) -> None:
        self.status_bar.tick_spinner()

    # ---- the drain loop (D-TUI: "30 Hz ... 8ms budget") -------------------

    async def _drain(self) -> None:
        start = time.monotonic()
        collected: list = []
        for source in (getattr(self.controller, "events", None), self._local_events):
            if source is None:
                continue
            while (time.monotonic() - start) < 0.008:
                try:
                    collected.append(source.get_nowait())
                except queue.Empty:
                    break
        real_events = [e for e in collected if e is not None]
        for item in drain_queue(real_events):
            await apply_event(self, item)
        if real_events:
            self.transcript.mark_seen()
            self.status_bar.set_new_count(self.transcript.new_since_scroll)

    def on_turn_done(self, reason: str) -> None:
        if reason == "interrupted":
            self.notify("Interrupted.", timeout=2)

    # ---- submit / slash dispatch ------------------------------------------

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        text = event.text
        if self._borrowing_card is not None:
            card = self._borrowing_card
            self._borrowing_card = None
            self.prompt_input.clear_submitted()
            self.prompt_input.placeholder = DEFAULT_PLACEHOLDER
            card.resolve_with_message(text)
            return
        if not text.strip():
            return
        self.prompt_input.clear_submitted()
        self.completion_popup.hide()
        if self.pending_card is not None:
            # scope 0(c)/review finding 7: a card pending does NOT answer
            # it (only the card's own keys/borrowed-input do that) -- text
            # typed here is a steer on the turn that's still running
            # underneath the card, same as any other mid-turn input.
            self.controller.submit(_expand_pasted(text, event.pasted), pasted=event.pasted or None)
            return
        await self._submit_prompt(text, event.pasted)

    async def _submit_prompt(self, text: str, pasted: dict) -> None:
        from rolo_claude import history as history_mod

        try:
            history_mod.append_history_entry(text, str(self.cwd), pasted_contents=pasted or None)
        except OSError:
            pass
        self._history_cache = []  # re-read next Up-arrow, this entry is now in it
        stripped = text.strip()
        if stripped.startswith("/") and "\n" not in stripped:
            from rolo_claude.tui.slash import handle_slash

            name, _, args = stripped[1:].partition(" ")
            await handle_slash(self, name, args)
            return
        # review finding 4: the MODEL gets the full pasted text, never the
        # literal "[Pasted text #n +N lines]" placeholder -- ONLY the
        # transcript/history `display` keeps the placeholder form (a 5k-
        # character paste doesn't need to render inline every time).
        expanded = _expand_pasted(text, pasted)
        result = self.controller.submit(expanded, pasted=pasted or None)
        if result is not None:  # FakeController: a synchronous scripted turn
            for e in result:
                self._local_events.put(e)

    # ---- / and @ completion ------------------------------------------

    async def on_prompt_input_completion_query(self, event: PromptInput.CompletionQuery) -> None:
        from rolo_claude.tui.completion import complete_at_path, complete_slash

        if event.kind == "accept":
            await self._accept_completion()
            return
        self._completion_kind = event.kind
        if event.kind == "slash":
            self._completion_items = [inv for inv, _desc in complete_slash(event.token, self.registry)]
        else:
            self._completion_items = complete_at_path(event.token, str(self.cwd))
        self.completion_popup.show(self._completion_items)

    def on_prompt_input_completion_dismissed(self, _event: PromptInput.CompletionDismissed) -> None:
        self.completion_popup.hide()
        self._completion_items = []

    async def _accept_completion(self) -> None:
        if not self.completion_popup.display or not self._completion_items:
            return
        idx = self.completion_popup.highlighted or 0
        chosen = self._completion_items[idx % len(self._completion_items)]
        text = (chosen[1:] if self._completion_kind == "slash" else chosen).rstrip("/")
        self.prompt_input.replace_current_token(self._completion_kind, text + " ")
        self.completion_popup.hide()
        self._completion_items = []

    # ---- history Up/Down (D-TUI: "history Up/Down with prefix filter") ---

    def on_prompt_input_history_nav(self, event: PromptInput.HistoryNav) -> None:
        from rolo_claude import history as history_mod

        if not self._history_cache:
            try:
                entries = history_mod.load_merged_history(str(self.cwd))
            except OSError:
                entries = []
            self._history_cache = [e.get("display", "") for e in entries if e.get("display")]
            self._history_index = len(self._history_cache)
            self._history_draft = self.prompt_input.text
        if event.direction < 0 and self._history_index > 0:
            self._history_index -= 1
            self.prompt_input.text = self._history_cache[self._history_index]
        elif event.direction > 0 and self._history_index < len(self._history_cache):
            self._history_index += 1
            self.prompt_input.text = (self._history_draft if self._history_index == len(self._history_cache)
                                       else self._history_cache[self._history_index])
        self.prompt_input.move_cursor(self.prompt_input.document.end)

    # ---- inline cards (D-TUI scope D) -------------------------------------

    def set_pending_card(self, card) -> None:
        # review finding 7 / scope 0(c): the prompt stays ENABLED while a
        # card is pending (it always did for an ordinary running turn;
        # a card is not different) -- steering must still work ("steer
        # during a pending card does not answer the card"). Focus still
        # defaults to the card so digit/Esc keys keep working normally;
        # a user who wants to type instead just clicks/tabs to the prompt.
        self.pending_card = card
        self.set_focus(card)
        self.bell()

    def clear_pending_card(self) -> None:
        self.pending_card = None
        self.prompt_input.placeholder = DEFAULT_PLACEHOLDER
        self.set_focus(self.prompt_input)

    def borrow_input(self, card, *, placeholder: str) -> None:
        """A card needs one line of free text (deny feedback, "Other...",
        plan feedback) -- give it back to PromptInput temporarily; the next
        `Submitted`/Esc routes to `card.resolve_with_message` instead of a
        new turn (see `on_prompt_input_submitted`/`action_escape_pressed`)."""
        self._borrowing_card = card
        self.prompt_input.disabled = False
        self.prompt_input.placeholder = placeholder
        self.prompt_input.clear_submitted()
        self.set_focus(self.prompt_input)

    def resolve_permission_decision(self, request_id: str, decision: dict, *, suggested_rule) -> None:
        from rolo_claude.permissions import SettingsWriteRefused

        scope = decision.get("scope")
        # review finding 3: a comma-joined multi-segment suggestion (e.g. a
        # `make build && npm test` card) names MORE than one rule --
        # learn/write every one of them, not just the first (`suggested_
        # rule` is now populated for every interactive ask, not just
        # print-mode, so this path is finally reachable at all).
        rules = ([r.strip() for r in suggested_rule.split(", ") if r.strip()]
                 if (scope in ("session", "always") and suggested_rule) else [])
        reply = {"action": decision["action"], "reason": "", "rule": rules[0] if rules else None,
                 "message": decision.get("message", "")}
        ok = self.controller.answer_permission(request_id, reply)
        if not ok:
            self.notify("That request is no longer waiting for an answer (already answered or the turn "
                        "was interrupted).", severity="warning", title="Permission")
        for extra_rule in rules[1:]:
            self.controller.add_permission_rule(extra_rule, "session")
        if scope == "always" and rules:
            written: list = []
            for rule_text in rules:
                try:
                    self.controller.add_permission_rule(rule_text, "local")
                    written.append(rule_text)
                except SettingsWriteRefused as e:
                    self.notify(str(e), severity="error", title="Could not save rule", timeout=8)
            if written:
                dest = str(self.cwd / ".claude" / "settings.local.json")
                self.notify(f"Rule(s) added to {dest}: {', '.join(written)}", title="Permission")

    # ---- keys ---------------------------------------------------------

    def action_cycle_mode(self) -> None:
        new_mode = next_mode(self.status_bar.mode)
        self.controller.set_permission_mode(new_mode)
        self.status_bar.set_mode(new_mode)

    async def action_clear_view(self) -> None:
        # review finding 7: `clear_view` used to remove a pending card
        # right along with everything else WITHOUT clearing `pending_
        # card`, leaving the app in a state where the prompt stayed
        # disabled and nothing (not even double Ctrl+C's own separate
        # path) but quit could recover -- re-mount the SAME card instance
        # (its own answer state lives on the Python object, not the DOM).
        card = self.pending_card
        await self.transcript.clear_view()
        if card is not None:
            await self.transcript.mount_widget(card)
            self.set_focus(card)

    def action_toggle_verbose(self) -> None:
        self.verbose = not self.verbose
        for card in self.transcript.tool_cards.values():
            card.set_verbose(self.verbose)
        for block in self.transcript._blocks.values():
            if hasattr(block, "set_expanded"):
                block.set_expanded(self.verbose)

    def action_escape_pressed(self) -> None:
        if self._borrowing_card is not None:
            card = self._borrowing_card
            self._borrowing_card = None
            self.prompt_input.clear_submitted()
            self.prompt_input.placeholder = DEFAULT_PLACEHOLDER
            card.resolve_with_message("")
            return
        if self.pending_card is not None:
            return  # the focused card's own Esc binding handles it
        self.controller.interrupt()

    def action_interrupt_or_quit(self) -> None:
        # review finding 16: the priority Ctrl+C binding shadowed
        # Textual's own `screen.copy_text` (Screen binds ctrl+c to it) --
        # a drag-selected transcript run then interrupted the turn and
        # armed quit instead of copying. OSC 52 (what copy_to_clipboard
        # uses) works over SSH, so this is the right default even
        # headless/remote.
        try:
            selected = self.screen.get_selected_text()
        except Exception:
            selected = None
        if selected:
            self.copy_to_clipboard(selected)
            self.notify("Copied selection to clipboard.", timeout=2)
            return
        now = time.monotonic()
        if self._ctrl_c_deadline is not None and now < self._ctrl_c_deadline:
            self._begin_quit()
            return
        self._ctrl_c_deadline = now + DOUBLE_CTRL_C_WINDOW_S
        if self._borrowing_card is None and self.pending_card is None:
            if self.prompt_input.text:
                self.prompt_input.clear_submitted()
            else:
                self.controller.interrupt()
        self.notify("Press Ctrl+C again to exit", timeout=DOUBLE_CTRL_C_WINDOW_S)

    def action_quit_on_empty(self) -> None:
        if self.prompt_input.text.strip():
            self.prompt_input.action_delete_right()  # restore TextArea's own Ctrl+D (forward-delete)
        else:
            self._begin_quit()

    async def action_quit_now(self) -> None:
        self._begin_quit()

    def _begin_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.run_worker(self._quit_worker, thread=True, exclusive=True, name="quit")

    def _quit_worker(self) -> None:
        quit_fn = getattr(self.controller, "quit", None)
        code = quit_fn() if callable(quit_fn) else 0
        message = self._build_scrollback_message()
        self.call_from_thread(self.exit, return_code=code, message=message)

    def _build_scrollback_message(self) -> Optional[str]:
        if self.tui_setting is not None and self.tui_setting != "fullscreen":
            text = "\n\n".join(self.transcript.plain_log)
            return text or None
        return None

    def action_show_help(self) -> None:
        from rolo_claude.tui.dialogs.help import HelpDialog

        self.push_screen(HelpDialog(self.registry))

    def action_history_search(self) -> None:
        from rolo_claude import history as history_mod
        from rolo_claude.tui.dialogs.history_search import HistorySearchDialog

        try:
            entries = history_mod.load_merged_history(str(self.cwd))
        except OSError:
            entries = []
        display = [e.get("display", "") for e in reversed(entries) if e.get("display")]

        def _on_pick(text) -> None:
            if text:
                self.prompt_input.text = text
                self.prompt_input.move_cursor(self.prompt_input.document.end)

        self.push_screen(HistorySearchDialog(display), _on_pick)

    def apply_theme(self, name: str) -> None:
        self.theme_name = name
        self.refresh_css(animate=False)
