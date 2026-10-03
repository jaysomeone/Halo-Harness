"""halo_harness.tui.dialogs.tasks -- the `/tasks` / Ctrl+T panel (Halo 2.0.2
round 3, brief C item 1): every running, queued, background and finished
sub-agent of this session (`Controller.list_agent_tasks()`, backed by
`agent.subagent.list_agent_task_rows` -- meta.json + each child's own
jsonl log, so this is correct even right after a `-c` resume) on one tab,
the shared task board (`Controller.read_task_board()`, `tools/task_board.py`)
on the other. Enter on an agent row opens `TranscriptViewer`, a live
follow-mode reader of that agent's own `subagents/agent-<id>.jsonl`.

Both `list_agent_tasks`/`read_task_board` are plain zero-arg callables
(never the Controller itself) so a test can hand this a `FakeController`
-- or a bare lambda returning a canned list -- with no real Session/
agent_runtime/sub-agent ever involved (see test_tui.py's own pilot tests,
next to the roles/org editors').
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, RichLog, Static
from textual.widgets.option_list import Option


def _status_word(row: dict) -> str:
    status = row.get("status") or "running"
    if status == "completed":
        return "error" if row.get("is_error") else "done"
    return status  # "queued" | "running" | "background"


def _fmt_elapsed(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    from halo_harness.model_display import format_elapsed_seconds
    return format_elapsed_seconds(seconds)


def _fmt_cost(cost) -> str:
    return f"${cost:.4f}" if isinstance(cost, (int, float)) else "-"


def agent_row_text(row: dict, *, phase_word: "Optional[str]" = None) -> str:
    """One `/tasks` "Agents" tab line -- indented by `depth` (an org
    run's own tree position), e.g. "  agent (Worker) · running 12s · 3
    tools · $0.0012". `phase_word` (round 3): the W2b word (thinking/
    writing/tool/waiting) from this agent's own LIVE `SubAgentCard`, when
    `TasksPanel.refresh_rows` found one still mounted -- overrides the
    coarse queued/running/done/error status word for a currently-running
    row the user is ALSO watching live in the transcript; every other
    row (no card, or already finished) keeps the coarse word, which is
    all meta.json/the jsonl log alone can ever say."""
    indent = "  " * int(row.get("depth") or 0)
    model = row.get("model") or "-"
    word = phase_word or _status_word(row)
    return (f"{indent}{row.get('title') or '?'} ({model}) · {word} "
            f"{_fmt_elapsed(row.get('elapsed_s'))} · {row.get('tool_count', 0)} tools · "
            f"{_fmt_cost(row.get('cost_usd'))}")


def board_row_text(row: dict) -> str:
    owner = f" owner={row['owner']}" if row.get("owner") else ""
    result = f" result={row['result']!r}" if row.get("result") else ""
    return f"[{row.get('status', '?')}]{owner} {row.get('title', '?')}{result}"


def _blocks_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def render_log_lines(node: dict) -> list:
    """Plain-text lines for one `agent-<id>.jsonl` node -- defensive
    (`.get()` throughout): an unrecognized node type is skipped rather
    than guessed at, so the viewer stays readable."""
    ntype = node.get("type")
    if ntype == "user":
        text = _blocks_to_text(node.get("content"))
        return [f"> {line}" for line in text.splitlines()] if text else ["> "]
    if ntype == "assistant":
        lines = []
        for block in (node.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                lines.extend((block.get("text") or "").splitlines())
            elif block.get("type") == "tool_use":
                args = json.dumps(block.get("input") or {}, ensure_ascii=False, default=str)
                lines.append(f"  * {block.get('name', '?')}({args[:200]})")
        return lines
    if ntype == "tool_result":
        ok = "error" if node.get("is_error") else "ok"
        first = _blocks_to_text(node.get("content")).splitlines()
        return [f"  <- {node.get('tool', '?')} [{ok}] {(first[0] if first else '')[:200]}"]
    return []


class TranscriptViewer(ModalScreen):
    """Enter on a `/tasks` agent row -- a live, follow-mode reader of that
    agent's own jsonl log (polled every second, same cadence the status
    bar's own spinner ticks at). PgUp/PgDn scroll (disabling follow, so a
    live-growing log doesn't yank the view back down mid-read); `o` opens
    the SAME full-content `PagerScreen` a `ToolCard`'s own `o` does; Esc
    goes back to the `TasksPanel` underneath."""

    BINDINGS = [
        Binding("escape", "dismiss_viewer", "Back", show=False),
        Binding("o", "open_pager", "Pager", show=False),
        Binding("pageup", "page_up", "PgUp", show=False),
        Binding("pagedown", "page_down", "PgDn", show=False),
    ]
    DEFAULT_CSS = """
    TranscriptViewer { align: center middle; }
    TranscriptViewer > Vertical { width: 96%; height: 92%; border: round $primary; background: $surface;
        padding: 1 2; }
    TranscriptViewer RichLog { height: 1fr; }
    """

    def __init__(self, log_path: str, *, title: str = "") -> None:
        super().__init__()
        self._log_path = Path(log_path)
        self._title = title or str(log_path)
        self.follow = True  # public: the pilot test asserts this directly
        self._last_size = -1
        self._timer = None

    def compose(self):
        yield Static(f"{self._title} -- follow mode (PgUp/PgDn: scroll, o: full pager, Esc: back)",
                      classes="dialog-title")
        yield RichLog(id="transcript-log", wrap=True, highlight=False, markup=False)

    def on_mount(self) -> None:
        self._load()
        self._timer = self.set_interval(1.0, self._poll)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _poll(self) -> None:
        try:
            size = self._log_path.stat().st_size
        except OSError:
            return
        if size != self._last_size:
            self._load()

    def _load(self) -> None:
        from halo_harness.agent.subagent import _agent_log_nodes
        try:
            self._last_size = self._log_path.stat().st_size
        except OSError:
            self._last_size = -1
        log_widget = self.query_one("#transcript-log", RichLog)
        log_widget.clear()
        for node in _agent_log_nodes(self._log_path):
            for line in render_log_lines(node):
                log_widget.write(line)
        if self.follow:
            log_widget.scroll_end(animate=False)

    def action_dismiss_viewer(self) -> None:
        self.dismiss()

    def action_open_pager(self) -> None:
        from halo_harness.tui.widgets.cards import PagerScreen
        try:
            body = self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = "(could not read the log file)"
        self.app.push_screen(PagerScreen(self._title, body))

    def action_page_up(self) -> None:
        self.follow = False
        self.query_one("#transcript-log", RichLog).scroll_page_up()

    def action_page_down(self) -> None:
        self.query_one("#transcript-log", RichLog).scroll_page_down()


class TasksPanel(ModalScreen):
    """`/tasks` and Ctrl+T -- full-height, two tabs switched with `tab`
    ("Agents": every running/queued/background/finished sub-agent and
    background job of this session; "Board": the shared task board).
    Refreshes once a second (the same cadence `TranscriptViewer` polls
    its own log at) for as long as it stays open; Enter on an agent row
    opens its `TranscriptViewer`. Ctrl+T or Esc closes it -- both bound
    here too (not just at the App level) so the key always does the
    SAME thing while this screen is focused, regardless of Textual's own
    screen/app binding fallback order."""

    # `enter` is NOT bound here: `OptionList` already binds it itself (to
    # its own `action_select`, which posts `OptionSelected` rather than
    # bubbling the raw key up to the Screen) -- `on_option_list_option_
    # selected` below is the real "Enter on a row" handler, same
    # convention every other OptionList-based dialog in this package
    # uses (roles_editor.py, org_editor.py, mcp_status.py, ...).
    BINDINGS = [
        Binding("escape,ctrl+t", "close_panel", "Close", show=False),
        Binding("tab", "next_tab", "Switch tab", show=False),
    ]
    DEFAULT_CSS = """
    TasksPanel { align: center middle; }
    TasksPanel > Vertical { width: 96%; height: 94%; border: round $primary; background: $surface;
        padding: 1 2; }
    TasksPanel OptionList { height: 1fr; }
    """

    def __init__(self, *, list_agent_tasks: Callable[[], list],
                 read_task_board: Optional[Callable[[], list]] = None) -> None:
        super().__init__()
        self._list_agent_tasks = list_agent_tasks
        self._read_task_board = read_task_board or (lambda: [])
        self.tab = "agents"  # public: the pilot test asserts this directly
        self._rows: list = []
        self._timer = None

    def compose(self):
        yield Static("", id="tasks-title", classes="dialog-title")
        yield OptionList(id="tasks-list")

    def on_mount(self) -> None:
        self.refresh_rows()
        # Live on the Kali VM: without focus, Enter on the panel did nothing
        # (OptionList's own Enter binding only fires while it is focused),
        # so the transcript viewer was unreachable from the keyboard.
        self.query_one("#tasks-list", OptionList).focus()
        self._timer = self.set_interval(1.0, self.refresh_rows)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def refresh_rows(self) -> None:
        option_list = self.query_one("#tasks-list", OptionList)
        highlighted = option_list.highlighted
        option_list.clear_options()
        if self.tab == "agents":
            self._rows = list(self._list_agent_tasks() or [])
            running = sum(1 for r in self._rows if _status_word(r) == "running")
            queued = sum(1 for r in self._rows if _status_word(r) == "queued")
            self.query_one("#tasks-title", Static).update(
                f"Sub-agents -- {running} running, {queued} queued, {len(self._rows)} total "
                f"(Tab: task board, Enter: transcript, Esc/Ctrl+T: close)")
            if not self._rows:
                option_list.add_option(Option("No sub-agents in this session yet.", disabled=True))
            # round 3: a currently-mounted SubAgentCard (tui/widgets/
            # cards.py) knows this agent's own LIVE W2b phase word
            # (thinking/writing/tool/waiting) -- meta.json/the jsonl log
            # alone can only ever say "running". `transcript` is absent
            # for a bare FakeController-driven pilot test, hence getattr.
            cards = getattr(getattr(self.app, "transcript", None), "subagent_cards", {}) or {}
            for row in self._rows:
                card = cards.get(row.get("agent_id"))
                phase_word = card.phase_word if (card is not None and not card.done) else None
                option_list.add_option(Option(agent_row_text(row, phase_word=phase_word), id=row.get("agent_id")))
        else:
            self._rows = list(self._read_task_board() or [])
            self.query_one("#tasks-title", Static).update(
                f"Task board -- {len(self._rows)} tasks (Tab: sub-agents, Esc/Ctrl+T: close)")
            if not self._rows:
                option_list.add_option(Option("The task board is empty.", disabled=True))
            for row in self._rows:
                option_list.add_option(Option(board_row_text(row), id=row.get("id")))
        if highlighted is not None and option_list.option_count and highlighted < option_list.option_count:
            option_list.highlighted = highlighted
        elif option_list.option_count and self._rows:
            option_list.highlighted = 0  # first open: a row is highlighted, so Enter has a target

    def action_close_panel(self) -> None:
        self.dismiss()

    def action_next_tab(self) -> None:
        self.tab = "board" if self.tab == "agents" else "agents"
        self.refresh_rows()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter (or a click) on a row -- `OptionList` itself binds Enter
        to `action_select`, which posts THIS message rather than
        bubbling the raw key up to a Screen-level binding, so this is
        the real "Enter opens the transcript viewer" handler, not an
        `action_*` method."""
        if self.tab != "agents":
            return
        agent_id = event.option_id
        row = next((r for r in self._rows if r.get("agent_id") == agent_id), None) if agent_id else None
        if row is None or not row.get("log_path"):
            return
        self.app.push_screen(TranscriptViewer(row["log_path"], title=row.get("title") or agent_id))
