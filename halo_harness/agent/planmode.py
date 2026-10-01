"""halo_harness.agent.planmode -- plan mode (H6 scope C): the plan file (the
ONLY writable path while `PermissionEngine.mode == "plan"`, enforced in
permissions.py's `_categorize`/`_MODE_TABLE`), and the `EnterPlanMode`/
`ExitPlanMode` tool DEFINITIONS (wire schema only -- both are special-cased
directly in agent/loop.py's `_resolve_tool_call`/`_dispatch_tools`, exactly
like AskUserQuestion, so their own `run()` below is a defensive fallback
that should never actually execute in a real session).

Plan D8 / brief C: `EnterPlanMode` (model-initiated -> a `permission_request`
in interactive sessions, allowed outright in `-p`) and `Shift+Tab`/
`--permission-mode plan` both land a session in "plan" mode; a plan file
`~/.claude/plans/<three-random-words>.md` (`plansDirectory` honoured) is
created per session and is the only writable path; `ExitPlanMode(plan)`
writes the file and emits `plan_review`; the reply
`{approved, feedback, mode_after}` flips the mode (default `acceptEdits`) or
returns the plan for revision.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from halo_harness.config.paths import plans_dir
from halo_harness.tools.base import Tool, ToolContext, ToolResult

# A deliberately plain word bank (finding: real Claude Code plan filenames
# look like arbitrary word triples -- "typed-tickling-squirrel",
# "we-created-an-mcp-lively-valiant" -- not a fixed adjective/noun scheme),
# large enough that a 3-word combination collides only very rarely; a
# collision just re-rolls (see `_unique_plan_name`).
_WORDS = [
    "amber", "ancient", "arc", "arrow", "autumn", "banjo", "basil", "birch", "blue", "bold",
    "bright", "brook", "calm", "canyon", "cedar", "cinder", "cloud", "comet", "coral", "crisp",
    "dawn", "delta", "dune", "eager", "echo", "ember", "fable", "falcon", "fern", "flint",
    "forge", "gentle", "glade", "gold", "grove", "harbor", "hazel", "hidden", "hollow", "honey",
    "ivory", "jade", "keen", "kite", "lively", "lunar", "maple", "meadow", "mint", "misty",
    "moss", "nimble", "noble", "ocean", "opal", "pillow", "pine", "plum", "quiet", "quill",
    "raven", "reef", "ridge", "river", "rowan", "sable", "sage", "shore", "silent", "silver",
    "sky", "solar", "spark", "squirrel", "steady", "stone", "swift", "tangle", "thicket", "tickling",
    "tidal", "timber", "typed", "valiant", "velvet", "violet", "willow", "winter", "wren", "zephyr",
]

PLAN_MODE_NOTE = (
    "Plan mode is active. Research the codebase (read-only tools only -- no edits, no writes except "
    "to the plan file itself) and, when you have a concrete plan, call ExitPlanMode with the full "
    "plan text. Do not implement anything until the plan is approved."
)


def _random_words(n: int = 3) -> str:
    return "-".join(random.choice(_WORDS) for _ in range(n))


def plans_directory(settings=None) -> Path:
    """`plansDirectory` [D-CFG] overrides the default `~/.claude/plans`
    (`config.paths.plans_dir()`). `settings` is duck-typed (tried via the
    `.plans_directory` property; a plain dict/None just falls through to
    the default) -- same pattern as `config.paths.memory_dir`."""
    override = None
    if settings is not None:
        override = getattr(settings, "plans_directory", None)
        if override is None and isinstance(settings, dict):
            override = settings.get("plansDirectory")
    if override:
        return Path(override).expanduser()
    return plans_dir()


def _unique_plan_name(directory: Path) -> str:
    for _ in range(50):
        name = _random_words(3) + ".md"
        if not (directory / name).exists():
            return name
    # astronomically unlikely fallback: widen with a 4th word.
    return _random_words(4) + ".md"


def ensure_plan_file(cwd, settings=None, *, existing: Optional[Path] = None) -> Path:
    """The plan file for a session entering plan mode: `existing` (already
    computed for this session, e.g. on --continue/--resume into a session
    that was already in plan mode) is reused verbatim when it still exists;
    otherwise a fresh `<three-random-words>.md` is created (empty) under
    `plans_directory()`."""
    directory = plans_directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    if existing is not None and Path(existing).exists():
        return Path(existing)
    path = directory / _unique_plan_name(directory)
    try:
        path.touch(exist_ok=True)
    except OSError:
        pass
    return path


def write_plan(path: Path, plan_text: str) -> None:
    try:
        Path(path).write_text(plan_text or "", encoding="utf-8")
    except OSError:
        pass


DESCRIPTION_ENTER = (
    "Switch into plan mode: research the codebase without making any changes, then call ExitPlanMode "
    "with your plan once you have one. Use this when the user's request calls for research and a plan "
    "before any edits, or when they explicitly asked for a plan."
)

DESCRIPTION_EXIT = (
    "Exit plan mode by presenting your plan for the user's review. Call this once your research is "
    "done and you have a concrete, step-by-step plan ready -- the user will approve it (you then "
    "implement it) or send back feedback (you keep planning)."
)


class EnterPlanModeTool(Tool):
    name = "EnterPlanMode"
    description = DESCRIPTION_ENTER
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True

    def summary(self, input: dict) -> str:
        return "EnterPlanMode()"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        # Defensive fallback only -- agent/loop.py special-cases this tool
        # before ever reaching tool_registry.dispatch().
        return ToolResult("Plan mode entry is handled by the session loop directly.")


class ExitPlanModeTool(Tool):
    name = "ExitPlanMode"
    description = DESCRIPTION_EXIT
    input_schema = {
        "type": "object",
        "properties": {"plan": {"type": "string", "description": "The plan, in markdown, for the user to review"}},
        "required": ["plan"],
    }

    def summary(self, input: dict) -> str:
        plan = input.get("plan", "") if isinstance(input, dict) else ""
        first_line = (plan or "").strip().splitlines()[0] if (plan or "").strip() else ""
        return f"ExitPlanMode({first_line[:60]})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        # Defensive fallback only -- see EnterPlanModeTool.run.
        plan = input.get("plan") if isinstance(input, dict) else None
        if not plan or not isinstance(plan, str):
            return ToolResult("The plan parameter is required", is_error=True)
        return ToolResult("Plan mode exit is handled by the session loop directly.")
