"""rolo_claude.headless -- print-mode session driver (U0 scope F rewrite).
Builds a Session and drives one turn (`--input-format text`, the default)
or several (`--input-format stream-json`: one already-parsed JSON object on
stdin per line = one user turn -- cli.py reads/parses stdin, since text-
input's own stdin-prompt read and stream-json's line read are mutually
exclusive ways of consuming the SAME stream) through it, printing via
`output.PrintModeSink` (text/json) or `output.StreamJsonSink` (stream-json).
A leading `/slash-command` resolves and runs through the commands registry's
headless facade BEFORE ever touching a model for a "core"/"ui" kind command;
a "prompt" kind command (a custom command or skill) expands into the turn's
actual prompt text instead.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude import events
from rolo_claude.agent.assemble import SessionContext
from rolo_claude.agent.catalog import SessionCatalog, host_cap, select_preload
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.loop import Session
from rolo_claude.commands.builtins import HeadlessFacade
from rolo_claude.commands.registry import Registry
from rolo_claude.config.claude_json import is_trusted, load_claude_json
from rolo_claude.config.paths import bridge_home, home, lookup_project
from rolo_claude.config.settings import resolve_settings
from rolo_claude.model import DEFAULT_MODEL_REF, parse_model_ref, resolve_model_profile
from rolo_claude.output import PrintModeSink, StreamJsonSink
from rolo_claude.permissions import (
    PermissionEngine, bare_deny_tool_names, build_rules_from_settings, freeze_tool_registry,
    mcp_deny_tool_names, normalize_permission_mode, split_tool_rule_list,
)
from rolo_claude.providers.config import derive_workspace_root, load_env_file, load_routes, resolve_databricks, resolve_openrouter
from rolo_claude.providers.profiles import model_family
from rolo_claude.providers.stream import ProviderCreds
from rolo_claude.theme import load_config as load_rolo_config, resolve_theme
from rolo_claude.tools.registry import ToolRegistry

_SLASH_RE = re.compile(r"^/(\S+)(?:\s+(.*))?$", re.DOTALL)


def _resolve_creds(ref, settings=None) -> Optional[ProviderCreds]:
    """must-do 6: `settings.effective_env` (shell < user < trusted
    project/local < flag < policy, resolved with the session's real
    trust/cwd) is what actually gates OPENROUTER_API_KEY/DATABRICKS_HOST/
    DATABRICKS_TOKEN etc. -- `settings=None` falls back to bare
    `os.environ`."""
    env = settings.effective_env if settings is not None else None
    if ref.provider == "openrouter":
        orc = resolve_openrouter(env)
        if orc is None:
            return None
        return ProviderCreds(base_url=orc.base_url, api_key=orc.api_key)
    if ref.provider == "databricks":
        dbx = resolve_databricks(env)
        if dbx is None:
            return None
        return ProviderCreds(base_url=derive_workspace_root(dbx.host), api_key=dbx.token)
    return None  # "ant:" (direct Anthropic) isn't wired into the openai-chat stream_completion path


def build_hook_runner(*, settings, cwd: Path, session_id: str, transcript_path: str,
                       effort: Optional[str], permission_mode: str, mcp_manager, bare: bool):
    """Shared by `run_print_mode` and `tui/bootstrap.py` (imported from
    there, same reuse pattern as `_resolve_creds`) -- one `HookRunner` per
    session, built from `settings.hooks` (already trust-filtered by
    `config/settings.py`'s own merge) + every enabled plugin's own
    `hooks/hooks.json`. `--bare`/`disableAllHooks` disable the runner
    outright (`enabled=False`), so a caller never has to special-case an
    empty result -- every `HookRunner.has_hooks(...)` call site is then
    simply always False. `prompt_caller` is left unbound here (the CALLER
    binds it to the just-constructed Session's own `_call_model_for_hook`,
    once one exists -- see the call site)."""
    from rolo_claude.config.plugins import load_installed_plugins, _plugin_roots
    from rolo_claude.hooks import HookRunner, load_plugin_hooks, merge_hook_maps, normalize_hooks

    disabled = bare or bool(settings is not None and getattr(settings, "disable_all_hooks", False))
    hooks_by_event: dict = {}
    if not disabled:
        if settings is not None:
            hooks_by_event = normalize_hooks(settings.hooks or {}, default_source="settings")
        manifest = load_installed_plugins()
        for _plugin_name, plugin_root in _plugin_roots(manifest):
            hooks_by_event = merge_hook_maps(hooks_by_event, load_plugin_hooks(plugin_root))
    return HookRunner(
        hooks_by_event, cwd=cwd, session_id=session_id, transcript_path=transcript_path,
        effective_env=(settings.effective_env if settings is not None else None),
        effort=effort, permission_mode=permission_mode, mcp_manager=mcp_manager, enabled=not disabled,
    )


def _extract_user_text(obj: dict) -> str:
    """One `--input-format stream-json` line's user text, whether
    `content` is a plain string or an Anthropic-shaped block list."""
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _maybe_run_slash_command(prompt_text: str, *, registry, facade, disable_slash_commands: bool):
    """`(final_prompt_or_None, direct_output_or_None)` -- exactly one is
    non-None when `prompt_text` resolves to a known command; both None
    when it isn't one (sent through unchanged)."""
    if disable_slash_commands or not prompt_text.startswith("/"):
        return None, None
    m = _SLASH_RE.match(prompt_text)
    if not m:
        return None, None
    cmd = registry.resolve(m.group(1))
    if cmd is None or cmd.run is None:
        return None, None
    output = cmd.run(m.group(2) or "", facade)
    return (output, None) if cmd.kind == "prompt" else (None, output)


def _append_at_mention_snapshots(session, text: Optional[str], cwd: Path) -> None:
    """H4 scope C: a slash-invoked command/skill's EXPANDED body's `@path`
    mentions, read via the Read tool's own path resolution and appended
    as snapshots BEFORE the turn that will carry `text` -- a no-op when
    `text` is None (an ordinary, non-slash prompt was never expanded)."""
    if not text:
        return
    from rolo_claude.commands.registry import read_at_mention_snapshots
    for path_str, content in read_at_mention_snapshots(text, cwd=cwd):
        session.log.append_snapshot([{"type": "text", "text": f"@{path_str}\n{content}"}], kind="at_mention")


def _events_for_direct_output(output: str, *, turn_no: int = 1):
    """A minimal, well-formed event sequence carrying `output` as the
    turn's whole reply -- lets a slash command's direct text run through
    the SAME sinks (text/json/stream-json) a real model turn does, instead
    of every output format needing its own special case."""
    yield events.user_message("(slash command)", turn=turn_no)
    yield events.message_start(turn=turn_no)
    yield events.text_delta(output, turn=turn_no)
    yield events.message_end(turn=turn_no, stop_reason="end_turn", usage={})
    yield events.turn_done(turn=turn_no, reason="end_turn")


def run_print_mode(
    *,
    prompt: Optional[str] = None,
    model_ref_raw: Optional[str] = None,
    small_model_ref_raw: Optional[str] = None,
    cwd: Optional[Path] = None,
    output_format: str = "text",
    input_format: str = "text",
    max_turns: int = 50,
    append_system_prompt: Optional[str] = None,
    system_prompt: Optional[str] = None,
    settings_flag: Optional[str] = None,
    setting_sources: Optional[list] = None,
    verbose: bool = False,
    effort: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    disallowed_tools: Optional[str] = None,
    permission_mode: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    tools: Optional[str] = None,
    add_dir: Optional[list] = None,
    bare: bool = False,
    disable_slash_commands: bool = False,
    session_id: Optional[str] = None,
    include_partial_messages: bool = False,
    max_budget_usd: Optional[float] = None,
    json_schema: Optional[str] = None,
    replay_user_messages: bool = False,
    stdin_lines: Optional[list] = None,
    chrome: bool = False,
    no_chrome: bool = False,
    playwright: bool = False,
    playwright_cdp: Optional[str] = None,
    playwright_headless: bool = False,
    mcp_config: Optional[list] = None,
    strict_mcp_config: bool = False,
) -> int:
    """Run one turn (`input_format="text"`) or several (`"stream-json"`,
    one turn per entry of `stdin_lines`) in print mode; returns the
    process's exit code (the LAST turn's, for stream-json input)."""
    cwd = Path(cwd).resolve() if cwd else Path.cwd()

    # must-do: validated FIRST, before anything (incl. MCP) starts -- the
    # old position (right before `Session(...)`, well after `build_manager`
    # already spawned real MCP subprocesses) meant a bad --session-id
    # returned 2 with those servers still running, `close_all()` never
    # called. Validating up front means a bad id never starts them at all,
    # rather than starting-then-cleaning-up.
    if session_id:
        try:
            uuid.UUID(session_id)
        except ValueError:
            print(f"rolo-claude: --session-id must be a valid UUID, got {session_id!r}", file=sys.stderr)
            return 2

    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, settings_flag=settings_flag, setting_sources=setting_sources, trusted=trusted)
    claude_json_allowed_tools = lookup_project(claude_json, cwd).get("allowedTools")

    cli_allow = split_tool_rule_list(allowed_tools) if allowed_tools else []
    cli_disallow = split_tool_rule_list(disallowed_tools) if disallowed_tools else []
    deny_rules, ask_rules, allow_rules = build_rules_from_settings(
        settings, cwd=cwd, claude_json_allowed_tools=claude_json_allowed_tools,
        cli_allow=cli_allow, cli_disallow=cli_disallow,
    )

    if dangerously_skip_permissions:
        resolved_mode = "bypassPermissions"
    elif permission_mode:
        resolved_mode = normalize_permission_mode(permission_mode)
    elif settings.permissions_default_mode:
        resolved_mode = normalize_permission_mode(settings.permissions_default_mode)
    else:
        resolved_mode = "default"

    frozen_registry = freeze_tool_registry(
        ToolRegistry(), tools_flag=tools, disallowed_tools=cli_disallow, deny_rules=deny_rules,
    )
    extra_dirs = [Path(d) for d in settings.permissions_additional_directories] + [Path(d) for d in (add_dir or [])]
    permission_engine = PermissionEngine(
        deny_rules=deny_rules, ask_rules=ask_rules, allow_rules=allow_rules, mode=resolved_mode,
        cwd=cwd, extra_dirs=extra_dirs, print_mode=True,
    )

    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)

    state_dir = bridge_home()
    routes = load_routes(state_dir / "routes.json")

    model_raw = model_ref_raw or os.environ.get("BRIDGE_MODEL") or routes.get("default") or DEFAULT_MODEL_REF
    model_ref = parse_model_ref(model_raw, routes)
    small_raw = small_model_ref_raw or os.environ.get("BRIDGE_MODEL_SMALL") or routes.get("small") or model_raw
    small_ref = parse_model_ref(small_raw, routes) if small_raw else None
    model_profile = resolve_model_profile(model_ref, state_dir, routes)
    family = model_family(model_ref.model)

    # H3 scope A-C/E: build the MCP manager (real servers + `--chrome`/
    # `--playwright` dynamic ones), then split its tools into "preload"
    # (added to the frozen registry now) vs "deferred" (SessionCatalog's
    # lazy-load pool) under this provider's host cap. `--bare` disables
    # MCP entirely (D-CFG: "--bare skips ... MCP ..."), matching every
    # other config source it turns off.
    mcp_manager = None
    mcp_notices: list = []
    session_catalog = None
    mcp_servers_for_prompt: list = []
    if not bare:
        from rolo_claude.mcp_setup import build_manager, resolve_chrome_enabled
        # finding 14: -p is always non-interactive -- claudeInChromeDefaultEnabled
        # must never auto-enable Chrome here (before this fix, every `-p`
        # on a box with that setting on spawned claude.CMD --claude-in-chrome-mcp,
        # which `claude -p` itself never does).
        chrome_enabled = resolve_chrome_enabled(claude_json, chrome_flag=chrome, no_chrome_flag=no_chrome,
                                                 interactive=False)
        mcp_manager, mcp_notices = build_manager(
            cwd=cwd, claude_json=claude_json, settings=settings, print_mode=True,
            mcp_config_flag=mcp_config, strict_mcp_config=strict_mcp_config,
            chrome=chrome_enabled, playwright=playwright,
            playwright_cdp=playwright_cdp, playwright_headless=playwright_headless,
            bypass_mode=(resolved_mode in ("auto", "bypassPermissions")), start=True, trusted=trusted,
        )
        if mcp_manager is None and mcp_notices:
            print(f"rolo-claude: {mcp_notices[0]}", file=sys.stderr)
        elif verbose:
            for n in mcp_notices:
                print(f"[rolo-claude] mcp: {n}", file=sys.stderr)

        if mcp_manager is not None:
            # scope B built-ins: only offered when there's something real
            # for them to list/read (finding 14's "no false capability
            # promises" rule, applied to MCP resources same as memory/Bash)
            # -- AND only when `--tools`/bare-deny would have let them
            # through `freeze_tool_registry` too (that call already ran,
            # against a registry that couldn't have known about these two
            # yet, so the SAME filter is re-applied here by hand rather
            # than silently bypassing it for just these two names).
            from rolo_claude.tools.mcp_tool import ListMcpResourcesTool, ReadMcpResourceTool
            tools_subset = None
            if tools is not None and tools.strip() != "" and tools.strip().lower() != "default":
                tools_subset = set(split_tool_rule_list(tools))
            elif tools is not None and tools.strip() == "":
                tools_subset = set()
            bare_denied_names = set(bare_deny_tool_names(deny_rules))
            bare_denied_names |= {r.strip() for r in cli_disallow if "(" not in r}

            def _builtin_allowed(name: str) -> bool:
                if tools_subset is not None and name not in tools_subset:
                    return False
                return name not in bare_denied_names

            if _builtin_allowed("ListMcpResourcesTool"):
                frozen_registry.add_tool(ListMcpResourcesTool())
            if _builtin_allowed("ReadMcpResourceTool"):
                frozen_registry.add_tool(ReadMcpResourceTool())

            all_mcp = mcp_manager.all_tools()
            candidate_names = [t[1] for t in all_mcp]
            denied = mcp_deny_tool_names(deny_rules, candidate_names)
            denied |= {r.strip() for r in cli_disallow if "(" not in r and r.strip().startswith("mcp__")}
            survivors = [t for t in all_mcp if t[1] not in denied]

            cap = host_cap(model_ref.provider)
            cap_budget = max(0, cap - len(frozen_registry.names()))
            preload_names = set((load_rolo_config().get("mcpPreload") or []))
            # finding 13 must-do: a SERVER-level "alwaysLoad" (parsed but
            # previously never consulted) preloads every tool from that
            # server, same as each tool having its own _meta[anthropic/
            # alwaysLoad].
            always_load_servers = {name for name, h in mcp_manager.handles.items() if h.config.always_load}
            preload, deferred = select_preload(survivors, preload_names=preload_names,
                                                always_load_servers=always_load_servers, cap_budget=cap_budget)

            from rolo_claude.tools.mcp_tool import McpTool
            for server_name, wire_name, sdk_tool in preload:
                frozen_registry.add_tool(McpTool(server_name, sdk_tool, mcp_manager, vision=model_profile.vision))

            # finding 13 must-do: keep ToolSearch whenever the deferred pool
            # is non-empty, even when --tools/a deny rule would otherwise
            # have excluded it from `frozen_registry` -- without this, e.g.
            # `--tools Read,Bash` strands every deferred MCP tool while the
            # prompt still tells the model to "call ToolSearch".
            if deferred and frozen_registry.get("ToolSearch") is None:
                from rolo_claude.tools.tool_search import ToolSearchTool
                frozen_registry.add_tool(ToolSearchTool())

            session_catalog = SessionCatalog(
                registry=frozen_registry, deferred=deferred, manager=mcp_manager, cap=cap,
                vision=model_profile.vision, names=frozen_registry.names(),
            )
            mcp_servers_for_prompt = [
                {"name": name, "instructions": h.instructions}
                for name, h in mcp_manager.handles.items() if h.state == "connected"
            ]

    # H4 scope E: creds resolved HERE (not after SessionContext, the old
    # position) so WebSearch can be added to `frozen_registry` BEFORE
    # `ctx`/its byte-stable system prompt are built from it -- added
    # after, the tool would still dispatch (same registry object) but the
    # prompt's own tool listing / "WebSearch available" sentence would
    # never mention it (both computed once, at ctx-construction time).
    creds = _resolve_creds(model_ref, settings)
    if not bare and model_ref.provider == "openrouter" and creds is not None and frozen_registry.get("WebSearch") is None:
        from rolo_claude.tools.websearch import build_websearch_tool
        ws_tool = build_websearch_tool(
            main_provider=model_ref.provider, creds=creds,
            small_model_raw=(small_ref.model if small_ref else None), main_model_raw=model_ref.model,
        )
        if ws_tool is not None:
            frozen_registry.add_tool(ws_tool)

    effective_append = append_system_prompt
    if json_schema:
        note = f"Respond with JSON matching this schema (no prose outside the JSON): {json_schema}"
        effective_append = f"{effective_append}\n\n{note}" if effective_append else note

    ctx = SessionContext(
        cwd=cwd, model_label=model_ref.raw, model_family=family, settings_flag=settings_flag,
        setting_sources=setting_sources, append_system_prompt=effective_append, bare=bare,
        tool_registry=frozen_registry, mcp_servers=mcp_servers_for_prompt,
    )
    if system_prompt:
        ctx.system_prompt = system_prompt  # --system-prompt[-file]: full replacement

    openrouter_base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None
    extra_headers = {"x-databricks-use-coding-agent-mode": "true"} if model_ref.provider == "databricks" else None

    # (--session-id already validated at the top of this function, before
    # any MCP server was ever started -- see the must-do note there.)
    session_log = SessionLog(cwd, session_id=session_id or uuid.uuid4().hex)
    hook_runner = build_hook_runner(
        settings=settings, cwd=cwd, session_id=session_log.session_id, transcript_path=str(session_log.path),
        effort=effort, permission_mode=resolved_mode, mcp_manager=mcp_manager, bare=bare,
    )
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=model_profile, creds=creds, state_dir=state_dir,
        model_label=model_ref.raw, session_context=ctx, small_model_ref=small_ref, session_log=session_log,
        max_turns=max_turns, openrouter_base_url=openrouter_base_url, effort=effort,
        extra_headers=extra_headers, permission_engine=permission_engine,
        session_catalog=session_catalog, mcp_manager=mcp_manager, hook_runner=hook_runner,
    )
    if hook_runner is not None:
        # a `prompt`/`agent` hook's one-shot model call is a Session method
        # (it needs the session's own route/profile/creds) -- bound here,
        # after construction, to avoid a chicken-and-egg dependency.
        hook_runner.prompt_caller = session._call_model_for_hook

    if verbose:
        print(f"[rolo-claude] model={model_ref.raw} provider={model_ref.provider} "
              f"dialect={model_ref.dialect} family={family} session={session_log.session_id}", file=sys.stderr)

    # H3: everything past this point may have live MCP subprocess/loop
    # state to tear down -- `finally` guarantees `mcp_manager.close_all()`
    # runs on EVERY exit path (an ordinary return, an early stream-json
    # error return, or an exception escaping `session.turn`), so a
    # `-p` invocation never leaves an orphaned server process or a live
    # daemon thread behind, and `~/.claude.json`/other state is quiescent
    # before the process actually exits (the checksum-stability contract).
    try:
        registry = Registry.discover(cwd, home())
        facade = HeadlessFacade(
            cwd=cwd, settings=settings, claude_json=claude_json, model_ref=model_ref.raw,
            permission_mode=resolved_mode, tool_registry=frozen_registry, registry=registry,
            memory_store=ctx.memory_store, instructions=ctx.instructions, session_id=session_log.session_id,
            effort=effort, theme=resolve_theme(settings_theme=settings.theme),
            context_limit=model_profile.context_tokens,
            mcp_servers={h.config.name: {"type": h.config.type, "command": h.config.command, "args": h.config.args,
                                          "url": h.config.url} for h in (mcp_manager.handles.values() if mcp_manager else [])},
            mcp_status=(mcp_manager.status() if mcp_manager is not None else None),
        )
        # finding 7 must-do: init.mcp_servers is [{name, status}, ...]
        # (Claude Code's own stream-json shape), never a bare name list.
        mcp_servers_status = sorted(
            ({"name": s.get("name"), "status": s.get("state")} for s in (facade.mcp_status or [])),
            key=lambda s: s["name"] or "",
        )
        slash_names = [f"/{c.name}" for c in registry.all()]

        def _make_sink():
            if output_format == "stream-json":
                return StreamJsonSink(
                    session_id=session_log.session_id, cwd=str(cwd), model=model_ref.raw,
                    permission_mode=resolved_mode, tools=frozen_registry.names(), mcp_servers=mcp_servers_status,
                    slash_commands=slash_names, include_partial_messages=include_partial_messages,
                    max_budget_usd=max_budget_usd, permission_denials=session.permission_denials,
                    json_schema=json_schema,
                )
            return PrintModeSink(output_format=output_format, session_id=session_log.session_id, model=model_ref.raw,
                                  verbose=verbose, permission_denials=session.permission_denials, json_schema=json_schema,
                                  max_budget_usd=max_budget_usd)

        if input_format == "stream-json":
            turns = [_extract_user_text(obj) for obj in (stdin_lines or [])]
            turns = [t for t in turns if t]
            if not turns:
                print("rolo-claude: --input-format stream-json requires at least one user message on stdin", file=sys.stderr)
                return 2
            exit_code = 0
            sink = _make_sink()  # ONE sink for the whole run: `init` (stream-json) is per-SESSION, not per-turn
            for i, turn_text in enumerate(turns):
                if replay_user_messages:
                    print(json.dumps({"type": "user", "message": {"role": "user", "content": turn_text}}))
                facade.cost_usd = session.cost_meter.total_usd if session.cost_meter.has_cost_data else facade.cost_usd
                final_prompt, direct_output = _maybe_run_slash_command(
                    turn_text, registry=registry, facade=facade, disable_slash_commands=disable_slash_commands)
                if direct_output is not None:
                    exit_code = sink.consume(_events_for_direct_output(direct_output, turn_no=i + 1))
                else:
                    _append_at_mention_snapshots(session, final_prompt, cwd)
                    exit_code = sink.consume(session.turn(final_prompt or turn_text))
                facade.num_turns += 1
            return exit_code

        prompt_text = prompt or ""
        final_prompt, direct_output = _maybe_run_slash_command(
            prompt_text, registry=registry, facade=facade, disable_slash_commands=disable_slash_commands)
        sink = _make_sink()
        if direct_output is not None:
            return sink.consume(_events_for_direct_output(direct_output))
        _append_at_mention_snapshots(session, final_prompt, cwd)
        return sink.consume(session.turn(final_prompt or prompt_text))
    finally:
        try:
            session._fire_session_end("quit")
        except Exception:
            pass
        if mcp_manager is not None:
            mcp_manager.close_all()
