"""rolo_claude.tools.skill -- the Skill tool (H4 scope C, real). Looks a
skill up by name via `commands/skills.py::find_skill` (the SAME discovery +
precedence `register_skills` uses for the `/` surface, so the tool and the
slash command can never disagree about which skill a name resolves to),
returns its SKILL.md body (frontmatter already stripped by
`config/frontmatter.py`, `$ARGUMENTS`/`$N` substitution + `` !`cmd` ``
pre-execution applied via `commands/registry.py::expand_command_body` --
reused, never duplicated) plus a listing of any sibling files the skill
ships alongside SKILL.md. `disable-model-invocation` hides a skill from
THIS tool only (a user can still type `/name` directly); `context: fork`/
`agent` returns an honest "deferred" error -- sub-agent runs aren't built
yet (finding 14's "never claim a capability this build doesn't have" rule).
"""

from __future__ import annotations

from pathlib import Path

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Invoke a packaged skill (reusable instructions the user or project has set up for a "
    "particular kind of task). Returns the skill's own instructions, with any $ARGUMENTS "
    "substituted, plus a list of any other files that live alongside it -- read those "
    "directly with the Read tool if the skill's instructions reference them."
)

_SIBLING_LIMIT = 200


class SkillTool(Tool):
    name = "Skill"
    description = DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "The name of the skill to invoke"},
            "args": {"type": "string", "description": "Optional arguments for the skill"},
        },
        "required": ["skill"],
    }

    def summary(self, input: dict) -> str:
        return f"Skill({input.get('skill', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("skill", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        skill_name = input.get("skill") if isinstance(input, dict) else None
        if not skill_name or not isinstance(skill_name, str):
            return ToolResult("The skill parameter is required", is_error=True)
        raw_args = input.get("args", "") if isinstance(input, dict) else ""
        args_text = raw_args if isinstance(raw_args, str) else ""

        from rolo_claude.commands.skills import find_skill

        cmd = find_skill(skill_name, Path(ctx.cwd))
        if cmd is None:
            return ToolResult(
                f"No skill named {skill_name!r} was found (checked project .claude/skills, "
                f"the user's ~/.claude/skills, and synced skills).",
                is_error=True,
            )
        if not cmd.model_invocable:
            return ToolResult(
                f"Skill {skill_name!r} has disable-model-invocation set in its frontmatter -- it "
                f"cannot be invoked through this tool (a user can still run it directly as "
                f"/{cmd.name}).",
                is_error=True,
            )
        if cmd.context_mode in ("fork", "agent"):
            return ToolResult(
                f"Skill {skill_name!r} declares context: {cmd.context_mode} (it expects to run in "
                f"its own forked/sub-agent context) -- sub-agent runs are not implemented in this "
                f"build yet (deferred to a later milestone). Invoke it as a plain /{cmd.name} slash "
                f"command instead if its instructions still work without an isolated context.",
                is_error=True,
            )
        if cmd.run is None or cmd.path is None:
            return ToolResult(f"Skill {skill_name!r} has no runnable body.", is_error=True)

        from rolo_claude.config.frontmatter import parse as parse_frontmatter

        try:
            raw = cmd.path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(f"Could not read skill {skill_name!r}: {e}", is_error=True)
        _fm, body = parse_frontmatter(raw)

        from rolo_claude.commands.registry import expand_command_body

        result = expand_command_body(body, args_text, allowed_tools=list(cmd.allowed_tools), cwd=Path(ctx.cwd))
        if result.error:
            return ToolResult(f"Skill {skill_name!r}: {result.error}", is_error=True)

        # D-CFG: "allowed-tools -> session rules until the next user
        # message" -- a REAL grant (Session.turn() clears everything
        # added this way at the start of the next turn), not just the
        # informational note below; `ctx.session_allow_rule` is None for
        # a bare ToolContext (every existing test), which just skips this
        # without erroring.
        if cmd.allowed_tools and ctx.session_allow_rule is not None:
            for rule_text in cmd.allowed_tools:
                ctx.session_allow_rule(rule_text)

        text = result.text
        siblings = _sibling_files(cmd.path)
        if siblings:
            text += ("\n\n---\nOther files in this skill's own directory (read them directly, "
                     "relative to that directory, if the instructions above reference them):\n")
            text += "\n".join(f"- {p}" for p in siblings)
        if cmd.allowed_tools:
            text += f"\n\n(This skill's own allowed-tools: {', '.join(cmd.allowed_tools)}.)"
        return ToolResult(text)


def _sibling_files(skill_md_path: Path, *, limit: int = _SIBLING_LIMIT) -> list:
    """Every OTHER file under the skill's own directory (recursive),
    SKILL.md itself excluded, relative posix-style paths, sorted, capped
    at `limit` (a safety valve, not expected to bite for a normal skill).
    Never raises -- an unreadable directory just yields an empty list."""
    skill_dir = skill_md_path.parent
    out: list = []
    try:
        resolved_md = skill_md_path.resolve()
        for p in sorted(skill_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.resolve() == resolved_md:
                continue
            out.append(str(p.relative_to(skill_dir)).replace("\\", "/"))
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out
