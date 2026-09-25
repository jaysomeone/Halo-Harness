"""rolo_claude.tui.dialogs.mcp_status -- `/mcp` (D-TUI: "McpStatus (/mcp, r
reconnect)"). `servers` is `Controller.list_mcp_servers()`'s shape (one dict
per configured server, `McpManager.status()`'s own fields: name/type/
command/args/url/state/error/tool_count/...). `reconnect` is
`controller.reconnect_mcp`, `approve` is `controller.approve_mcp_server`.

u2-h3b finding 9: `r`/`a` used to call `reconnect`/`approve` DIRECTLY from
the key-binding handler, which runs on the app's own event loop -- against
a hung or merely slow server, `McpManager.reconnect`'s own close+start
waits (up to `mcp_timeout_s()*2 + 9` seconds) froze the WHOLE APP, not just
this dialog. Both actions now run on a background worker thread (same
`run_worker(thread=True)` + `call_from_thread` pattern `tui/app.py` already
uses for `_git_branch_worker`/`_quit_worker`), and Esc while one is running
signals its own `abort` Event (threaded down to `Controller.reconnect_mcp`/
`approve_mcp_server` -> `McpManager.reconnect` -> the abort-aware waits in
`mcp/client.py`) instead of trying to dismiss a still-busy dialog.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

_STATE_GLYPH = {
    "connected": "✓", "connecting": "⋯", "pending": "○",
    "pending_approval": "⚠", "needs_auth": "⚠", "failed": "✗", "closed": "⊘",
}


def _row(entry: dict) -> str:
    from rolo_claude.mcp_cli import format_mcp_list_line
    glyph = _STATE_GLYPH.get(entry.get("state"), "?")
    return f"{glyph} {format_mcp_list_line(entry)}"


class McpStatus(ModalScreen):
    BINDINGS = [
        Binding("escape,q", "cancel", "Close", show=False),
        Binding("r", "reconnect", "Reconnect", show=False),
        Binding("a", "approve", "Approve", show=False),
    ]
    DEFAULT_CSS = """
    McpStatus { align: center middle; }
    McpStatus > Vertical { width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2; }
    McpStatus OptionList { height: 1fr; }
    """

    def __init__(self, servers: "list[dict]", *, reconnect: Optional[Callable] = None,
                 approve: Optional[Callable] = None) -> None:
        super().__init__()
        self.servers = servers
        self._reconnect = reconnect
        # H3b must-do (unwired seam): completes the `.mcp.json` interactive
        # approval flow -- a "⏸ pending_approval" server would otherwise
        # stay unreachable forever (nothing else in the TUI ever calls
        # `mcp_setup.record_mcp_approval`). `Controller.approve_mcp_server`.
        self._approve = approve
        # finding 9: the in-flight reconnect/approve's own abort Event, or
        # None when nothing is running -- `action_cancel` checks this to
        # decide "cancel the busy operation" vs. "close the dialog".
        self._busy_abort: Optional[threading.Event] = None
        self._busy_name: Optional[str] = None

    def compose(self):
        with Vertical():
            yield Static("MCP servers (r: reconnect, a: approve pending, highlighted; Esc: close/cancel)",
                          classes="dialog-title")
            option_list = OptionList()
            if not self.servers:
                option_list.add_option(Option("No MCP servers configured.", disabled=True))
            for entry in self.servers:
                option_list.add_option(Option(_row(entry), id=entry.get("name")))
            yield option_list
            yield Static("", id="mcp-hint")

    def action_cancel(self) -> None:
        if self._busy_abort is not None:
            # A reconnect/approve is in flight -- Esc cancels THAT (the
            # worker keeps running the underlying close/start regardless,
            # same "abandoned, not stopped" caveat as every other
            # abort-aware wait in this codebase; this only stops the
            # DIALOG from waiting on it), rather than dismissing a dialog
            # whose own state would otherwise keep changing under the
            # user after they've already left it.
            self._busy_abort.set()
            self._set_hint(f"Cancelling {self._busy_name}...")
            return
        self.dismiss(None)

    def action_reconnect(self) -> None:
        self._run_action_async(self._reconnect, verb="Reconnecting")

    def action_approve(self) -> None:
        self._run_action_async(self._approve, verb="Approving")

    def _set_hint(self, text: str) -> None:
        try:
            self.query_one("#mcp-hint", Static).update(text)
        except Exception:
            pass  # the dialog may already be closing/closed -- never crash on a stale update

    def _run_action_async(self, fn: Optional[Callable], *, verb: str) -> None:
        if fn is None or self._busy_abort is not None:
            return  # no handler wired, or an earlier action is still running
        option_list = self.query_one(OptionList)
        highlighted = option_list.highlighted
        if highlighted is None:
            return
        option = option_list.get_option_at_index(highlighted)
        name = option.id
        if not name:
            return

        abort = threading.Event()
        self._busy_abort = abort
        self._busy_name = name
        self._set_hint(f"{verb} {name}... (Esc to cancel)")

        def _worker() -> None:
            try:
                lines = fn(name, abort=abort)
            except TypeError:
                # a `reconnect`/`approve` callable that doesn't accept
                # `abort` at all (a minimal test double) -- still run it
                # off the UI thread, just without cancel support.
                try:
                    lines = fn(name)
                except Exception as e:
                    lines = [f"{name}: {type(e).__name__}: {e}"]
            except Exception as e:
                lines = [f"{name}: {type(e).__name__}: {e}"]
            self.app.call_from_thread(self._apply_result, name, highlighted, lines)

        self.run_worker(_worker, thread=True, name=f"mcp-{verb.lower()}-{name}")

    def _apply_result(self, name: str, index: int, lines) -> None:
        self._busy_abort = None
        self._busy_name = None
        self._set_hint("\n".join(lines) if isinstance(lines, list) else str(lines))
        for entry in self.servers:
            if entry.get("name") == name:
                if any("connected" in str(l) for l in (lines or [])):
                    entry["state"] = "connected"
                try:
                    self.query_one(OptionList).replace_option_prompt_at_index(index, _row(entry))
                except Exception:
                    pass
                break
