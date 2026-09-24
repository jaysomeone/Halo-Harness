"""rolo_claude.commands.builtins -- the ~22 built-in `/name` commands (U0
scope B), each with a headless-facade implementation: `-p "/cost"` and
`-p "/help"` must produce real text without a TUI (headless.py calls
`cmd.run(args_text, facade)` and either prints the string directly, for
kind "core"/"ui", or feeds it back through the agent loop as the turn's
actual prompt, for kind "prompt"). "ui" kind commands still return a real
(short, honest) string here -- they just describe what needs the
interactive TUI instead of performing it, rather than erroring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rolo_claude.commands.registry import Registry, SlashCommand


@dataclass
class HeadlessFacade:
    """Everything a builtin's `run()` needs, already resolved by the
    caller (headless.py) -- a thin read-only view, never a place a command
    mutates process state from."""
    cwd: Path
    settings: object = None
    claude_json: dict = field(default_factory=dict)
    model_ref: str = ""
    permission_mode: str = "default"
    tool_registry: object = None
    registry: Optional[Registry] = None
    memory_store: object = None
    instructions: object = None
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str = ""
    effort: Optional[str] = None
    theme: str = ""
    context_limit: Optional[int] = None
    mcp_servers: dict = field(default_factory=dict)


def _cmd_help(args: str, facade: HeadlessFacade) -> str:
    rows = facade.registry.help_rows() if facade.registry else []
    lines = ["Available commands:"]
    width = max((len(inv) for inv, _ in rows), default=0)
    for inv, desc in rows:
        lines.append(f"  {inv.ljust(width)}  {desc}" if desc else f"  {inv}")
    return "\n".join(lines)


def _cmd_clear(args: str, facade: HeadlessFacade) -> str:
    return "Conversation cleared (no-op outside an interactive session -- each -p call already starts fresh)."


def _cmd_compact(args: str, facade: HeadlessFacade) -> str:
    return "Nothing to compact: a single -p turn has no prior history to summarize."


def _cmd_cost(args: str, facade: HeadlessFacade) -> str:
    return (f"Total cost: ${facade.cost_usd:.4f} across {facade.num_turns} turn(s) "
            f"(model: {facade.model_ref or '?'})")


def _cmd_context(args: str, facade: HeadlessFacade) -> str:
    limit = facade.context_limit if facade.context_limit is not None else "?"
    return f"Context window: {limit} tokens (model: {facade.model_ref or '?'}); nothing used yet this call."


def _cmd_model(args: str, facade: HeadlessFacade) -> str:
    return f"Current model: {facade.model_ref or '?'}\nEffort: {facade.effort or 'default'}"


def _cmd_mcp(args: str, facade: HeadlessFacade) -> str:
    if not facade.mcp_servers:
        return "No MCP servers configured."
    lines = ["Configured MCP servers:"]
    for name, spec in sorted(facade.mcp_servers.items()):
        kind = spec.get("type", "stdio") if isinstance(spec, dict) else "stdio"
        lines.append(f"  {name} ({kind}) - not checked (MCP client arrives in a later milestone)")
    return "\n".join(lines)


def _cmd_memory(args: str, facade: HeadlessFacade) -> str:
    if facade.memory_store is None:
        return "Auto-memory is not available for this session."
    if not facade.memory_store.enabled:
        return "Auto-memory is disabled (autoMemoryEnabled=false or CLAUDE_CODE_DISABLE_AUTO_MEMORY=1)."
    idx = facade.memory_store.load_index()
    return (f"Memory directory: {facade.memory_store.memory_dir_path}\n"
            f"MEMORY.md: {'present' if idx.exists else 'not found'}\n"
            f"Topic files: {len(idx.topics)}")


def _cmd_permissions(args: str, facade: HeadlessFacade) -> str:
    s = facade.settings
    allow = len(s.permissions_allow) if s else 0
    ask = len(s.permissions_ask) if s else 0
    deny = len(s.permissions_deny) if s else 0
    return (f"Permission mode: {facade.permission_mode}\n"
            f"Settings rules -- allow: {allow}  ask: {ask}  deny: {deny}")


def _cmd_plan(args: str, facade: HeadlessFacade) -> str:
    return f"Plan review needs the interactive TUI. Current permission mode: {facade.permission_mode}."


def _cmd_resume(args: str, facade: HeadlessFacade) -> str:
    return "The session picker needs the interactive TUI; pass --resume <session-id> on the command line instead."


def _cmd_status(args: str, facade: HeadlessFacade) -> str:
    from rolo_claude import __version__
    mcp_n = len(facade.mcp_servers)
    return (f"rolo-claude {__version__}\n"
            f"Model: {facade.model_ref or '?'}\n"
            f"cwd: {facade.cwd}\n"
            f"Permission mode: {facade.permission_mode}\n"
            f"MCP servers: {mcp_n}\n"
            f"Theme: {facade.theme or '?'}")


def _cmd_config(args: str, facade: HeadlessFacade) -> str:
    if args.strip():
        return "rolo-claude: /config is read-only in headless mode; edit ~/.claude/settings.json or pass --settings."
    theme = facade.theme or "?"
    mode = facade.permission_mode
    return f"model={facade.model_ref or '?'} permissionMode={mode} theme={theme}"


def _cmd_skills(args: str, facade: HeadlessFacade) -> str:
    names = [c.name for c in (facade.registry.all() if facade.registry else []) if c.source == "skill"]
    if not names:
        return "No skills discovered."
    return "Skills:\n" + "\n".join(f"  /{n}" for n in names)


def _cmd_agents(args: str, facade: HeadlessFacade) -> str:
    return ("No custom sub-agents are loaded yet (sub-agents arrive in a later milestone).\n"
            "Built-in agent types: Explore, Plan, general-purpose.")


def _cmd_effort(args: str, facade: HeadlessFacade) -> str:
    return f"Effort level: {facade.effort or 'not set (provider default)'}"


def _cmd_init(args: str, facade: HeadlessFacade) -> str:
    return (
        "Please analyze this codebase and generate or update a CLAUDE.md file for future instances "
        "of the agent working in this repository. Cover: build/lint/test commands (especially for "
        "running a single test), the high-level architecture and structure, and any existing Cursor "
        "rules (.cursor/rules/ or .cursorrules) or Copilot rules (.github/copilot-instructions.md), "
        "incorporating them if present. Keep it concise and focused on non-obvious information a new "
        "contributor (or agent) would otherwise have to rediscover."
    )


def _cmd_doctor(args: str, facade: HeadlessFacade) -> str:
    from rolo_claude.doctor import run_checks
    lines, _ok = run_checks(cwd=facade.cwd)
    return "\n".join(lines)


def _cmd_export(args: str, facade: HeadlessFacade) -> str:
    return "Export needs the interactive TUI's file picker; nothing to export from a single -p turn."


def _cmd_add_dir(args: str, facade: HeadlessFacade) -> str:
    if not args.strip():
        return "Usage: /add-dir <directory> (or pass --add-dir on the command line to start with one)."
    return f"rolo-claude: /add-dir needs a running session to extend; pass --add-dir {args.strip()!r} on the command line instead."


def _cmd_theme(args: str, facade: HeadlessFacade) -> str:
    from rolo_claude import theme as theme_mod
    name = args.strip()
    if not name:
        return f"Current theme: {facade.theme or theme_mod.DEFAULT_THEME}"
    if not theme_mod.is_valid_theme(name):
        return f"rolo-claude: not a valid theme name: {name!r} (expected one of {sorted(theme_mod.VALID_THEMES)})"
    theme_mod.persist_theme(name)
    return f"Theme set to {name}."


def _cmd_exit(args: str, facade: HeadlessFacade) -> str:
    return "Nothing to exit: a single -p call already ends after this turn."


# name -> (kind, description, argument_hint, run)
_BUILTIN_SPECS = {
    "help": ("core", "Show available commands", None, _cmd_help),
    "clear": ("ui", "Clear the conversation history", None, _cmd_clear),
    "compact": ("ui", "Summarize the conversation to free up context", "[instructions]", _cmd_compact),
    "cost": ("core", "Show the total cost and duration of the session", None, _cmd_cost),
    "context": ("core", "Show current context window usage", None, _cmd_context),
    "model": ("core", "Show or change the active model", "[model]", _cmd_model),
    "mcp": ("core", "List configured MCP servers", None, _cmd_mcp),
    "memory": ("core", "Show the auto-memory directory and index", None, _cmd_memory),
    "permissions": ("core", "Show the active permission mode and rule counts", None, _cmd_permissions),
    "plan": ("ui", "Review the current plan", None, _cmd_plan),
    "resume": ("ui", "Resume a previous session", "[session-id]", _cmd_resume),
    "status": ("core", "Show session status", None, _cmd_status),
    "config": ("core", "Show or set a config value", "[key=value]", _cmd_config),
    "skills": ("core", "List discovered skills", None, _cmd_skills),
    "agents": ("core", "List available sub-agents", None, _cmd_agents),
    "effort": ("core", "Show the active reasoning effort level", None, _cmd_effort),
    "init": ("prompt", "Analyze the codebase and write/update CLAUDE.md", None, _cmd_init),
    "doctor": ("core", "Check the health of this rolo-claude installation", None, _cmd_doctor),
    "export": ("ui", "Export the conversation", None, _cmd_export),
    "add-dir": ("core", "Add a working directory", "<directory>", _cmd_add_dir),
    "theme": ("core", "Show or set the color theme", "[theme]", _cmd_theme),
    "exit": ("ui", "Exit rolo-claude", None, _cmd_exit),
}


def register_builtins(reg: Registry) -> None:
    for name, (kind, description, hint, run) in _BUILTIN_SPECS.items():
        reg.add(SlashCommand(name=name, description=description, kind=kind, argument_hint=hint,
                              source="builtin", run=run))
