"""halo_harness.tui.dialogs.mcp_status -- `/mcp` (D-TUI: "McpStatus (/mcp, r
reconnect)"). `servers` is `Controller.list_mcp_servers()`'s shape (one dict
per configured server, `McpManager.status()`'s own fields: name/type/
command/args/url/state/error/tool_count/backoff_status/...). `reconnect` is
`controller.reconnect_mcp`, `approve` is `controller.approve_mcp_server`.

u2-h3b finding 9: `r`/`a` used to call `reconnect`/`approve` DIRECTLY from
the key-binding handler, which runs on the app's own event loop -- against
a hung or merely slow server, `McpManager.reconnect`'s own close+start
waits (up to `mcp_timeout_s()*2 + 9` seconds) froze the WHOLE APP, not just
this dialog. Every network/subprocess action here (`r`, `R`, `a`, `l`, `t`)
runs on a background worker thread (same `run_worker(thread=True)` +
`call_from_thread` pattern `tui/app.py` already uses for `_git_branch_
worker`/`_quit_worker`), and Esc while one is running signals its own
`abort` Event (threaded down to the Controller methods that accept one)
instead of trying to dismiss a still-busy dialog.

round4 brief (2.0.2 D): gains the rest of the repair surface -- `R`
reconnect all, `l` login (OAuth for a local http/sse server, or the
connector re-auth pointer for a claude.ai connector row), `L` the
server's log tail, `e` edit the entry ($EDITOR at its line, or an inline
form with no $EDITOR), `i` the install hint for a command-not-found, `d`
disable/enable per scope, `t` a timed tools/list round trip. Every
failed/needs_auth/pending_approval/disabled row's `fix_line_for` (mcp_
cli.py, shared with `halo mcp fix` -- never duplicated) and `backoff_
status` (manager.py) are rendered inline so every action has data to act
on, and the key legend is always visible at the bottom.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, RichLog, Static
from textual.widgets.option_list import Option

_STATE_GLYPH = {
    "connected": "✓", "connecting": "⋯", "pending": "○",
    "pending_approval": "⚠", "needs_auth": "⚠", "failed": "✗", "closed": "⊘",
    "cached": "◐",  # H13 Part A: tools known from mcp.tools_cache, not yet connected
}

_LEGEND = ("r reconnect  R all  a approve  l login  L log  e edit  "
           "i install  d disable  t test  Esc close")

# Halo 2.0.2 round D leftover 1: `e`'s external-editor launch used to pass
# vim's own `+<line>` argument to EVERY `$EDITOR`, unconditionally -- fine
# for vim/nvim/nano/emacs (which all accept it), but `code`/`subl` don't
# understand `+42` at all and would just try to open a file literally
# named that. Keyed off the editor's own basename (extension stripped, so
# `code.cmd`/`vim.exe` on Windows match too) -- any editor not in this
# table opens the file plain, with no line argument at all, rather than
# guessing wrong.
_LINE_ARG_STYLE = {
    "vim": "plus", "vi": "plus", "nvim": "plus", "nano": "plus", "emacs": "plus",
    "code": "goto", "code-insiders": "goto", "codium": "goto",
    "subl": "colon", "sublime_text": "colon",
}


def _editor_command(editor: str, path, line: int) -> str:
    """The shell command STRING to launch `editor` at `line` in `path`
    (`shell=True`, matching this dialog's own pre-existing launch
    convention) -- pure, no I/O, so it's directly unit-testable with no
    subprocess/Textual involved. `editor` may carry its own flags
    (`"code --wait"`); only the first whitespace-separated token picks
    the line-argument style, and the whole string is kept as the command
    prefix. An editor not in `_LINE_ARG_STYLE` opens the file plain, with
    no line argument at all, rather than guessing a syntax that might not
    apply."""
    import re
    import shlex
    first_token = (shlex.split(editor) or [editor])[0]
    prog = re.sub(r"\.(exe|cmd|bat)$", "", os.path.basename(first_token).lower())
    style = _LINE_ARG_STYLE.get(prog)
    if style == "plus":
        return f'{editor} +{line} "{path}"'
    if style == "goto":
        return f'{editor} --goto "{path}:{line}"'
    if style == "colon":
        return f'{editor} "{path}:{line}"'
    return f'{editor} "{path}"'


def _row(entry: dict) -> str:
    from halo_harness.mcp_cli import fix_line_for, format_mcp_list_line
    glyph = _STATE_GLYPH.get(entry.get("state"), "?")
    line = f"{glyph} {format_mcp_list_line(entry)}"
    fix = fix_line_for(entry)
    if fix:
        line += f" -> fix: {fix}"
    backoff = entry.get("backoff_status")
    if backoff:
        line += f" [{backoff}]"
    return line


class McpStatus(ModalScreen):
    BINDINGS = [
        Binding("escape,q", "cancel", "Close", show=False),
        Binding("r", "reconnect", "Reconnect", show=False),
        Binding("R", "reconnect_all", "Reconnect all", show=False),
        Binding("a", "approve", "Approve", show=False),
        Binding("l", "login", "Login", show=False),
        Binding("L", "show_log", "Log", show=False),
        Binding("e", "edit_entry", "Edit", show=False),
        Binding("i", "install_hint", "Install hint", show=False),
        Binding("d", "toggle_disabled", "Disable/enable", show=False),
        Binding("t", "test_server", "Test", show=False),
    ]
    DEFAULT_CSS = """
    McpStatus { align: center middle; }
    McpStatus > Vertical { width: 94%; height: 84%; border: round $primary; background: $surface; padding: 1 2; }
    McpStatus OptionList { height: 1fr; }
    """

    def __init__(self, servers: "list[dict]", *, reconnect: Optional[Callable] = None,
                 approve: Optional[Callable] = None, reconnect_all: Optional[Callable] = None,
                 login: Optional[Callable] = None, test: Optional[Callable] = None,
                 disable: Optional[Callable] = None, resolve_config: Optional[Callable] = None,
                 refresh: Optional[Callable[[], list]] = None) -> None:
        super().__init__()
        self.servers = servers
        # 2.0.2 review finding 19: `Controller.list_mcp_servers` (cache-only,
        # see its own docstring), re-read after any action instead of
        # guessing a row's new state from the action's own result text.
        self._refresh = refresh
        self._reconnect = reconnect
        # H3b must-do (unwired seam): completes the `.mcp.json` interactive
        # approval flow -- a "⏸ pending_approval" server would otherwise
        # stay unreachable forever. `Controller.approve_mcp_server`.
        self._approve = approve
        # round4 brief item 1: the rest of the repair surface's own
        # Controller callables -- every one is optional (a test double /
        # an older FakeController missing one just disables that key, it
        # never crashes the dialog).
        self._reconnect_all = reconnect_all
        self._login = login
        self._test = test
        self._disable = disable
        self._resolve_config = resolve_config
        # finding 9: the in-flight action's own abort Event, or None when
        # nothing is running -- `action_cancel` checks this to decide
        # "cancel the busy operation" vs. "close the dialog".
        self._busy_abort: Optional[threading.Event] = None
        self._busy_name: Optional[str] = None
        self._pending_edit_name: Optional[str] = None

    def compose(self):
        with Vertical():
            yield Static("MCP servers", classes="dialog-title")
            yield Static(_LEGEND, id="mcp-legend", classes="dialog-subtitle")
            option_list = OptionList()
            if not self.servers:
                option_list.add_option(Option("No MCP servers configured.", disabled=True))
            for entry in self.servers:
                option_list.add_option(Option(_row(entry), id=entry.get("name")))
            yield option_list
            yield Static("", id="mcp-hint")

    def on_mount(self) -> None:
        # 1.0.1 hotfix addendum 7: see rewind_picker.py's matching comment.
        if self.servers:
            self.query_one(OptionList).action_first()

    def action_cancel(self) -> None:
        if self._busy_abort is not None:
            # 2.0.2 review finding 14: a SECOND Esc, while the first
            # one's own cancel request is still pending (the underlying
            # action can't be force-killed -- see the comment just
            # below), dismisses the dialog outright instead of leaving
            # the user stuck looking at "Cancelling..." for however long
            # the abandoned worker takes to actually notice and finish.
            if self._busy_abort.is_set():
                self.dismiss(None)
                return
            # An action is in flight -- Esc cancels THAT (the worker keeps
            # running the underlying close/start/login regardless, same
            # "abandoned, not stopped" caveat as every other abort-aware
            # wait in this codebase; this only stops the DIALOG from
            # waiting on it), rather than dismissing a dialog whose own
            # state would otherwise keep changing under the user after
            # they've already left it.
            self._busy_abort.set()
            self._set_hint(f"Cancelling {self._busy_name}... (Esc again to close this dialog)")
            return
        self.dismiss(None)

    # ---- small shared helpers ----------------------------------------

    def _highlighted_name(self) -> Optional[str]:
        option_list = self.query_one(OptionList)
        highlighted = option_list.highlighted
        if highlighted is None:
            return None
        option = option_list.get_option_at_index(highlighted)
        return option.id

    def _entry_for(self, name: str) -> Optional[dict]:
        return next((e for e in self.servers if e.get("name") == name), None)

    def _set_hint(self, text: str) -> None:
        try:
            self.query_one("#mcp-hint", Static).update(text)
        except Exception:
            pass  # the dialog may already be closing/closed -- never crash on a stale update

    # ---- r / R / a / l / t: background-thread actions ------------------

    def action_reconnect(self) -> None:
        self._run_action_async(self._reconnect, verb="Reconnecting")

    def action_approve(self) -> None:
        self._run_action_async(self._approve, verb="Approving")

    def action_login(self) -> None:
        # 2.0.2 review finding 14: `oauth.run_authorization_flow`'s own
        # "open this URL to authorize" line used `print_fn=print` by
        # default -- Textual's `App._print` drops a bare `print()` from a
        # worker thread entirely (never fed to devtools/capture, see
        # `halo_harness.termtitle`'s own matching comment on a similar
        # issue), so on SSH/headless Kali, where no browser opens either,
        # the user had no way at all to even see the URL to authorize
        # with by hand. Routed into this dialog's own hint line instead.
        def _with_live_url(name, abort=None):
            def _print_fn(line) -> None:
                self.app.call_from_thread(self._set_hint, str(line))
            try:
                return self._login(name, abort=abort, print_fn=_print_fn)
            except TypeError:
                # `self._login` doesn't accept print_fn at all (a minimal
                # test double) -- still run it, just without the live URL.
                return self._login(name, abort=abort)
        self._run_action_async(_with_live_url, verb="Logging in")

    def action_test_server(self) -> None:
        self._run_action_async(self._test, verb="Testing")

    def action_toggle_disabled(self) -> None:
        if self._disable is None or self._busy_abort is not None:
            return
        name = self._highlighted_name()
        if not name:
            return
        entry = self._entry_for(name)
        currently_disabled = bool(entry and entry.get("state") == "disabled")

        def _fn(nm, abort=None):
            return self._disable(nm, not currently_disabled)
        self._run_action_async(_fn, verb=("Enabling" if currently_disabled else "Disabling"), name=name)

    def action_reconnect_all(self) -> None:
        fn = self._reconnect_all
        if fn is None or self._busy_abort is not None:
            return
        abort = threading.Event()
        self._busy_abort = abort
        self._busy_name = "all servers"
        self._set_hint("Reconnecting all... (Esc to cancel)")

        def _worker() -> None:
            try:
                lines = fn(abort=abort)
            except Exception as e:
                lines = [f"reconnect all failed: {type(e).__name__}: {e}"]
            self.app.call_from_thread(self._apply_bulk_result, lines)
        self.run_worker(_worker, thread=True, name="mcp-reconnect-all")

    def _run_action_async(self, fn: Optional[Callable], *, verb: str, name: Optional[str] = None) -> None:
        if fn is None or self._busy_abort is not None:
            return  # no handler wired, or an earlier action is still running
        name = name or self._highlighted_name()
        if not name:
            return
        option_list = self.query_one(OptionList)
        try:
            index = next(i for i, o in enumerate(option_list.options) if o.id == name)
        except StopIteration:
            index = option_list.highlighted

        abort = threading.Event()
        self._busy_abort = abort
        self._busy_name = name
        self._set_hint(f"{verb} {name}... (Esc to cancel)")

        def _worker() -> None:
            try:
                lines = fn(name, abort=abort)
            except TypeError:
                # a callable that doesn't accept `abort` at all (a minimal
                # test double) -- still run it off the UI thread, just
                # without cancel support.
                try:
                    lines = fn(name)
                except Exception as e:
                    lines = [f"{name}: {type(e).__name__}: {e}"]
            except Exception as e:
                lines = [f"{name}: {type(e).__name__}: {e}"]
            self.app.call_from_thread(self._apply_result, name, index, lines)

        self.run_worker(_worker, thread=True, name=f"mcp-{verb.lower()}-{name}")

    def _apply_result(self, name: str, index: int, lines) -> None:
        self._busy_abort = None
        self._busy_name = None
        self._set_hint("\n".join(lines) if isinstance(lines, list) else str(lines))
        # 2.0.2 review finding 19: this used to flip the row to "connected"
        # whenever the result text merely CONTAINED that substring, which
        # also matched "... -- not connected (state=failed)" from a failed
        # `t`, and never reflected a `d` disable/enable at all (so a second
        # `d` disabled again instead of enabling). Re-read the real state
        # from the Controller (`list_mcp_servers` is cache-only, see its own
        # docstring -- safe to call here on the UI thread) when one was
        # wired; only a minimal test double with no `refresh` falls back to
        # the old best-effort substring guess.
        if self._refresh is not None and self._refresh_rows_from_controller():
            return
        for entry in self.servers:
            if entry.get("name") == name:
                if any("connected" in str(l) for l in (lines or [])):
                    entry["state"] = "connected"
                try:
                    if index is not None:
                        self.query_one(OptionList).replace_option_prompt_at_index(index, _row(entry))
                except Exception:
                    pass
                break

    def _refresh_rows_from_controller(self) -> bool:
        """Re-reads every row from `self._refresh` (`Controller.list_mcp_
        servers`) and rebuilds the option list from the result, preserving
        the highlighted index. Used after ANY single action (`_apply_
        result`) and after `R` (`_apply_bulk_result`, finding 19's "`R`
        refreshes no rows at all") so the dialog always shows the
        Controller's own truth instead of a locally-guessed state. Returns
        False (does nothing) when no `refresh` callable was wired or it
        raised/returned None, so callers can fall back."""
        if self._refresh is None:
            return False
        try:
            fresh = self._refresh()
        except Exception:
            return False
        if fresh is None:
            return False
        self.servers[:] = fresh
        try:
            option_list = self.query_one(OptionList)
        except Exception:
            return True
        highlighted = option_list.highlighted
        option_list.clear_options()
        if not self.servers:
            option_list.add_option(Option("No MCP servers configured.", disabled=True))
        for entry in self.servers:
            option_list.add_option(Option(_row(entry), id=entry.get("name")))
        if highlighted is not None and 0 <= highlighted < len(option_list.options):
            option_list.highlighted = highlighted
        return True

    def _apply_bulk_result(self, lines) -> None:
        self._busy_abort = None
        self._busy_name = None
        self._set_hint("\n".join(lines) if isinstance(lines, list) else str(lines))
        self._refresh_rows_from_controller()

    # ---- i / L: synchronous, local actions -----------------------------

    def action_install_hint(self) -> None:
        name = self._highlighted_name()
        if not name:
            return
        entry = self._entry_for(name)
        if entry is None:
            return
        from halo_harness.mcp_cli import install_hint
        hint = install_hint(entry)
        self._set_hint(hint or f"{name}: nothing to install -- the command resolves fine on PATH.")

    def action_show_log(self) -> None:
        name = self._highlighted_name()
        if not name:
            return
        if name.startswith("connector__"):
            self._set_hint("claude.ai connectors keep no per-server log here -- see claude's own logs.")
            return
        from halo_harness.mcp.manager import tail_server_log
        lines = tail_server_log(name)
        self.app.push_screen(McpLogViewer(name, lines))

    # ---- e: $EDITOR at the entry's line, or an inline form -------------

    def action_edit_entry(self) -> None:
        name = self._highlighted_name()
        if not name:
            return
        if name.startswith("connector__") or self._resolve_config is None:
            self._set_hint("Nothing to edit here -- claude.ai connectors are managed in claude.ai/claude.")
            return
        cfg = self._resolve_config(name)
        if cfg is None:
            self._set_hint(f"{name}: could not re-resolve its config to edit.")
            return
        if getattr(cfg, "scope", None) not in ("project", "local", "user"):
            self._set_hint(f"{name}: {cfg.scope} scope has no single source file to edit here.")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor:
            self._edit_with_external_editor(name, cfg, editor)
        else:
            from halo_harness.tui.dialogs.mcp_entry_form import McpEntryForm
            self._pending_edit_name = name
            self.app.push_screen(McpEntryForm(name, cfg, cwd=self.app.cwd), self._entry_edited)

    def _edit_with_external_editor(self, name, cfg, editor: str) -> None:
        from halo_harness.mcp_cli import locate_server_source
        path, line = locate_server_source(cfg, name=name, cwd=self.app.cwd)
        if path is None:
            self._set_hint(f"{name}: no single source file to open.")
            return
        app = self.app
        enter = getattr(app, "_enter_suspend_for_editor", None)
        leave = getattr(app, "_exit_suspend_for_editor", None)
        if callable(enter):
            enter()
        try:
            import subprocess as sp
            with app.suspend():
                sp.run(_editor_command(editor, path, line), shell=True)
        finally:
            if callable(leave):
                leave()
        self._set_hint(f"{name}: reconnecting after edit...")
        self._run_action_async(self._reconnect, verb="Reconnecting", name=name)

    def _entry_edited(self, saved) -> None:
        name = self._pending_edit_name
        self._pending_edit_name = None
        if saved and name:
            self._set_hint(f"{name}: reconnecting after edit...")
            self._run_action_async(self._reconnect, verb="Reconnecting", name=name)


class McpLogViewer(ModalScreen):
    """`L` -- a static tail of `~/.halo/mcp/<server>.log` (stdio stderr +
    connect/transport errors, see `mcp.manager._append_server_log`). No
    follow mode (unlike `tasks.TranscriptViewer`'s live agent log) -- this
    is a point-in-time read, re-opened fresh each time `L` is pressed."""

    BINDINGS = [Binding("escape,q", "close", "Back", show=False)]
    DEFAULT_CSS = """
    McpLogViewer { align: center middle; }
    McpLogViewer > Vertical { width: 92%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    McpLogViewer RichLog { height: 1fr; }
    """

    def __init__(self, name: str, lines: "list[str]") -> None:
        super().__init__()
        self._name = name
        self._lines = lines

    def compose(self):
        with Vertical():
            yield Static(f"{self._name} -- log tail (Esc: back)", classes="dialog-title")
            yield RichLog(id="mcp-log-body", wrap=True, highlight=False, markup=False)

    def on_mount(self) -> None:
        log_widget = self.query_one("#mcp-log-body", RichLog)
        for line in (self._lines or ["(no log yet)"]):
            log_widget.write(line)
        log_widget.scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss()
