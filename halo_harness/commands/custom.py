"""halo_harness.commands.custom -- `.claude/commands/**/*.md` +
`~/.claude/commands/**/*.md` (U0 scope B): each becomes a `kind="prompt"`
SlashCommand namespaced by its subdirectory (`.claude/commands/git/commit.md`
-> `/git:commit`), frontmatter via `config/frontmatter.py`. `@path` mentions
inside a command's body are intentionally left untouched here -- expanding
them is the core agent loop's job, not the command registry's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from halo_harness.commands.registry import Registry, SlashCommand, expand_command_body
from halo_harness.config.frontmatter import parse as parse_frontmatter


def _namespaced_name(root: Path, md_path: Path) -> str:
    rel = md_path.relative_to(root).with_suffix("")
    return ":".join(rel.parts)


def _allowed_tools_list(fm: dict) -> list:
    allowed = fm.get("allowed-tools") if isinstance(fm, dict) else None
    if isinstance(allowed, str):
        from halo_harness.permissions import split_tool_rule_list
        return split_tool_rule_list(allowed)
    return allowed if isinstance(allowed, list) else []


def _make_run(body: str, allowed_tools: list):
    def _run(args_text: str, facade) -> str:
        # finding 9: route `!cmd` pre-execution through the REAL session's
        # permission engine + stripped tool env when one is attached
        # (every real -p/TUI invocation); a bare/unit-test facade with no
        # session falls back to expand_command_body's own old strict-
        # frontmatter-only gate.
        session = getattr(facade, "session", None)
        result = expand_command_body(
            body, args_text, allowed_tools=allowed_tools, cwd=facade.cwd,
            permission_engine=getattr(session, "permission_engine", None),
            env=getattr(session, "tool_env", None),
        )
        if result.error:
            return f"halo: {result.error}"
        return result.text
    return _run


def _discover_dir(commands_dir: Path, *, source: str) -> list:
    if not commands_dir.is_dir():
        return []
    out = []
    for path in sorted(commands_dir.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, body = parse_frontmatter(raw)
        fm = fm if isinstance(fm, dict) else {}
        name = _namespaced_name(commands_dir, path)
        out.append(SlashCommand(
            name=name, description=fm.get("description", "") or "", kind="prompt",
            argument_hint=fm.get("argument-hint"), source="custom", path=path,
            run=_make_run(body, _allowed_tools_list(fm)),
        ))
    return out


def register_custom_commands(reg: Registry, *, cwd: Path, home: Optional[Path] = None) -> None:
    """Project `.claude/commands/**/*.md` registered first, so it wins a
    same-namespaced collision against the user's own copy (`Registry.add`
    never replaces an existing entry); then `~/.claude/commands/**/*.md`."""
    from halo_harness.config.paths import home as home_fn

    project_dir = Path(cwd) / ".claude" / "commands"
    for cmd in _discover_dir(project_dir, source="project"):
        reg.add(cmd)

    user_home = Path(home) if home is not None else home_fn()
    user_dir = user_home / ".claude" / "commands"
    for cmd in _discover_dir(user_dir, source="user"):
        reg.add(cmd)
