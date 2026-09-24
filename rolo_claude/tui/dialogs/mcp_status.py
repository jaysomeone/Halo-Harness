"""rolo_claude.tui.dialogs.mcp_status -- `/mcp` (D-TUI: "McpStatus (/mcp, r
reconnect)"). `servers` is `Controller.list_mcp_servers()`'s shape (one dict
per configured server, `McpManager.status()`'s own fields: name/type/
command/args/url/state/error/tool_count/...). `reconnect` is
`controller.reconnect_mcp`.
"""

from __future__ import annotations

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

    def compose(self):
        with Vertical():
            yield Static("MCP servers (r: reconnect, a: approve pending, highlighted; Esc: close)",
                          classes="dialog-title")
            option_list = OptionList()
            if not self.servers:
                option_list.add_option(Option("No MCP servers configured.", disabled=True))
            for entry in self.servers:
                option_list.add_option(Option(_row(entry), id=entry.get("name")))
            yield option_list
            yield Static("", id="mcp-hint")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_reconnect(self) -> None:
        self._run_action(self._reconnect)

    def action_approve(self) -> None:
        self._run_action(self._approve)

    def _run_action(self, fn: Optional[Callable]) -> None:
        option_list = self.query_one(OptionList)
        highlighted = option_list.highlighted
        if highlighted is None or fn is None:
            return
        option = option_list.get_option_at_index(highlighted)
        name = option.id
        if not name:
            return
        lines = fn(name)
        self.query_one("#mcp-hint", Static).update("\n".join(lines) if isinstance(lines, list) else str(lines))
        for entry in self.servers:
            if entry.get("name") == name:
                if any("connected" in str(l) for l in (lines or [])):
                    entry["state"] = "connected"
                option_list.replace_option_prompt_at_index(highlighted, _row(entry))
                break
