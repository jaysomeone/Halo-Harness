"""halo_harness.tools.send_user_message -- the SendUserMessage tool (W4a
`--brief`): claude's own help text for that flag is exactly "Enable
SendUserMessage tool for agent-to-user communication" -- a short status
update the model can send WITHOUT ending its turn (unlike its final answer,
which always does), for a user who asked for terser running commentary
instead of one long reply at the end. Only ever registered into a session's
tool catalog when `--brief` was given (see `headless.build_session`) --
absent otherwise, exactly like any other not-requested tool.
"""

from __future__ import annotations

from halo_harness.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Send a short status update to the user without ending your turn -- use this for brief progress "
    "notes ('reading the config now', 'found the bug, writing the fix') instead of staying silent until "
    "your final answer. Keep it to one or two sentences."
)


class SendUserMessageTool(Tool):
    name = "SendUserMessage"
    description = DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The short update to show the user"},
        },
        "required": ["message"],
    }
    is_read_only = True

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return f"SendUserMessage({input.get('message', '')[:60]})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        message = input.get("message")
        if not message or not isinstance(message, str):
            return ToolResult("The message parameter is required", is_error=True)
        # The message itself IS the result (shown as this tool call's own
        # result/summary in the transcript, print-mode verbose output and
        # stream-json) -- there is nothing further to "do" beyond that.
        return ToolResult(message)
