"""halo_harness.tools.skill -- the Skill tool (H4 scope C, real). Looks a
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

from halo_harness.tools.base import Tool, ToolContext, ToolResult

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

        from halo_harness.commands.skills import find_skill

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

        from halo_harness.config.frontmatter import parse as parse_frontmatter

        try:
            raw = cmd.path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(f"Could not read skill {skill_name!r}: {e}", is_error=True)
        _fm, body = parse_frontmatter(raw)

        from halo_harness.commands.registry import expand_command_body

        # finding 12 (major, h4-h5-h3c review): the documented ${CLAUDE_...}
        # variables a skill body may reference -- CLAUDE_SESSION_ID comes
        # off ctx.session_dir's own basename (agent/loop.py sets
        # `session_dir=log.dir / log.session_id`) rather than a new field,
        # since that value is already guaranteed present whenever a real
        # session is running.
        skill_dir_abs = str(cmd.path.parent.resolve())
        session_id = Path(ctx.session_dir).name if getattr(ctx, "session_dir", None) is not None else None
        claude_vars = {
            "CLAUDE_SKILL_DIR": skill_dir_abs,
            "CLAUDE_SESSION_ID": session_id,
            "CLAUDE_PROJECT_DIR": str(Path(ctx.cwd)),
            "CLAUDE_EFFORT": getattr(ctx, "effort", None),
        }

        # finding 9: route through the real session's permission engine +
        # stripped tool env (ctx.env is already tool_child_env()-stripped
        # -- see agent/loop.py's ToolContext construction) instead of the
        # old strict-frontmatter-only gate + raw os.environ.
        result = expand_command_body(body, args_text, allowed_tools=list(cmd.allowed_tools), cwd=Path(ctx.cwd),
                                      permission_engine=ctx.permission_engine, env=ctx.env, claude_vars=claude_vars)
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

        # finding 12: "Base directory for this skill: <abs>" as the FIRST
        # line (Claude Code's own convention) -- a skill whose
        # instructions reference its own sibling files with a cwd-relative
        # path used to send the model somewhere that doesn't exist; siblings
        # themselves are now listed as ABSOLUTE paths for the same reason.
        text = f"Base directory for this skill: {skill_dir_abs}\n\n" + result.text
        siblings = _sibling_files(cmd.path)
        if siblings:
            text += ("\n\n---\nOther files in this skill's own directory (read them directly, at the "
                     "absolute paths below, if the instructions above reference them):\n")
            text += "\n".join(f"- {p}" for p in siblings)
        if cmd.allowed_tools:
            text += f"\n\n(This skill's own allowed-tools: {', '.join(cmd.allowed_tools)}.)"
        return ToolResult(text)


def _sibling_files(skill_md_path: Path, *, limit: int = _SIBLING_LIMIT) -> list:
    """Every OTHER file under the skill's own directory (recursive),
    SKILL.md itself excluded, listed as ABSOLUTE paths (finding 12 --
    Claude Code's own convention; a cwd-relative path sent a model
    looking in the wrong place entirely once the harness's own cwd
    differed from the skill's directory), sorted, capped at `limit` (a
    safety valve, not expected to bite for a normal skill). Never raises
    -- an unreadable directory just yields an empty list."""
    skill_dir = skill_md_path.parent
    out: list = []
    try:
        resolved_md = skill_md_path.resolve()
        for p in sorted(skill_dir.rglob("*")):
            if not p.is_file():
                continue
            resolved_p = p.resolve()
            if resolved_p == resolved_md:
                continue
            out.append(str(resolved_p))
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out
