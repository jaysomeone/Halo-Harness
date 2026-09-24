"""rolo_claude.headless -- print-mode session driver (H1). Builds a Session
from CLI-ish parameters and runs exactly one turn through it, printing via
output.PrintModeSink. U1 owns the FULL headless.py (every flag from plan
D-TUI, stream-json, exit codes 0/1/2/130, --continue/--resume, ...) -- this
is deliberately just enough for `-p` to answer one prompt (now with real
tool loops) end to end.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude.agent.assemble import SessionContext
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.loop import Session
from rolo_claude.config.claude_json import is_trusted, load_claude_json
from rolo_claude.config.paths import bridge_home, home, lookup_project
from rolo_claude.config.settings import resolve_settings
from rolo_claude.model import DEFAULT_MODEL_REF, parse_model_ref, resolve_model_profile
from rolo_claude.output import PrintModeSink
from rolo_claude.permissions import (
    PermissionEngine, build_rules_from_settings, freeze_tool_registry,
    normalize_permission_mode, split_tool_rule_list,
)
from rolo_claude.providers.config import derive_workspace_root, load_env_file, load_routes, resolve_databricks, resolve_openrouter
from rolo_claude.providers.profiles import model_family
from rolo_claude.providers.stream import ProviderCreds
from rolo_claude.tools.registry import ToolRegistry


def _resolve_creds(ref, settings=None) -> Optional[ProviderCreds]:
    """must-do 6: `settings.effective_env` (shell < user < trusted
    project/local < flag < policy, resolved with the session's real
    trust/cwd) is what actually gates OPENROUTER_API_KEY/DATABRICKS_HOST/
    DATABRICKS_TOKEN etc. now -- `settings=None` (any caller that hasn't
    built a SessionContext yet) falls back to bare `os.environ`, matching
    the pre-H2 behavior exactly."""
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


def run_print_mode(
    *,
    prompt: str,
    model_ref_raw: Optional[str] = None,
    small_model_ref_raw: Optional[str] = None,
    cwd: Optional[Path] = None,
    output_format: str = "text",
    max_turns: int = 50,
    append_system_prompt: Optional[str] = None,
    settings_flag: Optional[str] = None,
    verbose: bool = False,
    effort: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    disallowed_tools: Optional[str] = None,
    permission_mode: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    tools: Optional[str] = None,
) -> int:
    """Run one turn (which may involve several model calls plus tool
    dispatch) in print mode; returns the process exit code."""
    # finding 2: resolve a relative --cwd (e.g. ".") to an absolute path
    # ONCE, here, before anything derives a slug from it.
    cwd = Path(cwd).resolve() if cwd else Path.cwd()

    # H2 scope C/D: resolve settings/trust ONCE here (a cheap re-read of a
    # few small JSON files -- SessionContext below resolves its OWN copy
    # too, since it has no way to accept a pre-built one without a larger
    # refactor) so the tool CATALOG can be frozen (bare-name deny removal,
    # --tools) BEFORE SessionContext builds the byte-stable system prompt
    # from whatever registry it's handed.
    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, settings_flag=settings_flag, trusted=trusted)
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
    permission_engine = PermissionEngine(
        deny_rules=deny_rules, ask_rules=ask_rules, allow_rules=allow_rules, mode=resolved_mode,
        cwd=cwd, extra_dirs=[Path(d) for d in settings.permissions_additional_directories], print_mode=True,
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

    # must-do 6: build the SessionContext (which resolves Settings with
    # this session's REAL cwd/trust) BEFORE resolving creds, so
    # _resolve_creds reads Settings.effective_env instead of a second,
    # untrusted, cwd-blind settings re-derivation.
    ctx = SessionContext(
        cwd=cwd, model_label=model_ref.raw, model_family=family, settings_flag=settings_flag,
        append_system_prompt=append_system_prompt, tool_registry=frozen_registry,
    )
    creds = _resolve_creds(model_ref, ctx.settings)
    openrouter_base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None
    # must-do 6: Databricks' own coding-agent-mode header.
    extra_headers = {"x-databricks-use-coding-agent-mode": "true"} if model_ref.provider == "databricks" else None

    session_log = SessionLog(cwd, session_id=uuid.uuid4().hex)
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=model_profile, creds=creds, state_dir=state_dir,
        model_label=model_ref.raw, session_context=ctx, small_model_ref=small_ref, session_log=session_log,
        max_turns=max_turns, openrouter_base_url=openrouter_base_url, effort=effort,
        extra_headers=extra_headers, permission_engine=permission_engine,
    )

    if verbose:
        import sys
        print(f"[rolo-claude] model={model_ref.raw} provider={model_ref.provider} "
              f"dialect={model_ref.dialect} family={family} session={session_log.session_id}", file=sys.stderr)

    sink = PrintModeSink(output_format=output_format, session_id=session_log.session_id, model=model_ref.raw,
                          verbose=verbose, permission_denials=session.permission_denials)
    return sink.consume(session.turn(prompt))
