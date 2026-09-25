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

import inspect
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

from rolo_claude import events as ev
from rolo_claude.tui import keys as tui_keys
from rolo_claude.tui import theme as tui_theme
from rolo_claude.tui.dispatch import apply_event
from rolo_claude.tui.events import drain_queue
from rolo_claude.tui.keys import DOUBLE_CTRL_C_WINDOW_S, DRAIN_HZ, next_mode
from rolo_claude.tui.widgets.input import CompletionPopup, PromptInput
from rolo_claude.tui.widgets.statusbar import StatusBar
from rolo_claude.tui.widgets.transcript import Transcript
from rolo_claude.tui.widgets.whichkey import WhichKeyOverlay

# U5 scope A: chord actions that are really just "run this slash command"
# (session titles/fork/export/stats, git-shadow rewind) reuse the EXACT
# same handler `tui/slash.py` runs for a typed `/name` -- one implementation,
# two triggers. `_METHOD_ACTIONS` covers everything else a chord/remap can
# reach: an actual BridgeApp method, sync or async.
_SLASH_CHORD_ACTIONS = {
    "session:export": "export", "session:rename": "rename", "session:fork": "fork",
    "session:resume": "resume", "session:stats": "stats",
    "rewind:undo": "undo", "rewind:redo": "redo",
}
_METHOD_ACTIONS = {
    "app:commandPalette": "action_command_palette",
    "app:help": "action_show_help",
    "app:toggleVerbose": "action_toggle_verbose",
    "chat:externalEditor": "action_open_editor",
    "chat:clearScreen": "action_clear_view",
    "history:search": "action_history_search",
    "session:nextChild": "action_next_subagent",
    "session:prevChild": "action_prev_subagent",
}

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
        # U5 scope A: brand-new keys, no existing single-key binding to
        # conflict with -- ctrl+p (palette) and ctrl+e (external editor)
        # are ordinary bindings; ctrl+x is a CHORD PREFIX and needs
        # `priority=True` (like ctrl+c/ctrl+d/shift+tab above) so it's
        # caught before PromptInput/Screen ever see it, regardless of
        # focus -- see `_on_key`'s own docstring for how the SECOND
        # keystroke of the chord is then captured.
        Binding("ctrl+p", "command_palette", "Palette", show=False),
        Binding("ctrl+e", "open_editor", "Editor", show=False),
        Binding("ctrl+x", "chord_prefix", "Chord", priority=True, show=False),
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
        # U5 scope A: the merged {context: {chord: action}} keymap (our
        # defaults + ~/.claude/keybindings.json), loaded once here (a
        # user editing that file mid-session picks it up on the next
        # launch, matching how Claude Code's own keybindings.json is
        # documented to be read at load time) -- `chord_prefix`/`_on_key`
        # below are the only readers.
        self._keymap = tui_keys.load_keymap()
        self._pending_chord: Optional[str] = None
        self._chord_timer = None
        # U5 scope C: kicks off the after-first-turn auto-title exactly
        # once, the first time `turn_done` fires (see `on_turn_done`).
        self._turn_done_count = 0

    # ---- composition -------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript()
        yield CompletionPopup()
        yield WhichKeyOverlay()
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
        self.which_key = self.query_one(WhichKeyOverlay)
        self.prompt_input = self.query_one(PromptInput)
        self.status_bar = self.query_one(StatusBar)
        # U5/review "must-do": git ran INLINE here even though the 5s
        # periodic refresh below was already threaded -- this first call
        # is now the same worker path, so startup never blocks on a
        # slow/hung `git` either.
        self._refresh_cwd_branch()
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
        statusline_cfg = self._statusline_config()
        if statusline_cfg is not None:
            self._refresh_statusline()
            self.set_interval(5.0, self._refresh_statusline)
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
        # U5 scope C: "session titles via the small model after the first
        # turn" -- kicked off exactly once, off the UI thread (a small-
        # model call is network I/O); `Controller.maybe_autoname_title`
        # itself is a no-op once a title already exists, so a resumed
        # session (already titled) never overwrites it.
        self._turn_done_count += 1
        if self._turn_done_count == 1 and callable(getattr(self.controller, "maybe_autoname_title", None)):
            self.run_worker(self._autoname_worker, thread=True, name="autoname-title")

    def _autoname_worker(self) -> None:
        try:
            title = self.controller.maybe_autoname_title()
        except Exception:
            title = None
        if title:
            self.call_from_thread(self.notify, f"Session titled: {title}", timeout=3, title="Session")

    # ---- statusLine command (U5 scope D) -------------------------------

    def _statusline_config(self) -> Optional[dict]:
        settings = getattr(self.facade, "settings", None) if self.facade is not None else None
        cfg = getattr(settings, "statusline", None) if settings is not None else None
        return cfg if isinstance(cfg, dict) and cfg.get("command") else None

    def _refresh_statusline(self) -> None:
        cfg = self._statusline_config()
        if cfg is None:
            return
        self.run_worker(lambda: self._statusline_worker(cfg), thread=True, exclusive=True, name="statusline")

    def _statusline_worker(self, cfg: dict) -> None:
        from rolo_claude import __version__
        from rolo_claude.statusline import build_payload, run_statusline_command

        session = getattr(self.controller, "session", None)
        cost_usd = None
        if session is not None:
            try:
                cost_usd = session.cost_meter.total_usd
            except Exception:
                cost_usd = None
        payload = build_payload(
            session_id=getattr(getattr(session, "log", None), "session_id", "") if session else "",
            cwd=str(self.cwd), model_id=self.status_bar.model, version=__version__, cost_usd=cost_usd,
        )
        text = run_statusline_command(cfg.get("command", ""), payload, cwd=self.cwd)
        self.call_from_thread(self.status_bar.set_statusline_text, text)

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
        # U5 scope A: `!cmd` runs a shell command inline, through the Bash
        # tool + permissions, OUTSIDE the model loop entirely -- checked
        # BEFORE the "/" branch (a bare "!" line is never a slash command).
        if stripped.startswith("!") and "\n" not in stripped:
            await self._handle_bang_command(stripped[1:].strip())
            return
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
        # U5 scope A: `@file#L10-20` mentions -- read via the Read tool's
        # own path resolution and appended as log SNAPSHOTS (never inlined
        # into the submitted text, same convention as commands/registry.py's
        # `@path` for custom commands). Best-effort; a FakeController (no
        # `.ingest_at_mentions`) just skips this.
        ingest = getattr(self.controller, "ingest_at_mentions", None)
        if callable(ingest):
            try:
                ingest(expanded)
            except Exception:
                pass
        result = self.controller.submit(expanded, pasted=pasted or None)
        if result is not None:  # FakeController: a synchronous scripted turn
            for e in result:
                self._local_events.put(e)

    # ---- `!cmd` inline shell (U5 scope A) ------------------------------

    async def _handle_bang_command(self, command: str) -> None:
        if not command:
            return
        decide = getattr(self.controller, "decide_inline_shell", None)
        if not callable(decide):
            await self.transcript.add_note("! inline shell needs a real session (not available here).",
                                            kind="error")
            return
        decision = decide(command)
        action = getattr(decision, "action", "deny")
        if action == "deny":
            await self.transcript.add_note(f"✗ !{command} -- denied: {getattr(decision, 'reason', '')}",
                                            kind="error")
            return
        if action == "allow":
            self._start_inline_shell_worker(command)
            return
        # "ask": a confirmation card, resolved entirely here (never through
        # `Session.resolve_permission` -- there's no in-flight turn for
        # this to belong to). Reuses PermissionCard verbatim: same keys
        # (1/2/3/4), same session/always rule-writing via
        # `Controller.add_permission_rule`.
        from rolo_claude.tui.widgets.cards import PermissionCard

        def on_decide(reply: dict) -> None:
            self.clear_pending_card()
            if reply.get("action") != "allow":
                return
            scope, rule = reply.get("scope"), reply.get("rule")
            if scope in ("session", "always") and rule:
                add_rule = getattr(self.controller, "add_permission_rule", None)
                if callable(add_rule):
                    try:
                        add_rule(rule, "local" if scope == "always" else "session")
                    except Exception:
                        pass
            self._start_inline_shell_worker(command)

        card = PermissionCard(request_id=f"inline-{id(command)}", summary=f"Bash({command})",
                               reason=getattr(decision, "reason", "") or "runs now, outside the model turn (! prefix)",
                               suggested_rule=getattr(decision, "suggested_rule", None), on_decide=on_decide)
        await self.transcript.mount_widget(card)
        self.set_pending_card(card)

    def _start_inline_shell_worker(self, command: str) -> None:
        self.run_worker(lambda: self._inline_shell_worker(command), thread=True, name="inline-shell")

    def _inline_shell_worker(self, command: str) -> None:
        try:
            tool_use_id, result = self.controller.run_inline_shell(command)
        except Exception as e:
            self._local_events.put(ev.error(f"!{command} failed: {type(e).__name__}: {e}"))
            return
        content = result.content if isinstance(result.content, str) else str(result.content)
        self._local_events.put(ev.Event("tool_use_ready", {
            "id": tool_use_id, "name": "Bash", "input": {"command": command}, "repaired": False,
        }))
        self._local_events.put(ev.Event("tool_result", {
            "id": tool_use_id, "ok": not result.is_error, "summary": content[:200], "content": content,
        }))

    # ---- Ctrl+P command palette (U5 scope A) --------------------------

    def action_command_palette(self) -> None:
        # U5 must-do: `list_sessions()` is file I/O -- built off the UI
        # thread, same fix as `_git_branch`/`/resume` below.
        self.run_worker(self._palette_worker, thread=True, name="palette-build")

    def _palette_worker(self) -> None:
        from rolo_claude.tui.dialogs.palette import CommandPalette

        items = self._build_palette_items()
        self.call_from_thread(self.push_screen, CommandPalette(items), self._on_palette_pick)

    def _build_palette_items(self) -> "list[dict]":
        items: "list[dict]" = []
        if self.registry is not None:
            for cmd in self.registry.all():
                kind = "skill" if cmd.source == "skill" else "command"
                items.append({"kind": kind, "label": f"/{cmd.name}", "detail": cmd.description, "value": cmd.name})
        try:
            from rolo_claude.tui.completion import complete_at_path
            for p in complete_at_path("", str(self.cwd))[:30]:
                items.append({"kind": "file", "label": p, "detail": "", "value": p})
        except OSError:
            pass
        list_sessions = getattr(self.controller, "list_sessions", None)
        if callable(list_sessions):
            try:
                for s in (list_sessions() or [])[:20]:
                    label = s.get("title") or s.get("summary") or s.get("id", "")
                    items.append({"kind": "session", "label": label, "detail": s.get("id", ""),
                                  "value": s.get("id", "")})
            except Exception:
                pass
        return items

    def _on_palette_pick(self, item: Optional[dict]) -> None:
        if not item:
            return
        kind = item.get("kind")
        if kind in ("command", "skill"):
            self.call_next(self._run_palette_command, item["value"])
        elif kind == "file":
            self.prompt_input.text = f"{self.prompt_input.text}@{item['value']} "
            self.prompt_input.move_cursor(self.prompt_input.document.end)
        elif kind == "session":
            resume = getattr(self.controller, "resume", None)
            if callable(resume):
                resume(item["value"])

    async def _run_palette_command(self, name: str) -> None:
        from rolo_claude.tui.slash import handle_slash
        await handle_slash(self, name, "")

    # ---- Ctrl+E: edit the prompt draft in $VISUAL/$EDITOR (U5 scope A) --

    def action_open_editor(self) -> None:
        import os
        import subprocess as sp
        import tempfile

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            self.notify("No $VISUAL/$EDITOR set.", severity="warning", title="Ctrl+E")
            return
        fd, tmp_path_str = tempfile.mkstemp(suffix=".md", prefix="rolo-claude-")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.prompt_input.text)
            with self.suspend():
                sp.run(f'{editor} "{tmp_path}"', shell=True)
            new_text = tmp_path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
            if new_text != self.prompt_input.text:
                self.prompt_input.text = new_text
                self.prompt_input.move_cursor(self.prompt_input.document.end)
        except Exception as e:
            self.notify(f"$EDITOR failed: {type(e).__name__}: {e}", severity="error", title="Ctrl+E")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    # ---- chord prefixes + which-key overlay (U5 scope A) ---------------

    def action_chord_prefix(self) -> None:
        self._begin_chord("ctrl+x")

    def action_next_subagent(self) -> None:
        self.transcript.scroll_to_next_subagent(1)

    def action_prev_subagent(self) -> None:
        self.transcript.scroll_to_next_subagent(-1)

    def _begin_chord(self, prefix: str) -> None:
        continuations = tui_keys.chord_continuations(self._keymap.get("Global", {}), prefix)
        if not continuations:
            return
        self._pending_chord = prefix
        self.which_key.show_for(prefix, continuations)
        if self._chord_timer is not None:
            self._chord_timer.stop()
        self._chord_timer = self.set_timer(tui_keys.CHORD_TIMEOUT_S, self._chord_timeout)

    def _chord_timeout(self) -> None:
        self._pending_chord = None
        self.which_key.hide()

    def _clear_pending_chord(self) -> None:
        if self._chord_timer is not None:
            self._chord_timer.stop()
            self._chord_timer = None
        self._pending_chord = None
        self.which_key.hide()

    async def _on_key(self, event) -> None:
        """Overridden ONLY to capture the SECOND keystroke of a chord.
        CRITICAL (same gotcha `PromptInput._on_paste`'s own docstring
        documents): Textual's message pump calls `_on_<event>` at EVERY
        class in the MRO INDEPENDENTLY (`_get_dispatch_methods` walks
        `self.__class__.__mro__`), NOT via normal `super()` chaining -- it
        already calls the base `App._on_key` (priority/non-priority
        binding resolution) automatically, separately, right alongside
        this one. This method must therefore NEVER call `super()._on_key
        (...)` itself (verified: doing so made EVERY key fire its bound
        action TWICE -- Esc interrupted twice, Ctrl+O toggled verbose
        twice/back-to-off, Tab advanced the question strip twice, ...). It
        only ever CONSUMES a key here (the live continuation of a pending
        chord) -- everything else is left for that automatic base
        dispatch to handle on its own, completely untouched."""
        if self._pending_chord is not None:
            prefix = self._pending_chord
            full = f"{prefix} {tui_keys.normalize_keystroke(event.key)}"
            self._clear_pending_chord()
            action = self._keymap.get("Global", {}).get(full)
            if action:
                event.stop()
                event.prevent_default()
                await self._run_keymap_action(action)

    async def _run_keymap_action(self, name: str) -> None:
        slash_name = _SLASH_CHORD_ACTIONS.get(name)
        if slash_name is not None:
            from rolo_claude.tui.slash import handle_slash
            await handle_slash(self, slash_name, "")
            return
        handler_name = _METHOD_ACTIONS.get(name)
        if handler_name is None:
            return
        method = getattr(self, handler_name, None)
        if method is None:
            return
        result = method()
        if inspect.isawaitable(result):
            await result

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
        self._maybe_notify_input_needed()

    def _maybe_notify_input_needed(self) -> None:
        """U5 scope D: "terminal bell + notify-send/toast when input is
        needed and `inputNeededNotifEnabled`" -- the bell above always
        fires; this ADDS a desktop `notify-send` (Linux), gated on that
        setting (default off -- see `Settings.input_needed_notif_enabled`).
        Best-effort: no `notify-send` on PATH (not Linux, minimal
        container, ...) is silently skipped, never an error."""
        settings = getattr(self.facade, "settings", None) if self.facade is not None else None
        if settings is None or not getattr(settings, "input_needed_notif_enabled", False):
            return
        import shutil
        if not shutil.which("notify-send"):
            return
        self.run_worker(self._notify_send_worker, thread=True, name="notify-send")

    def _notify_send_worker(self) -> None:
        try:
            subprocess.run(["notify-send", "rolo-claude", "Input needed"], timeout=3,
                            capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass

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
        # U5: Esc is a PRIORITY binding (caught in `on_event` before the
        # key ever reaches `_on_key`), so it's the one escape hatch a
        # pending chord's own timeout-based clearing can't otherwise see --
        # clear it here too, so Esc always cancels a stray "ctrl+x ..."
        # immediately rather than leaving the which-key overlay stale for
        # up to CHORD_TIMEOUT_S.
        if self._pending_chord is not None:
            self._clear_pending_chord()
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
            self.copy_to_clipboard(selected)  # OSC 52 -- primary mechanism, works over SSH
            # U5 scope E: belt-and-suspenders fallback for a terminal/
            # multiplexer that doesn't relay OSC 52 -- best-effort, off
            # the UI thread (spawns a subprocess), never blocks the copy.
            self.run_worker(lambda: self._clipboard_fallback_worker(selected), thread=True,
                            name="clipboard-fallback")
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

    def _clipboard_fallback_worker(self, text: str) -> None:
        from rolo_claude.tui.clipboard import copy_via_external_tool
        copy_via_external_tool(text)

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
