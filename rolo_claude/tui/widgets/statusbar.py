"""rolo_claude.tui.widgets.statusbar -- the docked-bottom `StatusBar`:
model, context bar (% + warn/error colours), cost, mode glyph, cwd+branch,
"MCP n/m", spinner+elapsed, "↓ N new". Built with `rich.Text` (never
Rich markup strings) so a model name or cwd containing a literal `[` can
never be misread as a markup tag.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from rolo_claude.tui.theme import mode_glyph

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusBar(Static):
    def __init__(self, *, cwd: str = "", branch: str = "") -> None:
        super().__init__(classes="status-bar")
        self.model = "-"
        self.context_pct: "float | None" = None
        self.cost_usd: "float | None" = None
        self.mode = "default"
        self.cwd = cwd
        self.branch = branch
        self.mcp_connected = 0
        self.mcp_total = 0
        self.phase = "idle"
        self._phase_started_at = time.monotonic()
        self.spinner_index = 0
        self.new_count = 0
        self._refresh_display()

    def apply_status(self, data: dict) -> None:
        if data.get("model"):
            self.model = data["model"]
        if data.get("permission_mode"):
            self.mode = data["permission_mode"]
        if data.get("cost_usd") is not None:
            self.cost_usd = data["cost_usd"]
        limit = data.get("context_limit")
        tokens = data.get("context_tokens")
        if isinstance(limit, (int, float)) and limit and isinstance(tokens, (int, float)):
            self.context_pct = round(100.0 * tokens / limit, 1)
        mcp = data.get("mcp") or {}
        if isinstance(mcp, dict):
            self.mcp_connected = mcp.get("connected", self.mcp_connected)
            self.mcp_total = mcp.get("total", self.mcp_total)
        phase = data.get("phase")
        if phase and phase != self.phase:
            self.phase = phase
            self._phase_started_at = time.monotonic()
        self._refresh_display()

    def apply_context_pct(self, pct) -> None:
        if pct is not None:
            self.context_pct = pct
            self._refresh_display()

    def apply_cost(self, cost_usd) -> None:
        if cost_usd is not None:
            self.cost_usd = cost_usd
            self._refresh_display()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._refresh_display()

    def set_new_count(self, n: int) -> None:
        if n != self.new_count:
            self.new_count = n
            self._refresh_display()

    def set_cwd_branch(self, cwd: str, branch: str) -> None:
        self.cwd, self.branch = cwd, branch
        self._refresh_display()

    def tick_spinner(self) -> None:
        if self.phase in ("thinking", "running"):
            self.spinner_index = (self.spinner_index + 1) % len(SPINNER_FRAMES)
        self._refresh_display()

    def _context_style(self) -> str:
        if self.context_pct is None:
            return "dim"
        if self.context_pct >= 90:
            return "bold red"
        if self.context_pct >= 70:
            return "yellow"
        return "green"

    def _refresh_display(self) -> None:
        # Named to avoid shadowing `Widget._render()` (a REAL Textual
        # internal called during layout to get this widget's Visual --
        # overriding it broke `get_content_height` outright).
        text = Text()
        text.append(f" {self.model} ", style="bold")
        text.append("│ ", style="dim")
        if self.context_pct is not None:
            filled = max(0, min(10, round(self.context_pct / 10)))
            bar = "#" * filled + "-" * (10 - filled)
            text.append(f"[{bar}] {self.context_pct:.0f}% ", style=self._context_style())
        else:
            text.append("ctx ? ", style="dim")
        text.append("│ ", style="dim")
        cost_str = f"${self.cost_usd:.4f}" if self.cost_usd is not None else "$?"
        text.append(f"{cost_str} ", style="dim")
        text.append("│ ", style="dim")
        text.append(f"{mode_glyph(self.mode)} ", style="bold cyan")
        if self.cwd:
            text.append("│ ", style="dim")
            loc = self.cwd if not self.branch else f"{self.cwd} ({self.branch})"
            text.append(f"{loc} ", style="dim")
        text.append("│ ", style="dim")
        mcp_style = "green" if (self.mcp_total and self.mcp_connected == self.mcp_total) else "yellow"
        text.append(f"MCP {self.mcp_connected}/{self.mcp_total} ", style=mcp_style)
        if self.phase in ("thinking", "running"):
            elapsed = time.monotonic() - self._phase_started_at
            text.append("│ ", style="dim")
            text.append(f"{SPINNER_FRAMES[self.spinner_index]} {elapsed:.0f}s ", style="bold yellow")
        if self.new_count:
            text.append("│ ", style="dim")
            text.append(f"↓ {self.new_count} new ", style="bold magenta")
        self.update(text)
