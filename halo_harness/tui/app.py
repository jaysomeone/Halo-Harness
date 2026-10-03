"""halo_harness.tui.app -- `BridgeApp`, the full-screen Textual UI (D-TUI
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
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from halo_harness import events as ev
from halo_harness.tui import keys as tui_keys
from halo_harness.tui import theme as tui_theme
from halo_harness.tui.dispatch import apply_event
from halo_harness.tui.events import drain_queue
from halo_harness.tui.keys import DOUBLE_CTRL_C_WINDOW_S, DRAIN_HZ, next_mode
from halo_harness.tui.widgets.input import CompletionPopup, PromptInput
from halo_harness.tui.widgets.pending_dock import PendingDock
from halo_harness.tui.widgets.statusbar import StatusBar
from halo_harness.tui.widgets.transcript import IntroLine, Transcript
from halo_harness.tui.widgets.whichkey import WhichKeyOverlay

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

_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #(\d+) \+\d+ lines\]")

# Halo 2.0.1 W2b (liveness-tips-brief Part B2): "every 15s while the input
# is empty and no turn is running" -- a module-level seam (same convention
# as HANG_HEARTBEAT_THRESHOLD_S just below) a test shrinks before
# constructing/mounting the app, so "idle rotation" doesn't need a real
# 15s wait.
TIP_ROTATE_INTERVAL_S = 15.0

# H15 Part B: hang diagnostics. `_tick_spinner` (set_interval(1.0, ...), see
# on_mount) bumps the heartbeat every second; a SEPARATE OS thread (never
# the asyncio loop itself -- the whole point is to keep working when THAT
# is the thing that's stuck) polls it independently and dumps diagnostics
# once it goes stale past this threshold, at most once a minute while it
# persists (so a long-wedged session doesn't fill the disk with repeats).
HANG_HEARTBEAT_THRESHOLD_S = 15.0
HANG_DUMP_MIN_INTERVAL_S = 60.0
# 1.0.1 part 2 fixpass finding 14: at most this many hang dumps per process
# (a long-lived session that keeps tripping the watchdog must not fill the
# disk), and at most this many hang-*.log files kept on disk across every
# past process's runs (pruned once, at startup).
MAX_HANG_DUMPS_PER_PROCESS = 5
MAX_HANG_DUMP_FILES_KEPT = 10
_DEBUG_LOG = logging.getLogger("halo_harness.tui")

# 1.0.1 part 2 fixpass finding 17: `--debug`'s own per-keystroke trace
# (`_on_key` below) must never be able to reconstruct a typed secret from
# bridge.log -- a bare printable character (a letter/digit/punctuation key,
# whatever a live terminal happens to name it) logs as this fixed
# placeholder; a named control/navigation key (Enter, Tab, arrows, a
# ctrl+/alt+/shift+ chord, a function key, ...) logs verbatim, exactly as
# before, since none of those can themselves spell out a password/token.
_DEBUG_KEY_PLACEHOLDER = "<char>"
_DEBUG_SAFE_KEY_NAMES = frozenset({
    "enter", "tab", "escape", "backspace", "delete", "insert", "space",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
})
_DEBUG_SAFE_KEY_PREFIXES = ("ctrl+", "alt+", "shift+")


def _debug_key_repr(key: str) -> str:
    """See `_DEBUG_KEY_PLACEHOLDER`'s own module-level comment."""
    if key in _DEBUG_SAFE_KEY_NAMES or key.startswith(_DEBUG_SAFE_KEY_PREFIXES):
        return key
    return _DEBUG_KEY_PLACEHOLDER


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
    TITLE = "halo"

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
        # W4c item 2: "Y for the whole transcript of the current turn" --
        # deliberately NOT priority=True (unlike the chord-prefix/quit keys
        # above, which all need to win over a TextArea's own key handling):
        # a bare capital "Y" must still just TYPE while the prompt input has
        # focus (someone writing "Yes"/"Yesterday"/...). Non-priority
        # bindings resolve from the focused widget up to the App, so
        # PromptInput's own printable-character handling already wins
        # whenever it has focus; `check_action` below additionally vetoes
        # this ONE action in that case, as a second, explicit guard (belt-
        # and-suspenders, same philosophy as the clipboard fallback).
        Binding("Y", "copy_turn", "Copy turn", show=False),
    ]

    def __init__(self, controller, *, registry=None, facade=None, tool_registry=None,
                 cwd: Optional[Path] = None, theme_name: Optional[str] = None,
                 tui_setting: Optional[str] = None, initial_prompt: Optional[str] = None,
                 initial_resume_filter: Optional[str] = None, no_inline_images: bool = False,
                 show_intro: bool = False) -> None:
        # `App.__init__` itself calls `get_css_variables()` (to build its
        # initial stylesheet) before returning -- `theme_name` must exist
        # on `self` BEFORE `super().__init__()` runs, not after.
        self.theme_name = theme_name or tui_theme.DEFAULT_THEME
        super().__init__()
        # 2.0.0 Launch intro: defaults OFF (never auto-detected here) so
        # every existing/future test that constructs a BridgeApp directly
        # without passing this is completely unaffected -- `tui/launch.py`'s
        # `run_tui` is the only real caller that computes the actual
        # tty/--no-intro/config-gated value and passes it explicitly; a
        # pilot that wants to exercise the intro passes `show_intro=True`
        # itself. `self.intro_line` is the currently-typing widget (or None
        # once finished/never shown) -- `_on_key`/`_submit_prompt` skip it.
        self.show_intro = show_intro
        self.intro_line: Optional[IntroLine] = None
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
        # ("~/.halo/config.json"'s "images" key) and `--no-inline-
        # images` together into "inline"|"caption"; `image_protocol` is the
        # one live detection pass (env + tmux passthrough + a real DA1
        # terminal query, all best-effort/never-raising) a real interactive
        # run needs -- `sys.stdout.isatty()` is naturally False under
        # `app.run_test()`'s headless pilot driver and in any piped/
        # captured context, so this is "none" (never touches a real
        # terminal) throughout the whole test suite with zero special-
        # casing needed there.
        from halo_harness.theme import get_config_value
        from halo_harness.tui.images import detect_image_protocol, effective_render_mode
        self.images_render_mode = effective_render_mode(
            get_config_value("images", None), no_inline_flag=no_inline_images)
        self.image_protocol = (
            detect_image_protocol(dict(os.environ), isatty=sys.stdout.isatty())
            if self.images_render_mode == "inline" else "none")

        # Halo 2.0.1 W2b (liveness-tips-brief Part B): the rotating-tip
        # placeholder -- `None` (the off switch, `tips: false`/`HALO_
        # TIPS=0`, OR simply no applicable tip exists at all) means the
        # STATIC placeholder is shown instead, computed fresh every time
        # from `_current_placeholder_text` rather than stored once. B2:
        # "never change while the input has text or while a turn runs" --
        # `_turn_running` is the one flag `_submit_prompt`/`on_turn_done`
        # both maintain for exactly that gate (see `_maybe_rotate_tip`).
        from halo_harness.tui import tips as tips_mod
        self._tip_rotator: "Optional[tips_mod.TipRotator]" = None
        if tips_mod.tips_enabled():
            pool = tips_mod.all_applicable_tips(self.registry, facade=self.facade)
            if pool:
                self._tip_rotator = tips_mod.TipRotator(pool)
        self._turn_running = False

        self._local_events: "queue.Queue" = queue.Queue()
        self.verbose = False
        self.pending_card = None
        # Halo 2.0.1 W3a (finding 16 / PendingDock): a FIFO of cards
        # waiting BEHIND whatever `self.pending_card` currently is -- a
        # second concurrent ask (another sub-agent, or a rare second
        # top-level one) no longer overwrites it; it waits its turn,
        # activated in `clear_pending_card` once the current one resolves.
        # `_pending_markers` is the matching `id(card) -> SystemNote`
        # transcript marker each queued/active card gets the MOMENT it
        # arrives (see `enqueue_pending_card`) -- popped and rewritten to
        # the card's own `decision_line` once it's actually answered.
        self._pending_queue: "list" = []
        self._pending_markers: "dict" = {}
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
        # H15 Part B: hang watchdog -- see on_mount/_watchdog_loop.
        self._last_heartbeat_monotonic = time.monotonic()
        self._watchdog_last_dump_at = 0.0
        self._watchdog_thread: Optional[threading.Thread] = None
        # 1.0.1 part 2 fixpass finding 14: paused around a KNOWN, genuinely
        # blocking main-thread call (`self.suspend()`, Ctrl+E/`/improve`'s
        # own edit action) -- see `_enter_suspend_for_editor`. Capped so a
        # long-lived session that keeps tripping the watchdog never fills
        # the disk with dumps.
        self._watchdog_paused = False
        self._watchdog_dump_count = 0

    # ---- Halo 2.0.1 W2b: rotating tip placeholder (liveness-tips-brief
    # Part B2) ------------------------------------------------------------

    def _current_placeholder_text(self) -> str:
        from halo_harness.tui import tips as tips_mod
        if self._tip_rotator is None or self._tip_rotator.current is None:
            return tips_mod.STATIC_PLACEHOLDER
        prompt_input = getattr(self, "prompt_input", None)
        width = prompt_input.size.width if prompt_input is not None else 0
        return tips_mod.format_tip_placeholder(self._tip_rotator.current.text, width or 80)

    def _refresh_tip_placeholder(self) -> None:
        prompt_input = getattr(self, "prompt_input", None)
        if prompt_input is not None:
            prompt_input.placeholder = self._current_placeholder_text()

    def _maybe_rotate_tip(self) -> None:
        """B2: the 15s idle-rotation timer AND `on_turn_done`'s own
        after-turn rotation both call this -- "never change while the
        input has text or while a turn runs" is enforced HERE, once, for
        both triggers, rather than duplicated at each call site."""
        if self._tip_rotator is None:
            return
        prompt_input = getattr(self, "prompt_input", None)
        if self._turn_running or (prompt_input is not None and prompt_input.text):
            return
        self._tip_rotator.advance()
        self._refresh_tip_placeholder()

    # ---- composition -------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript()
        yield CompletionPopup()
        yield WhichKeyOverlay()
        # Halo 2.0.1 W3a: PendingDock -- "nothing that waits for the user
        # may be off-screen". A NORMAL (non-docked) flow child, placed
        # right here between Transcript (height: 1fr, grows to fill
        # whatever's left) and `bottom-dock` below (dock: bottom, out of
        # normal flow entirely) -- this always ends up sitting directly
        # above the prompt row, hidden (zero height) until a card actually
        # needs it. See `enqueue_pending_card`/`clear_pending_card` below.
        yield PendingDock(id="pending-dock")
        # One bottom-docked container holds the prompt row ABOVE the status
        # bar. Docking both widgets to the bottom edge separately made
        # Textual overlap them: the status bar (composed last, height 1)
        # painted over the prompt row's input line, so the TUI showed a
        # separator and a status bar and nothing to type into (found live
        # on a Kali box; the pilots never checked geometry).
        with Vertical(id="bottom-dock"):
            with Horizontal(id="prompt-row"):
                yield Static("❯", id="prompt-glyph")
                yield PromptInput(placeholder=self._current_placeholder_text())
            yield StatusBar(cwd=str(self.cwd))

    def get_css_variables(self) -> dict:
        variables = dict(super().get_css_variables())
        variables.update(tui_theme.variables_for(self.theme_name))
        return variables

    def check_action(self, action: str, parameters: "tuple") -> "bool | None":
        """W4c item 2: the explicit guard `Y`'s own BINDINGS comment above
        promises -- `copy_turn` is disabled (never even attempted) while
        the prompt input has focus, so Textual's own non-priority binding
        resolution falls through to PromptInput's ordinary printable-
        character handling instead (a capital "Y" is typed, not copied).
        Every other action is unaffected (the default `True` every widget
        already gets from `DOMNode.check_action`)."""
        if action == "copy_turn" and self.screen.focused is self.prompt_input:
            return False
        return True

    async def on_mount(self) -> None:
        # 2.0.1 launch-hang investigation: scheduled FIRST (before any of
        # this method's own work below), so it fires right after the
        # NEXT real screen refresh -- the closest this app can get to a
        # true "first paint" timestamp without framework surgery. A no-op
        # unless `--debug` was passed (debug_timeline.mark's own contract).
        from halo_harness import debug_timeline
        self.call_after_refresh(lambda: debug_timeline.mark("first paint"))
        self.transcript = self.query_one(Transcript)
        self.completion_popup = self.query_one(CompletionPopup)
        self.which_key = self.query_one(WhichKeyOverlay)
        self.pending_dock = self.query_one(PendingDock)
        self.prompt_input = self.query_one(PromptInput)
        self.status_bar = self.query_one(StatusBar)
        # B2: the compose()-time placeholder was fitted against a guessed
        # width (prompt_input.size isn't real until after the first layout
        # pass) -- recompute now that it is, and start the 15s idle-
        # rotation timer (a no-op call every tick when tips are off/empty,
        # same "cheap to call unconditionally" convention as the drain
        # timer's own liveness ticks).
        self._refresh_tip_placeholder()
        self.set_interval(TIP_ROTATE_INTERVAL_S, self._maybe_rotate_tip)
        # 2.0.0 Launch intro: mounted FIRST, before anything else below --
        # including `--continue`/`--resume`'s own replayed transcript,
        # which only ever starts arriving once `starter()`/the initial-
        # prompt/resume-filter branches further down actually run -- so
        # this is always the dim first entry of the session, never racing
        # real content for that spot.
        if self.show_intro:
            from halo_harness import __version__ as _halo_version
            self.intro_line = IntroLine(f"I am just a copy, of a copy, of a copy... halo {_halo_version}")
            await self.transcript._mount_tracked(self.intro_line)
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
        # H15 part 2 addendum 3.2a: the SAME staleness-gated, every-
        # enabled-provider catalog refresh `/model` triggers on open also
        # runs once at launch -- a provider set up with just a key/token
        # (no `halo init` ever run) still gets a real catalog before
        # the FIRST time `/model` is opened, not only after.
        self.run_worker(self._catalog_startup_refresh_worker, thread=True, name="catalog-startup-refresh",
                         group="catalog-startup-refresh")
        # H15 part 2 addendum 4: the OpenRouter balance status-bar segment --
        # one fetch now (force=True, the launch case), then again every
        # BALANCE_REFRESH_INTERVAL_S (5 minutes) for as long as the app runs;
        # a no-op when OpenRouter isn't enabled/configured.
        self.run_worker(self._or_balance_startup_refresh_worker, thread=True, name="or-balance-startup",
                         group="or-balance-startup")
        from halo_harness.providers.openrouter_account import BALANCE_REFRESH_INTERVAL_S
        self.set_interval(BALANCE_REFRESH_INTERVAL_S, self._or_balance_refresh)
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
        # H15 Part B: a lambda, not the bound method itself -- `set_interval`
        # stores whatever callable it's given and keeps calling THAT object
        # forever, so passing the bound method directly would freeze in the
        # method that existed at mount time; a test that monkeypatches the
        # INSTANCE attribute `_tick_spinner` (to simulate a stalled heartbeat
        # without a real 15s+ block) needs this timer to keep re-reading
        # `self._tick_spinner` on every tick instead.
        self.set_interval(1.0, lambda: self._tick_spinner())
        self.set_interval(5.0, self._refresh_cwd_branch)
        self._start_watchdog()
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
            from halo_harness.tui.slash import _resume_list_worker
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
        else (`Controller.list_models()`, `init_providers.claude_login_
        available()`) only ever reads the cache this populates. Best-
        effort: a failure here just leaves the cache empty (list_models()'s
        own try/except already treats that the same as "not logged in" --
        no cc: group shown, never a crash). `refresh_cached_claude_auth_
        status()` itself skips the actual spawn entirely when `claude` is
        gateway-driven (2.0.1 launch-hang fix) -- this is the ONLY
        launch-time spawner for the TUI; headless mode has no startup
        worker of its own, since it has no UI thread to protect -- see
        `commands/builtins.py::_cmd_providers`'s own inline refresh."""
        try:
            from halo_harness.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, refresh_cached_claude_auth_status
            status = refresh_cached_claude_auth_status()
        except Exception:
            return
        # 2.0.1: `/model`'s own cc: group and `/providers`' own claude_
        # subscription row both already read this same cache fresh on
        # their NEXT open (no change needed there) -- but a session that
        # never reopens either would otherwise never learn a just-
        # discovered subscription login is now usable. One quiet, once-
        # per-launch notify, only when it actually becomes available --
        # never for "not available" (the common case on a Databricks/
        # OpenRouter-first or gateway-driven box), which must never be
        # announced as an absence on every single launch.
        available = bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)
        if available:
            self.call_from_thread(
                self.notify, "Claude subscription detected -- cc: models available (see /model).",
                title="providers", timeout=4,
            )
            # 2.0.1 part 11 (live on the Kali VM): `tui/bootstrap.py`'s
            # own background-discovery kick ran BEFORE this refresh, when
            # the cache was still empty, so `discovery_eligible()` refused
            # it and a cold-cache TUI never learned its claude.ai
            # connectors (no transcript note, MCP 0/0 for the whole
            # session). Now that the cache says claude.ai login, re-kick
            # the SAME once-per-process discovery from this worker -- still
            # off the UI thread, still the only launch-time spawner --
            # with the `on_done` bootstrap parked on the controller (adds
            # the tools to the live catalog and posts the one note).
            try:
                from halo_harness.mcp import connectors_bridge
                on_done = getattr(self.controller, "connectors_discovery_on_done", None)
                connectors_bridge.ensure_discovered_in_background(on_done=on_done)
            except Exception:
                pass

    def _catalog_startup_refresh_worker(self) -> None:
        """H15 part 2 addendum 3.2a: launch-time catalog refresh -- see
        `tui/slash.py::catalog_auto_refresh_worker`'s own docstring (the
        SAME function `/model` triggers on open); best-effort, a failure
        here just leaves whatever was already cached (or nothing) in place."""
        try:
            from halo_harness.tui.slash import catalog_auto_refresh_worker
            catalog_auto_refresh_worker(self)
        except Exception:
            pass

    def _or_balance_refresh(self) -> None:
        """H15 part 2 addendum 4: spawns the OpenRouter balance worker on
        its own thread -- same "thin method, real work on a worker"
        convention `_refresh_cwd_branch` already uses, so this is safe to
        call directly from `set_interval`."""
        self.run_worker(self._or_balance_startup_refresh_worker, thread=True, exclusive=True,
                         name="or-balance-refresh", group="or-balance-refresh")

    def _or_balance_startup_refresh_worker(self) -> None:
        try:
            from halo_harness.tui.slash import or_balance_refresh_worker
            or_balance_refresh_worker(self, force=True)
        except Exception:
            pass

    def _or_balance_turn_refresh_worker(self) -> None:
        try:
            from halo_harness.tui.slash import or_balance_refresh_worker
            or_balance_refresh_worker(self, force=False)
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        # H15 Part B: the heartbeat the watchdog thread polls -- this timer
        # only fires at all while the asyncio event loop is itself still
        # alive and responsive, so its own staleness IS the hang signal.
        self._last_heartbeat_monotonic = time.monotonic()
        self.status_bar.tick_spinner()

    # ---- H15 Part B: hang watchdog -----------------------------------

    def _start_watchdog(self) -> None:
        """A daemon thread, started once from `on_mount`, completely
        independent of the asyncio event loop/Textual message pump -- the
        whole point is that it keeps polling even when THOSE are what's
        stuck. SIGUSR1 (POSIX only) dumps the same diagnostics on demand,
        best-effort (never raises -- some environments refuse a non-main-
        thread signal registration, or don't have SIGUSR1 at all)."""
        if self._watchdog_thread is not None:
            return
        self._prune_old_hang_dumps()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="rolo-watchdog")
        self._watchdog_thread.start()
        try:
            import signal
            if hasattr(signal, "SIGUSR1"):
                signal.signal(signal.SIGUSR1, self._on_sigusr1)
        except (ValueError, OSError, AttributeError):
            pass  # not the main thread, not POSIX, or a platform that refuses it -- the timer-based watchdog still runs

    def _on_sigusr1(self, signum, frame) -> None:
        # 2.0.1 finding 25: once MAX_HANG_DUMPS_PER_PROCESS is reached,
        # `_dump_hang_diagnostics` below silently no-ops -- fine for the
        # heartbeat path (nobody is watching for it in real time), but a
        # user who deliberately sends SIGUSR1 (expecting an on-demand dump)
        # deserves to know why nothing happened, rather than wondering if
        # the signal was even delivered. One line per signal received while
        # capped (never the dump itself, which stays capped).
        if self._watchdog_dump_count >= MAX_HANG_DUMPS_PER_PROCESS:
            logging.getLogger("bridge").warning(
                "SIGUSR1 received but the per-process hang-dump cap (%d) was already reached -- "
                "no new dump written", MAX_HANG_DUMPS_PER_PROCESS)
            return
        self._dump_hang_diagnostics(time.monotonic() - self._last_heartbeat_monotonic, reason="SIGUSR1")

    def _prune_old_hang_dumps(self) -> None:
        """1.0.1 part 2 fixpass finding 14: keeps at most `MAX_HANG_DUMP_
        FILES_KEPT` `hang-*.log` files on disk, pruned once at startup --
        filenames are `hang-<UTC %Y%m%dT%H%M%SZ>.log`, so lexical sort order
        IS chronological order; best-effort, never raises."""
        try:
            from halo_harness.config.paths import bridge_home
            state_dir = bridge_home()
            dumps = sorted(state_dir.glob("hang-*.log"))
            for p in dumps[:-MAX_HANG_DUMP_FILES_KEPT] if len(dumps) > MAX_HANG_DUMP_FILES_KEPT else []:
                try:
                    p.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    def _enter_suspend_for_editor(self) -> None:
        """1.0.1 part 2 fixpass finding 14: pauses the hang watchdog around
        a KNOWN, genuinely blocking main-thread call -- `self.suspend()`
        (Ctrl+E here, `/improve`'s own edit action in tui/slash.py) stops
        the whole asyncio loop (and therefore `_tick_spinner`'s own
        heartbeat) for as long as the external $VISUAL/$EDITOR runs, which
        is expected, not a hang. Call `_exit_suspend_for_editor` in a
        `finally` right after -- both are safe to call unconditionally
        around any future blocking-editor-style call."""
        self._watchdog_paused = True

    def _exit_suspend_for_editor(self) -> None:
        self._watchdog_paused = False
        self._last_heartbeat_monotonic = time.monotonic()

    def _watchdog_loop(self) -> None:
        # A short poll interval (well under the 15s threshold) so a hang
        # that starts and ends quickly (a brief GC pause, a slow-but-
        # finite MCP call) is never mistaken for a real stall; checked
        # against wall-clock monotonic time, never counted in "ticks",
        # so this is correct regardless of how long each poll itself takes.
        # `_watchdog_stop.wait(2.0)` (never a bare `time.sleep`) so a
        # deliberate stop (app shutdown) is noticed within this same poll,
        # not up to 2s late.
        while not self._watchdog_stop.wait(2.0):
            if self._quitting or self._force_quitting or not self.is_running:
                return  # shutting down (on purpose, or already gone) -- not a hang, stop watching
            if self._watchdog_paused:
                continue  # finding 14: a KNOWN blocking call is in progress -- not a hang either
            age = time.monotonic() - self._last_heartbeat_monotonic
            if age < HANG_HEARTBEAT_THRESHOLD_S:
                continue
            now = time.monotonic()
            if now - self._watchdog_last_dump_at < HANG_DUMP_MIN_INTERVAL_S:
                continue  # already dumped recently -- at most once a minute while it persists
            self._watchdog_last_dump_at = now
            self._dump_hang_diagnostics(age, reason="heartbeat stalled")

    def _named_worker_summary(self) -> "list[str]":
        try:
            return [f"{w.name or '?'} (group={w.group}, state={w.state.name})" for w in self.workers]
        except Exception as e:
            return [f"<could not list workers: {type(e).__name__}: {e}>"]

    def _active_screen_name(self) -> str:
        try:
            return type(self.screen).__name__
        except Exception:
            return "?"

    def _dump_hang_diagnostics(self, heartbeat_age: float, *, reason: str) -> None:
        """Writes every thread's stack (`sys._current_frames()`) plus the
        named-worker list and the active screen name to `~/.halo/
        hang-<UTC>.log`, and one line to bridge.log naming that path --
        called from the watchdog thread OR a SIGUSR1 handler on the main
        thread, never from the asyncio loop itself. Best-effort throughout
        (a hang dump that itself raises must never take the process down
        with it, nor ever block long enough to matter).

        1.0.1 part 2 fixpass finding 14: capped at `MAX_HANG_DUMPS_PER_
        PROCESS` -- a long-wedged session that keeps re-triggering this
        (SIGUSR1 has no built-in rate limit of its own, unlike the
        heartbeat path's `HANG_DUMP_MIN_INTERVAL_S`) must not fill the disk
        with an unbounded number of these over a long process lifetime."""
        if self._watchdog_dump_count >= MAX_HANG_DUMPS_PER_PROCESS:
            return
        self._watchdog_dump_count += 1
        import traceback as tb_mod
        from datetime import datetime, timezone
        try:
            from halo_harness.config.paths import bridge_home
            lines = [
                f"halo hang diagnostics -- {reason} "
                f"(heartbeat stalled {heartbeat_age:.1f}s, threshold {HANG_HEARTBEAT_THRESHOLD_S:.0f}s)",
                f"active screen: {self._active_screen_name()}",
                "named workers:",
            ]
            worker_lines = self._named_worker_summary()
            if worker_lines:
                lines.extend(f"  {w}" for w in worker_lines)
            else:
                lines.append("  (none)")
            lines.append("")
            thread_names = {t.ident: t.name for t in threading.enumerate()}
            for ident, frame in sys._current_frames().items():
                lines.append(f"--- thread {thread_names.get(ident, '?')} (ident={ident}) ---")
                lines.extend(line.rstrip("\n") for line in tb_mod.format_stack(frame))
                lines.append("")
            text = "\n".join(lines)
            state_dir = bridge_home()
            state_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            hang_path = state_dir / f"hang-{ts}.log"
            hang_path.write_text(text, encoding="utf-8")
        except Exception as e:
            logging.getLogger("bridge").warning("hang diagnostics: could not write the dump (%s: %s)",
                                                  type(e).__name__, e)
            return
        logging.getLogger("bridge").warning(
            "hang detected (%s): heartbeat stalled %.1fs -- dumped to %s", reason, heartbeat_age, hang_path)

    def _debug_trace(self, msg: str, *args) -> None:
        """H15 Part B: `--debug` tracing -- a no-op cost-wise when the
        `halo_harness.tui` logger isn't at DEBUG level (stdlib logging's own
        lazy %-formatting, same convention `clamp_effort`'s own `log.debug`
        already uses), real lines in bridge.log once `--debug` turns it on."""
        _DEBUG_LOG.debug(msg, *args)

    # ---- H15 Part B: Textual's own worker/focus lifecycle events, traced
    # under --debug only (see _debug_trace) ------------------------------

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        worker = event.worker
        if event.state == WorkerState.RUNNING:
            self._debug_trace("worker start: %s (group=%s)", worker.name, worker.group)
        elif event.state == WorkerState.CANCELLED:
            self._debug_trace("worker cancel: %s (group=%s)", worker.name, worker.group)
        elif event.state in (WorkerState.SUCCESS, WorkerState.ERROR):
            self._debug_trace("worker finish: %s (group=%s, state=%s)", worker.name, worker.group, event.state.name)

    def on_app_blur(self, event) -> None:
        self._debug_trace("AppBlur (screen=%s)", self._active_screen_name())

    def on_app_focus(self, event) -> None:
        self._debug_trace("AppFocus (screen=%s)", self._active_screen_name())

    # ---- the drain loop (D-TUI: "30 Hz ... 8ms budget") -------------------

    async def _drain(self) -> None:
        """H15 Part B.3 audit (no behaviour change -- nothing unbounded was
        found): `source.get_nowait()` never blocks; `drain_queue`/
        `apply_event` do no I/O of their own; every `await
        self.transcript.mount_widget(...)` this method's own `apply_event`
        calls reach (permission/plan/question cards, tool-call diff
        previews) mounts onto the TRANSCRIPT on the BASE screen -- never a
        NEW screen pushed here -- so it never contends with an already-
        active MODAL screen's own event loop turn; Textual's `Widget.mount`
        is itself a bounded layout operation, not a wait on anything
        external. Every `call_from_thread` call site in this module (26 at
        last count) hands back a plain attribute write/`Static.update`/
        `push_screen` -- none of them call `Worker.wait()`, acquire a lock
        shared with the session's own worker thread, or run a subprocess
        with no timeout (the two `subprocess.run` calls in this file,
        `_git_branch`/`_notify_send_worker`, both pass `timeout=` and both
        run on their OWN `thread=True` worker, never here). The watchdog
        above (`_start_watchdog`) is the backstop for whatever this audit
        missed."""
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
        # Halo 2.0.1 W2b (liveness-tips-brief Part A1/A4/A5): "something
        # visible changes every second while a turn runs" -- the elapsed/
        # no-data text on every live phase line, running tool card and
        # sub-agent card is recomputed EVERY drain tick, unconditionally,
        # never only in reaction to a new event (`real_events` above can
        # be empty while the model is silently thinking -- that's exactly
        # the case this is for). Each is a no-op render-wise unless the
        # computed text actually changed (its own `_last_rendered` cache),
        # so this costs nothing extra on an idle session (no live widgets
        # at all -- every dict this walks is simply empty).
        self.transcript.tick_phase_lines()
        self.transcript.tick_tool_cards()
        self.transcript.tick_subagent_cards()

    def on_turn_done(self, reason: str) -> None:
        if reason == "interrupted":
            self.notify("Interrupted.", timeout=2)
        # B2: "the next tip ... at every turn_done" -- `_maybe_rotate_tip`
        # itself still gates on the input being empty (a steer draft the
        # user typed mid-turn and hasn't submitted yet).
        self._turn_running = False
        self._maybe_rotate_tip()
        # U5 scope C: "session titles via the small model after the first
        # turn" -- kicked off exactly once, off the UI thread (a small-
        # model call is network I/O); `Controller.maybe_autoname_title`
        # itself is a no-op once a title already exists, so a resumed
        # session (already titled) never overwrites it.
        self._turn_done_count += 1
        if self._turn_done_count == 1 and callable(getattr(self.controller, "maybe_autoname_title", None)):
            self.run_worker(self._autoname_worker, thread=True, name="autoname-title")
        # H15 part 2 addendum 4: a post-turn OpenRouter balance refresh --
        # the worker itself is a no-op unless OpenRouter is actually enabled
        # AND configured, and self-debounces to at most once every
        # BALANCE_POST_TURN_DEBOUNCE_S regardless of how many turns fire
        # this in between, so firing it after every turn unconditionally
        # (rather than threading "did THIS turn use or:" through the whole
        # event pipeline) costs nothing extra on a non-OpenRouter session.
        self.run_worker(self._or_balance_turn_refresh_worker, thread=True, name="or-balance-turn-refresh",
                         group="or-balance-turn-refresh")

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
        from halo_harness import __version__
        from halo_harness.statusline import build_payload, run_statusline_command

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
                # W2c item 1: this slash command used to bypass history
                # entirely (only the DEFAULT path at the bottom of this
                # method, via `_submit_prompt`, ever appended) -- Up now
                # recalls a command typed while a card was borrowing input
                # the same as any other submission.
                self._record_submitted_history(text, event.pasted)
                from halo_harness.tui.slash import handle_slash
                name, _, args = stripped[1:].partition(" ")
                await handle_slash(self, name, args)
                return
            card = self._borrowing_card
            self._borrowing_card = None
            self.prompt_input.clear_submitted()
            self.prompt_input.placeholder = self._current_placeholder_text()
            # finding 14: expanded, never the raw "[Pasted text #n ...]"
            # placeholder -- same reasoning as review finding 4's own fix
            # for the ordinary prompt path just below.
            card.resolve_with_message(_expand_pasted(text, event.pasted))
            return
        if not text.strip():
            return
        self.prompt_input.clear_submitted()
        self.completion_popup.hide()
        from halo_harness.tui.widgets.cards import EffortCard
        if self.pending_card is not None and not isinstance(self.pending_card, EffortCard):
            # finding 14: the /effort selector does not intercept typed
            # text at all -- it falls straight through to the ordinary
            # path below (slash handling, history, a fresh prompt), same
            # as if nothing were pending; every OTHER card still gates
            # here exactly as before.
            if is_slash_command:
                # W2c item 1: same history gap as the borrow-card branch
                # above -- a slash command typed while a permission/plan/
                # question card is pending used to never reach history.
                self._record_submitted_history(text, event.pasted)
                from halo_harness.tui.slash import handle_slash
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
            # before this fix. W2c item 1: THIS steer path (unlike the
            # common mid-turn steer, which reaches the default path below
            # via `_submit_prompt`) used to skip history entirely -- Up
            # must still recall it.
            self._record_submitted_history(text, event.pasted)
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

    def _record_submitted_history(self, text: str, pasted: "dict | None") -> None:
        """W2c item 1: append to `~/.halo/history.jsonl` and invalidate the
        Up-arrow cache -- called from EVERY place text the user typed
        leaves the prompt box as a real submission (a fresh prompt via
        `_submit_prompt` below, a slash command typed while a card is
        borrowing/pending, or a steer on the turn running underneath a
        non-answerable card), not only the default path, so Up always
        recalls the exact thing just typed regardless of which of those
        paths it went through. The raw (unexpanded) `text` is stored --
        same convention `_submit_prompt` always used: a paste placeholder
        stays a placeholder in history/the transcript, only the MODEL ever
        sees the expanded form."""
        from halo_harness import history as history_mod

        try:
            history_mod.append_history_entry(text, str(self.cwd), pasted_contents=pasted or None)
        except OSError:
            pass
        self._history_cache = []  # re-read next Up-arrow, this entry is now in it

    async def _submit_prompt(self, text: str, pasted: dict) -> None:
        # 2.0.0 Launch intro: "a submitted prompt finishes the line
        # instantly" -- covers the one case a keypress alone wouldn't (the
        # `--cwd`/positional PROMPT auto-submitted at startup via
        # `_initial_prompt`, with no user keystroke at all); harmless no-op
        # once already done/never shown.
        if self.intro_line is not None and not self.intro_line.done:
            self.intro_line.skip()
        self._record_submitted_history(text, pasted)
        stripped = text.strip()
        # U5 scope A: `!cmd` runs a shell command inline, through the Bash
        # tool + permissions, OUTSIDE the model loop entirely -- checked
        # BEFORE the "/" branch (a bare "!" line is never a slash command).
        if stripped.startswith("!") and "\n" not in stripped:
            await self._handle_bang_command(stripped[1:].strip())
            return
        if stripped.startswith("/") and "\n" not in stripped:
            from halo_harness.tui.slash import handle_slash

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
        # B2: "never change [the tip] while ... a turn runs" -- set ONLY
        # here (a real model turn), never for the "!"/"/" branches above
        # (which return early and may never fire a matching `turn_done` at
        # all -- this flag would otherwise freeze tip rotation forever).
        self._turn_running = True
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
        from halo_harness.tui.widgets.cards import PermissionCard

        request_id = f"inline-{id(command)}"

        def on_decide(reply: dict) -> None:
            # Part D2.3: discard the slot the SAME closure's slot-
            # registration below added -- a no-op if `reevaluate_pending_
            # permission` already resolved (and left in place) a since-
            # replaced slot; either path ends here exactly once (`done`
            # guards both PermissionCard entry points).
            discard = getattr(self.controller, "discard_pending_permission", None)
            if callable(discard):
                try:
                    discard(request_id)
                except Exception:
                    pass
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

        # Part D2.3: registered so Shift+Tab/`/permissions` can re-decide
        # THIS ask too, through the exact same `reevaluate_pending_
        # permission` path a live tool-call ask already uses -- without
        # this, a mode change while an inline `!cmd` card is up left it
        # completely unaffected (a plain digit press was the only way to
        # answer it, unlike every model-issued permission ask).
        register = getattr(self.controller, "register_pending_permission", None)
        if callable(register):
            try:
                register(request_id, "Bash", {"command": command})
            except Exception:
                pass

        card = PermissionCard(request_id=request_id, summary=f"Bash({command})",
                               reason=getattr(decision, "reason", "") or "runs now, outside the model turn (! prefix)",
                               suggested_rule=getattr(decision, "suggested_rule", None), on_decide=on_decide,
                               on_resolved_externally=on_decide)
        # Halo 2.0.1 W3a (finding 16 / PendingDock): same queued path every
        # other permission/question/plan ask goes through now.
        await self.enqueue_pending_card(card, marker_text=f"⏸ permission needed for Bash({command}), see below")

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
        from halo_harness.tui.dialogs.palette import CommandPalette

        items = self._build_palette_items()
        self.call_from_thread(self.push_screen, CommandPalette(items), self._on_palette_pick)

    def _build_palette_items(self) -> "list[dict]":
        items: "list[dict]" = []
        if self.registry is not None:
            for cmd in self.registry.all():
                kind = "skill" if cmd.source == "skill" else "command"
                items.append({"kind": kind, "label": f"/{cmd.name}", "detail": cmd.description, "value": cmd.name})
        try:
            from halo_harness.tui.completion import complete_at_path
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
        from halo_harness.tui.slash import handle_slash
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
        fd, tmp_path_str = tempfile.mkstemp(suffix=".md", prefix="halo-")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.prompt_input.text)
            # 1.0.1 part 2 fixpass finding 14: pause the hang watchdog for
            # as long as the external editor blocks the main thread --
            # otherwise editing for more than HANG_HEARTBEAT_THRESHOLD_S
            # (15s) writes a hang dump + WARNING every minute it stays open.
            self._enter_suspend_for_editor()
            try:
                with self.suspend():
                    sp.run(f'{editor} "{tmp_path}"', shell=True)
            finally:
                self._exit_suspend_for_editor()
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
        # W2c item 2: bare Ctrl+X is normally the chord PREFIX (session
        # export/rename/fork/... -- DEFAULT_KEYBINDINGS), caught here before
        # PromptInput ever sees it (priority=True on the App's own
        # Binding) -- exactly why plain Ctrl+X never cut a selection in the
        # chat box. A selection there means the user wants to CUT it
        # instead (same priority Ctrl+C's own fix above gives copying over
        # the ordinary interrupt/quit path); `TextArea.action_cut` already
        # deletes the selection and copies it via `self.app.copy_to_
        # clipboard` -- overridden above (W4c item 3) with the OSC-52-trust
        # gate and the external-tool fallback, so nothing further is needed
        # here beyond the friendly notice.
        if self.screen.focused is self.prompt_input and self.prompt_input.selected_text:
            selected = self.prompt_input.selected_text
            self.prompt_input.action_cut()
            self.notify(f"Cut {len(selected)} characters", timeout=2)
            return
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
        # 2.0.0 Launch intro: "any keypress ... completes it instantly" --
        # a side effect only, never `event.stop()`/`prevent_default()`, so
        # the SAME keystroke that skips the intro still reaches whatever
        # has focus afterward (the prompt input, from the first frame) the
        # same as it would have otherwise; `IntroLine.skip()` is a no-op
        # once already done, so this costs nothing on every ordinary key.
        if self.intro_line is not None and not self.intro_line.done:
            self.intro_line.skip()
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
        #
        # H15 Part B: --debug tracing -- every Key event, with the
        # currently-focused widget and the active screen, BEFORE the
        # self-heal/chord logic below touches either (so a trace always
        # shows the REAL pre-handling state a hang investigation needs).
        focused = self.screen.focused
        # 1.0.1 part 2 fixpass finding 17: `_debug_key_repr` -- a printable
        # character logs as a fixed placeholder, never the real key, so a
        # password/token typed while --debug is on can't be reconstructed
        # from bridge.log one keystroke at a time.
        self._debug_trace("key: %s focused=%s screen=%s", _debug_key_repr(event.key),
                           type(focused).__name__ if focused is not None else None,
                           self._active_screen_name())
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
            from halo_harness.tui.slash import handle_slash
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
        from halo_harness.tui.completion import complete_at_path, complete_slash

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
        from halo_harness import history as history_mod

        if not self._history_cache:
            try:
                entries = history_mod.load_merged_history(str(self.cwd))
            except OSError:
                entries = []
            all_displays = [e.get("display", "") for e in entries if e.get("display")]
            self._history_draft = self.prompt_input.text
            # W2c item 1: "with text typed on the first line, Up walks only
            # entries that start with that text" (Claude Code's own prefix-
            # filtered recall); "Up on an EMPTY first line recalls the
            # newest entry" -- no filter at all. The prefix is captured
            # ONCE, right here at the START of this navigation run (the
            # same moment `_history_draft` above is captured) -- walking
            # further with Up/Down never re-derives it from whatever
            # recalled text is currently showing.
            prefix = self.prompt_input.document.get_line(0)
            self._history_cache = ([d for d in all_displays if d.startswith(prefix)] if prefix
                                    else all_displays)
            self._history_index = len(self._history_cache)
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
        from halo_harness.tui.widgets.cards import PermissionCard
        if isinstance(card, PermissionCard):
            self.prompt_input.placeholder = "1-4 answers the request above, or type why not"
            self.status_bar.set_pending_permission(True)
        self._refresh_needs_you_tag()

    def _refresh_needs_you_tag(self) -> None:
        """Halo 2.0.1 W3a: `needs you · N` -- N is the active card (if any)
        plus everything still queued behind it; the tag disappears only
        once N reaches 0 (finding 16: "the status tag stays up while any
        entry remains")."""
        count = (1 if self.pending_card is not None else 0) + len(self._pending_queue)
        self.status_bar.set_needs_you(count)

    async def enqueue_pending_card(self, card, *, marker_text: str) -> None:
        """Halo 2.0.1 W3a (finding 16 / PendingDock): the ONE entry point
        `tui/dispatch.py`'s permission/question/plan handlers and `_handle_
        bang_command`'s inline-shell ask all go through now, replacing the
        old `await app.transcript.mount_widget(card); app.set_pending_card
        (card)` pair -- a queue keyed by each card's own `request_id`
        (carried on the card object itself) means a second concurrent ask
        no longer silently overwrites the first.

        The transcript ALWAYS gets a one-line marker right where the ask
        happened -- its natural position in the conversation -- whether
        this card becomes active immediately or has to wait; `clear_
        pending_card` rewrites it to the card's own `decision_line` once
        it's actually answered. The card itself only ever gets MOUNTED
        (into `self.pending_dock`) once it's actually active -- a queued
        card is tracked here but not yet a live widget anywhere."""
        marker = await self.transcript.add_note(marker_text, kind="pending-marker")
        self._pending_markers[id(card)] = marker
        if self.pending_card is None:
            await self.pending_dock.show_card(card)
            self.set_pending_card(card)
        else:
            self._pending_queue.append(card)
            self._refresh_needs_you_tag()

    async def _activate_next_pending_card(self, card) -> None:
        await self.pending_dock.show_card(card)
        self.set_pending_card(card)

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
            subprocess.run(["notify-send", "halo", "Input needed"], timeout=3,
                            capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass

    def clear_pending_card(self) -> None:
        # Halo 2.0.1 W3a (finding 16 / PendingDock): rewrite this card's own
        # transcript marker (if it went through `enqueue_pending_card`, i.e.
        # it's a permission/question/plan card -- an EffortCard/RewindCard/
        # ImproveCard, which never gets a marker, is a harmless no-op pop
        # here) to its decision line BEFORE anything else -- same
        # "⏸ ... see below" widget, now showing what was actually decided.
        finished = self.pending_card
        marker = self._pending_markers.pop(id(finished), None) if finished is not None else None
        if marker is not None:
            # W4c item 1: `set_text` (not a bare `.update()`) keeps the
            # marker's own `copy_text()` -- what a transcript copy actually
            # reads -- in sync with what's now on screen, so a selection
            # that includes this marker after the ask is answered copies
            # the DECISION line, never the stale "... needed, see below".
            marker.set_text(getattr(finished, "decision_line", None) or "(resolved)")
        self.pending_card = None
        self.prompt_input.placeholder = self._current_placeholder_text()
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
        # Finding 16: activate whatever's next in the queue (if anything) --
        # scheduled via call_later (Textual's own async-aware callback
        # scheduling) rather than awaited here, since this method is called
        # from many SYNC contexts (a card's own on_decide/on_answer/on_reply
        # closure, itself invoked from a Textual key-binding action).
        if self._pending_queue:
            nxt = self._pending_queue.pop(0)
            self.call_later(self._activate_next_pending_card, nxt)
        else:
            if marker is not None:
                # `finished` went through `enqueue_pending_card` (it's the
                # dock's own card) and nothing else is queued behind it --
                # hide the dock. An EffortCard/RewindCard/ImproveCard etc.
                # (no marker at all -- never routed through the dock) skips
                # this entirely, same as before this brief.
                self.call_later(self.pending_dock.clear)
            self._refresh_needs_you_tag()

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
        from halo_harness.permissions import SettingsWriteRefused

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
        # 2.0.1 W3a: a plain one-line toast naming what the new mode does --
        # describes behaviour only (tui/keys.py::MODE_DESCRIPTIONS), never
        # gates it.
        from halo_harness.tui.keys import MODE_DESCRIPTIONS
        self.notify(f"{new_mode}: {MODE_DESCRIPTIONS.get(new_mode, '')}", title="Mode", timeout=3)

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
        from halo_harness.tui.widgets.cards import PermissionCard
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
            # Halo 2.0.1 W3a: a permission/question/plan card now lives in
            # `self.pending_dock` (finding 16's queue), a SIBLING of the
            # transcript -- `/clear` only wipes the transcript, so that
            # card was never removed at all and must not be re-mounted a
            # second time (Textual raises on mounting an already-mounted
            # widget). An EffortCard/RewindCard/ImproveCard etc. still
            # mounts directly into the transcript (unchanged), so it DOES
            # need re-mounting here, same as before this brief.
            if getattr(card, "parent", None) is self.pending_dock:
                self.set_focus(card)
            else:
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
            self.prompt_input.placeholder = self._current_placeholder_text()
            card.resolve_with_message("")
            return
        if self.pending_card is not None:
            return  # the focused card's own Esc binding handles it
        self.controller.interrupt()

    def copy_to_clipboard(self, text: str) -> None:
        """W4c item 3: OSC 52 is only EMITTED when this terminal is known to
        relay it (`clipboard.osc52_trusted()` -- Windows Terminal yes, a
        plain `conhost` console host no). Overridden at the APP level
        (rather than gating each of our own call sites individually) so
        Textual's OWN internal callers go through the exact same check --
        `TextArea`/`Input`'s native cut/copy key bindings and `Screen.
        action_copy_text` all call `self.app.copy_to_clipboard(...)`
        directly, same as our own code below and in `tui/widgets/cards.py`.
        The external-tool fallback (`clip.exe` on win32, xclip/wl-copy/xsel
        on Linux, pbcopy on macOS) always ALSO runs, belt-and-suspenders,
        regardless of whether OSC 52 itself was trusted -- see tui/
        clipboard.py's own module docstring for why two mechanisms exist."""
        from halo_harness.tui.clipboard import osc52_trusted
        if self._driver is not None and osc52_trusted():
            super().copy_to_clipboard(text)
        else:
            self._clipboard = text
        self.run_worker(lambda: self._clipboard_fallback_worker(text), thread=True, name="clipboard-fallback")

    def _clipboard_fallback_worker(self, text: str) -> None:
        from halo_harness.tui.clipboard import copy_via_external_tool
        copy_via_external_tool(text)

    def perform_copy(self, text: "Optional[str]", *, label: str) -> bool:
        """W4c item 2: the ONE place every EXPLICIT copy action (`/copy`,
        `/copy code [N]`, `/copy tool`, `y`, `Y`) sends its already-cleaned
        text to the clipboard and tells the user what happened -- `copy_to_
        clipboard` above handles OSC 52 + the external-tool fallback; this
        adds the "nothing to copy yet" / "Copied <label> (N characters)"
        half of the contract (CRLF conversion, `clipboard.crlf`, is applied
        here too -- the very last step, so the reported character count
        matches what actually lands on the clipboard). Returns whether
        anything was actually copied, so a caller with its own more
        specific "nothing yet" wording (e.g. `/copy code`'s "no code
        blocks") can show that instead of a generic one."""
        if not text:
            return False
        from halo_harness.tui.clipboard import apply_crlf_if_configured
        final_text = apply_crlf_if_configured(text)
        self.copy_to_clipboard(final_text)
        self.notify(f"Copied {label} ({len(final_text)} characters)", timeout=2)
        return True

    def _copy_text_for_widgets(self, widgets) -> "Optional[str]":
        """W4c item 1: every widget's own `copy_text()` (its stored
        source -- see each widget class for what that means to it), cleaned
        (`clean_copy_text`) and concatenated in the given order
        (`join_copied_texts`). A widget with no `copy_text` at all (a
        permission/question/plan/effort/rewind/improve card -- none of
        them ever the target of a transcript drag in practice) simply
        contributes nothing; there is no cell-reading fallback."""
        from halo_harness.tui.clipboard import clean_copy_text, join_copied_texts
        parts = []
        for widget in widgets:
            get_text = getattr(widget, "copy_text", None)
            if callable(get_text):
                parts.append(clean_copy_text(get_text()))
        return join_copied_texts(parts) or None

    def _selected_transcript_text(self) -> "Optional[str]":
        """W4c item 1: `screen.selections` is Textual's own PUBLIC
        selection map (`{widget: Selection(start, end)}`, kept live by its
        native mouse-drag handling) -- only the KEYS matter here (which
        widgets the drag touched AT ALL, even just partially); the actual
        text always comes from `_copy_text_for_widgets` above, never
        `Selection.extract`/`widget.get_selection` (the screen-CELL path
        this replaces, `Screen.get_selected_text`'s own mechanism). Ordered
        by each widget's position in the transcript, not dict order (which
        Textual gives no documented guarantee about)."""
        selected = self.screen.selections
        if not selected:
            return None
        order = self.transcript.widgets_in_order()
        ordered = [w for w in order if w in selected]
        ordered.extend(w for w in selected if w not in ordered)
        return self._copy_text_for_widgets(ordered)

    def _current_turn_text(self) -> "Optional[str]":
        """W4c item 2 (`Y`): every transcript widget from the LAST
        UserMessage onward (that prompt plus everything produced for it so
        far -- the "current turn" even while still running), same
        extraction pipeline as a multi-widget selection. No UserMessage at
        all yet (a brand-new session) falls back to the whole transcript
        so far rather than copying nothing."""
        from halo_harness.tui.widgets.transcript import UserMessage
        order = self.transcript.widgets_in_order()
        start = 0
        for i in range(len(order) - 1, -1, -1):
            if isinstance(order[i], UserMessage):
                start = i
                break
        return self._copy_text_for_widgets(order[start:])

    def action_copy_turn(self) -> None:
        if not self.perform_copy(self._current_turn_text(), label="this turn"):
            self.notify("Nothing to copy yet.", severity="warning", title="Y")

    def action_interrupt_or_quit(self) -> None:
        # W2c item 2: a selection made INSIDE the prompt TextArea (mouse
        # drag or Shift+arrows) is `prompt_input.selected_text` -- a
        # COMPLETELY different mechanism from the transcript/screen-level
        # drag-select just below, so Ctrl+C in the chat box used to fall
        # straight through to the double-press-quit arming below instead of
        # copying. Checked FIRST, and only while the prompt genuinely has
        # focus (a stale selection left over from before focus moved
        # elsewhere must never be copied). Wording kept EXACTLY as W2c left
        # it (pinned by test_ctrl_c_with_input_selection_copies_and_does_
        # not_arm_quit) -- only the mechanism it copies THROUGH changed
        # (`copy_to_clipboard` above is now OSC-52-trust-gated).
        if self.screen.focused is self.prompt_input and self.prompt_input.selected_text:
            selected = self.prompt_input.selected_text
            self.copy_to_clipboard(selected)
            self.notify(f"Copied {len(selected)} characters", timeout=2)
            return
        # W4c item 1: review finding 16's original fix here (priority
        # Ctrl+C shadowing Textual's own screen-level copy) still applies,
        # but the EXTRACTION is now `_selected_transcript_text` (each
        # touched widget's own stored source, see its docstring) rather
        # than `Screen.get_selected_text()`'s cell-by-cell walk.
        selected = self._selected_transcript_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.notify(f"Copied selection ({len(selected)} characters)", timeout=2)
            return
        from halo_harness.theme import get_config_value
        # W4c item 4: "quit_on_double_ctrl_c (default true) turns the
        # double-press exit off" -- `_ctrl_c_deadline` is simply never armed
        # when it's false, so the `now < deadline` branch below can never
        # fire from Ctrl+C again; only /exit, Ctrl+D and Ctrl+Q still quit.
        quit_on_double = bool(get_config_value("quit_on_double_ctrl_c", True))
        now = time.monotonic()
        if quit_on_double and self._ctrl_c_deadline is not None and now < self._ctrl_c_deadline:
            self._begin_quit()
            return
        self._ctrl_c_deadline = now + DOUBLE_CTRL_C_WINDOW_S if quit_on_double else None
        if self._borrowing_card is None and self.pending_card is None:
            if self.prompt_input.text:
                self.prompt_input.clear_submitted()
            else:
                self.controller.interrupt()
        if quit_on_double:
            # W4c item 4: the exact wording rolo asked for -- "I worry that
            # doing ctrl c in a windows operating system will close the
            # terminal" -- Ctrl+C here only ever closes HALO, never the
            # terminal it's running in.
            self.notify("Press Ctrl+C again to exit halo (your terminal stays open)",
                        timeout=DOUBLE_CTRL_C_WINDOW_S)
        else:
            self.notify("Interrupted (Ctrl+C never exits halo here -- use /exit, Ctrl+D or Ctrl+Q)",
                        timeout=2)

    # ---- Ctrl+V: a real system-clipboard paste (W2c item 3) --------------
    #
    # Bracketed paste (the terminal's own native paste -- Ctrl+Shift+V on
    # most Linux terminals, right-click/Ctrl+V on Windows Terminal, Shift+
    # middle-click anywhere) already reaches `PromptInput._on_paste`
    # directly and is completely unchanged by this. Plain Ctrl+V itself is
    # a DIFFERENT story: `PromptInput.action_paste` posts `PasteRequested`
    # (never pastes Textual's own in-process `app.clipboard` register,
    # which nothing here ever populates from the real OS clipboard) and
    # this handler reads the REAL system clipboard off the UI thread
    # (`tui/clipboard.py`'s read-direction helper -- a subprocess call can
    # take real time) before handing the text back to the SAME widget
    # method a terminal-driven paste's own placeholder rule uses.
    def on_prompt_input_paste_requested(self, _event: PromptInput.PasteRequested) -> None:
        self.run_worker(self._paste_from_clipboard_worker, thread=True, name="clipboard-paste")

    def _paste_from_clipboard_worker(self) -> None:
        from halo_harness.tui.clipboard import read_via_external_tool
        text = read_via_external_tool()
        if text:
            self.call_from_thread(self.prompt_input.paste_text, text)
        else:
            self.call_from_thread(
                self.notify,
                "Paste with your terminal's shortcut (Ctrl+Shift+V) or Shift+middle-click",
                timeout=4,
            )

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
        # 1.0.1 part 2 (reviewer minor): killed HERE, unconditionally,
        # before the quit sequence below even starts -- `Controller.quit()`
        # already does this too, but only after its own worker-join
        # (up to QUIT_DEADLINE_S), which is exactly the path Ctrl+Q exists
        # to route around when it's wedged. A background Bash job (or one
        # a timed-out foreground command was moved to) must never outlive
        # the process just because the ORDINARY quit path never got there.
        try:
            self.controller.session.job_registry.kill_all()
        except Exception:
            pass
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
        from halo_harness.tui.dialogs.help import HelpDialog

        self.push_screen(HelpDialog(self.registry))

    def action_history_search(self) -> None:
        from halo_harness import history as history_mod
        from halo_harness.tui.dialogs.history_search import HistorySearchDialog

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
