"""rolo_claude.headless -- print-mode session driver (H0 subset). Builds a
Session from CLI-ish parameters and runs exactly one turn through it,
printing via output.PrintModeSink. U1 owns the FULL headless.py (every
flag from plan D-TUI, stream-json, exit codes 0/1/2/130, --continue/
--resume, ...) -- this is deliberately just enough for `-p` to answer one
prompt end to end.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude.agent.assemble import SessionContext
from rolo_claude.agent.loop import Session
from rolo_claude.agent.session_store import SessionStore
from rolo_claude.config.paths import bridge_home, home
from rolo_claude.model import parse_model_ref, resolve_model_profile
from rolo_claude.output import PrintModeSink
from rolo_claude.providers.config import derive_workspace_root, load_env_file, load_routes, resolve_databricks, resolve_openrouter
from rolo_claude.providers.stream import ProviderCreds

DEFAULT_MODEL_REF = "or:deepseek/deepseek-v3.2"


def _resolve_creds(ref) -> Optional[ProviderCreds]:
    if ref.provider == "openrouter":
        orc = resolve_openrouter()
        if orc is None:
            return None
        return ProviderCreds(base_url=orc.base_url, api_key=orc.api_key)
    if ref.provider == "databricks":
        dbx = resolve_databricks()
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
) -> int:
    """Run exactly one turn in print mode; returns the process exit code."""
    cwd = Path(cwd) if cwd else Path.cwd()

    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)

    state_dir = bridge_home()
    routes = load_routes(state_dir / "routes.json")

    model_raw = model_ref_raw or os.environ.get("BRIDGE_MODEL") or routes.get("default") or DEFAULT_MODEL_REF
    model_ref = parse_model_ref(model_raw, routes)

    small_raw = small_model_ref_raw or os.environ.get("BRIDGE_MODEL_SMALL") or routes.get("small") or model_raw
    small_ref = parse_model_ref(small_raw, routes) if small_raw else None

    profile = resolve_model_profile(model_ref, state_dir, routes)
    creds = _resolve_creds(model_ref)
    openrouter_base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None

    ctx = SessionContext(
        cwd=cwd, model_label=model_ref.raw, settings_flag=settings_flag,
        append_system_prompt=append_system_prompt,
    )

    session_store = SessionStore(cwd, session_id=uuid.uuid4().hex)
    session = Session(
        cwd=cwd, model_ref=model_ref, profile=profile, creds=creds, state_dir=state_dir,
        system_prompt=ctx.system_prompt, small_model_ref=small_ref, session_store=session_store,
        max_turns=max_turns, openrouter_base_url=openrouter_base_url,
    )
    claude_md_text = ctx.claude_md_text()
    if claude_md_text:
        session.set_initial_prefix_blocks([{"type": "text", "text": claude_md_text}])

    if verbose:
        import sys
        print(f"[rolo-claude] model={model_ref.raw} provider={model_ref.provider} "
              f"dialect={model_ref.dialect} session={session_store.session_id}", file=sys.stderr)

    sink = PrintModeSink(output_format=output_format, session_id=session_store.session_id, model=model_ref.raw)
    return sink.consume(session.turn(prompt))
