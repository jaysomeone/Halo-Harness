"""halo_harness.gym_send -- Halo 2.0.3 round 5d: the ONE place every gym
task (`gym_tool_tasks.py`/`gym_reply_tasks.py`) and the acceptance check
(`doctor_local.py`) send a real `/api/chat` turn and decode the reply --
built on the exact same primitives a live session uses
(`providers.ollama_request.build_ollama_request_body`, `providers.stream.
stream_ollama_completion`), the same pattern `work_matrix.py` already
uses for the openai/anthropic dialects (a `CompletionRequest` built
directly, no `agent.loop.Session` needed). This is "the real request/
decode path against the live host" the brief asks tool-call accuracy to
be measured through -- never a second, hand-rolled wire format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TurnResult:
    text: str = ""
    tool_blocks: "list" = field(default_factory=list)  # [{"name": str, "input": dict}, ...]
    stop_reason: Optional[str] = None
    timing_ns: dict = field(default_factory=dict)
    error: Optional[str] = None


def send_turn(*, host, route, profile, decision, system_text: str, messages: list,
              tools: Optional[list] = None, requested_max_tokens: int = 64,
              force_format: Optional[dict] = None, state_dir) -> TurnResult:
    """One real, end-to-end `/api/chat` turn -- never raises (every
    failure, upstream or local, comes back as `TurnResult(error=...)` so a
    gym task can count it as a failed attempt and move on to the next
    one, exactly like `work_matrix.py`'s own probes do)."""
    from halo_harness.providers.ollama_request import build_ollama_request_body
    from halo_harness.providers.stream import CompletionRequest, ProviderCreds, stream_ollama_completion
    try:
        body = build_ollama_request_body(
            system_text=system_text, messages=messages, tools=tools, route=route, profile=profile,
            effort=None, host=host, trained_context=decision.trained_context, fit_estimate=decision.fit_estimate,
            requested_max_tokens=requested_max_tokens, learned_cap=decision.learned_cap, remote=decision.remote,
            force_format=force_format,
        )
        req = CompletionRequest(
            body={"messages": []}, route=route,
            profile={"context_tokens": decision.num_ctx or 8192, "max_output_tokens": requested_max_tokens},
            creds=ProviderCreds(base_url=host.url, api_key=host.api_key or ""),
            state_dir=state_dir, extra_headers={}, model_label=route.upstream_model, harness_mode=True,
            prebuilt_ollama_body=body,
        )
    except Exception as e:
        return TurnResult(error=f"{type(e).__name__}: {e}")

    text_parts: list = []
    order: list = []
    blocks: dict = {}
    raw_json: dict = {}
    stop_reason = None
    timing_ns: dict = {}
    error = None
    try:
        for ev in stream_ollama_completion(req):
            et = ev.get("type")
            idx = ev.get("index")
            if et == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    order.append(idx)
                    blocks[idx] = {"name": block.get("name")}
                    raw_json[idx] = ""
            elif et == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text_parts.append(delta.get("text", ""))
                elif dtype == "input_json_delta" and idx in raw_json:
                    raw_json[idx] += delta.get("partial_json", "")
            elif et == "message_delta":
                delta = ev.get("delta") or {}
                if delta.get("stop_reason") is not None:
                    stop_reason = delta.get("stop_reason")
                meta = ev.get("harness_meta")
                if isinstance(meta, dict) and isinstance(meta.get("timing_ns"), dict):
                    timing_ns = meta["timing_ns"]
            elif et == "error":
                error = (ev.get("error") or {}).get("message", "upstream error")
    except Exception as e:
        error = error or f"{type(e).__name__}: {e}"

    tool_blocks = []
    for idx in order:
        try:
            parsed = json.loads(raw_json.get(idx) or "{}")
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        tool_blocks.append({"name": blocks[idx].get("name"), "input": parsed if isinstance(parsed, dict) else {}})
    return TurnResult(text="".join(text_parts), tool_blocks=tool_blocks, stop_reason=stop_reason,
                       timing_ns=timing_ns, error=error)


def send_repair(*, host, route, profile, decision, tool_name: str, schema: Optional[dict],
                 error_message: str, raw_input, state_dir) -> "tuple[Optional[dict], Optional[str]]":
    """ONE local repair round, identical in spirit to `agent/loop.py`'s
    `Session._attempt_tool_repair` -- a tools-less, history-less
    completion constrained to `schema` when this host supports constrained
    decoding (`providers.tool_call_schema.supports_constrained_tool_calls`),
    free-decoded otherwise. `(coerced_args, None)` on success, `(None,
    reason)` on any failure -- `schema is None` included, since there is
    no single schema to constrain (or even just validate) against."""
    if schema is None or not tool_name:
        return None, "no schema resolved for this tool -- repair needs one to constrain/validate against"
    from halo_harness.providers.ollama_hw import is_local_host
    from halo_harness.providers.tool_call_schema import repair_prompt_for, supports_constrained_tool_calls
    constrained = supports_constrained_tool_calls(provider="ollama", dialect="ollama", local=is_local_host(host))
    system_text, user_text = repair_prompt_for(
        tool_name=tool_name, schema=schema, error_message=error_message, raw_input=raw_input)
    messages = [{"role": "user", "content": [{"type": "text", "text": user_text}]}]
    result = send_turn(host=host, route=route, profile=profile, decision=decision, system_text=system_text,
                        messages=messages, tools=[], requested_max_tokens=256,
                        force_format=schema if constrained else None, state_dir=state_dir)
    if result.error:
        return None, result.error
    try:
        parsed = json.loads(result.text.strip())
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"repair reply was not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return None, "repair reply was not a JSON object"
    from halo_harness.agent.repair import validate_and_coerce
    coerced, errors = validate_and_coerce(parsed, schema)
    if errors:
        return None, f"repair reply still failed validation: {'; '.join(errors)}"
    return coerced, None
