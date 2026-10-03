"""halo_harness.tools.agent -- the Agent/Task tool wire definitions (H6
scope B). The real work is agent/subagent.py's `run_agent_call`, called
DIRECTLY by agent/loop.py's `_dispatch_tools` for the live/parallel/
tagged-event path (never through this class's own `run()` there); `run()`
below calls the SAME function as a correctness fallback for any caller
that dispatches it through the generic `tool_registry.dispatch()` instead
(a unit test, or a future caller) -- it still does the real work, the
child's own events just aren't forwarded anywhere in that fallback path.
"""

from __future__ import annotations

from halo_harness.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Launch a sub-agent to handle a complex, multi-step task autonomously. Give it a clear "
    "`description` (3-5 words) and a self-contained `prompt` -- the sub-agent has no memory of this "
    "conversation, so include everything it needs. `subagent_type` picks which agent definition runs "
    "(default \"general-purpose\"); `model` overrides which model it uses; `role` (one of orchestrator, "
    "coder, reviewer, researcher, small) resolves the model from the configured role table instead, "
    "overriding the agent's own default role for just this call; `effort` overrides the reasoning effort "
    "sent for this call. `run_in_background=true` starts it without blocking this turn -- its result is "
    "reported to you as a notice once it finishes. Pass `task_id` (from an earlier <task_result>) to "
    "resume that same sub-agent with more context instead of starting a new one. Sub-agents cannot spawn "
    "further sub-agents. Pass `org` (an organization name from `halo org list`) instead of `subagent_type` "
    "to run that organization's root position on `prompt` as its goal -- it delegates through this same "
    "tool to its own positions, each restricted to the positions it reports to; the final result flows "
    "back the same way a plain sub-agent's does. You may spawn several agents in one call: `count` runs "
    "N identical copies of `prompt`; `batch` takes a list of `{prompt, role?, model?, effort?}` objects, "
    "one per agent. Either way you get ONE combined result, one section per agent, in the order they were "
    "spawned; `/tasks` (or Ctrl+T) shows every one of them, including any still queued behind the "
    "concurrency cap, while they run. `count`/`batch` cannot be combined with `run_in_background`."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "A short (3-5 word) description of the task"},
        "prompt": {"type": "string", "description": "The task for the agent to perform, or an organization's goal"},
        "subagent_type": {"type": "string", "description": "The type of agent to use (default general-purpose)"},
        "model": {"type": "string", "description": "Optional model override for this agent"},
        "role": {"type": "string", "description": "Optional role override (orchestrator|coder|reviewer|"
                                                    "researcher|small) resolving the model from the role table"},
        "effort": {"type": "string", "description": "Optional reasoning-effort override for this call"},
        "run_in_background": {"type": "boolean", "description": "Run without blocking this turn"},
        "task_id": {"type": "string",
                     "description": "Resume a previously returned task_id instead of starting a new sub-agent"},
        "org": {"type": "string", "description": "Run this organization's root position instead of a single "
                                                    "sub-agent (see `halo org list`); `prompt` is its goal"},
        "count": {"type": "integer", "description": "Spawn this many identical copies of `prompt` in "
                                                      "parallel (capped at agents.max_concurrent at a time); "
                                                      "mutually exclusive with `batch`"},
        "batch": {"type": "array", "description": "Spawn one agent per entry, in parallel (capped at "
                                                    "agents.max_concurrent at a time); mutually exclusive "
                                                    "with `count`",
                   "items": {"type": "object", "properties": {
                       "prompt": {"type": "string"}, "role": {"type": "string"}, "model": {"type": "string"},
                       "effort": {"type": "string"}, "description": {"type": "string"},
                   }, "required": ["prompt"]}},
    },
    # `prompt` is required for a single spawn or `count`, but NOT for
    # `batch` (each item supplies its own) -- enforced in `run()` below
    # instead of here, so a strict provider's own schema validation never
    # rejects a valid batch call for lacking a top-level prompt.
    "required": ["description"],
}


class AgentTool(Tool):
    name = "Agent"
    description = DESCRIPTION
    input_schema = INPUT_SCHEMA
    # agent/subagent.py's run_agent_call already spills/caps its own
    # <task_result> text -- a second generic cap here would double-spill.
    result_cap = None

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        t = input.get("subagent_type") or "general-purpose"
        d = input.get("description", "")
        return f"Agent({t}: {d[:60]})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        from halo_harness.agent.subagent import run_agent_call, run_org_call

        runtime = ctx.agent_runtime
        if runtime is None:
            return ToolResult("Sub-agents are not available in this session.", is_error=True)
        input = input if isinstance(input, dict) else {}
        # round 3 (brief C): `batch` supplies its own per-item prompt --
        # only `count`/a plain single spawn need this top-level one.
        if not input.get("prompt") and not input.get("batch"):
            return ToolResult("The prompt parameter is required (unless using batch, where each item "
                               "supplies its own)", is_error=True)
        if not input.get("description"):
            return ToolResult("The description parameter is required", is_error=True)
        # Halo 2.0.2 round 2 (brief B): `org=` picks the org-running path
        # instead of a single sub-agent -- same dispatch split as the
        # LIVE/parallel path in agent/loop.py's own `_run_agent_batch`.
        dispatch = run_org_call if input.get("org") else run_agent_call
        _events, result = dispatch(
            runtime=runtime, tool_id=ctx.tool_use_id or "", tool_input=input, tool_name=self.name,
        )
        return result


class TaskTool(AgentTool):
    """`Task` is Claude Code's own historical alias for the Agent tool
    (brief A/B) -- identical behaviour, registered under the other name so
    either spelling works and `tools: [Task]` / `Agent(type)`-restricted
    frontmatter both resolve correctly."""
    name = "Task"
