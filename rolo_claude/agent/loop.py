"""rolo_claude.agent.loop -- the agent loop (H1 rewrite, scope E-H).

Every model request is DERIVED from the append-only SessionLog
(agent/log.py + agent/derive.py) -- `self.messages` (H0's ad-hoc
in-memory list) is gone. Real tool dispatch (Read only in H1 --
tools/registry.py), the loop breaker (rule 8), serialize-time invariants on
interrupt/error (agent/invariants.py), canonical Anthropic block
accumulation (accumulate `partial_json`, decode once at content_block_stop
-- finding 12), and per-profile reasoning replay via providers/request.py.

Fixes findings 3/4/12 versus H0: `_step` yields each translated Event AS ITS
wire event arrives (never buffers the whole turn before yielding anything),
closes the upstream generator in `finally`, and a wire `error` event drops
the partial assistant message instead of committing it to the log.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterator, Optional

from rolo_claude import events
from rolo_claude.agent.derive import content_hash, derive_request
from rolo_claude.agent.invariants import repair_truncated_text, synthesize_missing_results
from rolo_claude.agent.log import SessionLog
from rolo_claude.model import CostMeter, ModelProfile, ModelRef
from rolo_claude.providers.errors import backoff_delay, is_reasoning_replay_bug
from rolo_claude.providers.profiles import ProviderProfile, resolve_profile
from rolo_claude.providers.request import ToolCatalogTooLarge, build_request_body
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import (
    CompletionRequest, ContextOverflow, ProviderCreds, ProviderNotConfigured,
    UpstreamError, stream_completion,
)
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.registry import ToolRegistry

_MAX_RETRY_WAIT_S = 60.0  # finding 12: capped, never the uncapped `time.sleep(Retry-After)` H0 had
_LOOP_BREAKER_REMIND_AT = 3
_LOOP_BREAKER_DENY_AT = 5
_LOOP_BREAKER_END_AT = 8


def _canonical_args(args) -> str:
    """Canonical JSON (sorted keys) for loop-breaker hashing -- property
    order must not matter (rule 8: "same tool name + canonically-equal
    arguments, property order ignored")."""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _rough_estimate(system_text: str, messages: list) -> int:
    """len(json)/4, matching providers.config.estimate_tokens' own rule of
    thumb -- used only for the max_tokens budget headroom calculation."""
    try:
        blob = json.dumps({"system": system_text, "messages": messages}, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(messages)
    return max(1, len(blob) // 4)


class _StepResult:
    def __init__(self, *, assistant_blocks, stop_reason, usage, reasoning):
        self.assistant_blocks = assistant_blocks
        self.stop_reason = stop_reason
        self.usage = usage
        self.reasoning = reasoning


class Session:
    """One conversation against one model. `session_context.system_prompt`
    is computed ONCE by the caller and logged as the session's single
    `system` node -- every derived request reuses it byte-for-byte."""

    def __init__(
        self, *, cwd, model_ref: ModelRef, model_profile: ModelProfile,
        creds: Optional[ProviderCreds], state_dir, model_label: str, session_context,
        small_model_ref: Optional[ModelRef] = None, session_log: Optional[SessionLog] = None,
        max_turns: int = 50, openrouter_base_url: Optional[str] = None,
        extra_headers: Optional[dict] = None, effort: Optional[str] = None,
    ):
        self.cwd = cwd
        self.model_ref = model_ref
        self.small_model_ref = small_model_ref
        self.model_profile = model_profile
        self.creds = creds
        self.state_dir = state_dir
        self.model_label = model_label
        self.effort = effort
        self.turn_count = 0
        self.max_turns = max_turns
        self.cost_meter = CostMeter()
        self.tool_registry: ToolRegistry = session_context.tool_registry
        self.route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
        self.provider_profile: ProviderProfile = resolve_profile(self.route)
        self.openrouter_base_url = openrouter_base_url
        self.extra_headers = extra_headers or {}
        self._loop_breaker: dict = {}  # canonical (name,args) -> consecutive count, reset every turn

        self.log = session_log or SessionLog(cwd)
        self.log.append_meta(
            model=model_ref.raw, cwd=str(cwd),
            system_prompt_bytes=len(session_context.system_prompt.encode("utf-8")),
            tools=self.tool_registry.definitions(),
        )
        self.log.append_system(session_context.system_prompt)

        claude_md = session_context.claude_md_text()
        if claude_md:
            self.log.append_snapshot([{"type": "text", "text": claude_md}], kind="claude_md")
        memory_text = session_context.memory_snapshot_text()
        if memory_text:
            self.log.append_snapshot([{"type": "text", "text": memory_text}], kind="memory_index")
        env_text = session_context.environment_snapshot_text(model_label)
        self.log.append_snapshot([{"type": "text", "text": env_text}], kind="environment")

    # ---- request construction ------------------------------------------

    def _derive_and_build(self):
        tools = self.tool_registry.definitions()
        system_text, messages, tools = derive_request(self.log, tools=tools)
        body = build_request_body(
            system_text=system_text, messages=messages, tools=tools, route=self.route,
            profile=self.provider_profile, effort=self.effort,
            context_tokens=self.model_profile.context_tokens,
            prompt_estimate=_rough_estimate(system_text, messages),
        )
        return system_text, messages, tools, body

    def _build_request(self, body: dict) -> CompletionRequest:
        return CompletionRequest(
            body={"messages": []}, route=self.route,
            profile={"context_tokens": self.model_profile.context_tokens,
                     "max_output_tokens": self.model_profile.max_output_tokens},
            creds=self.creds, state_dir=self.state_dir, extra_headers=self.extra_headers,
            model_label=self.model_ref.raw, openrouter_base_url=self.openrouter_base_url,
            harness_mode=True, prebuilt_oai_body=body,
            ping_interval=float(os.environ.get("BRIDGE_PING_INTERVAL", "15")),
        )

    # ---- one model call --------------------------------------------------

    def _step(self, turn_no: int):
        """One model call, streamed: yields translated Events AS EACH wire
        event arrives (finding 3 -- never buffers the whole reply before
        yielding anything) and `return`s a `_StepResult` (retrieved by the
        caller via `result = yield from self._step(...)`), or `None` if a
        terminal failure already emitted its own `error` event (the caller
        must then stop the turn, never commit a partial assistant message).
        A retryable upstream failure (429/5xx/connect) gets ONE retry after
        a capped, ladder-backed wait (finding 12); a DeepSeek
        reasoning-replay 400 is a request-builder BUG, surfaced immediately,
        never retried."""
        _, _, _, body = self._derive_and_build()
        req = self._build_request(body)

        attempts = 0
        while True:
            attempts += 1
            gen = stream_completion(req)
            assistant_blocks: list = []
            partial_json: dict = {}
            stop_reason = None
            usage: dict = {}
            harness_meta: dict = {}
            wire_error: Optional[dict] = None
            phase1_failure: Optional[Exception] = None
            try:
                for ev in gen:
                    kind = ev.get("type")
                    if kind == "message_start":
                        yield events.message_start(turn=turn_no, model=self.model_ref.raw)
                    elif kind == "content_block_start":
                        block = ev.get("content_block") or {}
                        idx = ev.get("index", len(assistant_blocks))
                        while len(assistant_blocks) <= idx:
                            assistant_blocks.append(None)
                        btype = block.get("type")
                        if btype == "text":
                            assistant_blocks[idx] = {"type": "text", "text": ""}
                        elif btype == "thinking":
                            assistant_blocks[idx] = {"type": "thinking", "text": "", "signature": block.get("signature", "")}
                        elif btype == "tool_use":
                            assistant_blocks[idx] = {"type": "tool_use", "id": block.get("id"),
                                                      "name": block.get("name"), "input": {}}
                            partial_json[idx] = ""
                            yield events.Event("tool_use_start", {"id": block.get("id"), "name": block.get("name")}, turn=turn_no)
                    elif kind == "content_block_delta":
                        idx = ev.get("index", 0)
                        delta = ev.get("delta") or {}
                        dtype = delta.get("type")
                        if idx >= len(assistant_blocks) or assistant_blocks[idx] is None:
                            continue
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            text = text if isinstance(text, str) else str(text)  # finding 3: never crash on a non-str delta
                            assistant_blocks[idx]["text"] += text
                            yield events.text_delta(text, index=idx, turn=turn_no)
                        elif dtype == "thinking_delta":
                            text = delta.get("text", "")
                            text = text if isinstance(text, str) else str(text)
                            assistant_blocks[idx]["text"] += text
                            yield events.thinking_delta(text, index=idx, turn=turn_no)
                        elif dtype == "signature_delta":
                            assistant_blocks[idx]["signature"] = assistant_blocks[idx].get("signature", "") + str(delta.get("signature", ""))
                        elif dtype == "input_json_delta":
                            # finding 12: ACCUMULATE partial_json across deltas; never overwrite
                            partial_json[idx] = partial_json.get(idx, "") + delta.get("partial_json", "")
                    elif kind == "content_block_stop":
                        idx = ev.get("index", 0)
                        if idx in partial_json and idx < len(assistant_blocks) and assistant_blocks[idx] is not None:
                            raw = partial_json.pop(idx)
                            try:
                                assistant_blocks[idx]["input"] = json.loads(raw) if raw.strip() else {}
                            except json.JSONDecodeError:
                                assistant_blocks[idx]["input"] = {}
                                assistant_blocks[idx]["_raw_unparsed"] = raw
                    elif kind == "message_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("stop_reason") is not None:
                            stop_reason = delta.get("stop_reason")
                        if isinstance(ev.get("usage"), dict):
                            usage.update(ev["usage"])
                        if isinstance(ev.get("harness_meta"), dict):
                            harness_meta = ev["harness_meta"]
                    elif kind == "error":
                        wire_error = ev.get("error") or {}
                        break
                    # "ping"/"message_stop": nothing to translate
            except (ContextOverflow, ProviderNotConfigured, UpstreamError) as e:
                # Phase 1 (translate + connect, before anything is yielded)
                # raises these as plain Python exceptions, not wire "error"
                # events -- caught here so they get the SAME retry/terminal
                # handling as a phase-2 wire error below.
                phase1_failure = e
            finally:
                gen.close()  # finding 3: always close the upstream generator

            if phase1_failure is not None:
                if isinstance(phase1_failure, ContextOverflow):
                    e = phase1_failure
                    yield events.error(
                        f"context window overflow (limit={e.limit} tokens, prompt~={e.prompt_tokens}) -- "
                        f"compaction isn't implemented yet in this build",
                        turn=turn_no, err_type="context_overflow",
                    )
                    return None
                if isinstance(phase1_failure, ProviderNotConfigured):
                    yield events.error(str(phase1_failure), turn=turn_no, err_type="not_configured")
                    return None
                e = phase1_failure  # UpstreamError
                if e.retryable and attempts <= 1:
                    delay = min(backoff_delay(attempts - 1, e.retry_after), _MAX_RETRY_WAIT_S)
                    time.sleep(delay)
                    continue
                yield events.error(e.message, turn=turn_no, err_type=e.err_type, retryable=e.retryable)
                return None

            if wire_error is None:
                break  # a clean stream -- proceed to build the result below

            message = wire_error.get("message", "unknown upstream error")
            if is_reasoning_replay_bug(message):
                # This ALWAYS means OUR OWN reasoning-replay logic has a bug
                # (providers/request.py) -- surface it loudly, never retry.
                yield events.error(f"reasoning-replay bug (never retried): {message}", turn=turn_no, err_type="reasoning_replay_bug")
                return None
            retryable = wire_error.get("type") in ("overloaded_error", "rate_limit_error", "api_error")
            if retryable and attempts <= 1:
                delay = min(backoff_delay(attempts - 1, wire_error.get("retry_after")), _MAX_RETRY_WAIT_S)
                time.sleep(delay)
                continue
            # finding 12: a wire error drops the partial reply -- never committed to the log
            yield events.error(message, turn=turn_no, err_type=wire_error.get("type", "error"))
            return None

        reasoning = None
        if harness_meta.get("reasoning_text") or harness_meta.get("reasoning_details"):
            reasoning = {
                "format": "details" if harness_meta.get("reasoning_details") else "text",
                "value": harness_meta.get("reasoning_details") or repair_truncated_text(harness_meta.get("reasoning_text") or ""),
            }
        cleaned = []
        for b in assistant_blocks:
            if b is None:
                continue
            if b.get("type") in ("text", "thinking") and isinstance(b.get("text"), str):
                b["text"] = repair_truncated_text(b["text"])
            cleaned.append(b)
        return _StepResult(assistant_blocks=cleaned, stop_reason=stop_reason, usage=usage, reasoning=reasoning)

    # ---- the turn: model call(s) + tool dispatch ------------------------

    def turn(self, text: str, images: Optional[list] = None) -> Iterator[events.Event]:
        """Run one turn to completion (which may involve several model
        calls interleaved with tool dispatch). Always ends by yielding a
        `turn_done` event."""
        self.turn_count += 1
        turn_no = self.turn_count
        self._loop_breaker = {}

        if self.turn_count > self.max_turns:
            yield events.error("max turns exceeded", turn=turn_no, err_type="max_turns")
            yield events.turn_done(turn=turn_no, reason="max_turns")
            return

        blocks = [{"type": "text", "text": text}]
        for img in (images or []):
            blocks.append(img)
        self.log.append_user(blocks)
        yield events.user_message(text, turn=turn_no, images=images)
        yield events.status(
            phase="thinking", model=self.model_ref.raw, turn=turn_no,
            context_limit=self.model_profile.context_tokens,
            cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None,
        )

        try:
            yield from self._turn_body(turn_no)
        except GeneratorExit:
            # rule 2/scope F: an interrupted turn must never leave a
            # tool_use without a matching tool_result in the log.
            synthesize_missing_results(self.log, reason="Tool call interrupted by user")
            raise
        except BaseException:
            synthesize_missing_results(self.log, reason="ABORTED_BEFORE_DISPATCH")
            raise

    def _turn_body(self, turn_no: int) -> Iterator[events.Event]:
        while True:
            result = yield from self._step(turn_no)
            if result is None:
                yield events.turn_done(turn=turn_no, reason="error")
                return

            pre_system, pre_messages, pre_tools = derive_request(self.log, tools=self.tool_registry.definitions())
            req_hash = content_hash(pre_system, pre_messages, pre_tools)
            self.log.append_assistant(
                content=result.assistant_blocks, reasoning=result.reasoning,
                stop_reason=result.stop_reason, request_hash=req_hash,
            )

            cost = self.cost_meter.add_usage(self.model_ref.provider, result.usage)
            self.log.append_usage(result.usage, cost)
            input_tokens = result.usage.get("input_tokens") if isinstance(result.usage, dict) else None
            context_pct = None
            if isinstance(input_tokens, int) and self.model_profile.context_tokens:
                context_pct = round(100.0 * input_tokens / self.model_profile.context_tokens, 1)
            yield events.message_end(turn=turn_no, stop_reason=result.stop_reason, usage=result.usage,
                                      cost_usd=cost, context_pct=context_pct)

            tool_use_blocks = [b for b in result.assistant_blocks if b.get("type") == "tool_use"]
            if result.stop_reason != "tool_use" or not tool_use_blocks:
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no, context_tokens=input_tokens,
                                     context_limit=self.model_profile.context_tokens,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                reason = "max_turns" if result.stop_reason == "max_tokens" else "end_turn"
                yield events.turn_done(turn=turn_no, reason=reason)
                return

            ended = yield from self._dispatch_tools(turn_no, tool_use_blocks)
            if ended:
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                yield events.turn_done(turn=turn_no, reason="end_turn")
                return
            # else: loop back for another _step() call with the new tool_results

    def _dispatch_tools(self, turn_no: int, tool_use_blocks: list) -> Iterator[events.Event]:
        """Runs every tool_use in this assistant turn (loop-breaker rule 8:
        remind at 3, deny at 5, end the turn at 8 -- a cumulative per-turn
        hash of tool name + canonical args). Returns True (via the
        generator's return value) iff the turn should end now rather than
        call the model again."""
        ctx = ToolContext(cwd=self.cwd)
        end_turn = False
        for tu in tool_use_blocks:
            tool_id, name = tu.get("id"), tu.get("name")
            tool_input = tu.get("input") or {}
            key = (name, _canonical_args(tool_input))
            count = self._loop_breaker.get(key, 0) + 1
            self._loop_breaker[key] = count
            yield events.Event("tool_use_ready", {"id": tool_id, "name": name, "input": tool_input, "repaired": False}, turn=turn_no)

            if count >= _LOOP_BREAKER_END_AT:
                text = f"Loop breaker: {name} called with the same arguments {count} times this turn -- ending the turn."
                self.log.append_tool_result(tool_use_id=tool_id, content=text, is_error=True)
                yield events.Event("tool_result", {"id": tool_id, "ok": False, "summary": text}, turn=turn_no)
                end_turn = True
                continue
            if count >= _LOOP_BREAKER_DENY_AT:
                text = f"Loop breaker: {name} called with the same arguments {count} times -- denied. Try a different approach."
                self.log.append_tool_result(tool_use_id=tool_id, content=text, is_error=True)
                yield events.Event("tool_result", {"id": tool_id, "ok": False, "summary": text}, turn=turn_no)
                continue

            tr = self.tool_registry.dispatch(name, tool_input, ctx)
            content_text = tr.content if isinstance(tr.content, str) else json.dumps(tr.content, default=str)
            if _LOOP_BREAKER_REMIND_AT <= count < _LOOP_BREAKER_DENY_AT:
                content_text += (f"\n\n[reminder: {name} has now been called with these same arguments "
                                  f"{count} times this turn -- consider a different approach if unintentional]")
            self.log.append_tool_result(tool_use_id=tool_id, content=content_text, is_error=tr.is_error)
            yield events.Event("tool_result", {"id": tool_id, "ok": not tr.is_error, "summary": content_text[:200]}, turn=turn_no)
        return end_turn
