"""halo_harness.output -- print-mode ("headless") sinks. `PrintModeSink`
(H0/H1) covers `text`/`json`; `StreamJsonSink` (U0 scope F) adds
`stream-json`: an `init` line, `assistant`/`user` message lines, an optional
`stream_event` line per delta (only with `--include-partial-messages`), and
a final `result` line -- Claude Code's own SDK wire shape.

H2 finding 15: a message's fate (the FINAL one of the turn, vs. an
INTERMEDIATE one a tool loop produced along the way) is only knowable in
ARREARS -- once the NEXT `message_start` arrives (this one was
intermediate) or `turn_done` fires (this one was final) -- so nothing here
streams text live character-by-character anymore; every message is
buffered, then either discarded (an intermediate message, non-verbose),
shown dimmed-and-stripped (an intermediate message, `--verbose`), or
always shown as the turn's `result` (the final message, both modes).
"""

from __future__ import annotations

import json as json_module
import sys
import time
import uuid as uuid_module
from typing import Iterator, Optional

from halo_harness import events as ev
from halo_harness.providers.hooks import think_tag_strip

_CUMULATIVE_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)


def _try_structured_output(text: str, json_schema: Optional[str]):
    """`--json-schema`: best-effort re-parse of the final reply text as
    JSON (the schema itself was appended to the system prompt as an
    instruction -- see headless.py -- there is no separate validation
    library dependency here, just "did the model actually return JSON").
    None when `json_schema` wasn't requested, or the text isn't valid
    JSON."""
    if not json_schema or not text:
        return None
    try:
        return json_module.loads(text)
    except ValueError:
        return None


def _merge_usage(total: dict, delta: dict) -> None:
    """Sum numeric usage fields from one model call into the turn's
    running total -- finding 15: a three-call turn must report the WHOLE
    turn's tokens, not just the last call's."""
    if not isinstance(delta, dict):
        return
    for k in _CUMULATIVE_USAGE_KEYS:
        v = delta.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


def build_result_object(
    *,
    session_id: str,
    model: str,
    num_turns: int,
    stop_reason: Optional[str],
    usage: dict,
    total_cost_usd: Optional[float],
    result_text: str,
    is_error: bool = False,
    subtype: str = "success",
    permission_denials: Optional[list] = None,
    structured_output=None,
    background_notices: Optional[list] = None,
    duration_ms: Optional[int] = None,
    uuid: Optional[str] = None,
    prompt_suggestion: Optional[str] = None,
) -> dict:
    """The `-p --output-format json` result object -- a subset of plan
    D-TUI's full shape. H2 scope D: `permission_denials` is a list of
    `{"tool_name","tool_input","reason"}` -- one entry per `ask`-turned-
    deny outcome print mode hit this turn (D6: "an `ask` outcome -> deny
    with an error tool_result naming the suggested rule" -- this is the
    SAME information surfaced structurally for a `--output-format json`
    caller, empty when nothing was denied for that reason). `structured_output`
    (U0 scope F, `--json-schema`) is the final text re-parsed as JSON, or
    None when `--json-schema` wasn't used or the model's reply wasn't valid
    JSON.

    W4a: `duration_ms` (wall-clock from the sink's own construction to this
    call -- the whole run, not just the last model call) and `uuid` (a
    fresh id for THIS result message, Claude Code's own stream-json/json
    result-line convention) are real now -- both sinks' `finish()` supply
    them. `prompt_suggestion` (`--prompt-suggestions`) is the predicted next
    user message, or None when the flag wasn't given."""
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result_text,
        "session_id": session_id,
        "num_turns": num_turns,
        "stop_reason": stop_reason,
        "usage": usage or {},
        "total_cost_usd": total_cost_usd,
        "structured_output": structured_output,
        "model": model,
        "permission_denials": permission_denials or [],
        # H9 whole-tree review finding 9: a background job's completion
        # notice (see PrintModeSink.add_background_notice /
        # StreamJsonSink.add_background_notice) folded into THIS SAME
        # result object -- never a fresh one of its own -- so a `-p
        # --output-format json` run with a still-running background job at
        # the end of its turn prints exactly ONE JSON object, always.
        "background_notices": background_notices or [],
        "duration_ms": duration_ms,
        "uuid": uuid,
        "prompt_suggestion": prompt_suggestion,
    }


class PrintModeSink:
    """Drains a Session.turn(...) event iterator and either prints plain
    text (the FINAL assistant message only, non-verbose; every message
    plus dimmed reasoning, verbose) or accumulates everything and prints
    one JSON result object at the end (cumulative usage/cost across every
    model call the turn made). Returns the process exit code (0 success,
    1 on an error event)."""

    def __init__(self, *, output_format: str = "text", session_id: str = "", model: str = "", stream=None,
                 verbose: bool = False, permission_denials: Optional[list] = None, json_schema: Optional[str] = None,
                 max_budget_usd: Optional[float] = None):
        if output_format not in ("text", "json"):
            raise ValueError(f"unsupported output format: {output_format!r} (H0 supports text|json only)")
        self.json_schema = json_schema
        self.max_budget_usd = max_budget_usd
        self.output_format = output_format
        self.session_id = session_id
        self.model = model
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self._budget_exceeded = False
        # A reference to the CALLER's (agent/loop.py Session's own) list --
        # mutated in place as the turn runs, so it's complete by the time
        # `finish()` reads it after `consume()` has drained the generator.
        self._permission_denials = permission_denials if permission_denials is not None else []
        self._pending_text_parts: list = []
        self._pending_thinking_parts: list = []
        self._final_text = ""  # the LAST message's text, decided once turn_done fires
        # H9 whole-tree review finding 9: background-job completion notices
        # queued here (add_background_notice), NEVER fed through a fresh
        # `consume()`/`finish()` cycle of their own -- see this class's own
        # `add_background_notice` docstring for exactly what that used to
        # break.
        self._background_notices: list = []
        self._had_error = False
        self._error_message: Optional[str] = None
        self._stop_reason: Optional[str] = None
        self._usage: dict = {}
        self._total_cost_usd: Optional[float] = None
        self._num_turns = 0
        self._saw_any_message = False
        # W4a: `duration_ms`/`uuid` on the eventual result object -- see
        # `build_result_object`'s own docstring.
        self._start_monotonic = time.monotonic()
        self._prompt_suggestion: Optional[str] = None
        # parity gap (W6a): `--brief`'s whole point is RUNNING commentary
        # before the final answer -- SendUserMessage's own text otherwise
        # only ever showed up as this tool call's result/summary (TUI
        # transcript, --verbose, stream-json; see that tool's own
        # docstring), invisible in the single most common case (`-p`,
        # plain text, non-verbose). Tracks `tool_use_ready` ids for THIS
        # one tool name so the matching `tool_result` is recognized and
        # printed regardless of `--verbose`.
        self._pending_send_user_message_ids: "set" = set()

    def add_prompt_suggestion(self, text: str) -> None:
        """W4a `--prompt-suggestions`: folded into the one result object,
        same reasoning as `add_background_notice`."""
        self._prompt_suggestion = text

    def consume(self, event_iter: Iterator[ev.Event], *, finish: bool = True) -> Optional[int]:
        """Safe to call again on the SAME sink for a later turn
        (`--input-format stream-json`, reusing one sink for the whole
        run): `_had_error`/`_error_message` reset here so a prior turn's
        failure never leaks into a later, otherwise-successful turn's
        result.

        H9 whole-tree review finding 9: `finish=False` drains `event_iter`
        into this sink's own pending state WITHOUT printing anything or
        returning an exit code (returns None) -- used by headless.py's
        single-turn `-p` path so it can drain any still-running background
        job (folding each completion notice into the SAME pending result
        via `add_background_notice`) BEFORE the one and only `finish()`
        call for the whole process. Calling `finish()` once per `consume()`
        (the old, still-default behaviour) used to mean a background
        notice drained via a SECOND `consume()` call printed a WHOLE EXTRA
        JSON result object (`json.loads(stdout)` on the real caller's side
        raised "Extra data") and overwrote a failed real turn's exit code
        with the notice's own always-successful one."""
        self._had_error = False
        self._error_message = None
        self._budget_exceeded = False
        for event in event_iter:
            self._handle(event)
            if self._budget_exceeded:
                event_iter.close()
                break
        return self.finish() if finish else None

    def add_background_notice(self, text: str) -> None:
        """H9 whole-tree review finding 9: a background job's completion
        notice becomes part of THIS sink's one eventual result object
        instead of triggering a brand new `consume()`/`finish()` cycle.
        Kept OUT of `_final_text`/`structured_output` -- a `--json-schema`
        caller re-parses `_final_text` as JSON, and a stray notice string
        appended to it would break that re-parse -- surfaced as its own
        `background_notices` list in the JSON result object instead, and
        appended as extra plain text after the real answer for `text`
        format (the only format with no separate-field concept to put it
        in)."""
        self._background_notices.append(text)

    def _flush_pending_as_intermediate(self) -> None:
        """A message just ended WITHOUT being the turn's last one (another
        `message_start` followed it) -- shown only under `--verbose`
        (dimmed reasoning, then plain stripped text), discarded otherwise."""
        if not self.verbose:
            self._pending_text_parts = []
            self._pending_thinking_parts = []
            return
        thinking = "".join(self._pending_thinking_parts).strip()
        if thinking and self.output_format == "text":
            self.stream.write("\x1b[2m" + thinking + "\x1b[0m\n")
        text = think_tag_strip("".join(self._pending_text_parts))
        if text and self.output_format == "text":
            self.stream.write(text)
            if not text.endswith("\n"):
                self.stream.write("\n")
        self._pending_text_parts = []
        self._pending_thinking_parts = []

    def _handle(self, event: ev.Event) -> None:
        # H6 scope E: a sub-agent's own text/thinking/tool events (tagged
        # with `agent_id`) never feed the MAIN session's text/json result
        # -- text/json output has no nested-message shape to put them in
        # (that's stream-json's job, output.StreamJsonSink); `--verbose`
        # in text mode still gets a one-line breadcrumb so the work isn't
        # invisible, without corrupting `_final_text`.
        if event.kind in ("subagent_start", "subagent_end"):
            if self.verbose and self.output_format == "text":
                label = "started" if event.kind == "subagent_start" else "finished"
                self.stream.write(f"\x1b[2m[sub-agent {label}: {event.data.get('name', '?')}]\x1b[0m\n")
            return
        if event.agent_id is not None:
            return
        if event.kind == "message_start":
            if self._saw_any_message:
                self._flush_pending_as_intermediate()
            self._saw_any_message = True
            self._pending_text_parts = []
            self._pending_thinking_parts = []
        elif event.kind == "thinking_delta":
            self._pending_thinking_parts.append(event.data.get("text", ""))
        elif event.kind == "text_delta":
            self._pending_text_parts.append(event.data.get("text", ""))
        elif event.kind == "tool_use_ready":
            # H2 scope D acceptance: "three tool calls visible with
            # --verbose" -- a real Anthropic client shows a tool-use card
            # for every call; text-mode --verbose gets the plain-text
            # equivalent (dimmed, like thinking), never shown non-verbose
            # (the whole point of --verbose here is showing the WORK, not
            # just the final answer).
            if event.data.get("name") == "SendUserMessage":
                self._pending_send_user_message_ids.add(event.data.get("id"))
            if self.verbose and self.output_format == "text":
                name = event.data.get("name", "?")
                tool_input = event.data.get("input") or {}
                summary = json_module.dumps(tool_input, default=str)
                if len(summary) > 200:
                    summary = summary[:200] + "...}"
                repaired = " (repaired)" if event.data.get("repaired") else ""
                self.stream.write(f"\x1b[2m[tool] {name}{repaired} {summary}\x1b[0m\n")
        elif event.kind == "tool_result":
            # parity gap (W6a): printed in PLAIN text, regardless of
            # --verbose -- see this sink's own `__init__` comment on
            # `_pending_send_user_message_ids`.
            if event.data.get("id") in self._pending_send_user_message_ids:
                self._pending_send_user_message_ids.discard(event.data.get("id"))
                if event.data.get("ok") and self.output_format == "text":
                    text = event.data.get("content") or event.data.get("summary") or ""
                    if text:
                        self.stream.write(text if text.endswith("\n") else text + "\n")
            elif self.verbose and self.output_format == "text":
                ok = "ok" if event.data.get("ok") else "error"
                self.stream.write(f"\x1b[2m[tool result: {ok}]\x1b[0m\n")
        elif event.kind == "steer_restart":
            # GLM-brief.md item 3: "headless --verbose prints one line" --
            # the call was silently aborted and is being resent with the
            # steer appended, so nothing was lost; A7 ("print mode is
            # unchanged, no progress noise") is why the per-second `phase`
            # events get no line here at all, unlike this rare, meaningful one.
            if self.verbose and self.output_format == "text":
                self.stream.write("\x1b[2m[steering: restarting the model call]\x1b[0m\n")
        elif event.kind == "message_end":
            self._stop_reason = event.data.get("stop_reason")
            _merge_usage(self._usage, event.data.get("usage") or {})
            cost = event.data.get("cost_usd")
            if cost is not None:
                self._total_cost_usd = cost  # already the SESSION's cumulative total (agent/loop.py)
                if self.max_budget_usd is not None and cost >= self.max_budget_usd:
                    self._budget_exceeded = True
                    # `turn_done` (the only other place _final_text is set)
                    # never arrives once consume() breaks out below -- capture
                    # whatever text this message already produced right now.
                    self._final_text = think_tag_strip("".join(self._pending_text_parts))
        elif event.kind == "error":
            self._had_error = True
            self._error_message = event.data.get("message")
        elif event.kind == "turn_done":
            self._num_turns = event.turn
            # finding 15: whatever's still pending here IS the final message.
            self._final_text = think_tag_strip("".join(self._pending_text_parts))
            if self.verbose:
                thinking = "".join(self._pending_thinking_parts).strip()
                if thinking and self.output_format == "text":
                    self.stream.write("\x1b[2m" + thinking + "\x1b[0m\n")
            self._pending_text_parts = []
            self._pending_thinking_parts = []

    def finish(self) -> int:
        is_error = self._had_error or self._budget_exceeded
        if self.output_format == "text":
            if self._budget_exceeded:
                if self._final_text:
                    self.stream.write(self._final_text)
                    if not self._final_text.endswith("\n"):
                        self.stream.write("\n")
                    self.stream.flush()
                print(f"\nerror: --max-budget-usd (${self.max_budget_usd}) reached", file=sys.stderr)
            elif self._had_error:
                print(f"\nerror: {self._error_message}", file=sys.stderr)
            elif self._final_text:
                self.stream.write(self._final_text)
                if not self._final_text.endswith("\n"):
                    self.stream.write("\n")
                self.stream.flush()
            for notice in self._background_notices:
                self.stream.write(("\n" if notice.startswith("\n") else "\n\n") + notice)
                if not notice.endswith("\n"):
                    self.stream.write("\n")
            if self._prompt_suggestion:
                self.stream.write(f"\n[next: {self._prompt_suggestion}]\n")
            if self._background_notices or self._prompt_suggestion:
                self.stream.flush()
        else:
            if self._budget_exceeded:
                subtype = "error_max_budget_usd"
            elif self._had_error:
                subtype = "error_during_execution"
            else:
                subtype = "success"
            obj = build_result_object(
                session_id=self.session_id,
                model=self.model,
                num_turns=self._num_turns,
                stop_reason=self._stop_reason,
                usage=self._usage,
                total_cost_usd=self._total_cost_usd,
                result_text=self._final_text,
                is_error=is_error,
                subtype=subtype,
                permission_denials=self._permission_denials,
                structured_output=_try_structured_output(self._final_text, self.json_schema),
                background_notices=self._background_notices,
                duration_ms=int((time.monotonic() - self._start_monotonic) * 1000),
                uuid=str(uuid_module.uuid4()),
                prompt_suggestion=self._prompt_suggestion,
            )
            # write to self.stream (defaults to sys.stdout, same as a bare
            # print() would have -- but this way a caller that passed its
            # OWN stream (e.g. testing.fake_controller.run_demo) gets json
            # output there too, matching the text branch above).
            self.stream.write(json_module.dumps(obj) + "\n")
            self.stream.flush()
        return 1 if is_error else 0


class StreamJsonSink:
    """`--output-format stream-json` (U0 scope F). Emits, in order: one
    `system`/`init` line, then an `assistant`/`user` line per completed
    message/tool-result batch (buffered exactly like `PrintModeSink` --
    finding 15's "only knowable in arrears" applies here too: a message is
    flushed when the NEXT `message_start` or `turn_done` arrives, tool_use
    blocks attached to it from any `tool_use_ready` events seen since its
    own `message_end`), optionally a `stream_event` line per text/thinking
    delta (`--include-partial-messages`), and a final `result` line.
    `max_budget_usd`, when given, stops consuming further events (closing
    the underlying generator) the first time the session's cumulative cost
    (message_end's own `cost_usd`, already the running total) meets or
    exceeds it -- the result comes back `is_error=True`,
    `subtype="error_max_budget_usd"`."""

    def __init__(self, *, session_id: str, cwd: str, model: str, permission_mode: str,
                 tools: Optional[list] = None, mcp_servers: Optional[list] = None,
                 slash_commands: Optional[list] = None, include_partial_messages: bool = False,
                 max_budget_usd: Optional[float] = None, stream=None,
                 permission_denials: Optional[list] = None, json_schema: Optional[str] = None,
                 effort: Optional[str] = None, effort_sent: Optional[str] = None,
                 hook_events_fn=None):
        # Halo 2.0.1 W2a (GLM-brief.md item 1 / HALO-2.0.1-liveness-tips-
        # brief.md Part C): `effort` is whatever was last explicitly
        # requested (None when nothing was); `effort_sent` is the value
        # THIS route actually puts on the wire for it (`Session.effort`,
        # already clamped) -- carried on `init` so a scripted stream-json
        # caller can tell "medium (requested)" from "high (sent)" apart
        # without re-deriving the clamp itself.
        self.effort = effort
        self.effort_sent = effort_sent
        self.session_id = session_id
        self.cwd = str(cwd)
        self.model = model
        self.permission_mode = permission_mode
        self.tools = tools or []
        self.mcp_servers = mcp_servers or []
        self.slash_commands = slash_commands or []
        self.include_partial_messages = include_partial_messages
        self.max_budget_usd = max_budget_usd
        self.json_schema = json_schema
        self.stream = stream or sys.stdout
        self._permission_denials = permission_denials if permission_denials is not None else []

        # H6 scope E: keyed by `agent_id` (None = the main session) so a
        # sub-agent's own text/tool_use/tool_result stream never corrupts
        # the PARENT's pending assistant message -- every event a child
        # session produces arrives here tagged (agent/subagent.py's
        # `_tag()` sets both `Event.agent_id` and `data["parent_tool_use_
        # id"]`), and each agent_id gets its own independent buffer,
        # flushed into its own `assistant`/`user` lines the same shape the
        # main session's own lines use, plus `parent_tool_use_id`.
        self._buffers: dict = {}
        self._final_text = ""
        self._usage: dict = {}
        self._total_cost_usd: Optional[float] = None
        self._num_turns = 0
        self._stop_reason: Optional[str] = None
        self._had_error = False
        self._error_message: Optional[str] = None
        self._budget_exceeded = False
        self._initted = False
        # H9 whole-tree review finding 9: same role as PrintModeSink's own
        # field -- see its docstring.
        self._background_notices: list = []
        # W4a `--include-hook-events`: `Session.drain_hook_events`, only
        # when headless.py's caller actually asked for this -- None means
        # "never check" (the cheap, overwhelmingly common default path).
        self.hook_events_fn = hook_events_fn
        # W4a: `duration_ms`/`uuid`/`--prompt-suggestions` on the final
        # result line -- see `build_result_object`'s own docstring.
        self._start_monotonic = time.monotonic()
        self._prompt_suggestion: Optional[str] = None

    def add_prompt_suggestion(self, text: str) -> None:
        self._prompt_suggestion = text

    def _write(self, obj: dict) -> None:
        self.stream.write(json_module.dumps(obj) + "\n")
        self.stream.flush()

    def emit_init(self) -> None:
        """Idempotent: a caller driving several turns through ONE sink
        instance (`--input-format stream-json`) calls this once up front;
        `consume()` below only emits it itself when it hasn't happened
        yet, so reusing one sink across turns never repeats the `init`
        line (real Claude Code: one per SESSION, not one per turn)."""
        if self._initted:
            return
        self._initted = True
        # 2.0.0 fixpass finding 8: `rolo_claude_version` kept as an alias,
        # same value, alongside the new key -- a script that already reads
        # the OLD field name from stream-json output (never documented as
        # renamed in the CHANGELOG) must keep working with zero changes.
        version = __import__("halo_harness").__version__
        self._write({
            "type": "system", "subtype": "init", "session_id": self.session_id, "cwd": self.cwd,
            "model": self.model, "permissionMode": self.permission_mode, "tools": self.tools,
            "mcp_servers": self.mcp_servers, "slash_commands": self.slash_commands,
            "halo_harness_version": version, "rolo_claude_version": version,
            # Halo 2.0.1 W2a: see __init__'s own docstring for the
            # requested-vs-sent contract these two carry.
            "effort": self.effort, "effort_sent": self.effort_sent,
        })

    def _buf(self, agent_id: Optional[str]) -> dict:
        return self._buffers.setdefault(agent_id, {
            "active": False, "text": [], "thinking": [], "tool_use": [], "tool_results": [],
            "stop_reason": None, "usage": {}, "parent_tool_use_id": None,
        })

    def _flush_pending(self, agent_id: Optional[str]) -> None:
        buf = self._buf(agent_id)
        extra = {"parent_tool_use_id": buf["parent_tool_use_id"]} if agent_id is not None else {}
        if buf["active"]:
            content = []
            thinking = "".join(buf["thinking"])
            if thinking:
                content.append({"type": "thinking", "thinking": thinking})
            text = think_tag_strip("".join(buf["text"]))
            if text:
                content.append({"type": "text", "text": text})
            content.extend(buf["tool_use"])
            self._write({
                "type": "assistant", "session_id": self.session_id,
                "message": {"role": "assistant", "model": self.model, "content": content,
                            "stop_reason": buf["stop_reason"], "usage": buf["usage"]},
                **extra,
            })
            buf["active"] = False
            buf["text"], buf["thinking"], buf["tool_use"] = [], [], []
            buf["stop_reason"], buf["usage"] = None, {}
        if buf["tool_results"]:
            self._write({
                "type": "user", "session_id": self.session_id,
                "message": {"role": "user", "content": list(buf["tool_results"])},
                **extra,
            })
            buf["tool_results"] = []

    def _emit_pending_hook_events(self) -> None:
        if self.hook_events_fn is None:
            return
        for record in self.hook_events_fn():
            self._write({"type": "system", "subtype": "hook_event", "session_id": self.session_id, **record})

    def _handle(self, event: ev.Event) -> None:
        self._emit_pending_hook_events()
        kind = event.kind
        agent_id = event.agent_id
        parent_tool_use_id = event.data.get("parent_tool_use_id") if isinstance(event.data, dict) else None

        if kind == "subagent_start":
            self._write({"type": "subagent_start", "session_id": self.session_id, "agent_id": agent_id,
                          "name": event.data.get("name"), "description": event.data.get("description"),
                          "parent_tool_use_id": event.data.get("parent_tool_use_id")})
            return
        if kind == "subagent_end":
            self._write({"type": "subagent_stop", "session_id": self.session_id, "agent_id": agent_id,
                          "name": event.data.get("name"), "parent_tool_use_id": event.data.get("parent_tool_use_id")})
            return

        buf = self._buf(agent_id)
        if parent_tool_use_id is not None:
            buf["parent_tool_use_id"] = parent_tool_use_id
        if kind == "message_start":
            self._flush_pending(agent_id)
            buf["active"] = True
        elif kind == "text_delta":
            buf["text"].append(event.data.get("text", ""))
            if self.include_partial_messages and agent_id is None:
                self._write({"type": "stream_event", "session_id": self.session_id, "event": {
                    "type": "content_block_delta", "delta": {"type": "text_delta", "text": event.data.get("text", "")}}})
        elif kind == "thinking_delta":
            buf["thinking"].append(event.data.get("text", ""))
            if self.include_partial_messages and agent_id is None:
                self._write({"type": "stream_event", "session_id": self.session_id, "event": {
                    "type": "content_block_delta", "delta": {"type": "thinking_delta", "text": event.data.get("text", "")}}})
        elif kind == "tool_use_ready":
            buf["tool_use"].append({"type": "tool_use", "id": event.data.get("id"),
                                     "name": event.data.get("name"), "input": event.data.get("input") or {}})
        elif kind == "tool_result":
            buf["tool_results"].append({"type": "tool_result", "tool_use_id": event.data.get("id"),
                                         "content": event.data.get("summary", ""),
                                         "is_error": not event.data.get("ok", True)})
        elif kind == "message_end":
            buf["stop_reason"] = event.data.get("stop_reason")
            buf["usage"] = event.data.get("usage") or {}
            if agent_id is None:
                _merge_usage(self._usage, buf["usage"])
                self._stop_reason = buf["stop_reason"]
                cost = event.data.get("cost_usd")
                if cost is not None:
                    self._total_cost_usd = cost
                    if self.max_budget_usd is not None and cost >= self.max_budget_usd:
                        self._budget_exceeded = True
        elif kind == "error":
            if agent_id is None:
                self._had_error = True
                self._error_message = event.data.get("message")
        elif kind == "turn_done":
            if agent_id is None:
                self._num_turns = event.turn
                self._final_text = think_tag_strip("".join(buf["text"]))
            self._flush_pending(agent_id)

    def consume(self, event_iter: Iterator[ev.Event], *, finish: bool = True) -> Optional[int]:
        """Emits `init` only the first time (see `emit_init`), then drains
        `event_iter` for exactly one turn and prints its `result` line --
        safe to call again on the SAME sink for a later turn
        (`--input-format stream-json`): per-turn flags reset here so a
        prior turn's error/budget state never leaks into a later turn's
        otherwise-successful result, while `_usage`/`_num_turns` (session-
        cumulative) intentionally carry over.

        H9 whole-tree review finding 9: `finish=False` -- see
        PrintModeSink.consume's own docstring; the SAME bug applied here
        (an extra, unsolicited `result` line with no matching user turn)."""
        self.emit_init()
        self._had_error = False
        self._error_message = None
        self._budget_exceeded = False
        for event in event_iter:
            self._handle(event)
            if self._budget_exceeded:
                event_iter.close()
                break
        return self.finish() if finish else None

    def add_background_notice(self, text: str) -> None:
        """H9 whole-tree review finding 9: see PrintModeSink's own
        docstring -- folded into the one `result` line's own
        `background_notices` field, never a `result` line of its own."""
        self._background_notices.append(text)

    def finish(self) -> int:
        if self._budget_exceeded:
            subtype, is_error = "error_max_budget_usd", True
        elif self._had_error:
            subtype, is_error = "error_during_execution", True
        else:
            subtype, is_error = "success", False
        if self._prompt_suggestion:
            # claude's own wording: "emits a prompt_suggestion message" --
            # its own line, distinct from the result line below (which ALSO
            # carries it in `prompt_suggestion`, for a plain `json` caller
            # that never sees stream-json's extra message types at all).
            self._write({"type": "prompt_suggestion", "session_id": self.session_id,
                         "suggestion": self._prompt_suggestion})
        result = build_result_object(
            session_id=self.session_id, model=self.model, num_turns=self._num_turns,
            stop_reason=self._stop_reason, usage=self._usage, total_cost_usd=self._total_cost_usd,
            result_text=self._error_message if self._had_error else self._final_text,
            is_error=is_error, subtype=subtype, permission_denials=self._permission_denials,
            structured_output=_try_structured_output(self._final_text, self.json_schema),
            background_notices=self._background_notices,
            duration_ms=int((time.monotonic() - self._start_monotonic) * 1000),
            uuid=str(uuid_module.uuid4()),
            prompt_suggestion=self._prompt_suggestion,
        )
        self._write(result)
        # review finding 31: neither was ever reset at the end of a turn --
        # a LATER turn whose own prompt-suggestion call failed (or simply
        # never ran yet) re-emitted the PREVIOUS turn's suggestion, and
        # `duration_ms` (meant to be per-turn, see its own field docstring)
        # kept accumulating across every turn in this multi-turn
        # stream-json loop instead of measuring just this one.
        self._prompt_suggestion = None
        self._start_monotonic = time.monotonic()
        return 1 if is_error else 0
