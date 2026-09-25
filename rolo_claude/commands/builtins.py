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
    # H3 scope D: McpManager.status()'s own list of dicts, when a real
    # manager was built this session -- None (the default, and every H2-
    # era call site) means "no MCP client ran" and `_cmd_mcp` falls back
    # to its old raw-config-only wording.
    mcp_status: Optional[list] = None
    # H5 scope D must-do: the review's "TUI's /cost, /status, /context,
    # /model currently read a static facade showing $0.0000" -- `cost_usd`/
    # `num_turns`/`context_limit` above are snapshotted ONCE at facade-
    # construction time and never updated. `session` (the real, live
    # agent.loop.Session instance, when one is running -- None for every
    # bare/unit-test facade and for a facade built before a Session exists)
    # is a REFERENCE, not a snapshot: `session.cost_meter`/`session.log`
    # reflect whatever has ACTUALLY happened by the time a command reads
    # them, no matter how long after facade construction that is.
    session: object = None


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
    """H5 scope B: `/compact [instructions]` -- runs the REAL dsh-replay
    compaction synchronously (this command's own contract is "return a
    string", so the compaction generator is simply drained here rather
    than streamed) when a live Session is attached; the old canned
    response remains the fallback for a bare -p turn/unit-test facade with
    no session (a single turn has no prior history worth summarising)."""
    session = getattr(facade, "session", None)
    if session is None:
        return "Nothing to compact: a single -p turn has no prior history to summarize."
    custom = args.strip() or None
    done = failed = None
    for ev in session._run_compaction(session.turn_count, trigger="manual", custom_instructions=custom):
        if ev.kind != "compaction":
            continue
        if ev.data.get("phase") == "done":
            done = ev.data
        elif ev.data.get("phase") == "failed":
            failed = ev.data
    # finding 4: a manual /compact can genuinely fail (overflow even after
    # the flattened-serialisation fallback, an exhausted-retries upstream
    # failure, Esc) -- the log is then left byte-for-byte unchanged, so say
    # so plainly instead of the old blanket "reported no result (see logs)".
    if failed is not None:
        return f"Compaction failed: {failed.get('reason') or 'see logs'}. The conversation is unchanged."
    if done is None:
        return "Compaction ran but reported no result (see logs)."
    before, after = done.get("tokens_before"), done.get("tokens_after")
    saved = f", freed ~{before - after} tokens" if isinstance(before, int) and isinstance(after, int) else ""
    return f"Compacted the conversation (~{before} -> ~{after} tokens{saved})."


def _cmd_cost(args: str, facade: HeadlessFacade) -> str:
    session = getattr(facade, "session", None)
    if session is not None:
        cm = session.cost_meter
        cost_str = f"${cm.total_usd:.4f}" if cm.has_cost_data else "n/a (provider does not report cost)"
        return f"Total cost: {cost_str} across {cm.turns} turn(s) (model: {facade.model_ref or '?'})"
    return (f"Total cost: ${facade.cost_usd:.4f} across {facade.num_turns} turn(s) "
            f"(model: {facade.model_ref or '?'})")


def _cmd_context(args: str, facade: HeadlessFacade) -> str:
    """H5 scope D: a real breakdown (system, tools, messages, pruned) from
    the LIVE session log when one is attached, via agent/derive.py +
    agent/prune.py -- the same pipeline a real request would build."""
    limit = facade.context_limit if facade.context_limit is not None else "?"
    session = getattr(facade, "session", None)
    if session is not None:
        from rolo_claude.agent.compact import opencode_usable
        from rolo_claude.agent.derive import derive_request
        from rolo_claude.agent.prune import context_breakdown, prune_messages
        system_text, messages, tools = derive_request(session.log, tools=None)
        pruned = prune_messages(messages)
        bd = context_breakdown(system_text, messages, tools, pruned)
        pct = round(100.0 * bd["total"] / limit, 1) if isinstance(limit, int) and limit else None
        usable = opencode_usable(session.model_profile.context_tokens, session.model_profile.max_output_tokens)
        lines = [
            f"Context window: {limit} tokens (model: {facade.model_ref or '?'})",
            f"  system:   ~{bd['system']:>7} tokens",
            f"  tools:    ~{bd['tools']:>7} tokens",
            f"  messages: ~{bd['messages']:>7} tokens" + (f"  (pruned ~{bd['pruned']} tokens)" if bd["pruned"] else ""),
            f"  total:    ~{bd['total']:>7} tokens" + (f"  ({pct}% of window)" if pct is not None else ""),
            f"  compacts once usable prompt tokens reach ~{usable} (OpenCode floor) or the 80% dsh trigger, whichever is lower",
        ]
        return "\n".join(lines)
    return f"Context window: {limit} tokens (model: {facade.model_ref or '?'}); nothing used yet this call."


def _cmd_model(args: str, facade: HeadlessFacade) -> str:
    # U5 must-do: read from the LIVE session when one is attached, not the
    # facade's construction-time snapshot -- a `/model` switch earlier in
    # the same session used to never show up here.
    session = getattr(facade, "session", None)
    if session is not None:
        model_id = getattr(getattr(session, "model_ref", None), "raw", None) or facade.model_ref or "?"
        effort = getattr(session, "effort", None) or facade.effort or "default"
        return f"Current model: {model_id}\nEffort: {effort}"
    return f"Current model: {facade.model_ref or '?'}\nEffort: {facade.effort or 'default'}"


def _cmd_mcp(args: str, facade: HeadlessFacade) -> str:
    if facade.mcp_status is not None:
        # H3 scope D: real per-server health, same line format `mcp list` uses.
        from rolo_claude.mcp_cli import format_mcp_list_line
        if not facade.mcp_status:
            return "No MCP servers configured."
        lines = ["Configured MCP servers:"]
        for entry in sorted(facade.mcp_status, key=lambda e: e.get("name", "")):
            lines.append(f"  {format_mcp_list_line(entry)}")
        return "\n".join(lines)
    if not facade.mcp_servers:
        return "No MCP servers configured."
    lines = ["Configured MCP servers:"]
    for name, spec in sorted(facade.mcp_servers.items()):
        kind = spec.get("type", "stdio") if isinstance(spec, dict) else "stdio"
        lines.append(f"  {name} ({kind}) - not checked (no MCP client ran this session)")
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


def _cmd_improve(args: str, facade: HeadlessFacade) -> str:
    """H10 Part B5: `-p "/improve"` NEVER drafts or writes -- it needs the
    interactive TUI's card review (`a`/`e`/`s`/`d`/`q`); a real `-p`
    invocation points at the real headless surface instead
    (`rolo-claude improve [--json] [--apply ...]`, a separate top-level
    subcommand, never this slash command)."""
    return "Improve review needs the interactive TUI. Use `rolo-claude improve` for the headless surface."


def _cmd_resume(args: str, facade: HeadlessFacade) -> str:
    from rolo_claude.agent import sessions as agent_sessions

    if args.strip():
        session_id, err = agent_sessions.resolve_resume(facade.cwd, args.strip())
        if session_id is None:
            return f"rolo-claude: --resume: {err}"
        return (f"Found session {session_id} -- headless mode has no interactive picker to switch into "
                f"it mid-turn; pass `-r {session_id}` on the command line to actually resume it.")
    rows = agent_sessions.list_sessions(facade.cwd) if hasattr(agent_sessions, "list_sessions") else []
    if not rows:
        return "The session picker needs the interactive TUI; pass --resume <session-id|name> on the command line instead."
    lines = ["The interactive picker needs the TUI; recent sessions for this directory (pass --resume <id> or a title):"]
    for row in rows[:10]:
        title = row.get("title") or "(untitled)"
        lines.append(f"  {row['id']}  {title}")
    return "\n".join(lines)


def _cmd_status(args: str, facade: HeadlessFacade) -> str:
    from rolo_claude import __version__
    mcp_n = len(facade.mcp_servers)
    session = getattr(facade, "session", None)
    # U5 must-do: model/permission-mode also read from the LIVE session
    # (a `/model` switch or a plan-mode/Shift+Tab mode change mid-session
    # used to never show up in `/status`, only the facade's snapshot).
    model_id = facade.model_ref or "?"
    permission_mode = facade.permission_mode
    if session is not None:
        cm = session.cost_meter
        cost_line = f"Cost so far: ${cm.total_usd:.4f} ({cm.turns} turn(s))" if cm.has_cost_data else "Cost so far: n/a"
        model_id = getattr(getattr(session, "model_ref", None), "raw", None) or model_id
        engine = getattr(session, "permission_engine", None)
        permission_mode = getattr(engine, "mode", None) or permission_mode
    else:
        cost_line = f"Cost so far: ${facade.cost_usd:.4f} ({facade.num_turns} turn(s))"
    return (f"rolo-claude {__version__}\n"
            f"Model: {model_id}\n"
            f"cwd: {facade.cwd}\n"
            f"Permission mode: {permission_mode}\n"
            f"MCP servers: {mcp_n}\n"
            f"Theme: {facade.theme or '?'}\n"
            f"{cost_line}")


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
    """H6 scope A/F: lists every discovered agent definition (built-ins +
    `.claude/agents`/`~/.claude/agents`/`--agents`/managed/plugin), name-
    sorted, `name -- description` -- pulled straight off the live
    session's own `agent_runtime.agents` (the SAME catalog `Agent(subagent_
    type=...)` resolves against) when one is running, so this never drifts
    from what a sub-agent call would actually see."""
    session = getattr(facade, "session", None)
    agents = getattr(getattr(session, "agent_runtime", None), "agents", None)
    if not agents:
        from rolo_claude.config.agents_md import discover_agents
        agents = discover_agents(facade.cwd, settings=facade.settings)
    if not agents:
        return "No agent definitions found (not even the built-ins -- this shouldn't happen)."
    lines = ["Available sub-agents:"]
    for name in sorted(agents):
        spec = agents[name]
        lines.append(f"  {name} ({spec.source}) -- {spec.description}")
    return "\n".join(lines)


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


# ---- U5 sessions UX + git-shadow rewind + keymap: "ui"-kind stubs here
# (headless -p has no interactive picker/card/worker thread to run these
# for real), real behaviour lives in tui/slash.py's own handler dict --
# same split as /model, /mcp, /resume, /permissions, /theme above. ------

def _cmd_rename(args: str, facade: HeadlessFacade) -> str:
    """H6 scope D: sets `index.json[session_id].title` for real -- cwd/
    session_id are both known to a headless facade (unlike the picker/
    fork-and-switch commands above, this needs no running worker thread)."""
    title = args.strip()
    if not title:
        return "Usage: /rename <title>"
    from rolo_claude.agent import sessions as agent_sessions
    agent_sessions.set_title(facade.cwd, facade.session_id, title)
    return f"Renamed this session to {title!r}."


def _cmd_fork(args: str, facade: HeadlessFacade) -> str:
    """H6 scope D: copies THIS session's log under a new id right now (the
    original is never touched) -- `-p` has no live worker thread to hand
    the new session off to mid-turn, so the result is the id to `-r` into
    afterward, not a live switch (that part IS the TUI's own job)."""
    from rolo_claude.agent import sessions as agent_sessions
    if not facade.session_id:
        return "rolo-claude: no active session to fork."
    new_id = agent_sessions.fork_session(facade.cwd, facade.session_id)
    return f"Forked this session -> {new_id}. Continue it with: -r {new_id}"


def _cmd_stats(args: str, facade: HeadlessFacade) -> str:
    session = getattr(facade, "session", None)
    if session is None:
        return f"Total cost: ${facade.cost_usd:.4f} across {facade.num_turns} turn(s)."
    from rolo_claude.controller import compute_session_stats, format_cache_tokens_suffix
    stats = compute_session_stats(session.log.nodes())
    lines = [f"Turns: {stats['turns']}", f"Total cost: ${stats['total_cost_usd']:.4f}"]
    for model, bucket in sorted(stats["per_model"].items()):
        lines.append(f"  {model}: {bucket['calls']} call(s), "
                     f"{bucket['input_tokens']}in/{bucket['output_tokens']}out tok"
                     f"{format_cache_tokens_suffix(bucket)}, ${bucket['cost_usd']:.4f}")
    for name, n in sorted(stats["tool_counts"].items()):
        lines.append(f"  tool {name}: {n} call(s)")
    return "\n".join(lines)


def _cmd_tasks(args: str, facade: HeadlessFacade) -> str:
    """H8 scope A: lists every background Bash job this session has
    started (via `run_in_background` or a timed-out foreground command
    moved to the background), most-recently-started last."""
    session = getattr(facade, "session", None)
    registry = getattr(session, "job_registry", None)
    jobs = registry.list_jobs() if registry is not None else []
    if not jobs:
        return "No background jobs in this session."
    lines = ["Background jobs:"]
    for job in jobs:
        cmd = job["command"]
        if len(cmd) > 60:
            cmd = cmd[:60] + "..."
        lines.append(f"  {job['id']}  [{job['status']}]  {job['description'] or cmd}")
    return "\n".join(lines)


def _cmd_rewind(args: str, facade: HeadlessFacade) -> str:
    return "rolo-claude: /rewind needs the interactive TUI (a file's history lives per-session)."


def _cmd_undo(args: str, facade: HeadlessFacade) -> str:
    return "rolo-claude: /undo needs the interactive TUI."


def _cmd_redo(args: str, facade: HeadlessFacade) -> str:
    return "rolo-claude: /redo needs the interactive TUI."


def _cmd_keybindings(args: str, facade: HeadlessFacade) -> str:
    from rolo_claude.tui.keys import load_keymap
    keymap = load_keymap()
    lines = ["Keybindings (~/.claude/keybindings.json merges onto these):"]
    for ctx in sorted(keymap):
        lines.append(f"  [{ctx}]")
        for chord in sorted(keymap[ctx]):
            lines.append(f"    {chord:<20} {keymap[ctx][chord]}")
    return "\n".join(lines)


# name -> (kind, description, argument_hint, run)
_BUILTIN_SPECS = {
    "help": ("core", "Show available commands", None, _cmd_help),
    "clear": ("ui", "Clear the conversation history", None, _cmd_clear),
    "compact": ("core", "Summarize the conversation to free up context", "[instructions]", _cmd_compact),
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
    "rename": ("ui", "Rename this session", "<title>", _cmd_rename),
    "fork": ("ui", "Fork this session into a new one", None, _cmd_fork),
    "stats": ("core", "Show tokens/cost per model and tool-call counts (--models, --tools)", None, _cmd_stats),
    "tasks": ("core", "List background Bash jobs started this session", None, _cmd_tasks),
    "rewind": ("ui", "Restore the working tree to a recorded step", "[step-id]", _cmd_rewind),
    "undo": ("ui", "Rewind one recorded step back", None, _cmd_undo),
    "redo": ("ui", "Rewind one recorded step forward", None, _cmd_redo),
    "keybindings": ("core", "Show the active keybindings", None, _cmd_keybindings),
    "improve": ("ui", "Review self-improvement candidates from recent sessions", None, _cmd_improve),
}


def register_builtins(reg: Registry) -> None:
    for name, (kind, description, hint, run) in _BUILTIN_SPECS.items():
        reg.add(SlashCommand(name=name, description=description, kind=kind, argument_hint=hint,
                              source="builtin", run=run))
