"""halo_harness.commands.skills -- skills surfaced as slash commands (U0
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

from halo_harness.commands.registry import Registry, SlashCommand, expand_command_body
from halo_harness.config.frontmatter import parse as parse_frontmatter


def _allowed_tools_list(fm: dict) -> list:
    allowed = fm.get("allowed-tools") if isinstance(fm, dict) else None
    if isinstance(allowed, str):
        from halo_harness.permissions import split_tool_rule_list
        return split_tool_rule_list(allowed)
    return allowed if isinstance(allowed, list) else []


def _make_run(body: str, allowed_tools: list, skill_path: "Optional[Path]" = None):
    def _run(args_text: str, facade) -> str:
        # finding 9: see commands/custom.py's own identical comment.
        session = getattr(facade, "session", None)
        # H5c finding 15: `/skill-name` used to build NONE of what
        # `tools/skill.py`'s own Skill TOOL gives a model-invoked skill --
        # a skill whose body uses `${CLAUDE_SKILL_DIR}/scripts/x.py` sent
        # the literal, unsubstituted variable name (expanding to an empty
        # string inside `` !`...` ``, so the command ran `python
        # /scripts/x.py`), and there was no "Base directory for this
        # skill:" line or sibling-file listing either. Built here from the
        # SAME inputs `SkillTool.run` uses (`skill_path`, closed over from
        # discovery time; `session.log.session_id`/`session.effort` at
        # invocation time), so the two paths can never disagree.
        claude_vars = None
        skill_dir_abs = None
        if skill_path is not None:
            skill_dir_abs = str(skill_path.parent.resolve())
            session_log = getattr(session, "log", None)
            session_id = getattr(session_log, "session_id", None) if session_log is not None else None
            claude_vars = {
                "CLAUDE_SKILL_DIR": skill_dir_abs,
                "CLAUDE_SESSION_ID": session_id,
                "CLAUDE_PROJECT_DIR": str(facade.cwd),
                "CLAUDE_EFFORT": getattr(session, "effort", None),
            }
        result = expand_command_body(
            body, args_text, allowed_tools=allowed_tools, cwd=facade.cwd,
            permission_engine=getattr(session, "permission_engine", None),
            env=getattr(session, "tool_env", None), claude_vars=claude_vars,
        )
        if result.error:
            return f"halo: {result.error}"
        text = result.text
        if skill_dir_abs is not None:
            from halo_harness.tools.skill import _sibling_files

            text = f"Base directory for this skill: {skill_dir_abs}\n\n" + text
            siblings = _sibling_files(skill_path)
            if siblings:
                text += ("\n\n---\nOther files in this skill's own directory (read them directly, at the "
                         "absolute paths below, if the instructions above reference them):\n")
                text += "\n".join(f"- {p}" for p in siblings)
            if allowed_tools:
                text += f"\n\n(This skill's own allowed-tools: {', '.join(allowed_tools)}.)"
        return text
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
        allowed = tuple(_allowed_tools_list(fm))
        out.append(SlashCommand(
            name=name, description=description, kind="prompt", argument_hint=fm.get("argument-hint"),
            source="skill", path=md, run=_make_run(body, list(allowed), skill_path=md),
            model_invocable=fm.get("disable-model-invocation", False) is not True,
            allowed_tools=allowed, context_mode=(fm.get("context") if fm.get("context") in ("fork", "agent") else None),
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
            allowed = tuple(_allowed_tools_list(fm))
            out.append(SlashCommand(
                name=f"anthropic-skills:{base_name}", description=description, kind="prompt",
                aliases=alias, source="skill", path=path, run=_make_run(body, list(allowed), skill_path=path),
                model_invocable=fm.get("disable-model-invocation", False) is not True,
                allowed_tools=allowed, context_mode=(fm.get("context") if fm.get("context") in ("fork", "agent") else None),
            ))
    return out


def discover_all_skills(cwd: Path, *, home: Optional[Path] = None) -> dict:
    """`{name: SlashCommand}` for every skill visible from `cwd`, with the
    SAME internal precedence `register_skills` applies to the `/` surface
    (closest project ancestor > user > synced) -- the ONE traversal both
    `register_skills` (the slash-command surface) and `tools/skill.py`'s
    real Skill TOOL (a by-name lookup, never through the shared Registry
    object a Tool has no reference to) build on, so the two can never
    disagree about which skill a name resolves to."""
    from halo_harness.config.paths import claude_config_dir as claude_config_dir_fn, home as home_fn

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

    return collected


def find_skill(name: str, cwd: Path, *, home: Optional[Path] = None) -> Optional[SlashCommand]:
    """By-name lookup for the Skill TOOL (`tools/skill.py`) -- also
    resolves a synced skill's bare alias (e.g. `docx` for `anthropic-
    skills:docx`) when it isn't itself a collision, same rule `Registry.
    resolve` applies to the `/` surface."""
    skills = discover_all_skills(cwd, home=home)
    if name in skills:
        return skills[name]
    for cmd in skills.values():
        if name in cmd.aliases:
            return cmd
    return None


def register_skills(reg: Registry, *, cwd: Path, home: Optional[Path] = None) -> None:
    """Register every skill `discover_all_skills` finds into `reg` exactly
    once via `add_skill` (skill beats a same-named command, never a
    builtin)."""
    for cmd in discover_all_skills(cwd, home=home).values():
        reg.add_skill(cmd)
