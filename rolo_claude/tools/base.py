"""rolo_claude.tools.base -- the Tool protocol. H1 had only `Read`; H2 adds
Write/Edit/Bash/PowerShell/Glob/Grep/WebFetch/TodoWrite/ToolSearch/
AskUserQuestion/Skill on top of this same small shape.

`is_read_only`/`is_destructive` are INFORMATIONAL ONLY (used for the
read-only concurrency pool in tools/registry.py and for a tool card's
display) -- rolo's "no cyber blocks" decision means neither one is ever
consulted by the permission engine (rolo_claude/permissions.py) to gate or
auto-deny anything; only the user's own rules and modes do that.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class ToolContext:
    """Whatever a tool's `run()` needs from the session. Every field has a
    default so `ToolContext(cwd=...)` (H1's own call shape, and every
    existing Read test) keeps working unchanged.

    - `read_cache`: str(path) -> mtime-at-Read-time, shared for the whole
      session. Write's must-Read-first check and Edit's stale-file check
      both consult/update it (agent/loop.py owns the ONE dict instance and
      threads it through every dispatch call).
    - `abort`: set by an interrupt source (Bash kill, a future Esc) --
      Bash.run() polls it to kill its subprocess group early.
    - `bash_state`: persistent shell state across Bash calls IN ONE SESSION
      (currently just {"cwd": Path}) -- `cd` in one call is visible to the
      next (scope A: "cd persistence per session via a trailing marker
      line").
    - `session_dir`: base directory for this session's spilled tool results
      (`<session_dir>/tool-results/<tool_use_id>.txt`); None means "don't
      spill" (e.g. a bare `ToolContext(cwd=...)` in a unit test).
    - `extra_dirs`: additional trusted working directories (settings
      `additionalDirectories` + `--add-dir`), used only by the permission
      engine, but threaded through ToolContext too so a tool that wants to
      know its own working-directory set (none do yet) can.
    """
    cwd: Path
    read_cache: dict = field(default_factory=dict)
    abort: "threading.Event" = field(default_factory=threading.Event)
    bash_state: dict = field(default_factory=dict)
    session_dir: Optional[Path] = None
    extra_dirs: list = field(default_factory=list)
    tool_use_id: Optional[str] = None
    # Bash/PowerShell only: called with each NEW chunk of merged stdout/
    # stderr roughly every 0.5s while a subprocess runs, so a caller (agent/
    # loop.py) can bridge it into `tool_progress` events; None (the default,
    # e.g. a bare unit test) just means "nobody's listening" -- the tool
    # still buffers its full output normally either way.
    progress_cb: Optional[Callable[[str], None]] = None
    # Effective environment for a subprocess tool (settings.effective_env);
    # None means "use this process's own os.environ" (every existing/unit
    # test call site).
    env: Optional[dict] = None
    # ToolSearch's own registry to search over -- untyped (`object`, not
    # ToolRegistry) so this module never has to import tools/registry.py,
    # which itself imports THIS module (that reverse import would cycle).
    registry: Optional[object] = None
    # H3 scope C: the session's SessionCatalog (agent/catalog.py), when MCP
    # tools exist -- untyped for the same reverse-import reason as
    # `registry`. Only ToolSearchTool consults this; when it's None (every
    # existing test's bare ToolContext, and any session with zero MCP
    # servers) ToolSearch falls back to searching `registry` alone,
    # unchanged from H2.
    catalog: Optional[object] = None
    # H3 scope B: the session's McpManager, when one exists -- consulted by
    # ListMcpResourcesTool/ReadMcpResourceTool only (McpTool itself carries
    # its own manager reference directly, set at construction).
    mcp_manager: Optional[object] = None
    # H4 scope C: the Skill tool's `allowed-tools` -> session rule (D-CFG:
    # "until the next user message") -- a callable(rule_text) -> bool, set
    # by agent/loop.py to `PermissionEngine.add_session_allow_rule(...,
    # temporary=True)`; None (every existing test's bare ToolContext) just
    # means a skill's allowed-tools grant its own `` !`cmd` `` pre-exec
    # (still enforced) but never widens the session's OWN permission rules.
    session_allow_rule: Optional[Callable[[str], bool]] = None
    # H6 scope B: `agent/subagent.AgentRuntime` for the OWNING session --
    # everything tools/agent.py's AgentTool needs that a bare ToolContext
    # doesn't otherwise carry (the discovered AgentSpec catalog, the live
    # parent Session, the depth counter, a bounded concurrency gate).
    # Untyped for the same reverse-import reason as `registry`/`catalog`
    # (agent/subagent.py imports agent/loop.py, which imports THIS module).
    # None means "Agent tool unavailable" (every pre-H6 test, a bare
    # ToolContext, or a session that opted out) -- AgentTool.run() reports
    # a plain error instead of crashing.
    agent_runtime: Optional[object] = None
    # H6 scope B: a PER-CALL sink for a child session's own events, set via
    # `dataclasses.replace(ctx, tool_use_id=..., agent_event_cb=...)` at
    # dispatch time exactly like `progress_cb` -- AgentTool.run() calls
    # this once per child event (already tagged with the child's agent_id)
    # as the child's own turn() generator is drained, so agent/loop.py can
    # replay them into the PARENT's live event stream right after dispatch
    # returns, the same "collect during a blocking call, replay after"
    # idiom `progress_cb`/`_BoundedChunks` already uses for Bash. None
    # (every non-Agent tool, and any bare ToolContext) means "nobody's
    # collecting" -- AgentTool.run() still works, it just has no live
    # side-channel to report through.
    agent_event_cb: Optional[Callable[[object], None]] = None
    # finding 6: the session's own CLAUDE_ENV_FILE path (hooks.env_file_
    # path(session_id)) -- the Bash tool sources it FRESH, in its own
    # shell, before every command (never just a one-time literal-parse
    # merged into `env`), so a LATER SessionStart/Setup/CwdChanged/
    # FileChanged hook that appends more `export` lines takes effect on
    # the very next Bash call with no restart needed, and `$VAR`
    # expansion/`export PATH="$PATH:/x"`-style appends behave like a real
    # shell script instead of a literal string. None (every pre-finding-6
    # test, a bare ToolContext, or a hookless session) just means no
    # `source` line is added -- unchanged prior behaviour.
    env_file: Optional[Path] = None
    # finding 9: the owning Session's PermissionEngine -- the Skill tool's
    # OWN `` !`cmd` `` pre-execution (commands/registry.run_preexec_
    # commands) needs it to route through `decide()` instead of the old
    # strict-frontmatter-only gate. Untyped for the same reverse-import
    # reason as `registry`/`catalog` (permissions.py has no reason to
    # import this module, but keeping the pattern consistent). None
    # (every pre-finding-9 test, or a bare ToolContext) falls back to
    # `run_preexec_commands`'s own old behaviour.
    permission_engine: Optional[object] = None
    # finding 12: the session's current --effort, for the Skill tool's own
    # ${CLAUDE_EFFORT} substitution -- None (every pre-finding-12 test)
    # just means that variable is left unsubstituted in a skill body.
    effort: Optional[str] = None


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
    is_read_only: bool = False
    is_destructive: bool = False
    # Generic result-truncation cap in CHARS for tools/registry.py's spill
    # step; None means "this tool manages its own truncation" (Read already
    # does its own line/char caps -- see tools/read.py). Per-tool overrides
    # below (Bash 30_000, WebFetch 100_000).
    result_cap: Optional[int] = 25_000 * 4

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def summary(self, input: dict) -> str:
        """One-line human summary for a tool card / verbose log, e.g.
        "Read(src/main.py)". Defaults to just the tool name; a tool with a
        single obvious "subject" argument should override this."""
        return self.name

    def permission_content(self, input: dict) -> str:
        """The string a permission `Rule` is matched against for this call
        (rolo_claude/permissions.py never inspects `input` itself -- every
        tool decides what its own "content" means: a file path, a shell
        command, a URL, ...). Defaults to "" (a tool with no natural
        content position, e.g. TodoWrite) so an unrecognized/param-only
        rule shape still has SOMETHING deterministic to compare against."""
        return ""

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}
