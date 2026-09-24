"""rolo_claude.tools.base -- the Tool protocol (H1 scope G). Only `Read`
exists in H1 (tools/read.py); Write/Edit/Bash/PowerShell/Glob/Grep/... are
H2 -- this module's shape is deliberately small enough that adding them
later doesn't need to change it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ToolContext:
    """Whatever a tool's `run()` needs from the session -- H1 keeps this to
    just `cwd` (Read has no other dependency); H2's Bash/Edit/etc. will
    extend it (permissions, abort event, ...) without breaking Read's own
    signature, since every field here has a default."""
    cwd: Path


@dataclass
class ToolResult:
    """`content` is a plain string or a list of Anthropic content blocks
    (a Read of an image, later); `is_error` maps to the tool_result
    block's own `is_error` flag."""
    content: Any
    is_error: bool = False


class Tool:
    """Base class every built-in tool subclasses. `name`/`description`/
    `input_schema` together ARE the Anthropic tool definition sent on the
    wire (via `definition()`); `description` is Claude Code's own wording
    (binary-facts sec.14) so a weaker model gets the identical guidance a
    real Claude Code session would."""

    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}}

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def summary(self, input: dict) -> str:
        """One-line human summary for a tool card / verbose log, e.g.
        "Read(src/main.py)". Defaults to just the tool name; a tool with a
        single obvious "subject" argument should override this."""
        return self.name

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}
