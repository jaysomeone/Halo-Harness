"""rolo_claude.tui.bootstrap -- builds a real `agent.loop.Session` +
`controller.Controller` for an interactive TUI launch. Deliberately mirrors
`headless.py.run_print_mode`'s own setup (same config/permission/MCP
resolution order) up through constructing the `Session` -- the two entry
points must resolve config identically, they just drive the result
differently (one sink loop, one interactive Controller). Kept as its own
module (not a refactor of headless.py, which U0/H owns) so the TUI can
evolve its tail independently.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude.agent.assemble import SessionContext
from rolo_claude.agent.catalog import SessionCatalog, host_cap, select_preload
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.loop import Session
from rolo_claude.commands.builtins import HeadlessFacade
from rolo_claude.commands.registry import Registry
from rolo_claude.config.claude_json import is_trusted, load_claude_json
from rolo_claude.config.paths import bridge_home, home, lookup_project
from rolo_claude.config.settings import resolve_settings
from rolo_claude.controller import Controller
from rolo_claude.model import DEFAULT_MODEL_REF, parse_model_ref, resolve_model_profile
from rolo_claude.permissions import (
    PermissionEngine, bare_deny_tool_names, build_rules_from_settings, freeze_tool_registry,
    mcp_deny_tool_names, normalize_permission_mode, split_tool_rule_list,
)
from rolo_claude.providers.config import derive_workspace_root, load_env_file, load_routes, resolve_databricks, resolve_openrouter
from rolo_claude.providers.profiles import model_family
from rolo_claude.theme import load_config as load_rolo_config, resolve_theme
from rolo_claude.tools.registry import ToolRegistry


def _resolve_creds(ref, settings=None):
    """Same rule headless.py's own (module-private) helper uses -- imported
    from there rather than duplicated, since the two must never drift."""
    from rolo_claude.headless import _resolve_creds as _headless_resolve_creds
    return _headless_resolve_creds(ref, settings)


def build_controller(args) -> "tuple[Controller, Registry, object]":
    """`args`: an argparse.Namespace with the same attributes cli.py's flag
    table produces (only the ones relevant to an interactive launch are
    read). Returns `(controller, registry, facade)`, NOT yet started
    (`controller.start()` is the caller's job, once the App is ready to
    receive events)."""
    cwd = Path(args.cwd).resolve() if getattr(args, "cwd", None) else Path.cwd()

    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, settings_flag=getattr(args, "settings", None),
                                 setting_sources=None, trusted=trusted)
    claude_json_allowed_tools = lookup_project(claude_json, cwd).get("allowedTools")

    allowed_tools = getattr(args, "allowed_tools", None)
    disallowed_tools = getattr(args, "disallowed_tools", None)
    cli_allow = split_tool_rule_list(allowed_tools) if allowed_tools else []
    cli_disallow = split_tool_rule_list(disallowed_tools) if disallowed_tools else []
    deny_rules, ask_rules, allow_rules = build_rules_from_settings(
        settings, cwd=cwd, claude_json_allowed_tools=claude_json_allowed_tools,
        cli_allow=cli_allow, cli_disallow=cli_disallow,
    )

    permission_mode = getattr(args, "permission_mode", None)
    if getattr(args, "dangerously_skip_permissions", False):
        resolved_mode = "bypassPermissions"
    elif permission_mode:
        resolved_mode = normalize_permission_mode(permission_mode)
    elif settings.permissions_default_mode:
        resolved_mode = normalize_permission_mode(settings.permissions_default_mode)
    else:
        resolved_mode = "default"

    bare = bool(getattr(args, "bare", False))
    tools_flag = getattr(args, "tools", None)
    frozen_registry = freeze_tool_registry(ToolRegistry(), tools_flag=tools_flag,
                                            disallowed_tools=cli_disallow, deny_rules=deny_rules)
    add_dir = getattr(args, "add_dir", None) or []
    extra_dirs = [Path(d) for d in settings.permissions_additional_directories] + [Path(d) for d in add_dir]
    permission_engine = PermissionEngine(deny_rules=deny_rules, ask_rules=ask_rules, allow_rules=allow_rules,
                                          mode=resolved_mode, cwd=cwd, extra_dirs=extra_dirs, print_mode=False)

    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    state_dir = bridge_home()
    routes = load_routes(state_dir / "routes.json")

    model_raw = (getattr(args, "model", None) or os.environ.get("BRIDGE_MODEL")
                 or routes.get("default") or DEFAULT_MODEL_REF)
    model_ref = parse_model_ref(model_raw, routes)
    small_raw = getattr(args, "small_model", None) or os.environ.get("BRIDGE_MODEL_SMALL") or routes.get("small") or model_raw
    small_ref = parse_model_ref(small_raw, routes) if small_raw else None
    model_profile = resolve_model_profile(model_ref, state_dir, routes)
    family = model_family(model_ref.model)

    mcp_manager, mcp_notices, session_catalog, mcp_servers_for_prompt = _build_mcp(
        args, cwd=cwd, claude_json=claude_json, settings=settings, resolved_mode=resolved_mode,
        deny_rules=deny_rules, cli_disallow=cli_disallow, frozen_registry=frozen_registry,
        model_ref=model_ref, model_profile=model_profile, bare=bare,
    )

    ctx = SessionContext(cwd=cwd, model_label=model_ref.raw, model_family=family,
                          settings_flag=getattr(args, "settings", None), setting_sources=None,
                          append_system_prompt=getattr(args, "append_system_prompt", None), bare=bare,
                          tool_registry=frozen_registry, mcp_servers=mcp_servers_for_prompt)

    creds = _resolve_creds(model_ref, ctx.settings)
    openrouter_base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None
    extra_headers = {"x-databricks-use-coding-agent-mode": "true"} if model_ref.provider == "databricks" else None

    session_id = getattr(args, "session_id", None)
    session_log = SessionLog(cwd, session_id=session_id or uuid.uuid4().hex)
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=model_profile, creds=creds, state_dir=state_dir,
        model_label=model_ref.raw, session_context=ctx, small_model_ref=small_ref, session_log=session_log,
        max_turns=getattr(args, "max_turns", None) or 50, openrouter_base_url=openrouter_base_url,
        effort=getattr(args, "effort", None), extra_headers=extra_headers, permission_engine=permission_engine,
        session_catalog=session_catalog, mcp_manager=mcp_manager,
    )

    registry = Registry.discover(cwd, home())
    facade = HeadlessFacade(
        cwd=cwd, settings=settings, claude_json=claude_json, model_ref=model_ref.raw,
        permission_mode=resolved_mode, tool_registry=frozen_registry, registry=registry,
        memory_store=ctx.memory_store, instructions=ctx.instructions, session_id=session_log.session_id,
        effort=getattr(args, "effort", None), theme=resolve_theme(settings_theme=settings.theme),
        context_limit=model_profile.context_tokens,
        mcp_servers={h.config.name: {"type": h.config.type, "command": h.config.command, "args": h.config.args,
                                      "url": h.config.url} for h in (mcp_manager.handles.values() if mcp_manager else [])},
        mcp_status=(mcp_manager.status() if mcp_manager is not None else None),
    )

    def _mcp_status_fn() -> dict:
        if mcp_manager is None:
            return {"connected": 0, "total": 0}
        rows = mcp_manager.status()
        return {"connected": sum(1 for r in rows if r.get("state") == "connected"), "total": len(rows)}

    def _reconnect_fn(name: str) -> list:
        if mcp_manager is None:
            return [f"MCP support is not enabled this session ({name} unchanged)."]
        ok = mcp_manager.reconnect(name)
        row = next((r for r in mcp_manager.status() if r.get("name") == name), None)
        state = row.get("state") if row else "unknown"
        return [f"{name}: {'connected' if ok else 'failed'} (state={state})"]

    controller = Controller(
        session=session, cwd=cwd, state_dir=state_dir, routes=routes, registry=registry, facade=facade,
        mcp_status_fn=_mcp_status_fn, reconnect_fn=_reconnect_fn, settings=ctx.settings,
    )
    controller.mcp_manager = mcp_manager  # tui/app.py's clean shutdown hook
    for n in mcp_notices:
        print(f"[rolo-claude] mcp: {n}", file=sys.stderr)
    return controller, registry, facade


def _build_mcp(args, *, cwd, claude_json, settings, resolved_mode, deny_rules, cli_disallow,
                frozen_registry, model_ref, model_profile, bare):
    if bare:
        return None, [], None, []
    from rolo_claude.mcp_setup import build_manager, resolve_chrome_enabled

    chrome_enabled = resolve_chrome_enabled(claude_json, chrome_flag=getattr(args, "chrome", False),
                                             no_chrome_flag=getattr(args, "no_chrome", False))
    mcp_manager, mcp_notices = build_manager(
        cwd=cwd, claude_json=claude_json, settings=settings, print_mode=False,
        mcp_config_flag=getattr(args, "mcp_config", None), strict_mcp_config=getattr(args, "strict_mcp_config", False),
        chrome=chrome_enabled, playwright=getattr(args, "playwright", False),
        playwright_cdp=getattr(args, "playwright_cdp", None), playwright_headless=getattr(args, "playwright_headless", False),
        bypass_mode=(resolved_mode in ("auto", "bypassPermissions")), start=True,
    )
    if mcp_manager is None:
        return None, mcp_notices, None, []

    from rolo_claude.tools.mcp_tool import ListMcpResourcesTool, McpTool, ReadMcpResourceTool
    tools_str = getattr(args, "tools", None)
    tools_subset = None
    if tools_str is not None and tools_str.strip() not in ("", "default"):
        tools_subset = set(split_tool_rule_list(tools_str))
    elif tools_str is not None and tools_str.strip() == "":
        tools_subset = set()
    bare_denied = set(bare_deny_tool_names(deny_rules)) | {r.strip() for r in cli_disallow if "(" not in r}

    def _builtin_allowed(name: str) -> bool:
        if tools_subset is not None and name not in tools_subset:
            return False
        return name not in bare_denied

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
    preload, deferred = select_preload(survivors, preload_names=preload_names, cap_budget=cap_budget)
    for server_name, wire_name, sdk_tool in preload:
        frozen_registry.add_tool(McpTool(server_name, sdk_tool, mcp_manager, vision=model_profile.vision))

    session_catalog = SessionCatalog(registry=frozen_registry, deferred=deferred, manager=mcp_manager,
                                      cap=cap, vision=model_profile.vision, names=frozen_registry.names())
    mcp_servers_for_prompt = [{"name": name, "instructions": h.instructions}
                               for name, h in mcp_manager.handles.items() if h.state == "connected"]
    return mcp_manager, mcp_notices, session_catalog, mcp_servers_for_prompt
