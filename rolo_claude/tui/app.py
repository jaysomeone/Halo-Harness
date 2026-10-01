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
import os
import queue
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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
        # 1.0.1 hotfix 16: re-anchor the transcript to follow new output.
        # `ctrl+end` is `priority=True` since nothing else binds it (always
        # reliable, whatever has focus); plain `end` is deliberately NOT
        # priority -- the focused PromptInput (a TextArea) already binds
        # bare `end` to cursor-to-end-of-line, which must keep winning
        # while the prompt has focus (the overwhelmingly common case) --
        # this one only ever fires when something else (a card) is
        # focused instead and doesn't claim the key itself.
        Binding("ctrl+end", "scroll_transcript_end", "Follow output", priority=True, show=False),
        Binding("end", "scroll_transcript_end", "Follow output", show=False),
        # 1.0.1 hotfix 15.6: a hard exit that can't get stuck, unlike
        # double-Ctrl+C's own quit path (see action_force_quit's docstring).
        Binding("ctrl+q", "force_quit", "Force quit", priority=True, show=False),
    ]

    def __init__(self, controller, *, registry=None, facade=None, tool_registry=None,
                 cwd: Optional[Path] = None, theme_name: Optional[str] = None,
                 tui_setting: Optional[str] = None, initial_prompt: Optional[str] = None,
                 initial_resume_filter: Optional[str] = None, no_inline_images: bool = False) -> None:
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
        # H13 Part C: an ambiguous (or no-match) `--resume <text>` at launch
        # -- opened, pre-filtered, from `on_mount` below instead of silently
        # starting a plain new session with no feedback at all.
        self._initial_resume_filter = initial_resume_filter
        # H13 Part B ("inline images in the terminal"): resolved ONCE at
        # startup, never per-image -- `images_render_mode` folds the config
        # ("~/.rolo-claude/config.json"'s "images" key) and `--no-inline-
        # images` together into "inline"|"caption"; `image_protocol` is the
        # one live detection pass (env + tmux passthrough + a real DA1
        # terminal query, all best-effort/never-raising) a real interactive
        # run needs -- `sys.stdout.isatty()` is naturally False under
        # `app.run_test()`'s headless pilot driver and in any piped/
        # captured context, so this is "none" (never touches a real
        # terminal) throughout the whole test suite with zero special-
        # casing needed there.
        from rolo_claude.theme import get_config_value
        from rolo_claude.tui.images import detect_image_protocol, effective_render_mode
        self.images_render_mode = effective_render_mode(
            get_config_value("images", None), no_inline_flag=no_inline_images)
        self.image_protocol = (
            detect_image_protocol(dict(os.environ), isatty=sys.stdout.isatty())
            if self.images_render_mode == "inline" else "none")

        self._local_events: "queue.Queue" = queue.Queue()
        self.verbose = False
        self.pending_card = None
        self._borrowing_card = None
        self._ctrl_c_deadline: Optional[float] = None
        self._quitting = False
        # 1.0.1 fixpass finding 5: Ctrl+Q's OWN flag, never shared with
        # `_quitting` -- see action_force_quit's own docstring.
        self._force_quitting = False
        # 1.0.1 fixpass finding 15: whether the transcript was following
        # (anchored to the bottom) right before the CURRENTLY pending card
        # took focus -- see set_pending_card/clear_pending_card.
        self._card_interrupted_following = False
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
        # One bottom-docked container holds the prompt row ABOVE the status
        # bar. Docking both widgets to the bottom edge separately made
        # Textual overlap them: the status bar (composed last, height 1)
        # painted over the prompt row's input line, so the TUI showed a
        # separator and a status bar and nothing to type into (found live
        # on a Kali box; the pilots never checked geometry).
        with Vertical(id="bottom-dock"):
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
        # 1.0.1 fixpass finding 1: primes `cc_models.cached_claude_auth_
        # status()` off the UI thread, once, at startup -- `Controller.
        # list_models()` (every `/model` open) only ever READS that cache
        # now, never spawns the `claude auth status` subprocess itself.
        self.run_worker(self._prime_auth_status_worker, thread=True, name="auth-status-startup",
                         group="auth-status-startup")
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
        elif self._initial_resume_filter is not None:
            # H13 Part C: reuses the exact same off-UI-thread listing +
            # picker-opening path `/resume` itself uses (tui/slash.py) --
            # just pre-filtered by the text that made the startup
            # `--resume <text>` ambiguous (or match nothing) in the first place.
            from rolo_claude.tui.slash import _resume_list_worker
            self.run_worker(lambda: _resume_list_worker(self, self._initial_resume_filter),
                             thread=True, name="list-sessions-startup")

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
        # 1.0.1 hotfix 15.4: `group=` -- without it, `exclusive=True`'s
        # default group is "default", the SAME group EVERY OTHER worker in
        # this app (models-refresh, dbx-auto-refresh, list-sessions,
        # improve-draft, autoname-title, ...) also lands in with no group=
        # of their own -- Textual's WorkerManager cancels every other
        # worker in an exclusive worker's group each time it (re)starts, so
        # this 5s-interval timer was silently killing unrelated in-flight
        # work network-wide, every 5 seconds, for the whole app's life.
        # Confirmed against the installed Textual (8.2.8): `exclusive`
        # scopes to `group`, and cancellation runs on every NEW start of an
        # exclusive worker, not just the first.
        self.run_worker(self._git_branch_worker, thread=True, exclusive=True, name="git-branch", group="git-branch")

    def _git_branch_worker(self) -> None:
        branch = self._git_branch()
        self.call_from_thread(self.status_bar.set_cwd_branch, str(self.cwd), branch)

    def _prime_auth_status_worker(self) -> None:
        """1.0.1 fixpass finding 1: the ONE place that ever spawns `claude
        auth status` for the model picker's own cc: group -- everywhere
        else (`Controller.list_models()`) only ever reads the cache this
        populates. Best-effort: a failure here just leaves the cache empty
        (list_models()'s own try/except already treats that the same as
        "not logged in" -- no cc: group shown, never a crash)."""
        try:
            from rolo_claude.providers.cc_models import refresh_cached_claude_auth_status
            refresh_cached_claude_auth_status()
        except Exception:
            pass

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
        # 1.0.1 hotfix 15.4: own group= -- see _refresh_cwd_branch's own
        # comment just above for why (the identical bug, the same 5s timer
        # shape, sharing the default group with it too before this fix).
        self.run_worker(lambda: self._statusline_worker(cfg), thread=True, exclusive=True, name="statusline",
                         group="statusline")

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
        stripped = text.strip()
        # 1.0.1 fixpass finding 14: a `/`-prefixed submission is ALWAYS an
        # attempted slash command, whatever card/borrow state is active --
        # typing "/permissions" (or Tab-completing a `/` command then Enter,
        # which re-enters here the identical way, see _accept_completion)
        # while a PermissionCard was still pending used to DENY the tool
        # with "The user said: /permissions" instead of ever running the
        # command. Checked BEFORE either branch below -- the card/borrow
        # itself is left completely untouched (neither answered nor
        # cancelled) so the user can still answer it afterward; only PLAIN
        # text (no leading "/") ever answers a card.
        is_slash_command = stripped.startswith("/") and "\n" not in stripped

        if self._borrowing_card is not None:
            if is_slash_command:
                self.prompt_input.clear_submitted()
                from rolo_claude.tui.slash import handle_slash
                name, _, args = stripped[1:].partition(" ")
                await handle_slash(self, name, args)
                return
            card = self._borrowing_card
            self._borrowing_card = None
            self.prompt_input.clear_submitted()
            self.prompt_input.placeholder = DEFAULT_PLACEHOLDER
            # finding 14: expanded, never the raw "[Pasted text #n ...]"
            # placeholder -- same reasoning as review finding 4's own fix
            # for the ordinary prompt path just below.
            card.resolve_with_message(_expand_pasted(text, event.pasted))
            return
        if not text.strip():
            return
        self.prompt_input.clear_submitted()
        self.completion_popup.hide()
        from rolo_claude.tui.widgets.cards import EffortCard
        if self.pending_card is not None and not isinstance(self.pending_card, EffortCard):
            # finding 14: the /effort selector does not intercept typed
            # text at all -- it falls straight through to the ordinary
            # path below (slash handling, history, a fresh prompt), same
            # as if nothing were pending; every OTHER card still gates
            # here exactly as before.
            if is_slash_command:
                from rolo_claude.tui.slash import handle_slash
                name, _, args = stripped[1:].partition(" ")
                await handle_slash(self, name, args)
                return
            if hasattr(self.pending_card, "resolve_with_message"):
                # 1.0.1 hotfix 17.1: free text typed while a permission/
                # plan/question card is pending ANSWERS the card -- exactly
                # Claude Code's own behavior (a permission card denies with
                # the text as feedback, a plan card keeps planning with it,
                # a question card takes it as "Other") -- never a silent
                # steer the user has no way to see resolve anything. This
                # was the real mechanism behind the reported "freeze": the
                # auto-scroll bug (item 16) left the card below the fold,
                # so the user kept typing into what looked like the normal
                # prompt, each one only ever logged as "↳ steering…" while
                # the pending ask underneath was never actually answered.
                card = self.pending_card
                card.resolve_with_message(_expand_pasted(text, event.pasted))
                return
            # A card with no free-text answer (a rewind confirmation) --
            # unchanged: a steer on the turn running underneath it, same as
            # before this fix.
            self.controller.submit(_expand_pasted(text, event.pasted), pasted=event.pasted or None)
            return
        # 1.0.1 hotfix 16: a fresh prompt always re-anchors the transcript
        # to follow the coming reply, even if the user had scrolled up to
        # re-read something earlier -- scroll_end() moving scroll_y back to
        # the true bottom is exactly what Textual's own anchor-reacquire
        # watcher treats as "the user returned to the bottom" (the same
        # mechanism a manual scroll-back-down already triggers), so this
        # needs no anchor-specific API at all. The counter reset is
        # UNCONDITIONAL (see action_scroll_transcript_end's own docstring
        # for why `mark_seen()`'s is_at_bottom()-gated version isn't used
        # here either).
        self.transcript.scroll_end(animate=False)
        self.transcript.new_since_scroll = 0
        self.status_bar.set_new_count(0)
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
        dispatch to handle on its own, completely untouched.

        1.0.1 hotfix 15.3: self-heal -- run BEFORE the chord check, on
        EVERY key, never stopping/consuming the event itself: if the
        active screen has somehow ended up with no focused widget at all
        (this hotfix's own item 16/17 fixes address the known causes --
        the auto-scroll bug leaving a card's own focus in a bad state, a
        pending permission card getting lost -- but this is a net for
        whatever's not yet found), refocus before letting the key proceed,
        so a key never silently goes nowhere."""
        # 1.0.1 fixpass finding 3: healing to the pending card must be
        # scoped to the BASE screen (`self.screen is self.screen_stack[0]`)
        # -- a modal with nothing focusable of its own (PagerScreen, opened
        # with `o` from a ToolCard or from the pending ImproveCard) used to
        # get healed to the CARD instead, which lives on the screen
        # UNDERNEATH it: the binding chain for the next key then built from
        # that hidden card, not the modal actually on top, so the pager's
        # own `q`/`o` bindings vanished and the hidden card's keys fired
        # instead (`q` ended `/improve`, `a` applied a candidate unseen,
        # `1`/`y` approved a permission unseen). A modal screen now always
        # goes through `focus_next()` instead -- a no-op when it has
        # nothing focusable (exactly PagerScreen's case), the correct
        # first-focusable pick when it does (an ordinary picker dialog).
        if self.screen.focused is None:
            if self.screen is self.screen_stack[0]:
                if self.pending_card is not None and self.pending_card.is_mounted:
                    self.set_focus(self.pending_card)
                else:
                    self.set_focus(self.prompt_input)
            else:
                self.screen.focus_next()
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

        if event.kind in ("accept", "accept_submit"):
            await self._accept_completion(submit=(event.kind == "accept_submit"))
            return
        self._completion_kind = event.kind
        if event.kind == "slash":
            self._completion_items = [inv for inv, _desc in complete_slash(event.token, self.registry)]
        else:
            self._completion_items = complete_at_path(event.token, str(self.cwd))
        self.completion_popup.show(self._completion_items)
        # 1.0.1 hotfix 1: keeps PromptInput's own `_completion_open` flag in
        # sync with the popup's real (post-`show()`) display state -- `show`
        # itself sets `display = bool(items)`, so an empty result (nothing
        # matches the filter) correctly falls back to ordinary Up/Down.
        self.prompt_input.set_completion_open(bool(self._completion_items))

    def on_prompt_input_completion_dismissed(self, _event: PromptInput.CompletionDismissed) -> None:
        self.completion_popup.hide()
        self._completion_items = []
        self.prompt_input.set_completion_open(False)

    def on_prompt_input_completion_nav(self, event: PromptInput.CompletionNav) -> None:
        """1.0.1 hotfix 1: Up/Down while the popup is open move its
        highlighted entry (wrapping around both ends) instead of moving the
        prompt cursor or walking history."""
        if not self.completion_popup.display or not self._completion_items:
            return
        count = len(self._completion_items)
        current = self.completion_popup.highlighted or 0
        self.completion_popup.highlighted = (current + event.direction) % count

    async def _accept_completion(self, submit: bool = False) -> None:
        """Insert the highlighted completion. `submit=True` (Enter, not
        Tab) additionally runs a `/` command right away -- Claude Code
        parity, see PromptInput._on_key -- through the ordinary submit path
        so pending cards, history and the input reset all behave exactly as
        for a typed-out command. `@` path completions are never submitted."""
        if not self.completion_popup.display or not self._completion_items:
            return
        idx = self.completion_popup.highlighted or 0
        chosen = self._completion_items[idx % len(self._completion_items)]
        kind = self._completion_kind
        text = (chosen[1:] if kind == "slash" else chosen).rstrip("/")
        self.prompt_input.replace_current_token(kind, text + " ")
        self.completion_popup.hide()
        self._completion_items = []
        self.prompt_input.set_completion_open(False)
        if submit and kind == "slash":
            await self.on_prompt_input_submitted(
                PromptInput.Submitted(self.prompt_input.text, self.prompt_input.pasted))

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
        # 1.0.1 fixpass finding 15: recorded BEFORE set_focus -- a card too
        # tall to already be fully visible makes Textual's own
        # Screen.set_focus(scroll_visible=True) scroll it into view (a
        # plain `not self.can_view_entire(widget)` check upstream, so a
        # card that DOES already fit never triggers this at all, unchanged
        # from Textual's own default), which releases the transcript's
        # bottom anchor same as any ordinary scroll -- clear_pending_card
        # below uses this to re-anchor, but only when it's actually needed.
        self._card_interrupted_following = self.transcript.is_following()
        self.set_focus(card)
        self.bell()
        self._maybe_notify_input_needed()
        # 1.0.1 hotfix 17.3: a PermissionCard specifically (not the plan/
        # question cards, nor the /effort selector) gets its own status bar
        # tag and prompt placeholder the MOMENT it mounts, not only once the
        # user presses "4" and borrows the input for deny feedback -- a
        # pending permission ask must be impossible to miss even if the
        # auto-scroll fix (item 16) still somehow leaves it out of view.
        from rolo_claude.tui.widgets.cards import PermissionCard
        if isinstance(card, PermissionCard):
            self.prompt_input.placeholder = "1-4 answers the request above, or type why not"
            self.status_bar.set_pending_permission(True)

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
        self.status_bar.set_pending_permission(False)
        self.set_focus(self.prompt_input)
        # 1.0.1 fixpass finding 15: re-anchor if the transcript was
        # following right before this card interrupted it -- scroll_end()
        # is exactly what Textual's own anchor-reacquire watcher treats as
        # "back at the bottom" (the same mechanism on_prompt_input_
        # submitted/action_scroll_transcript_end already use), so output
        # after this point (e.g. right after "approve plan") follows again
        # instead of staying wherever a tall card's own scroll-to-center
        # left it. Conditional, never unconditional -- a user who had
        # scrolled UP to re-read something before the card ever appeared
        # must not be yanked back down just for answering it.
        if self._card_interrupted_following:
            self.transcript.scroll_end(animate=False)
        self._card_interrupted_following = False

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
        self._reevaluate_pending_permission_for_mode(new_mode)

    def _reevaluate_pending_permission_for_mode(self, mode: str) -> None:
        """1.0.1 hotfix 17.2: switching mode (Shift+Tab here, `/permissions`
        in slash.py) WHILE a permission card is still pending must resolve
        THAT card under the new mode right away -- `Controller.
        set_permission_mode` only changes `permission_engine.mode` for
        FUTURE asks (a plain, thread-safe attribute write); an ask already
        blocked in `Session._await_permission_decision` is untouched by it,
        so without this it sits there forever even once `auto`/
        `bypassPermissions` would have allowed it. Root-cause match for
        rolo's report: he pressed Shift+Tab to `auto`, the status bar
        updated, but the turn never continued -- the pending ask underneath
        was never re-decided.

        1.0.1 fixpass finding 4: the old unconditional `action_choose_once()`
        blindly ALLOWED regardless of an explicit `ask:` rule (which still
        asks in every mode but `bypassPermissions` -- `auto` does not skip
        it); `Controller.reevaluate_pending_permission` re-runs the real
        `permission_engine.decide()` for the parked request instead, and
        only resolves on an actual "allow"/"deny" verdict -- a card still
        "ask" under the new mode is left alone, exactly like any other
        unresolved ask. A card the user is already answering (pressed "4",
        typing why not) is skipped entirely -- a mode change must never
        turn that in-progress rejection into "allow once" out from under
        them (the old bug: `_borrowing_card` was then left pointing at the
        now-finished card, so the NEXT Enter answered nothing and cleared
        whatever card was ACTUALLY pending by then instead)."""
        from rolo_claude.tui.widgets.cards import PermissionCard
        card = self.pending_card
        if not isinstance(card, PermissionCard) or card.done or card.awaiting_feedback:
            return
        action = self.controller.reevaluate_pending_permission(card.request_id)
        if action not in ("allow", "deny"):
            return  # still "ask" under the new mode (or nothing pending any more) -- leave it up
        card.resolve_externally(action)
        if self._borrowing_card is card:
            self._borrowing_card = None
        self.clear_pending_card()

    def action_scroll_transcript_end(self) -> None:
        """1.0.1 hotfix 16: End/Ctrl+End (bound above) and a click on the
        status bar's own "N new" indicator (StatusBar.on_click) all land
        here -- jump to the bottom and re-anchor (scroll_end() moving
        scroll_y to the true max is exactly what Textual's own anchor-
        reacquire watcher treats as "back at the bottom", so nothing
        anchor-specific needs calling here beyond the plain scroll).

        The counter reset is UNCONDITIONAL (never `mark_seen()`'s own
        `is_at_bottom()`-gated version) -- `scroll_y` from a just-issued
        `scroll_end()` is not guaranteed to have already propagated through
        Textual's reactive watcher by the time this next line runs, so
        gating on it here read stale state and left "N new" showing a
        nonzero count right after the very key that's supposed to clear it."""
        self.transcript.scroll_end(animate=False)
        self.transcript.new_since_scroll = 0
        self.status_bar.set_new_count(0)

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
        # 1.0.1 hotfix 1: Esc closes an open `/`/`@` completion popup first
        # (never also interrupts a running turn in the same keypress) --
        # checked before the borrowing-card/pending-card/interrupt branches
        # below, which is what Esc did unconditionally before this fix.
        if self.completion_popup.display:
            self.completion_popup.hide()
            self._completion_items = []
            self.prompt_input.set_completion_open(False)
            return
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

    def action_force_quit(self) -> None:
        """1.0.1 hotfix 15.6: Ctrl+Q exits even when the session/transcript
        is wedged (a hang neither Ctrl+C nor a card can recover from) --
        `_quit_worker` above calls `controller.quit()` and waits on it with
        NO bound, so a genuinely hung controller (a stuck permission wait, a
        subprocess that won't die) means even double-Ctrl+C's own quit path
        never actually exits. This gives `controller.quit()` a 2s head
        start on its own daemon thread, then calls `self.exit()` regardless
        of whether it finished.

        1.0.1 fixpass finding 5: gated on its OWN `_force_quitting` flag,
        never `_quitting` -- the exact case this exists for is double-
        Ctrl+C/`/quit` having ALREADY set `_quitting` and started the
        unbounded `_quit_worker` (now hung); the old shared flag made THIS
        action a no-op in exactly that moment, the one time it's needed."""
        if self._force_quitting:
            return
        self._force_quitting = True
        self.run_worker(self._force_quit_worker, thread=True, exclusive=True, name="force-quit",
                         group="force-quit")

    def _force_quit_worker(self) -> None:
        import threading
        result = {"code": 0}

        def _run_quit() -> None:
            quit_fn = getattr(self.controller, "quit", None)
            try:
                result["code"] = quit_fn() if callable(quit_fn) else 0
            except Exception:
                import logging
                logging.getLogger("bridge").exception("controller.quit() raised during force-quit")

        t = threading.Thread(target=_run_quit, daemon=True)
        t.start()
        t.join(timeout=2.0)  # deliberately never longer -- see action_force_quit's docstring
        message = self._build_scrollback_message()
        self.call_from_thread(self.exit, return_code=result["code"], message=message)
        # 1.0.1 fixpass finding 5: self.exit() alone is not a guarantee --
        # it goes through Textual's own asyncio.run(...), whose
        # shutdown_default_executor() joins every thread=True worker still
        # alive (a hung _quit_worker from an EARLIER double-Ctrl+C/`/quit`,
        # or an unrelated `/models refresh`/`/improve` worker mid-request)
        # before the process can actually exit -- forever on 3.10/3.11, up
        # to 300s on 3.12+. A daemon timer force-kills the whole process
        # 2.5s later regardless of what Textual/asyncio is still waiting on.
        timer = threading.Timer(2.5, os._exit, args=(result["code"],))
        timer.daemon = True
        timer.start()

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
