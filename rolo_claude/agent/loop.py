"""rolo_claude.agent.loop -- the agent loop, H0 scope: NO TOOLS yet (H1
adds Read/Write/Edit/Bash/... and the repair layer). `Session.turn(text)`
streams exactly one assistant message via providers.stream.stream_completion,
translates its wire events into rolo_claude.events.Event objects, and
appends both sides of the exchange to the session transcript.

What H0 deliberately does NOT do (left for later milestones, noted so a
reader doesn't mistake omission for oversight):
  * No tool_use handling at all -- the request never carries a `tools` list,
    so the model can never actually emit one; a content_block_start for
    type "tool_use" is simply not a code path this loop needs yet.
  * ContextOverflow is surfaced as a plain error event, not compacted (H4).
  * UpstreamError retries are a single, short, fixed-delay retry for a
    `retryable` error -- not the full 1-2-4-8-16s/Retry-After backoff
    ladder from plan D4 (H4 owns that).
  * No permission engine, hooks, or plan mode (H1/H3).
"""

from __future__ import annotations

import os
import time
from typing import Iterator, Optional

from rolo_claude import events
from rolo_claude.agent.session_store import SessionStore
from rolo_claude.model import CostMeter, ModelProfile, ModelRef
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import (
    CompletionRequest,
    ContextOverflow,
    ProviderCreds,
    ProviderNotConfigured,
    UpstreamError,
    stream_completion,
)

_RETRY_DELAY_S = 2.0


class Session:
    """One conversation against one model. `system_prompt` is computed ONCE
    by the caller (agent.assemble.SessionContext) and never recomputed here
    -- every turn reuses the identical string, satisfying the "byte-stable
    per session" requirement."""

    def __init__(
        self,
        *,
        cwd,
        model_ref: ModelRef,
        profile: ModelProfile,
        creds: Optional[ProviderCreds],
        state_dir,
        system_prompt: str,
        small_model_ref: Optional[ModelRef] = None,  # accepted, unused in H0 (no compaction/subagents yet)
        session_store: Optional[SessionStore] = None,
        max_turns: int = 50,
        openrouter_base_url: Optional[str] = None,
        extra_headers: Optional[dict] = None,
    ):
        self.cwd = cwd
        self.model_ref = model_ref
        self.small_model_ref = small_model_ref
        self.profile = profile
        self.creds = creds
        self.state_dir = state_dir
        self.system_prompt = system_prompt
        self.messages: list = []
        self.turn_count = 0
        self.max_turns = max_turns
        self.cost_meter = CostMeter()
        self.session_store = session_store or SessionStore(cwd)
        self.openrouter_base_url = openrouter_base_url
        self.extra_headers = extra_headers or {}
        self._pending_first_blocks: list = []  # e.g. the CLAUDE.md chain, prepended to turn 1 only

        self.session_store.append_meta({
            "model": model_ref.raw,
            "cwd": str(cwd),
            "system_prompt_bytes": len(system_prompt.encode("utf-8")),
        })

    def set_initial_prefix_blocks(self, blocks: list) -> None:
        """Content blocks (e.g. the rendered CLAUDE.md chain, see
        agent.assemble.build_initial_user_message) to prepend to the FIRST
        turn's user message only."""
        self._pending_first_blocks = list(blocks)

    def _build_request(self, oai_body_messages: list) -> CompletionRequest:
        route = Route(provider=self.model_ref.provider, upstream_model=self.model_ref.model,
                       dialect=self.model_ref.dialect)
        profile_dict = {"context_tokens": self.profile.context_tokens, "max_output_tokens": self.profile.max_output_tokens}
        body = {
            "model": self.model_ref.raw,
            "max_tokens": self.profile.max_output_tokens,
            "system": self.system_prompt,
            "messages": oai_body_messages,
        }
        return CompletionRequest(
            body=body, route=route, profile=profile_dict, creds=self.creds, state_dir=self.state_dir,
            extra_headers=self.extra_headers, model_label=self.model_ref.raw,
            openrouter_base_url=self.openrouter_base_url,
            ping_interval=float(os.environ.get("BRIDGE_PING_INTERVAL", "15")),
        )

    def turn(self, text: str, images: Optional[list] = None) -> Iterator[events.Event]:
        """Run exactly one turn. Always ends by yielding a `turn_done`
        event (the loop's caller -- print mode today, the TUI later --
        should treat that as "stop reading events for this turn")."""
        self.turn_count += 1
        turn_no = self.turn_count

        if self.turn_count > self.max_turns:
            yield events.error("max turns exceeded", turn=turn_no, err_type="max_turns")
            yield events.turn_done(turn=turn_no, reason="max_turns")
            return

        blocks = list(self._pending_first_blocks)
        self._pending_first_blocks = []
        blocks.append({"type": "text", "text": text})
        for img in (images or []):
            blocks.append(img)
        user_message = {"role": "user", "content": blocks}
        self.messages.append(user_message)
        self.session_store.append_message(user_message)

        yield events.user_message(text, turn=turn_no, images=images)
        yield events.status(
            phase="thinking", model=self.model_ref.raw, turn=turn_no,
            context_limit=self.profile.context_tokens,
            cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None,
        )

        attempts = 0
        while True:
            attempts += 1
            try:
                result = self._stream_one(turn_no)
                break
            except _RetryableFailure as rf:
                if attempts <= 1 and rf.retry_after_s is not None:
                    time.sleep(rf.retry_after_s)
                    continue
                yield events.error(rf.message, turn=turn_no, err_type=rf.err_type, retryable=rf.retryable)
                yield events.turn_done(turn=turn_no, reason="error")
                return
            except _TerminalFailure as tf:
                yield events.error(tf.message, turn=turn_no, err_type=tf.err_type)
                yield events.turn_done(turn=turn_no, reason="error")
                return

        for ev in result.wire_events:
            yield ev

        assistant_message = {"role": "assistant", "content": result.assistant_blocks}
        self.messages.append(assistant_message)
        self.session_store.append_message(assistant_message)

        cost = self.cost_meter.add_usage(self.model_ref.provider, result.usage)
        self.session_store.append_usage(result.usage, cost)
        context_pct = None
        input_tokens = result.usage.get("input_tokens") if isinstance(result.usage, dict) else None
        if isinstance(input_tokens, int) and self.profile.context_tokens:
            context_pct = round(100.0 * input_tokens / self.profile.context_tokens, 1)

        yield events.message_end(
            turn=turn_no, stop_reason=result.stop_reason, usage=result.usage,
            cost_usd=cost, context_pct=context_pct,
        )
        yield events.status(
            phase="idle", model=self.model_ref.raw, turn=turn_no,
            context_tokens=input_tokens, context_limit=self.profile.context_tokens,
            cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None,
        )
        reason = "max_turns" if result.stop_reason == "max_tokens" else "end_turn"
        yield events.turn_done(turn=turn_no, reason=reason)

    def _stream_one(self, turn_no: int) -> "_TurnResult":
        """Run stream_completion once to completion, translating its wire
        events into rolo_claude.events.Event objects and accumulating the
        assistant message's content blocks. Raises _RetryableFailure /
        _TerminalFailure instead of emitting error events itself, so
        `turn()` owns the single retry decision."""
        req = self._build_request(self.messages)
        gen = stream_completion(req)
        try:
            first = next(gen)
        except ContextOverflow as e:
            raise _TerminalFailure(
                f"context window overflow (limit={e.limit} tokens, prompt~={e.prompt_tokens}) -- "
                f"compaction isn't implemented yet in this build",
                err_type="context_overflow",
            ) from e
        except ProviderNotConfigured as e:
            raise _TerminalFailure(str(e), err_type="not_configured") from e
        except UpstreamError as e:
            if e.retryable:
                delay = _retry_delay_seconds(e.retry_after)
                raise _RetryableFailure(e.message, err_type=e.err_type, retryable=True, retry_after_s=delay) from e
            raise _TerminalFailure(e.message, err_type=e.err_type) from e

        wire_events: list = []
        assistant_blocks: list = []
        block_kind_by_index: dict = {}
        stop_reason = None
        usage: dict = {}

        def handle(ev: dict) -> None:
            nonlocal stop_reason, usage
            kind = ev.get("type")
            if kind == "message_start":
                wire_events.append(events.message_start(turn=turn_no, model=self.model_ref.raw))
            elif kind == "content_block_start":
                block = ev.get("content_block") or {}
                idx = ev.get("index", len(assistant_blocks))
                btype = block.get("type")
                while len(assistant_blocks) <= idx:
                    assistant_blocks.append(None)
                if btype == "text":
                    assistant_blocks[idx] = {"type": "text", "text": ""}
                    block_kind_by_index[idx] = "text"
                elif btype == "thinking":
                    assistant_blocks[idx] = {"type": "thinking", "text": ""}
                    block_kind_by_index[idx] = "thinking"
                elif btype == "tool_use":
                    # Not reachable in H0 (no `tools` are ever sent), kept
                    # so a future dialect quirk doesn't crash this loop --
                    # stored verbatim, never executed.
                    assistant_blocks[idx] = dict(block)
                    block_kind_by_index[idx] = "tool_use"
            elif kind == "content_block_delta":
                idx = ev.get("index", 0)
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text = delta.get("text", "")
                    if idx < len(assistant_blocks) and assistant_blocks[idx] is not None:
                        assistant_blocks[idx]["text"] += text
                    wire_events.append(events.text_delta(text, index=idx, turn=turn_no))
                elif dtype == "thinking_delta":
                    text = delta.get("text", "")
                    if idx < len(assistant_blocks) and assistant_blocks[idx] is not None:
                        assistant_blocks[idx]["text"] += text
                    wire_events.append(events.thinking_delta(text, index=idx, turn=turn_no))
                elif dtype == "input_json_delta" and idx < len(assistant_blocks) and assistant_blocks[idx] is not None:
                    assistant_blocks[idx]["input"] = delta.get("partial_json", "")
            elif kind == "message_delta":
                delta = ev.get("delta") or {}
                if delta.get("stop_reason") is not None:
                    stop_reason = delta.get("stop_reason")
                if isinstance(ev.get("usage"), dict):
                    usage.update(ev["usage"])
            elif kind == "error":
                err = ev.get("error") or {}
                wire_events.append(events.error(err.get("message", "unknown upstream error"),
                                                 turn=turn_no, err_type=err.get("type", "error")))
            # "ping" and "message_stop" and "content_block_stop": nothing to translate.

        handle(first)
        for ev in gen:
            handle(ev)

        assistant_blocks = [b for b in assistant_blocks if b is not None]
        return _TurnResult(
            wire_events=wire_events, assistant_blocks=assistant_blocks,
            stop_reason=stop_reason, usage=usage,
        )


class _TurnResult:
    def __init__(self, *, wire_events, assistant_blocks, stop_reason, usage):
        self.wire_events = wire_events
        self.assistant_blocks = assistant_blocks
        self.stop_reason = stop_reason
        self.usage = usage


class _RetryableFailure(Exception):
    def __init__(self, message, *, err_type, retryable, retry_after_s):
        self.message = message
        self.err_type = err_type
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        super().__init__(message)


class _TerminalFailure(Exception):
    def __init__(self, message, *, err_type):
        self.message = message
        self.err_type = err_type
        super().__init__(message)


def _retry_delay_seconds(retry_after) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return _RETRY_DELAY_S
