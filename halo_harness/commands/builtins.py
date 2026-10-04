"""halo_harness.commands.builtins -- the ~22 built-in `/name` commands (U0
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

from halo_harness.commands.registry import Registry, SlashCommand


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
        line = f"Total cost: {cost_str} across {cm.turns} turn(s) (model: {facade.model_ref or '?'})"
    else:
        line = (f"Total cost: ${facade.cost_usd:.4f} across {facade.num_turns} turn(s) "
                f"(model: {facade.model_ref or '?'})")
    # H15 part 2 addendum 4: the same OpenRouter balance figure the status
    # bar shows, with the key label and reading time -- omitted (no second
    # line) when no fetch has ever succeeded (not enabled, or offline).
    from halo_harness.providers.openrouter_account import format_balance_line
    balance_line = format_balance_line()
    return f"{line}\n{balance_line}" if balance_line else line


def _cmd_context(args: str, facade: HeadlessFacade) -> str:
    """H5 scope D: a real breakdown (system, tools, messages, pruned) from
    the LIVE session log when one is attached, via agent/derive.py +
    agent/prune.py -- the same pipeline a real request would build."""
    limit = facade.context_limit if facade.context_limit is not None else "?"
    session = getattr(facade, "session", None)
    if session is not None:
        from halo_harness.agent.compact import opencode_usable
        from halo_harness.agent.derive import derive_request
        from halo_harness.agent.prune import context_breakdown, prune_messages
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
        # Halo 2.0.1 W2a (HALO-2.0.1-liveness-tips-brief.md Part C): every
        # request parameter THIS route changed from what was asked --
        # effort/temperature/max_tokens clamps -- as "requested X, sent Y",
        # the SAME comparison the stream-json init line's `effort_sent` and
        # (W2b) the TUI card/status chip read.
        profile = getattr(session, "provider_profile", None)
        if profile is not None:
            from halo_harness.providers.effort import format_requested_vs_sent, requested_vs_sent
            changes = requested_vs_sent(
                profile, effort_requested=getattr(session, "effort_requested", None),
                effort_sent=getattr(session, "effort", None),
                context_tokens=limit if isinstance(limit, int) else None, prompt_estimate=bd["total"],
            )
            change_lines = format_requested_vs_sent(changes)
            if change_lines:
                lines.append("  this route changed:")
                lines.extend(change_lines)
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


def _cmd_models(args: str, facade: HeadlessFacade) -> str:
    """H14 scope J (widened to every enabled provider by the H15 part 2
    addendum 3.2b): `/models [refresh]` (`/dbx` is a plain alias that always
    refreshes) -- headless surface for the same catalog refresh `halo
    models --refresh`/the TUI's own off-UI-thread `/models refresh` use.
    Bare `/models` reports the cached catalog's size/age without touching
    the network. 1.0.1 hotfix 3: bare now ALSO renders the cached Databricks
    table (family/path/chat -- same `catalog_cli.format_dbx_table_lines` the
    CLI and the TUI's own `/models` use, so all three never drift apart); a
    failed refresh shows that same (unchanged) table plus the one-line
    error, never just the error alone. OpenRouter/Anthropic (no per-endpoint
    table of their own here) get a one-line cached-count/refreshed-count
    summary alongside it -- `halo models --cc`/the full OpenRouter
    table live elsewhere, this command's own job is the refresh trigger."""
    from halo_harness.catalog_cli import _dbx_rows, format_dbx_table_lines
    from halo_harness.config.paths import bridge_home
    from halo_harness.providers.config import derive_workspace_root, resolve_databricks
    from halo_harness.providers.databricks import (
        dbx_endpoints_age_seconds, format_dbx_diff, load_dbx_endpoints_json, load_models_json,
        models_json_age_seconds, refresh_dbx_catalog, refresh_openrouter_catalog_if_stale,
    )
    from halo_harness.providers.anthropic_catalog import (
        ant_models_age_seconds, load_ant_models_json, refresh_anthropic_catalog_if_stale,
    )
    from halo_harness.providers.enablement import is_enabled
    state_dir = bridge_home()
    wants_refresh = args.strip().lower() in ("refresh", "--refresh")
    dbx_enabled = is_enabled("databricks")
    dbx = resolve_databricks() if dbx_enabled else None

    def _table_text(endpoints: dict, root: str) -> str:
        if not endpoints:
            return ""
        rows = _dbx_rows(endpoints, root, state_dir, urls=False)
        return "\n".join(format_dbx_table_lines(rows)) + "\n\n"

    lines = []
    dbx_text = ""
    if dbx is not None:
        root = derive_workspace_root(dbx.host)
        if wants_refresh:
            ok, diff, note = refresh_dbx_catalog(state_dir, root, dbx.token)
            endpoints = load_dbx_endpoints_json(state_dir)
            dbx_text = _table_text(endpoints, root)
            if not ok:
                # Exact pre-existing wording (tests/test_dbx_tui_surface.py
                # pins "Refresh failed"/"endpoint(s) still cached" verbatim)
                # for a GENUINE failure. 2.0.1 finding 24: a lock LOST to a
                # concurrent refresh is not a failure -- never this prefix,
                # which would read as self-contradictory next to REFRESH_
                # BUSY_NOTE's own "already running" wording.
                from halo_harness.providers.databricks import REFRESH_BUSY_NOTE
                if note == REFRESH_BUSY_NOTE:
                    lines.append(f"Databricks: {note} ({len(endpoints)} endpoint(s) still cached).")
                else:
                    lines.append(f"Refresh failed: {note} ({len(endpoints)} endpoint(s) still cached).")
            else:
                from halo_harness.providers.models_dev import refresh_models_dev_cache
                md_ok, md_note = refresh_models_dev_cache(state_dir)
                suffix = "" if md_ok else f" (models.dev refresh failed: {md_note} -- cached price data stays in use.)"
                # Exact pre-existing wording ("Refreshed: N" pinned verbatim).
                lines.append(f"Refreshed: {len(endpoints)} endpoint(s) cached. "
                             f"Diff: {format_dbx_diff(diff)}{suffix}")
        else:
            endpoints = load_dbx_endpoints_json(state_dir)
            dbx_text = _table_text(endpoints, root)
            age = dbx_endpoints_age_seconds(state_dir)
            age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
            # Exact pre-existing wording ("N Databricks endpoint(s) cached").
            lines.append(f"{len(endpoints)} Databricks endpoint(s) cached (last refreshed {age_str}).")
    elif dbx_enabled:
        lines.append("Databricks is not configured -- nothing to refresh (see `halo doctor --work`).")

    if is_enabled("openrouter"):
        if wants_refresh:
            from halo_harness.providers.databricks import CATALOG_REFRESH_BUSY, REFRESH_BUSY_NOTE
            ok = refresh_openrouter_catalog_if_stale(state_dir, force=True)
            if ok is CATALOG_REFRESH_BUSY:
                lines.append(f"OpenRouter: {REFRESH_BUSY_NOTE}.")
            elif ok is False:
                lines.append("OpenRouter refresh failed -- see `halo doctor`.")
            elif ok is None:
                lines.append("OpenRouter: not configured -- nothing to refresh.")
            else:
                lines.append(f"OpenRouter refreshed: {len(load_models_json(state_dir))} model(s) cached.")
        else:
            age = models_json_age_seconds(state_dir)
            age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
            lines.append(f"OpenRouter: {len(load_models_json(state_dir))} model(s) cached (last refreshed {age_str}).")

    if is_enabled("anthropic"):
        if wants_refresh:
            from halo_harness.providers.databricks import CATALOG_REFRESH_BUSY, REFRESH_BUSY_NOTE
            ok = refresh_anthropic_catalog_if_stale(state_dir, force=True)
            if ok is CATALOG_REFRESH_BUSY:
                lines.append(f"Anthropic: {REFRESH_BUSY_NOTE}.")
            elif ok is False:
                lines.append("Anthropic refresh failed -- see `halo doctor`.")
            elif ok is None:
                lines.append("Anthropic: not configured -- nothing to refresh.")
            else:
                lines.append(f"Anthropic refreshed: {len(load_ant_models_json(state_dir))} model(s) cached.")
        else:
            age = ant_models_age_seconds(state_dir)
            age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
            lines.append(f"Anthropic: {len(load_ant_models_json(state_dir))} model(s) cached (last refreshed {age_str}).")

    if not lines:
        return ("No provider is set up -- not configured (see `halo providers`, "
                 "or run `halo init`).")
    suffix = "" if wants_refresh else "\nUse `/models refresh` (or `/dbx`) to update."
    return f"{dbx_text}{chr(10).join(lines)}{suffix}"


def _cmd_dbx(args: str, facade: HeadlessFacade) -> str:
    """`/dbx` -- always behaves like `/models refresh`, regardless of args."""
    return _cmd_models("refresh", facade)


def _cmd_mcp(args: str, facade: HeadlessFacade) -> str:
    # "explain the zero" (gap-list brief, W4b item 1): scopes searched +
    # claude.ai connectors, shared verbatim with `halo mcp list`/doctor so
    # the three never disagree. Never allowed to crash `/mcp`.
    explain_lines: list = []
    try:
        from halo_harness.mcp import explain
        explain_lines = explain.explain_lines(cwd=facade.cwd, claude_json=facade.claude_json,
                                                settings=facade.settings)
    except Exception:
        pass

    if facade.mcp_status is not None:
        # H3 scope D: real per-server health, same line format `mcp list` uses.
        from halo_harness.mcp_cli import format_mcp_list_line
        if not facade.mcp_status:
            return "\n".join(explain_lines + ["No MCP servers configured in this directory."])
        lines = explain_lines + ["Configured MCP servers:"]
        for entry in sorted(facade.mcp_status, key=lambda e: e.get("name", "")):
            lines.append(f"  {format_mcp_list_line(entry)}")
        return "\n".join(lines)
    if not facade.mcp_servers:
        return "\n".join(explain_lines + ["No MCP servers configured in this directory."])
    lines = explain_lines + ["Configured MCP servers:"]
    for name, spec in sorted(facade.mcp_servers.items()):
        kind = spec.get("type", "stdio") if isinstance(spec, dict) else "stdio"
        lines.append(f"  {name} ({kind}) - not checked (no MCP client ran this session)")
    return "\n".join(lines)


def _cmd_ollama(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.3 round 3 (brief item 4): the plain-text fallback (`-p`,
    or the TUI falling through to `Controller.run_slash`) -- reports
    exactly like `halo ollama [--host NAME] [--refresh]`; the TUI's own
    `/ollama` (`tui/slash.py::_handle_ollama`) opens the interactive
    dialog instead, off the UI thread (a real network read)."""
    from halo_harness.providers.ollama import resolve_ollama_hosts
    from halo_harness.providers.ollama_panel import analyze_host, format_host_analysis
    tokens = (args or "").split()
    force = "--refresh" in tokens
    names = [t for t in tokens if t != "--refresh"]
    hosts = resolve_ollama_hosts()
    if names:
        wanted = {n.lower() for n in names}
        hosts = [h for h in hosts if h.name.lower() in wanted]
        if not hosts:
            return f"/ollama: no configured host matching {', '.join(names)!r}"
    if not hosts:
        return "No Ollama hosts configured."
    return "\n\n".join(format_host_analysis(analyze_host(h, force=force)) for h in hosts)


def _cmd_local(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.3 round 3 (brief item 6) + round 5 (brief item 4): the
    plain-text fallback (`-p`, or the TUI falling through to `Controller.
    run_slash`) -- the TUI's own `/local` (`tui/slash.py::_handle_local`)
    opens the interactive merged-view dialog instead, off the UI thread,
    the same "print vs. dialog" split `/ollama`/`_cmd_ollama` already use.

    Bare `/local` (or `/local refresh`) prints the SAME merged view `halo
    local [--refresh]` does (`providers.local_models.build_local_view`/
    `format_local_view`). Any OTHER argument text answers `args` as a
    question from `roles.small` (an `ol:` OR `hf:` ref, round 5 widens
    this from `ol:`-only) via `Session.call_small_model` -- NEVER through
    `derive_request`/`session.log`, so this never becomes part of the
    main transcript's context (see that method's own docstring).
    `facade.session` is the live session when one is running; `None` in a
    context with no live session at all (e.g. a unit test facade) is
    reported plainly, never a traceback."""
    stripped = (args or "").strip()
    if not stripped or stripped.lower() in ("refresh", "--refresh"):
        from halo_harness.providers.local_models import build_local_view, format_local_view
        return format_local_view(build_local_view(refresh=bool(stripped)))
    question = stripped
    session = facade.session
    if session is None:
        return "/local: no live session."
    ref = getattr(session, "small_model_ref", None) or session.model_ref
    # Round 5b part 2 (brief item 3): "/local <question> follows the same
    # rule" -- a DIFFERENT local ol: model configured for the small role
    # that would not fit beside the session's own main model falls back
    # to the main model for THIS question, same redirection `roles.
    # vram_aware_override` already applies at config-resolution time (this
    # is the RUNTIME twin, for whatever `small_model_ref` actually ended
    # up resolving to this session).
    if ref is not session.model_ref and ref.provider == "ollama" and session.model_ref.provider == "ollama":
        from halo_harness.roles import vram_aware_override
        _value, _reason = vram_aware_override("small", ref.raw, main_ref=session.model_ref)
        if _reason:
            ref = session.model_ref
    if ref.provider not in ("ollama", "huggingface"):
        return (f"/local needs roles.small set to an ol: or hf: model (currently resolves to {ref.raw!r}); "
                f"set one via /roles, the model picker's u action, or `ollama.hosts`/`huggingface.*`/roles.small "
                f"in config.")
    try:
        return session.call_small_model(
            system_text="You are a fast local assistant answering a standalone question directly and "
                        "concisely. This exchange is not part of any other conversation.",
            user_text=question, model_ref=ref)
    except Exception as e:
        return f"/local: {type(e).__name__}: {e}"


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
    (`halo improve [--json] [--apply ...]`, a separate top-level
    subcommand, never this slash command)."""
    return "Improve review needs the interactive TUI. Use `halo improve` for the headless surface."


def _cmd_resume(args: str, facade: HeadlessFacade) -> str:
    from halo_harness.agent import sessions as agent_sessions

    if args.strip():
        session_id, err = agent_sessions.resolve_resume(facade.cwd, args.strip())
        if session_id is None:
            return f"halo: --resume: {err}"
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
    from halo_harness import __version__
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
    lines = [f"halo {__version__}", f"Model: {model_id}", f"cwd: {facade.cwd}",
             f"Permission mode: {permission_mode}", f"MCP servers: {mcp_n}",
             f"Theme: {facade.theme or '?'}", cost_line]
    # Halo 2.0.1 W2a (HALO-2.0.1-liveness-tips-brief.md Part C): same
    # "requested X, sent Y" comparison `/context` shows, from the same
    # shared helper -- /status is the quick one-shot check with no
    # context-breakdown cost, so it skips the max_tokens entry (no prompt
    # estimate at hand here) and reports effort/temperature only.
    profile = getattr(session, "provider_profile", None) if session is not None else None
    if profile is not None:
        from halo_harness.providers.effort import format_requested_vs_sent, requested_vs_sent
        changes = requested_vs_sent(
            profile, effort_requested=getattr(session, "effort_requested", None),
            effort_sent=getattr(session, "effort", None),
        )
        change_lines = format_requested_vs_sent(changes)
        if change_lines:
            lines.append("This route changed:")
            lines.extend(change_lines)
    return "\n".join(lines)


def _cmd_config(args: str, facade: HeadlessFacade) -> str:
    if args.strip():
        return "halo: /config is read-only in headless mode; edit ~/.claude/settings.json or pass --settings."
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
        from halo_harness.config.agents_md import discover_agents
        agents = discover_agents(facade.cwd, settings=facade.settings)
    if not agents:
        return "No agent definitions found (not even the built-ins -- this shouldn't happen)."
    lines = ["Available sub-agents:"]
    for name in sorted(agents):
        spec = agents[name]
        lines.append(f"  {name} ({spec.source}) -- {spec.description}")
    return "\n".join(lines)


def _cmd_roles(args: str, facade: HeadlessFacade) -> str:
    """V2c (H15), extended Halo 2.0.2 (brief A.2/A.5): bare `/roles` --
    the role table (model, effort, endpoint/path type, price per role),
    read from the LIVE session's own `agent_runtime.role_table`/
    `.cli_role_overrides` (the SAME table `Agent(role=...)`/a role-bearing
    agent actually resolves against) when one is running, exactly like
    `/agents` above. `/roles templates|save <name>|load <name>|new <name>|
    show <name>` manage `~/.halo/roles/<name>.json` templates; `/roles set
    <name> <model> [effort]` is the long form of `/role` (below) -- both
    set ONE role for THIS session only, mutating the live `session.roles`
    dict in place (the SAME object `session.agent_runtime.role_table`
    already points at). `/roles edit <name>` has no headless/print-mode
    form (the TUI form, `tui/dialogs/roles_editor.py`, intercepts it
    first); here it just names that."""
    session = getattr(facade, "session", None)
    state_dir = getattr(session, "state_dir", None) if session is not None else None
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "templates":
        from halo_harness.roles import list_role_templates
        names = list_role_templates(state_dir=state_dir)
        if not names:
            return "No role templates saved yet. /roles save <name> saves the current table as one."
        return "Role templates:\n" + "\n".join(f"  {n}" for n in names)

    if sub == "save":
        if not rest:
            return "Usage: /roles save <name>"
        from halo_harness.roles import save_role_template
        runtime = getattr(session, "agent_runtime", None) if session is not None else None
        role_table = getattr(runtime, "role_table", None) or getattr(session, "roles", None) or {}
        ok, problems = save_role_template(rest, {"roles": role_table}, state_dir=state_dir)
        return (f"Saved the current role table as template {rest!r}." if ok
                else "Could not save: " + "; ".join(problems))

    if sub == "new":
        if not rest:
            return "Usage: /roles new <name>"
        from halo_harness.roles import save_role_template
        ok, problems = save_role_template(rest, {"roles": {}}, state_dir=state_dir)
        return f"Created an empty role template {rest!r}." if ok else "Could not create: " + "; ".join(problems)

    if sub == "show":
        if not rest:
            return "Usage: /roles show <name>"
        from halo_harness.roles import load_role_template, role_value_parts
        template = load_role_template(rest, state_dir=state_dir)
        if template is None:
            return f"No such role template: {rest!r} (or it failed validation)"
        lines = [f"{template['name']} -- {template['description'] or '(no description)'}"]
        for role_name, value in sorted(template["roles"].items()):
            model, effort = role_value_parts(value)
            lines.append(f"  {role_name}: {model}" + (f" ({effort})" if effort else ""))
        return "\n".join(lines)

    if sub == "load":
        if not rest:
            return "Usage: /roles load <name>"
        from halo_harness.roles import apply_role_template, load_role_template
        ok, problems = apply_role_template(rest, state_dir=state_dir)
        if not ok:
            return "Could not load: " + "; ".join(problems)
        # Takes effect immediately for THIS live session too -- without
        # this, a running session would only pick up the template after
        # the next full restart (config.json is re-read at session start,
        # never mid-session).
        if session is not None:
            template = load_role_template(rest, state_dir=state_dir)
            runtime = getattr(session, "agent_runtime", None)
            role_table = getattr(runtime, "role_table", None)
            if template and isinstance(role_table, dict):
                role_table.update(template["roles"])
            elif template and hasattr(session, "roles"):
                session.roles.update(template["roles"])
        return f"Loaded role template {rest!r}."

    if sub == "edit":
        return ("/roles edit opens the roles editor form in the TUI only -- "
                "use /roles set <name> <model> [effort] here instead.")

    if sub == "set":
        return _set_one_role(rest, facade)

    return _render_roles_table(facade)


def _render_roles_table(facade: HeadlessFacade) -> str:
    from halo_harness.roles import format_roles_table, resolve_all_roles, resolve_role_table
    hint = "(tip: /setup roles opens a guided setup screen with templates)\n"
    session = getattr(facade, "session", None)
    if session is None:
        return hint + "Nothing to show yet: /roles needs a live session to resolve against."
    runtime = getattr(session, "agent_runtime", None)
    role_table = getattr(runtime, "role_table", None)
    if role_table is None:
        role_table = resolve_role_table(provider=getattr(session.model_ref, "provider", None))
    cli_overrides = getattr(runtime, "cli_role_overrides", None) or {}
    rows = resolve_all_roles(
        role_table=role_table, cli_overrides=cli_overrides, parent_ref=session.model_ref,
        parent_profile=session.model_profile, state_dir=session.state_dir,
        routes=getattr(runtime, "routes", None),
    )
    return hint + format_roles_table(rows)


def _set_one_role(rest: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 brief A.4: `/role <name> <model> [effort]` and `/roles
    set <name> <model> [effort]` are the same operation -- sets ONE role
    for THIS session only (never persisted; `/roles save <name>` is the
    explicit "keep this" action), the same live-mutation pattern
    `_cmd_effort` above uses for `session.effort`.

    2.0.2 review finding 23: this used to write into `runtime.role_table`
    (or a throwaway `session.roles` dict when no runtime was attached),
    which `resolve_agent_model`'s own chain (config/agents_md.py) ranks
    BELOW both an `--role` CLI override and an agent file's own `model:`
    -- so `/role coder X` reported success but changed nothing whenever
    either of those applied, although ROLES.md says `/role` always wins.
    `runtime.cli_role_overrides` is the rung that chain actually treats
    as "wins even over the agent file's own model:", and it is never
    None (a real dict by construction) -- so the `session.roles` escape
    hatch now only matters for a session with no `agent_runtime` at all."""
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.profiles import EFFORT_LEVELS
    from halo_harness.roles import known_role_names
    session = getattr(facade, "session", None)
    if session is None:
        return "Roles can only be set once a session is running."
    parts = rest.split()
    if len(parts) < 2:
        return "Usage: /role <name> <model> [effort] (or /setup roles for a guided setup screen)"
    name, model = parts[0], parts[1]
    effort = parts[2] if len(parts) > 2 else None
    if effort is not None and effort not in EFFORT_LEVELS:
        return f"Unknown effort {effort!r} (expected one of {', '.join(EFFORT_LEVELS)})"
    if model not in ("inherit", "haiku"):
        try:
            parse_model_ref(model)
        except Exception as e:
            return f"{model!r} is not a valid model reference: {e}"
    runtime = getattr(session, "agent_runtime", None)
    overrides = getattr(runtime, "cli_role_overrides", None)
    if not isinstance(overrides, dict):
        if not hasattr(session, "roles") or not isinstance(session.roles, dict):
            session.roles = {}
        overrides = session.roles
    known = known_role_names(getattr(runtime, "role_table", None), overrides)
    if name not in known:
        return f"Unknown role {name!r} (expected one of {', '.join(known)})"
    overrides[name] = {"model": model, "effort": effort} if effort else model
    effort_note = f" (effort: {effort})" if effort else ""
    return f"Role {name!r} set to {model!r}{effort_note} for this session."


def _cmd_role(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 brief A.4: `/role <name> <model> [effort]` -- the short
    form of `/roles set` (see `_set_one_role`'s own docstring)."""
    if not (args or "").strip():
        return "Usage: /role <name> <model> [effort] (or /setup roles for a guided setup screen)"
    return _set_one_role(args.strip(), facade)


def _cmd_org(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 round 2 (brief B), extended round D (brief items 1/3/4):
    bare `/org` (and `/org list`) lists saved organizations, each with
    its own one-line README (its `description`); `/org show <name>`
    prints its text tree; `/org new <name>` creates a one-position
    starter; `/org load <name>` re-installs a built-in's shipped
    definition over a local copy (a plain file write -- unlike `/roles
    edit`, this never needs the TUI); `/org install <name> [--force]`
    copies a shipped or saved template into `~/.halo/orgs/`; `/org run
    <name> "<goal>"` runs it for real against the LIVE session, through
    the exact same `run_org_call` an `Agent(org=...)` tool call uses;
    `/org export <name> [file]`/`/org import <file>` move an org as
    plain JSON; `/org resume` continues THIS session's own interrupted
    run from its saved record and shared task board. `/org edit <name>`
    has no headless/print-mode form (the TUI form, `tui/dialogs/
    org_editor.py`, intercepts it first); here it just names that, like
    `/roles edit` does."""
    session = getattr(facade, "session", None)
    state_dir = getattr(session, "state_dir", None) if session is not None else None
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("", "list"):
        from halo_harness.orgs import list_orgs, load_org
        names = list_orgs(state_dir=state_dir)
        lines = ["Organizations (~/.halo/orgs/):"]
        for n in names:
            org = load_org(n, state_dir=state_dir)
            desc = org.get("description") if org else None
            lines.append(f"  {n}" + (f" -- {desc}" if desc else ""))
        return "\n".join(lines)

    if sub == "show":
        if not rest:
            return "Usage: /org show <name>"
        from halo_harness.orgs import describe, load_org
        org = load_org(rest, state_dir=state_dir)
        if org is None:
            return f"No such organization: {rest!r} (or it failed validation)"
        return describe(org)

    if sub == "new":
        if not rest:
            return "Usage: /org new <name>"
        from halo_harness.orgs import save_org
        ok, problems = save_org(rest, {"name": rest, "positions": [
            {"title": "Orchestrator", "role": "orchestrator", "reports": [], "instructions": ""}]},
            state_dir=state_dir)
        return (f"Created organization {rest!r} with a single 'Orchestrator' root position." if ok
                else "Could not create: " + "; ".join(problems))

    if sub == "load":
        if not rest:
            return "Usage: /org load <name>"
        from halo_harness.orgs import reload_builtin_org
        ok, problems = reload_builtin_org(rest, state_dir=state_dir)
        return f"Reloaded built-in organization {rest!r}." if ok else "Could not load: " + "; ".join(problems)

    if sub == "install":
        # Halo 2.0.2 round D (brief item 1): `--force` as a trailing token
        # (`/org install <name> --force`) -- this facade path is plain
        # text, not argparse, so it's parsed by hand the same way other
        # `/org`/`/roles` subcommands here already split their own rest.
        if not rest:
            return "Usage: /org install <name> [--force]"
        tokens = rest.split()
        force = "--force" in tokens
        name = next((t for t in tokens if t != "--force"), "")
        if not name:
            return "Usage: /org install <name> [--force]"
        from halo_harness.orgs import install_org_template
        ok, problems = install_org_template(name, force=force, state_dir=state_dir)
        return (f"Installed organization template {name!r} to ~/.halo/orgs/{name}.json."
                if ok else "Could not install: " + "; ".join(problems))

    if sub == "export":
        # Halo 2.0.2 round D (brief item 3): `/org export <name> [file]` --
        # plain JSON; no file given returns it as the command's own text
        # (headless.py prints that directly, same as every other "core"
        # command's result) rather than silently writing somewhere unasked.
        if not rest:
            return "Usage: /org export <name> [file]"
        import json
        from halo_harness.orgs import load_org
        name, _, file_arg = rest.partition(" ")
        file_arg = file_arg.strip() or None
        org = load_org(name, state_dir=state_dir)
        if org is None:
            return f"No such organization: {name!r} (or it failed validation)"
        text = json.dumps(org, indent=2, sort_keys=True) + "\n"
        if not file_arg:
            return text
        try:
            Path(file_arg).write_text(text, encoding="utf-8")
        except OSError as e:
            return f"Could not write {file_arg}: {e}"
        return f"Exported organization {name!r} to {file_arg}."

    if sub == "import":
        # Halo 2.0.2 round D (brief item 3): `/org import <file>` -- plain
        # JSON, validated the same way any other org write is (`orgs.
        # save_org` -> `validate_org`), with every problem listed.
        if not rest:
            return "Usage: /org import <file>"
        import json
        from halo_harness.orgs import save_org
        path = Path(rest)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            return f"Could not read {rest}: {e}"
        try:
            data = json.loads(raw)
        except ValueError as e:
            return f"{rest} is not valid JSON: {e}"
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name.strip():
            name = path.stem
        ok, problems = save_org(name, data, state_dir=state_dir)
        return (f"Imported organization {name!r} from {rest}." if ok
                else f"{rest} is not a valid organization: " + "; ".join(problems))

    if sub == "edit":
        return ("/org edit opens the organization editor form in the TUI only -- "
                "use `halo org edit <name>` ($EDITOR) from the CLI instead.")

    if sub == "run":
        if not rest:
            return 'Usage: /org run [<name>] "<goal>" (no name uses orgs.default, see /setup orgs)'
        from halo_harness.orgs import default_org_name, parse_run_args
        name, goal = parse_run_args(rest)
        if not goal:
            return 'Usage: /org run [<name>] "<goal>" (no name uses orgs.default, see /setup orgs)'
        if name is None:
            name = default_org_name(state_dir=state_dir)
            if name is None:
                return ('No organization name given, and no default is set -- /org run <name> "<goal>", '
                         'or set a default in /setup orgs.')
        if session is None or getattr(session, "agent_runtime", None) is None:
            return "Organizations can only be run once a session is running."
        from halo_harness.agent.subagent import run_org_call
        _events, result = run_org_call(
            runtime=session.agent_runtime, tool_id=f"org-run-{name}", tool_name="Agent",
            tool_input={"org": name, "prompt": goal, "description": f"Run org {name}"},
        )
        return result.content

    if sub == "resume":
        # Halo 2.0.2 round D (brief item 4): `/org resume` (no argument --
        # the headless/print-mode form has no "current session id" of its
        # own to type, unlike `halo org resume <session-id>`) continues
        # THIS session's own interrupted run, from its own saved record +
        # shared task board.
        if session is None or getattr(session, "agent_runtime", None) is None:
            return "Organizations can only be resumed once a session is running."
        from halo_harness.agent.subagent import resume_org_run
        session_dir = session.log.dir / session.log.session_id
        _events, result = resume_org_run(
            runtime=session.agent_runtime, tool_id="org-resume", tool_name="Agent", session_dir=session_dir,
        )
        return result.content

    return f"/org: unknown subcommand {sub!r} (known: list, show, new, load, install, edit, run, export, import, resume)"


def _cmd_setup(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 round 7: `/setup [roles|orgs]` -- the guided setup
    SCREEN only exists in the TUI (`tui/slash.py::_handle_setup`
    intercepts this first, same split as `/roles edit`/`/org edit`); this
    headless fallback (a bare `-p "/setup"`, or a custom command/skill
    that happens to invoke it) just names the real surfaces instead of
    half-implementing a form with no screen to draw it on."""
    sub = (args or "").strip().lower()
    if sub == "roles":
        return "/setup roles opens the roles setup screen in the TUI only -- use `halo setup roles` from the CLI."
    if sub == "orgs":
        return "/setup orgs opens the organizations setup screen in the TUI only -- use `halo setup orgs` from the CLI."
    return ("/setup opens the roles and organizations setup screens in the TUI only -- "
            "use `halo setup` from the CLI, or `/roles`/`/org` here.")


def _cmd_providers(args: str, facade: HeadlessFacade) -> str:
    """H15 item 21.5: `/providers` -- the SAME table `halo providers`
    prints (`format_providers_table`/`provider_rows`, so the two surfaces
    never drift apart), plus `enable <name>`/`disable <name>` right here.
    `setup <name>` needs the interactive provider picker/tabs `init` itself
    shows -- not available headless, so this just points at the real
    command instead of half-implementing it.

    2.0.1 launch-hang fix: `claude_login_available()`/`credentials_present
    ("claude_subscription")` (what `provider_rows()` reads for its own row)
    are cache-only now and never spawn `claude auth status` themselves --
    the "list" branch below does the one, staleness-gated, synchronous
    refresh this headless surface needs (same `cached_auth_status_is_stale`
    gate the TUI's own `/model`-open worker uses; print mode has no UI
    thread to protect here, so this runs inline rather than on a worker)."""
    from halo_harness.providers.enablement import PROVIDER_NAMES, canonical, disable, enable, label_for
    from halo_harness.providers_cli import format_providers_table, provider_rows
    tokens = (args or "").split()
    if not tokens or tokens[0] == "list":
        try:
            from halo_harness.providers.cc_models import cached_auth_status_is_stale, refresh_cached_claude_auth_status
            if cached_auth_status_is_stale():
                refresh_cached_claude_auth_status()
        except Exception:
            pass
        # Findings 22/23 (2.0.1): this session's OWN cwd, and (when a real
        # Settings object was resolved for it) its own effective_env --
        # passed straight through rather than letting provider_rows()
        # re-derive a fresh listing_effective_env() against bare
        # Path.cwd()/no settings-flag, which could disagree with a session
        # actually launched via --cwd/--settings (same fix `/doctor` already
        # had for --cwd via facade.cwd, below).
        live_env = facade.settings.effective_env if getattr(facade, "settings", None) is not None else None
        return format_providers_table(provider_rows(cwd=facade.cwd, env=live_env))
    action = tokens[0]
    if action in ("enable", "disable"):
        if len(tokens) < 2:
            return f"/providers {action}: needs a provider name ({', '.join(PROVIDER_NAMES)})"
        name = canonical(tokens[1])
        if name not in PROVIDER_NAMES:
            return f"/providers {action}: unknown provider {tokens[1]!r} (expected one of {', '.join(PROVIDER_NAMES)})"
        if action == "enable":
            enable(name)
            return f"{label_for(name)}: enabled"
        disable(name)
        return f"{label_for(name)}: disabled"
    if action == "setup":
        return ("/providers setup needs the interactive picker -- run `halo providers setup "
                f"{tokens[1] if len(tokens) > 1 else '<name>'}` (or `halo init`) from a real terminal.")
    return "Usage: /providers [list|enable <name>|disable <name>|setup <name>]"


def _cmd_effort(args: str, facade: HeadlessFacade) -> str:
    """1.0.1 hotfix 19/20. Bare `/effort`: the effective level, its source,
    and this route's own accepted levels (print mode's whole answer; the
    TUI additionally offers the inline selector card for this same bare
    case -- see `tui/slash.py::_handle_effort`, which checks `args` itself
    before ever reaching here). `/effort <level>`: sets `facade.session.
    effort` directly (a live reference, not a snapshot -- see the class
    docstring above; the SAME plain-attribute-write pattern `Controller.
    set_permission_mode` already uses for `permission_engine.mode`) so the
    NEXT model call picks it up with no other wiring -- clamped through the
    same `clamp_effort` the request builders use, so `/effort` can never
    set a value this route will 400 on.

    Before this fix, `/effort <anything>` was pure decoration: the
    registration was `kind=None` (show-only) and this function ignored
    `args` completely, so typing `/effort medium` printed whatever
    `facade.effort` was snapshotted as at session start (the owner's own report:
    "every time I change the effort level it only selects xhigh") --
    `facade.effort` is a one-time snapshot (see the class docstring), never
    updated, which is exactly why the live `facade.session` reference is
    used here instead."""
    session = facade.session
    profile = getattr(session, "provider_profile", None) if session is not None else None
    current = getattr(session, "effort", None) if session is not None else facade.effort
    source = getattr(session, "effort_source", None) if session is not None else None
    # Halo 2.0.1 W2a (HALO-2.0.1-liveness-tips-brief.md Part C): the RAW
    # value last explicitly requested, before clamping (`Session.
    # effort_requested` -- `getattr` with a default so a bare/fake session
    # from before this field existed just skips the "sent as" note below).
    requested_value = getattr(session, "effort_requested", None) if session is not None else None
    supported = getattr(profile, "effort_values_supported", None) if profile is not None else None

    requested = (args or "").strip().lower()
    if not requested:
        levels = ", ".join(supported) if supported else "(this model has no adjustable effort)"
        # 1.0.1 part 2 (item 22 remainder): this route forces an explicit
        # reasoning_effort override whenever a turn carries tools (the
        # gpt-6 table rule, or a learned per-endpoint rule) -- shown as the
        # EFFECTIVE value ("none (tools)"), with the source line explaining
        # why, instead of the configured value that the next (tool-
        # carrying) turn will ignore anyway.
        from halo_harness.providers.profiles import effort_display_override
        override_display = effort_display_override(profile)
        if override_display:
            return (f"Effort level: {override_display} (source: this route forces reasoning_effort="
                     f"{profile.reasoning_effort_with_tools!r} whenever a turn carries tools)\n"
                     f"Accepted for this model: {levels}")
        # Part C: "never show a control value the gateway will silently
        # change" -- when what was last requested differs from what's
        # actually sent (`current`, already clamped), say so inline rather
        # than just showing the sent value with no explanation.
        if requested_value is not None and current is not None and requested_value != current:
            value_line = f"{requested_value} (sent as {current} on this route)"
        else:
            value_line = current or "not set (provider default)"
        return (f"Effort level: {value_line} (source: {source or 'default'})\n"
                f"Accepted for this model: {levels}")

    if session is None or profile is None:
        return "Effort can only be changed once a session is running."
    if not profile.reasoning_effort_supported:
        return f"{session.model_ref.raw} has no adjustable effort level -- nothing to set."
    from halo_harness.providers.profiles import clamp_effort
    clamped = clamp_effort(requested, profile)
    session.effort = clamped
    session.effort_requested = requested
    session.effort_source = "session"
    clamp_note = "" if clamped == requested else f" (requested '{requested}', sent as '{clamped}' on this route)"
    return f"Effort level set to '{clamped}'{clamp_note} (source: session) -- takes effect on the next message."


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
    from halo_harness.doctor import run_checks
    lines, _ok = run_checks(cwd=facade.cwd)
    return "\n".join(lines)


def _cmd_update(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 round 6: the plain-text fallback (`-p`, or the TUI
    falling through to `Controller.run_slash` for any reason) -- reports
    exactly like `halo update --check`, but never applies anything; only
    the TUI's own `/update` (`tui/slash.py::_handle_update`) can actually
    offer the update-and-restart dialog."""
    from halo_harness import update as upd
    build = upd.installed_build()
    # Halo 2.0.2 round C: an explicit /update always queries live (5 s
    # cap, cache only as the fallback on failure) -- see latest_
    # available's own docstring; this path is explicitly "exactly like
    # halo update --check", which gets the same treatment.
    avail = upd.latest_available(upd.default_channel(build), refresh=True)
    kind = upd.install_kind()
    lines = [f"installed: {upd.format_version_line(build)}"]
    if avail.get("commit"):
        ref = f", {avail['ref']}" if avail.get("ref") else ""
        lines.append(f"available ({avail.get('channel')}): {avail['commit']}{ref}")
    else:
        lines.append(f"available ({avail.get('channel')}): unknown ({avail.get('reason') or 'no reason given'})")
    lines.append(f"update command: {kind.get('reinstall_cmd') or '(unknown)'}")
    if build.get("commit") and avail.get("commit") and build["commit"] == avail["commit"]:
        lines.append("Already up to date.")
    else:
        lines.append("Run `halo update` from a shell, or open the full-screen TUI and use /update there "
                      "to update and restart in place.")
    return "\n".join(lines)


def _cmd_export(args: str, facade: HeadlessFacade) -> str:
    return "Export needs the interactive TUI's file picker; nothing to export from a single -p turn."


def _cmd_add_dir(args: str, facade: HeadlessFacade) -> str:
    if not args.strip():
        return "Usage: /add-dir <directory> (or pass --add-dir on the command line to start with one)."
    return f"halo: /add-dir needs a running session to extend; pass --add-dir {args.strip()!r} on the command line instead."


def _cmd_theme(args: str, facade: HeadlessFacade) -> str:
    from halo_harness import theme as theme_mod
    name = args.strip()
    if not name:
        return f"Current theme: {facade.theme or theme_mod.DEFAULT_THEME}"
    if not theme_mod.is_valid_theme(name):
        return f"halo: not a valid theme name: {name!r} (expected one of {sorted(theme_mod.VALID_THEMES)})"
    theme_mod.persist_theme(name)
    return f"Theme set to {name}."


def _cmd_exit(args: str, facade: HeadlessFacade) -> str:
    return "Nothing to exit: a single -p call already ends after this turn."


def _cmd_copy(args: str, facade: HeadlessFacade) -> str:
    """W4c item 2: a REAL, interactive-TUI-only behaviour (`tui/slash.py::
    _handle_copy` copies the last reply, a fenced code block, or the last
    tool output to the system clipboard) -- `-p` has no transcript widgets
    and no clipboard to copy TO, so this just says so honestly rather than
    copying nothing silently."""
    return "Nothing to copy outside an interactive session -- each -p call has no clipboard to copy to."


def _cmd_bugreport(args: str, facade: HeadlessFacade) -> str:
    """2.0.1 W3a: "one paste instead of screenshots" -- a REAL, fully
    functional command here (unlike /export's interactive-picker-only
    stub above), since writing a report file (and optionally copying it)
    needs no picker and works identically headless or in the TUI. `halo
    bugreport` itself (`halo_harness.bugreport.cmd_bugreport`) is the
    separate entry point for when nothing is running at all."""
    from halo_harness.bugreport import build_bugreport_text, copy_to_clipboard, write_bugreport
    from halo_harness.config.paths import bridge_home
    opts = (args or "").split()
    state_dir = bridge_home()
    text = build_bugreport_text(facade=facade, state_dir=state_dir, cwd=facade.cwd,
                                 include_content="--include-content" in opts)
    path = write_bugreport(text, state_dir)
    lines = [f"Bugreport written to {path}"]
    if "--copy" in opts:
        lines.append("Copied to clipboard." if copy_to_clipboard(text) else "No clipboard tool found -- see the path above.")
    return "\n".join(lines)


def _cmd_timeline(args: str, facade: HeadlessFacade) -> str:
    """2.0.1 W3a/W3b: THIS session's own per-turn timeline -- `halo
    timeline --last N` (a separate command, `bugreport_timeline_cli.py`)
    reads the same data back from the session LOG instead, for after the
    fact / a different process.

    W3b item 11: reads `facade.session._timeline` (the REAL session's own
    instance, see `debug_timeline.TurnTimeline`) rather than the
    `debug_timeline` module's shared default -- a live session is the
    common case this command exists for at all; the module-level default
    is only ever a fallback for a bare/test facade with no real session
    attached (never written to by a real `Session.turn()` any more, so it
    naturally stays empty there, which is the correct, honest answer)."""
    from halo_harness import debug_timeline
    from halo_harness.bugreport_timeline_cli import format_timeline_record
    try:
        n = int(args.strip()) if args.strip() else 1
    except ValueError:
        n = 1
    session = getattr(facade, "session", None)
    timeline = getattr(session, "_timeline", None) if session is not None else None
    records = timeline.last_n_turns(n) if timeline is not None else debug_timeline.last_n_turns(n)
    if not records:
        return "No turns recorded yet this session."
    return "\n\n".join(format_timeline_record(r) for r in records)


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
    from halo_harness.agent import sessions as agent_sessions
    agent_sessions.set_title(facade.cwd, facade.session_id, title)
    return f"Renamed this session to {title!r}."


def _cmd_fork(args: str, facade: HeadlessFacade) -> str:
    """H6 scope D: copies THIS session's log under a new id right now (the
    original is never touched) -- `-p` has no live worker thread to hand
    the new session off to mid-turn, so the result is the id to `-r` into
    afterward, not a live switch (that part IS the TUI's own job)."""
    from halo_harness.agent import sessions as agent_sessions
    if not facade.session_id:
        return "halo: no active session to fork."
    new_id = agent_sessions.fork_session(facade.cwd, facade.session_id)
    return f"Forked this session -> {new_id}. Continue it with: -r {new_id}"


def _cmd_stats(args: str, facade: HeadlessFacade) -> str:
    session = getattr(facade, "session", None)
    if session is None:
        return f"Total cost: ${facade.cost_usd:.4f} across {facade.num_turns} turn(s)."
    from halo_harness.controller import compute_session_stats, format_cache_tokens_suffix
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
    moved to the background), most-recently-started last. Halo 2.0.2
    round 3 (brief C item 1): in the TUI, `/tasks`/Ctrl+T instead opens
    a live panel covering sub-agents too (plus the shared task board) --
    `tui/slash.py`'s own handler intercepts it there before this
    headless-text fallback is ever reached; this plain-text form (used
    from print mode/the CLI, where no such panel exists) still only ever
    lists background Bash jobs, with one line pointing at the richer
    TUI panel added."""
    session = getattr(facade, "session", None)
    registry = getattr(session, "job_registry", None)
    jobs = registry.list_jobs() if registry is not None else []
    if not jobs:
        return "No background jobs in this session. (The TUI's own /tasks/Ctrl+T also shows sub-agents " \
               "and the shared task board.)"
    lines = ["Background jobs:"]
    for job in jobs:
        cmd = job["command"]
        if len(cmd) > 60:
            cmd = cmd[:60] + "..."
        lines.append(f"  {job['id']}  [{job['status']}]  {job['description'] or cmd}")
    lines.append("(The TUI's own /tasks/Ctrl+T also shows sub-agents and the shared task board.)")
    return "\n".join(lines)


def _cmd_rewind(args: str, facade: HeadlessFacade) -> str:
    return "halo: /rewind needs the interactive TUI (a file's history lives per-session)."


def _cmd_editor(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 round C (macOS/VS Code terminal brief): the keyboard-
    independent twin of Ctrl+E -- `tui/slash.py`'s own handler intercepts
    this in the TUI (opening $VISUAL/$EDITOR on the real prompt draft)
    before this headless-text fallback is ever reached; there is no
    prompt draft to edit outside an interactive session."""
    return "halo: /editor needs the interactive TUI (there's no prompt draft to edit in print mode)."


def _cmd_keys(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.2 round C: the key-name tester dialog -- TUI-only (print
    mode reads no keystrokes at all); `tui/slash.py`'s own handler opens
    the real dialog there before this fallback is ever reached."""
    return "halo: /keys needs the interactive TUI (it shows the key name halo receives per press)."


def _cmd_undo(args: str, facade: HeadlessFacade) -> str:
    return "halo: /undo needs the interactive TUI."


def _cmd_redo(args: str, facade: HeadlessFacade) -> str:
    return "halo: /redo needs the interactive TUI."


def _cmd_intro(args: str, facade: HeadlessFacade) -> str:
    """2.0.0 Launch intro: the headless ("ui"-kind builtin) fallback --
    `tui/slash.py::_handle_intro` is what actually replays the typewriter
    line in the real TUI; this text is only ever seen from `-p`/a context
    with no interactive session at all."""
    return "halo: /intro needs the interactive TUI (it replays the launch typewriter line)."


def _cmd_tips(args: str, facade: HeadlessFacade) -> str:
    """Halo 2.0.1 W2b (liveness-tips-brief Part B4): `/tips` prints every
    tip applicable to THIS session as a list -- the same curated+generated,
    `needs`-filtered set the input placeholder rotates through (`tui/tips.
    py::all_applicable_tips`), so the two surfaces never drift apart.
    Works headlessly too (no TUI/controller needed) -- `needs` detection
    degrades gracefully with no session/registry at all (a bare `-p
    "/tips"` still shows every provider-neutral tip)."""
    from halo_harness.tui import tips as tips_mod
    applicable = tips_mod.all_applicable_tips(facade.registry, facade=facade)
    if not applicable:
        return "No tips available."
    return "\n".join(f"- {t.text}" for t in applicable)


def _cmd_keybindings(args: str, facade: HeadlessFacade) -> str:
    from halo_harness.tui.keys import load_keymap
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
    "models": ("core", "List/refresh the Databricks endpoint catalog", "[refresh]", _cmd_models),
    "dbx": ("core", "Alias for /models refresh", None, _cmd_dbx),
    "mcp": ("core", "List configured MCP servers", None, _cmd_mcp),
    "ollama": ("core", "Per-host Ollama analysis: reachability, loaded models, context, tool-catalog sizing",
               "[--host NAME] [--refresh]", _cmd_ollama),
    "local": ("core", "Local models: merged Ollama/Hugging Face/cache view, or ask roles.small a question",
              "[refresh] | <question>", _cmd_local),
    "memory": ("core", "Show the auto-memory directory and index", None, _cmd_memory),
    "permissions": ("core", "Show the active permission mode and rule counts", None, _cmd_permissions),
    "plan": ("ui", "Review the current plan", None, _cmd_plan),
    "resume": ("ui", "Resume a previous session", "[session-id]", _cmd_resume),
    "status": ("core", "Show session status", None, _cmd_status),
    "config": ("core", "Show or set a config value", "[key=value]", _cmd_config),
    "skills": ("core", "List discovered skills", None, _cmd_skills),
    "agents": ("core", "List available sub-agents", None, _cmd_agents),
    "roles": ("core", "Show the role table, or manage role templates/set a role",
              "[templates|save|load|new|edit|show <name>|set <name> <model> [effort]]", _cmd_roles),
    "role": ("core", "Set one role's model/effort for this session", "<name> <model> [effort]", _cmd_role),
    "org": ("core", "List/show/run organizations (trees of sub-agent positions)",
             "[list|show|new|load|edit|run [<name>] \"<goal>\"]", _cmd_org),
    "setup": ("core", "Open the roles/organizations guided setup screens", "[roles|orgs]", _cmd_setup),
    "providers": ("core", "Show/enable/disable providers (dbx:/or:/ant:/cc:)", "[list|enable|disable <name>]",
                  _cmd_providers),
    "effort": ("core", "Show or change the active reasoning effort level", "[level]", _cmd_effort),
    "init": ("prompt", "Analyze the codebase and write/update CLAUDE.md", None, _cmd_init),
    "doctor": ("core", "Check the health of this halo installation", None, _cmd_doctor),
    "export": ("ui", "Export the conversation", None, _cmd_export),
    "bugreport": ("core", "Write a redacted diagnostic report (one paste instead of screenshots)",
                  "[--copy] [--include-content]", _cmd_bugreport),
    "timeline": ("core", "Show the last turn's request/tool/timing timeline", "[N]", _cmd_timeline),
    "add-dir": ("core", "Add a working directory", "<directory>", _cmd_add_dir),
    "theme": ("core", "Show or set the color theme", "[theme]", _cmd_theme),
    "exit": ("ui", "Exit halo", None, _cmd_exit),
    "copy": ("ui", "Copy the last reply, a code block, or the last tool output to the clipboard",
             "[code [N]|tool]", _cmd_copy),
    "rename": ("ui", "Rename this session", "<title>", _cmd_rename),
    "fork": ("ui", "Fork this session into a new one", None, _cmd_fork),
    "stats": ("core", "Show tokens/cost per model and tool-call counts (--models, --tools)", None, _cmd_stats),
    "tasks": ("core", "List background Bash jobs started this session", None, _cmd_tasks),
    "rewind": ("ui", "Restore the working tree to a recorded step", "[step-id]", _cmd_rewind),
    "undo": ("ui", "Rewind one recorded step back", None, _cmd_undo),
    "redo": ("ui", "Rewind one recorded step forward", None, _cmd_redo),
    "intro": ("ui", "Replay the launch intro", None, _cmd_intro),
    "keybindings": ("core", "Show the active keybindings", None, _cmd_keybindings),
    "tips": ("core", "List tips for using halo's features", None, _cmd_tips),
    "improve": ("ui", "Review self-improvement candidates from recent sessions", None, _cmd_improve),
    "update": ("ui", "Check for a halo update (the TUI can also update and restart in place)",
               None, _cmd_update),
    "editor": ("ui", "Edit the current prompt draft in $VISUAL/$EDITOR (same as Ctrl+E)", None, _cmd_editor),
    "keys": ("ui", "Open the key tester dialog (shows the key name halo receives per press)", None, _cmd_keys),
}


def register_builtins(reg: Registry) -> None:
    for name, (kind, description, hint, run) in _BUILTIN_SPECS.items():
        reg.add(SlashCommand(name=name, description=description, kind=kind, argument_hint=hint,
                              source="builtin", run=run))
