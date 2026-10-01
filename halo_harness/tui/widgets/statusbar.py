"""halo_harness.tui.widgets.statusbar -- the docked-bottom `StatusBar`:
model, context bar (% + warn/error colours), cost, mode glyph, cwd+branch,
"MCP n/m", spinner+elapsed, "↓ N new". Built with `rich.Text` (never
Rich markup strings) so a model name or cwd containing a literal `[` can
never be misread as a markup tag.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from halo_harness.model_display import (
    format_elapsed_seconds, format_live_token_count, format_status_context, format_status_cost,
    truncate_label_left,
)
from halo_harness.tui.theme import mode_glyph

# Halo 2.0.1 W2b (liveness-tips-brief Part A3): phase words that get the
# rich "word elapsed · tokens"/"word elapsed" treatment -- distinct from
# the older "running"/"compacting" words, which keep their pre-2.0.1 bare
# "glyph elapsed" rendering (out of this brief's scope; left unchanged).
_LIVE_PHASE_WORDS = frozenset({"thinking", "writing", "tool", "waiting"})

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusBar(Static):
    def __init__(self, *, cwd: str = "", branch: str = "") -> None:
        super().__init__(classes="status-bar")
        self.model = "-"
        # 1.0.1 hotfix 14: the RAW numbers, not just a derived percentage --
        # `context_pct` is still kept (recomputed by `apply_status` below),
        # used only for the bar-fill/colour threshold, since the "ctx
        # 12k/1M 1%" TEXT itself is built straight from these two by
        # `model_display.format_status_context`.
        self.context_tokens: "int | float" = 0
        self.context_limit: "int | float | None" = None
        self.context_pct: "float | None" = None
        self.cost_usd: "float | None" = None
        self.total_input_tokens: "int | float" = 0
        self.total_output_tokens: "int | float" = 0
        # H15 part 2 addendum 4: the OpenRouter account-balance segment,
        # right after cost -- the ALREADY-FORMATTED text (e.g. "OR $12.40
        # left"/"OR $3.21 used" -- providers/openrouter_account.py's own
        # `format_status_bar_segment` decides the figure AND the verb, this
        # widget just displays it), None (segment omitted entirely) until
        # the first successful background fetch ever lands; a LATER failed
        # fetch never clears a good reading, it just keeps aging (see
        # `_refresh_display`'s own 10-minute dim threshold).
        self.or_balance_text: "str | None" = None
        self.or_balance_fetched_at: "float | None" = None
        # 1.0.1 hotfix 20.3: the session's current reasoning-effort level,
        # already clamped to this model's own accepted set -- None for a
        # model with no adjustable effort at all (renders no tag).
        self.effort: "str | None" = None
        # 1.0.1 hotfix 17.3: True for as long as a PermissionCard is
        # mounted and unanswered -- the bar shows "permission needed: ..."
        # in the warning colour so a pending ask is impossible to miss.
        self.permission_pending: bool = False
        self.mode = "default"
        self.cwd = cwd
        self.branch = branch
        self.mcp_connected = 0
        self.mcp_total = 0
        self.phase = "idle"
        self._phase_started_at = time.monotonic()
        self.spinner_index = 0
        # Halo 2.0.1 W2b (liveness-tips-brief Part A3): the received-token
        # counter ("↓412"/"↓1.2k") -- RAW character count, same convention
        # as ThinkingBlock's own `written_chars` (tokens computed as
        # chars//4 at render time so incremental small deltas never
        # under-count via repeated integer-division truncation). Reset
        # whenever the phase WORD changes (a fresh sub-phase's own count,
        # e.g. reasoning tokens while "thinking" vs. text tokens while
        # "writing" -- never a running total across both) and at
        # `start_phase_clock`/`go_idle`.
        self.received_chars = 0
        # The currently-running tool's own name ("Bash" in "tool Bash
        # 4 s") -- set by `set_tool_name`, cleared whenever the phase word
        # changes away from "tool".
        self.running_tool_name: "str | None" = None
        self.new_count = 0
        # U5 scope D: the `statusLine` command's own last output (None
        # until the first successful run, or if none is configured).
        self.statusline_text: "str | None" = None
        self._refresh_display()

    def set_statusline_text(self, text: "str | None") -> None:
        self.statusline_text = text
        self._refresh_display()

    def on_click(self, event) -> None:
        # 1.0.1 hotfix 16: "a click on the 'N new' indicator" re-anchors
        # the transcript -- the whole bar is the click target (it renders
        # as one `rich.Text` line with no addressable sub-regions) rather
        # than only the exact glyph, which is a no-op in practice since
        # this only ever does anything while `new_count` is nonzero.
        if self.new_count and hasattr(self.app, "action_scroll_transcript_end"):
            self.app.action_scroll_transcript_end()

    def apply_status(self, data: dict) -> None:
        if data.get("model"):
            self.model = data["model"]
        if data.get("permission_mode"):
            self.mode = data["permission_mode"]
        if data.get("effort") is not None:
            self.effort = data["effort"]
        if data.get("cost_usd") is not None:
            self.cost_usd = data["cost_usd"]
        # 1.0.1 hotfix 14: keep the raw tokens/limit (not just the derived
        # percentage) so `_refresh_display` can render "ctx 12k/1M 1%" --
        # each only overwrites its own attribute when THIS event actually
        # carries a real number, same "absent means unchanged" rule every
        # other field here already follows (an idle-phase status fired with
        # no context data must never blank out the last real reading).
        tokens = data.get("context_tokens")
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            self.context_tokens = tokens
        limit = data.get("context_limit")
        if isinstance(limit, (int, float)) and not isinstance(limit, bool):
            self.context_limit = limit
        self.context_pct = round(100.0 * self.context_tokens / self.context_limit, 1) if self.context_limit else None
        total_in = data.get("total_input_tokens")
        if isinstance(total_in, (int, float)) and not isinstance(total_in, bool):
            self.total_input_tokens = total_in
        total_out = data.get("total_output_tokens")
        if isinstance(total_out, (int, float)) and not isinstance(total_out, bool):
            self.total_output_tokens = total_out
        mcp = data.get("mcp") or {}
        if isinstance(mcp, dict):
            self.mcp_connected = mcp.get("connected", self.mcp_connected)
            self.mcp_total = mcp.get("total", self.mcp_total)
        phase = data.get("phase")
        if phase and phase != self.phase:
            self.phase = phase
            self._phase_started_at = time.monotonic()
        self._refresh_display()

    # ---- Halo 2.0.1 W2b: the liveness cluster (liveness-tips-brief Part A3)
    # -- `tui/dispatch.py`'s own `phase`/`tool_use_ready`/`thinking_delta`/
    # `text_delta`/`turn_done` handlers are the only callers. Kept SEPARATE
    # from `apply_status`'s own generic phase diffing above (still used
    # as-is for "compacting"/"idle" from a plain `status` event) because
    # the elapsed CLOCK and the phase WORD reset independently here: the
    # clock only resets at a NEW model call (`phase(request_sent)`) or a
    # fresh wait (`phase(waiting_for_model)`), never at an internal word
    # change like thinking -> writing within the SAME call (A3's own
    # "⠋ thinking 18 s"/"⠙ writing 23 s" -- 5 more seconds, not reset to
    # zero), while the received-token counter resets at EVERY word change
    # (a fresh sub-phase's own count, not a running total across both). ---

    def start_phase_clock(self, word: str) -> None:
        """A NEW model call is starting (`phase(state="request_sent")`) or
        a fresh wait begins (`phase(state="waiting_for_model")`): resets
        the elapsed clock AND the received-token counter to zero."""
        self.phase = word
        self._phase_started_at = time.monotonic()
        self.received_chars = 0
        self.running_tool_name = None
        self._refresh_display()

    def set_phase_word(self, word: str) -> None:
        """An internal transition WITHIN the current call (thinking ->
        writing -> tool, or back) -- the elapsed clock keeps running;
        only a CHANGED word resets the received-token counter (and clears
        the running tool's name once the word moves away from "tool")."""
        if word != self.phase:
            self.phase = word
            self.received_chars = 0
            if word != "tool":
                self.running_tool_name = None
        self._refresh_display()

    def set_tool_name(self, name: "str | None") -> None:
        self.running_tool_name = name
        self._refresh_display()

    def add_received_chars(self, n: int) -> None:
        self.received_chars += max(0, n)
        self._refresh_display()

    def go_idle(self) -> None:
        """`turn_done`: "the cluster returns to idle" -- also where the
        received-token counter/running tool name get their own reset, so
        neither can ever carry a stale reading into the NEXT turn (the
        "stuck-glyph defect" this brief's A3 calls out by name)."""
        self.phase = "idle"
        self.received_chars = 0
        self.running_tool_name = None
        self._refresh_display()

    def apply_context_pct(self, pct) -> None:
        if pct is not None:
            self.context_pct = pct
            self._refresh_display()

    def apply_cost(self, cost_usd) -> None:
        if cost_usd is not None:
            self.cost_usd = cost_usd
            self._refresh_display()

    def set_or_balance(self, segment_text: "str | None", *, fetched_at: "float | None" = None) -> None:
        """H15 part 2 addendum 4: called only on a SUCCESSFUL background
        fetch (`tui/slash.py`'s own OpenRouter balance worker) -- a failed
        fetch never calls this at all, so a good reading just ages (and
        eventually dims) rather than disappearing. `segment_text` is
        ALREADY formatted (`providers/openrouter_account.py::format_status_
        bar_segment` -- "OR $12.40 left"/"OR $3.21 used", this widget never
        decides the figure or the verb itself). `fetched_at` defaults to now
        (`time.monotonic()`); a test passes an explicit backdated value to
        simulate staleness without a real 10-minute wait."""
        self.or_balance_text = segment_text
        self.or_balance_fetched_at = time.monotonic() if fetched_at is None else fetched_at
        self._refresh_display()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._refresh_display()

    def set_pending_permission(self, pending: bool) -> None:
        self.permission_pending = pending
        self._refresh_display()

    def set_effort(self, effort: "str | None") -> None:
        # 1.0.1 hotfix 20.3: `/effort`'s own immediate UI update -- unlike
        # apply_status's fields, this DOES accept None (a switch to a model
        # with no adjustable effort at all clears the tag), since it's
        # always called deliberately with the session's freshly-read value,
        # never from a status event that might just be "not touched".
        self.effort = effort
        self._refresh_display()

    def set_new_count(self, n: int) -> None:
        if n != self.new_count:
            self.new_count = n
            self._refresh_display()

    def set_cwd_branch(self, cwd: str, branch: str) -> None:
        self.cwd, self.branch = cwd, branch
        self._refresh_display()

    def tick_spinner(self) -> None:
        if self.phase in _LIVE_PHASE_WORDS or self.phase in ("running", "compacting"):
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
        #
        # 1.0.1 hotfix 14: `ctx_str`/`cost_str` can NEVER be the literal
        # "ctx ?"/"$?" any more -- `format_status_context`/`format_status_
        # cost` (model_display.py, shared with the model-listing row
        # format from hotfix 12) always produce a real string, falling back
        # to used-tokens-alone or raw token totals when a limit/price
        # genuinely isn't known.
        ctx_str = format_status_context(self.context_tokens, self.context_limit)
        cost_str = format_status_cost(self.cost_usd, self.total_input_tokens, self.total_output_tokens)
        # H15 part 2 addendum 4: "OR $12.40 left"/"OR $3.21 used" --
        # omitted entirely (blank, no segment at all, same convention as
        # effort_str/permission_str below) until a fetch has ever
        # succeeded; dimmed (never hidden) once the reading is more than
        # 10 minutes old.
        or_balance_str = self.or_balance_text or ""
        or_balance_stale = (self.or_balance_fetched_at is not None
                             and (time.monotonic() - self.or_balance_fetched_at) > 600)
        mode_str = mode_glyph(self.mode)
        mcp_style = "green" if (self.mcp_total and self.mcp_connected == self.mcp_total) else "yellow"
        mcp_str = f"MCP {self.mcp_connected}/{self.mcp_total}"
        # Halo 2.0.1 W2b (liveness-tips-brief Part A3): "⠋ thinking 18 s ·
        # ↓412", "⠙ writing 23 s · ↓1.2k", "⠹ tool Bash 4 s", "⠸ waiting
        # 3 s" -- the phase WORD plus elapsed (and, for thinking/writing, a
        # received-token counter) so a frozen UI is distinguishable from a
        # silent model purely by whether this segment is still changing.
        # "running"/"compacting" keep their pre-2.0.1 bare "glyph elapsed"
        # form -- outside this brief's scope.
        spinner_str = ""
        glyph = SPINNER_FRAMES[self.spinner_index]
        if self.phase in _LIVE_PHASE_WORDS:
            elapsed_str = format_elapsed_seconds(time.monotonic() - self._phase_started_at)
            if self.phase == "tool":
                word = f"tool {self.running_tool_name}" if self.running_tool_name else "tool"
                spinner_str = f"{glyph} {word} {elapsed_str}"
            elif self.phase == "waiting":
                spinner_str = f"{glyph} waiting {elapsed_str}"
            else:  # "thinking" | "writing"
                tok_str = format_live_token_count(self.received_chars // 4)
                spinner_str = f"{glyph} {self.phase} {elapsed_str} · ↓{tok_str}"
        elif self.phase in ("running", "compacting"):
            elapsed = time.monotonic() - self._phase_started_at
            spinner_str = f"{glyph} {elapsed:.0f}s"
        new_str = f"↓ {self.new_count} new" if self.new_count else ""
        # 1.0.1 hotfix 20.3: a short effort tag next to the mode glyph --
        # blank (no segment at all) for a model with no adjustable effort,
        # never a placeholder like "ctx ?"/"$?" would have been.
        effort_str = self.effort or ""
        # 1.0.1 hotfix 17.3: impossible to miss -- rendered in the warning
        # style, same widths math as every other segment below.
        permission_str = "permission needed: 1 yes · 2 session · 3 always · 4 no" \
            if self.permission_pending else ""
        loc_str = self.cwd if not self.branch else f"{self.cwd} ({self.branch})"
        model_label = self.model

        # point 4: order is model, ctx, cost, mode, THEN cwd/branch and MCP
        # -- on a narrow terminal the cwd/branch is what shrinks first
        # (down to nothing), and only once THAT alone can't make it fit
        # does the model label itself get left-truncated (with a leading
        # "…" -- `truncate_label_left` -- so the end of a long ref, the
        # part that actually distinguishes it from a sibling model, stays
        # visible). `self.size.width` is 0 before this widget's first
        # layout pass (e.g. a bare unit test that never mounted it) --
        # skip shrinking entirely then, same as an unbounded-width terminal.
        width = self.size.width
        if width and self.cwd:
            fixed_bits = [b for b in (ctx_str, cost_str, or_balance_str, mode_str, effort_str, permission_str,
                                       mcp_str, spinner_str, new_str) if b]
            # Each segment below is rendered as "<text> " with a "│ "
            # separator before it -- 3 extra columns per segment is that
            # separator plus its own trailing space, a close-enough
            # approximation of the real layout to decide when to shrink
            # (not a character-exact fit -- Rich wraps a genuine overflow
            # instead of clipping, so erring a little wide costs nothing).
            fixed_width = sum(len(b) + 3 for b in fixed_bits) + len(model_label) + 3
            overflow = fixed_width + len(loc_str) + 3 - width
            if overflow > 0:
                if len(loc_str) > overflow:
                    loc_str = loc_str[:len(loc_str) - overflow].rstrip()
                else:
                    overflow -= len(loc_str)
                    loc_str = ""
                    model_label = truncate_label_left(model_label, max(4, len(model_label) - overflow))

        text = Text()
        text.append(f" {model_label} ", style="bold")
        text.append("│ ", style="dim")
        if self.context_limit:
            filled = max(0, min(10, round((self.context_pct or 0) / 10)))
            bar = "#" * filled + "-" * (10 - filled)
            text.append(f"[{bar}] {ctx_str} ", style=self._context_style())
        else:
            text.append(f"{ctx_str} ", style="dim")
        text.append("│ ", style="dim")
        text.append(f"{cost_str} ", style="dim")
        if or_balance_str:
            text.append("│ ", style="dim")
            text.append(f"{or_balance_str} ", style="dim" if or_balance_stale else "")
        text.append("│ ", style="dim")
        text.append(f"{mode_str} ", style="bold cyan")
        if effort_str:
            text.append("│ ", style="dim")
            text.append(f"{effort_str} ", style="magenta")
        if permission_str:
            text.append("│ ", style="dim")
            text.append(f"{permission_str} ", style="bold yellow")
        if loc_str:
            text.append("│ ", style="dim")
            text.append(f"{loc_str} ", style="dim")
        text.append("│ ", style="dim")
        text.append(f"{mcp_str} ", style=mcp_style)
        if spinner_str:
            # U5 must-do: "compacting" (Session._run_compaction's own
            # "Compacting..." indicator, via the new `compaction` event
            # handler in tui/dispatch.py) gets the SAME live spinner a
            # running turn already does.
            text.append("│ ", style="dim")
            text.append(f"{spinner_str} ", style="bold yellow")
        if new_str:
            text.append("│ ", style="dim")
            text.append(f"{new_str} ", style="bold magenta")
        if self.statusline_text:
            text.append("│ ", style="dim")
            # U5 scope D: a `statusLine` script's own ANSI colour codes
            # (a common thing for these scripts to emit) are preserved,
            # not stripped -- `Text.from_ansi` is Rich's own parser for
            # exactly this, so a red/green segment in the script's output
            # renders as red/green here too, not literal escape bytes.
            try:
                text.append_text(Text.from_ansi(self.statusline_text))
            except Exception:
                text.append(self.statusline_text)
            text.append(" ")
        self.update(text)
