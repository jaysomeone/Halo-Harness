"""halo_harness.providers.settings_merge -- Halo 2.0.3 round 5i part 2:
the merged "where do my settings come from" view the brief asks for --
`halo doctor`'s codex_settings line, the `/settings` command, and the
init wizard's "Settings sources" step all read from `effective_settings`
below. Read-only: Halo never writes to Claude Code's or Codex's own files.

Precedence (brief wording): Halo's own config, then Claude Code settings,
then Codex -- EXCEPT on a `cx:` session, where Codex's own model/
reasoning-effort/approval+sandbox policy leads instead. `settings.primary:
claude|codex` flips which of the other two is preferred (Halo's own config
always stays first either way)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SettingRow:
    key: str
    label: str
    halo: Optional[object] = None
    claude: Optional[object] = None
    codex: Optional[object] = None
    effective: Optional[object] = None
    effective_source: Optional[str] = None  # "halo" | "claude" | "codex" | None


@dataclass
class MergedSettingsView:
    rows: "list[SettingRow]" = field(default_factory=list)
    mcp_servers: "list[dict]" = field(default_factory=list)  # [{"name","source","command"/"url"}]
    instructions: "list[dict]" = field(default_factory=list)  # [{"header","path","source"}]
    codex_config_found: bool = False
    codex_agents_md_count: int = 0
    claude_instructions_count: int = 0
    primary: str = "claude"


def settings_primary(*, state_dir=None) -> str:
    from halo_harness.theme import get_config_value
    value = get_config_value("settings.primary", default="claude")
    return value if value in ("claude", "codex") else "claude"


def set_settings_primary(value: str) -> None:
    from halo_harness.theme import set_config_value
    set_config_value("settings.primary", "codex" if value == "codex" else "claude")


def _halo_settings() -> dict:
    """The handful of Halo-own config.json keys that overlap with
    Claude Code's/Codex's own settings -- never the FULL config.json."""
    from halo_harness.theme import get_config_value
    return {
        "model": get_config_value("model", default=None),
    }


def _claude_settings(cwd: Path) -> dict:
    """Reuses `config.settings.resolve_settings` (the SAME chain `cc:`/
    every other route already resolves against) -- never a second reader."""
    try:
        from halo_harness.config.settings import resolve_settings
        settings = resolve_settings(cwd)
    except Exception:
        return {}
    return {
        "model": settings.model,
        "permission_mode": settings.permissions_default_mode,
    }


def _claude_instruction_headers(cwd: Path) -> "list[dict]":
    """One entry per file Halo's EXISTING CLAUDE.md/AGENTS.md loader found
    (`config/claude_md.py`, unchanged by this round) -- `{"header": "<name>
    (<source tag>)", "source": "claude"}` each, the "one-line header
    naming the file" the brief asks for, same role a CLAUDE.md header
    already plays in the real instructions block this merge view is
    SEPARATE from (display only -- see module docstring)."""
    try:
        from halo_harness.config.claude_md import discover_instructions
        from halo_harness.config.settings import resolve_settings
        settings = resolve_settings(cwd)
        bundle = discover_instructions(cwd, settings, trusted=True)
    except Exception:
        return []
    return [{"header": f"{f.path.name} ({f.source})", "source": "claude"} for f in bundle.files]


def _codex_settings(cwd: Path) -> "tuple[dict, bool]":
    from halo_harness.providers.codex_settings import load_codex_config, load_project_codex_config
    home_config = load_codex_config()
    project_config = load_project_codex_config(cwd)
    found = bool(home_config) or bool(project_config)
    merged = dict(home_config)
    merged.update(project_config)  # project scoping layers over the home config
    return {
        "model": merged.get("model"),
        "permission_mode": merged.get("approval_policy"),
        "sandbox_mode": merged.get("sandbox_mode"),
        "model_reasoning_effort": merged.get("model_reasoning_effort"),
        "mcp_servers": merged.get("mcp_servers") or {},
    }, found


def _pick_effective(key: str, halo_val, claude_val, codex_val, *, primary: str,
                     session_provider: Optional[str]) -> "tuple[object, Optional[str]]":
    """One precedence chain, per key -- see module docstring. `session_
    provider == "codex"` (an active `cx:` session) makes Codex's OWN
    model/reasoning-effort/permission/sandbox values lead for THOSE keys
    specifically, regardless of `primary`."""
    cx_led_keys = ("model", "permission_mode", "sandbox_mode", "model_reasoning_effort")
    if session_provider == "codex" and key in cx_led_keys and codex_val is not None:
        return codex_val, "codex"
    if halo_val is not None:
        return halo_val, "halo"
    ordered = [("claude", claude_val), ("codex", codex_val)] if primary != "codex" \
        else [("codex", codex_val), ("claude", claude_val)]
    for source, value in ordered:
        if value is not None:
            return value, source
    return None, None


_ROW_LABELS = {
    "model": "Default model",
    "permission_mode": "Permission / approval mode",
    "sandbox_mode": "Sandbox",
    "model_reasoning_effort": "Reasoning effort",
}


def effective_settings(cwd: Path, *, primary: Optional[str] = None,
                        session_provider: Optional[str] = None) -> MergedSettingsView:
    """The one function every surface (doctor/`/settings`/the init wizard
    step) calls. `primary` defaults to the persisted `settings.primary`
    config value; `session_provider` is the CURRENT session's
    `ModelRef.provider` when called from inside a live session (None for
    a static CLI/doctor call, which never has one)."""
    cwd = Path(cwd)
    primary = primary or settings_primary()
    halo = _halo_settings()
    claude = _claude_settings(cwd)
    codex, codex_found = _codex_settings(cwd)

    rows = []
    for key, label in _ROW_LABELS.items():
        halo_val, claude_val, codex_val = halo.get(key), claude.get(key), codex.get(key)
        effective, source = _pick_effective(key, halo_val, claude_val, codex_val, primary=primary,
                                              session_provider=session_provider)
        rows.append(SettingRow(key=key, label=label, halo=halo_val, claude=claude_val, codex=codex_val,
                                 effective=effective, effective_source=source))

    mcp_servers = [{"name": name, "source": "codex", "command": (cfg or {}).get("command"),
                      "url": (cfg or {}).get("url")}
                    for name, cfg in (codex.get("mcp_servers") or {}).items()]

    instructions = _claude_instruction_headers(cwd)
    try:
        from halo_harness.providers.codex_settings import load_codex_agents_md_chain
        agents_chain = load_codex_agents_md_chain(cwd)
    except Exception:
        agents_chain = []
    instructions += [{"header": f"{entry['path'].name} ({entry['path'].parent})", "source": "codex"}
                       for entry in agents_chain]
    claude_count = sum(1 for e in instructions if e["source"] == "claude")

    return MergedSettingsView(
        rows=rows, mcp_servers=mcp_servers, instructions=instructions, codex_config_found=codex_found,
        codex_agents_md_count=len(agents_chain), claude_instructions_count=claude_count,
        primary=primary,
    )


def render_settings_text(view: MergedSettingsView) -> str:
    """Plain-text rendering shared by `/settings` and doctor's
    `codex_settings` line's own detail -- one source of wording so the two
    surfaces never drift apart."""
    lines = [f"Settings sources (primary: {view.primary}):"]
    for row in view.rows:
        lines.append(f"  {row.label}: {row.effective!r} (from {row.effective_source or 'nothing configured'}) "
                      f"-- halo={row.halo!r} claude={row.claude!r} codex={row.codex!r}")
    lines.append(f"  Claude Code instructions: {view.claude_instructions_count} file(s) found")
    lines.append(f"  Codex AGENTS.md chain: {view.codex_agents_md_count} file(s) found")
    for entry in view.instructions:
        lines.append(f"    -- {entry['header']} ({entry['source']})")
    if view.mcp_servers:
        names = ", ".join(f"{m['name']} (codex)" for m in view.mcp_servers)
        lines.append(f"  Codex-only MCP servers: {names}")
    lines.append(f"  Codex config.toml found: {'yes' if view.codex_config_found else 'no'}")
    return "\n".join(lines)
