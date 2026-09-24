"""rolo_claude.tools.skill -- the Skill tool STUB (H2 scope A). Real skill
discovery (`.claude/skills/`, synced skills, frontmatter, substitution,
pre-exec) is H4's job; this build has no skills loaded at all, so every call
here honestly errors instead of pretending to have run something -- finding
14's "never claim a capability this build doesn't have" rule applies to a
new tool exactly as much as it did to the system prompt.
"""

from __future__ import annotations

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Invoke a packaged skill (reusable instructions the user or project has set up for a "
    "particular kind of task). This build does not load any skills yet -- calling this tool "
    "always returns an error naming that; do not claim to have followed a skill's guidance if "
    "this tool errors."
)


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
        skill = input.get("skill") if isinstance(input, dict) else None
        if not skill or not isinstance(skill, str):
            return ToolResult("The skill parameter is required", is_error=True)
        return ToolResult(
            f"No skills are available in this build (skill {skill!r} not found) -- "
            f"skill discovery is not implemented yet.",
            is_error=True,
        )
