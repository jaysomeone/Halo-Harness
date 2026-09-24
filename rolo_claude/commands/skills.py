"""rolo_claude.commands.skills -- skills surfaced as slash commands (U0
scope B): three locations -- project `.claude/skills/**/SKILL.md` (found at
cwd and every ancestor up to the filesystem root, closest wins), user
`~/.claude/skills/**/SKILL.md` (excluding the `synced/` subtree), and synced
`~/.claude/skills/synced/<bucket>/manifest.json` + `<bucket>/<name>/SKILL.md`
-> `/anthropic-skills:<name>` (binary facts sec.11), plus a bare `/<name>`
alias when it doesn't collide and has no `:`/`mcp__`. Honors
`user-invocable` (default true -- false hides it from the slash-command
surface entirely). Per plan: "a skill wins over a same-named command" --
enforced via `Registry.add_skill`, which never overrides a builtin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from rolo_claude.commands.registry import Registry, SlashCommand, expand_command_body
from rolo_claude.config.frontmatter import parse as parse_frontmatter


def _allowed_tools_list(fm: dict) -> list:
    allowed = fm.get("allowed-tools") if isinstance(fm, dict) else None
    if isinstance(allowed, str):
        from rolo_claude.permissions import split_tool_rule_list
        return split_tool_rule_list(allowed)
    return allowed if isinstance(allowed, list) else []


def _make_run(body: str, allowed_tools: list):
    def _run(args_text: str, facade) -> str:
        result = expand_command_body(body, args_text, allowed_tools=allowed_tools, cwd=facade.cwd)
        if result.error:
            return f"rolo-claude: {result.error}"
        return result.text
    return _run


def _read_skill_md(skill_dir: Path):
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm, body = parse_frontmatter(raw)
    return (fm if isinstance(fm, dict) else {}), body, md


def _discover_skills_tree(skills_root: Path, *, exclude_synced: bool) -> list:
    """Every `<...>/SKILL.md` under `skills_root`; when `exclude_synced`,
    skips anything under a top-level `synced/` subdirectory (handled
    separately by `_discover_synced`, which needs the manifest for
    metadata rather than a bare directory walk)."""
    if not skills_root.is_dir():
        return []
    out = []
    for md in sorted(skills_root.rglob("SKILL.md")):
        skill_dir = md.parent
        try:
            rel = skill_dir.relative_to(skills_root)
        except ValueError:
            continue
        if exclude_synced and rel.parts and rel.parts[0] == "synced":
            continue
        parsed = _read_skill_md(skill_dir)
        if parsed is None:
            continue
        fm, body, _path = parsed
        if fm.get("user-invocable", True) is False:
            continue
        name = ":".join(rel.parts)
        if not name:
            continue
        description = fm.get("description", "") or fm.get("when_to_use", "") or ""
        out.append(SlashCommand(
            name=name, description=description, kind="prompt", argument_hint=fm.get("argument-hint"),
            source="skill", path=md, run=_make_run(body, _allowed_tools_list(fm)),
        ))
    return out


def _discover_synced(skills_root: Path) -> list:
    synced_root = skills_root / "synced"
    if not synced_root.is_dir():
        return []
    out = []
    for bucket_dir in sorted(p for p in synced_root.iterdir() if p.is_dir()):
        manifest_path = bucket_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = manifest.get("skills") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            base_name = str(entry["name"])
            parsed = _read_skill_md(bucket_dir / base_name)
            fm, body, path = parsed if parsed is not None else ({}, "", bucket_dir / base_name / "SKILL.md")
            if fm.get("user-invocable", True) is False:
                continue
            description = entry.get("description") or fm.get("description", "") or ""
            alias = () if (":" in base_name or base_name.startswith("mcp__")) else (base_name,)
            out.append(SlashCommand(
                name=f"anthropic-skills:{base_name}", description=description, kind="prompt",
                aliases=alias, source="skill", path=path, run=_make_run(body, _allowed_tools_list(fm)),
            ))
    return out


def register_skills(reg: Registry, *, cwd: Path, home: Optional[Path] = None) -> None:
    """Two-phase: collect every skill into a LOCAL name->command map with
    its own internal precedence (closest project ancestor wins over a
    farther one; project wins over user; user wins over synced -- later
    `setdefault` calls in this function never override an already-collected
    name), then register each into `reg` exactly once via `add_skill` (skill
    beats a same-named command, never a builtin) -- avoids re-deriving two
    different "who wins" rules against the shared registry's own mutable
    state."""
    from rolo_claude.config.paths import claude_config_dir as claude_config_dir_fn, home as home_fn

    user_home = Path(home) if home is not None else home_fn()
    if "CLAUDE_CONFIG_DIR" in os.environ:
        skills_root = claude_config_dir_fn() / "skills"
    else:
        skills_root = user_home / ".claude" / "skills"

    collected: dict = {}

    def _collect(cmd: SlashCommand) -> None:
        collected.setdefault(cmd.name, cmd)

    cwd = Path(cwd).resolve()
    for directory in [cwd] + list(cwd.parents):
        # exclude_synced=True unconditionally: "synced" is always a
        # user-account concept (claude.ai delivers it to
        # ~/.claude/skills/synced/, handled by _discover_synced below,
        # never a project-local thing) -- without this, an ancestor walk
        # that happens to reach the real home directory (cwd nested under
        # ~/, the common case) would re-discover the SAME synced skills a
        # second time under a bogus "synced:<bucket>:<name>" name.
        for cmd in _discover_skills_tree(directory / ".claude" / "skills", exclude_synced=True):
            _collect(cmd)

    for cmd in _discover_skills_tree(skills_root, exclude_synced=True):
        _collect(cmd)

    for cmd in _discover_synced(skills_root):
        _collect(cmd)

    for cmd in collected.values():
        reg.add_skill(cmd)
