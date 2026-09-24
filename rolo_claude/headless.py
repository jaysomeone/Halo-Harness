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
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.loop import Session
from rolo_claude.commands.builtins import HeadlessFacade
from rolo_claude.commands.registry import Registry
from rolo_claude.config.claude_json import is_trusted, load_claude_json, mcp_servers_for
from rolo_claude.config.paths import bridge_home, home, lookup_project
from rolo_claude.config.settings import resolve_settings
from rolo_claude.model import DEFAULT_MODEL_REF, parse_model_ref, resolve_model_profile
from rolo_claude.output import PrintModeSink, StreamJsonSink
from rolo_claude.permissions import (
    PermissionEngine, build_rules_from_settings, freeze_tool_registry,
    normalize_permission_mode, split_tool_rule_list,
)
from rolo_claude.providers.config import derive_workspace_root, load_env_file, load_routes, resolve_databricks, resolve_openrouter
from rolo_claude.providers.profiles import model_family
from rolo_claude.providers.stream import ProviderCreds
from rolo_claude.theme import resolve_theme
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
) -> int:
    """Run one turn (`input_format="text"`) or several (`"stream-json"`,
    one turn per entry of `stdin_lines`) in print mode; returns the
    process's exit code (the LAST turn's, for stream-json input)."""
    cwd = Path(cwd).resolve() if cwd else Path.cwd()

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

    effective_append = append_system_prompt
    if json_schema:
        note = f"Respond with JSON matching this schema (no prose outside the JSON): {json_schema}"
        effective_append = f"{effective_append}\n\n{note}" if effective_append else note

    ctx = SessionContext(
        cwd=cwd, model_label=model_ref.raw, model_family=family, settings_flag=settings_flag,
        setting_sources=setting_sources, append_system_prompt=effective_append, bare=bare,
        tool_registry=frozen_registry,
    )
    if system_prompt:
        ctx.system_prompt = system_prompt  # --system-prompt[-file]: full replacement

    creds = _resolve_creds(model_ref, ctx.settings)
    openrouter_base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None
    extra_headers = {"x-databricks-use-coding-agent-mode": "true"} if model_ref.provider == "databricks" else None

    if session_id:
        try:
            uuid.UUID(session_id)
        except ValueError:
            print(f"rolo-claude: --session-id must be a valid UUID, got {session_id!r}", file=sys.stderr)
            return 2
    session_log = SessionLog(cwd, session_id=session_id or uuid.uuid4().hex)
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=model_profile, creds=creds, state_dir=state_dir,
        model_label=model_ref.raw, session_context=ctx, small_model_ref=small_ref, session_log=session_log,
        max_turns=max_turns, openrouter_base_url=openrouter_base_url, effort=effort,
        extra_headers=extra_headers, permission_engine=permission_engine,
    )

    if verbose:
        print(f"[rolo-claude] model={model_ref.raw} provider={model_ref.provider} "
              f"dialect={model_ref.dialect} family={family} session={session_log.session_id}", file=sys.stderr)

    registry = Registry.discover(cwd, home())
    facade = HeadlessFacade(
        cwd=cwd, settings=settings, claude_json=claude_json, model_ref=model_ref.raw,
        permission_mode=resolved_mode, tool_registry=frozen_registry, registry=registry,
        memory_store=ctx.memory_store, instructions=ctx.instructions, session_id=session_log.session_id,
        effort=effort, theme=resolve_theme(settings_theme=settings.theme),
        context_limit=model_profile.context_tokens, mcp_servers=mcp_servers_for(cwd, claude_json),
    )
    mcp_names = sorted(facade.mcp_servers)
    slash_names = [f"/{c.name}" for c in registry.all()]

    def _make_sink():
        if output_format == "stream-json":
            return StreamJsonSink(
                session_id=session_log.session_id, cwd=str(cwd), model=model_ref.raw,
                permission_mode=resolved_mode, tools=frozen_registry.names(), mcp_servers=mcp_names,
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
                exit_code = sink.consume(session.turn(final_prompt or turn_text))
            facade.num_turns += 1
        return exit_code

    prompt_text = prompt or ""
    final_prompt, direct_output = _maybe_run_slash_command(
        prompt_text, registry=registry, facade=facade, disable_slash_commands=disable_slash_commands)
    sink = _make_sink()
    if direct_output is not None:
        return sink.consume(_events_for_direct_output(direct_output))
    return sink.consume(session.turn(final_prompt or prompt_text))
