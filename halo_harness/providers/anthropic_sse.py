"""halo_harness.providers.anthropic_sse -- native-Anthropic-dialect SSE
decoding for the harness's own agent loop (H0 D2/D3). This is the
"AnthropicSSEDecoder" counterpart to providers.oai_stream's
OpenAIStreamToAnthropic: where that state machine TRANSLATES OpenAI-dialect
chunks into Anthropic-shaped events, this one just RECONSTITUTES an
already-Anthropic-shaped SSE stream (Databricks' Claude passthrough gateway
today; a future direct `ant:` Anthropic provider later) into the same plain
event-dict shape, so the agent loop can consume either dialect uniformly.

Not used by `claude-bridge proxy` at all -- the proxy's own
Handler._handle_passthrough_stream keeps relaying passthrough bytes raw and
unparsed, which is deliberately unaffected by this module (see
providers/stream.py's module docstring).
"""

from __future__ import annotations

import json

from halo_harness.providers.routing import Route, select_tools


class AnthropicSSEDecoder:
    """Feed it raw SSE lines (with or without the trailing newline) one at a
    time; it hands back the zero-or-more parsed event dicts each line
    produced. Anthropic's own SSE framing is ``event: <type>`` then
    ``data: <json>`` then a blank line -- the event NAME is redundant with
    ``data.type`` (exactly like this proxy's own `sse_frame` always sets
    them equal on the way out), so only the `data:` line's JSON is actually
    needed to reconstruct the event.
    """

    def __init__(self):
        self.done = False

    def feed_line(self, line) -> list[dict]:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\r\n")
        if not line or line.startswith(":") or line.startswith("event:"):
            return []
        if not line.startswith("data:"):
            return []
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            return []
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return []
        if isinstance(event, dict) and event.get("type") == "message_stop":
            self.done = True
        return [event]

    def on_eof(self) -> list[dict]:
        """Native Anthropic streams always end with an explicit
        `message_stop` event of their own (unlike the openai-chat dialect's
        `[DONE]`-or-just-EOF ambiguity), so there is nothing left to
        synthesize on a clean EOF; a mid-stream death is the caller's own
        concern (it has the connection, this decoder only sees lines)."""
        return []


def build_anthropic_body(body: dict, route: Route, profile: dict | None = None) -> dict:
    """Rewrite an Anthropic Messages request body for a native (non-OpenAI-
    dialect) upstream. This is the minimal, provider-agnostic version of
    routing.build_passthrough_body: model rewrite plus the same 128-tool
    cap/defer_loading handling every dialect needs. It deliberately does NOT
    do routing.build_passthrough_body's two Databricks-gateway-specific
    steps (stripping bridge-signed `thinking` blocks, replacing
    `tool_reference` blocks with text) -- those work around quirks of
    Databricks' specific relay, not general Anthropic Messages API
    semantics, so a future direct `ant:` provider should not inherit them.
    `profile` is accepted for signature symmetry with anthropic_to_openai
    but unused today (a native Claude endpoint enforces its own limits)."""
    result = dict(body)
    result["model"] = route.upstream_model
    if body.get("tools"):
        result["tools"] = select_tools(body["tools"], body.get("messages", []))
    return result
