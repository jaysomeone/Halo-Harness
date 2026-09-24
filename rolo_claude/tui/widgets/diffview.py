"""rolo_claude.tui.widgets.diffview -- `DiffView`, mounted by app.py right
after an Edit/Write `ToolCard` (D-TUI: "Edit/Write cards mount DiffView
(unified diff, context 3)"). Pure-Python diff (stdlib `difflib`); no file
I/O of its own -- callers hand it the two text snapshots directly (Edit's
`old_string`/`new_string` come straight from the tool_use input; Write has
no "before" text available at event time, so `unified_diff_text` is called
with `before=""`, rendering the new content as a plain addition, which is
still an honest, readable diff for a brand-new/fully-rewritten file).
"""

from __future__ import annotations

import difflib

from rich.text import Text
from textual.widgets import Static


def unified_diff_text(before: str, after: str, *, path: str = "", context: int = 3) -> str:
    before_lines = before.splitlines(keepends=True) or [""]
    after_lines = after.splitlines(keepends=True) or [""]
    diff = difflib.unified_diff(before_lines, after_lines, fromfile=path or "before",
                                 tofile=path or "after", n=context)
    text = "".join(diff)
    return text if text else "(no changes)"


class DiffView(Static):
    """Renders a unified diff with +/- coloring via Rich `Text`, so callers
    never need their own ANSI/markup escaping concerns."""

    def __init__(self, before: str, after: str, *, path: str = "", context: int = 3) -> None:
        super().__init__(classes="diff-view")
        self._diff_text = unified_diff_text(before, after, path=path, context=context)
        self._render_diff()

    def _render_diff(self) -> None:
        rendered = Text()
        for i, line in enumerate(self._diff_text.splitlines()):
            if i:
                rendered.append("\n")
            if line.startswith("+++") or line.startswith("---"):
                rendered.append(line, style="bold")
            elif line.startswith("@@"):
                rendered.append(line, style="cyan")
            elif line.startswith("+"):
                rendered.append(line, style="green")
            elif line.startswith("-"):
                rendered.append(line, style="red")
            else:
                rendered.append(line, style="dim")
        self.update(rendered)
