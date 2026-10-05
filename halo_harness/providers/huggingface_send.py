"""halo_harness.providers.huggingface_send -- the generic openai-chat
turn-sender `doctor_local.py` uses for an `hf:local/*`/`hf:mlx/*` ref.

`gym_send.py` (`halo_harness/gym*.py`) is reserved this round for the
Ollama-only gym task battery and is deliberately NOT touched/imported here
(a separate worker may be editing it concurrently) -- its own `send_turn`
is hard-wired to `providers.ollama_request`/`stream_ollama_completion` and
has no dialect branch at all, so it cannot serve an openai-chat host. This
module is the parallel primitive for THAT dialect, built on the exact same
generic pair `work_matrix.py`'s own `_drive_openai` already uses for a
Databricks openai-chat probe (`providers.request.build_request_body` +
`providers.stream.stream_completion`) -- never a third, hand-rolled wire
format. `stream_completion` emits the identical harness-normalized event
shape `stream_ollama_completion` does (content_block_start/delta,
message_delta, error -- confirmed by `gym_send.py`'s own module
docstring: "the same pattern work_matrix.py already uses for the openai/
anthropic dialects"), so the decode loop below is a deliberate byte-for-
byte mirror of `gym_send.send_turn`'s, with its own, uncoupled
`TurnResult` dataclass (not imported from gym_send.py, so this module
keeps working unchanged whatever the concurrent gym round does to that
file).
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
    error: Optional[str] = None


def send_hf_turn(*, base_url: str, api_key: Optional[str], model_id: str, system_text: str, messages: list,
                  tools: "Optional[list]" = None, requested_max_tokens: int = 64,
                  force_format: "Optional[dict]" = None, context_tokens: int = 8192,
                  max_output_tokens: int = 4096, state_dir, extra_headers: "Optional[dict]" = None) -> TurnResult:
    """One real, end-to-end openai-chat turn against `base_url` -- never
    raises (every failure, upstream or local, comes back as `TurnResult
    (error=...)`, same contract `gym_send.send_turn` has for the Ollama
    dialect, so `doctor_local.py`'s step functions can treat either
    sender identically)."""
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.request import build_request_body
    from halo_harness.providers.routing import Route
    from halo_harness.providers.stream import CompletionRequest, ProviderCreds, stream_completion
    route = Route(provider="huggingface", upstream_model=model_id, dialect="openai-chat")
    try:
        profile = resolve_profile(route, state_dir=state_dir)
        body = build_request_body(system_text=system_text, messages=messages, tools=tools, route=route,
                                   profile=profile, context_tokens=context_tokens, prompt_estimate=0,
                                   requested_max_tokens=requested_max_tokens, force_response_format=force_format)
        req = CompletionRequest(
            body={"messages": []}, route=route,
            profile={"context_tokens": context_tokens, "max_output_tokens": max_output_tokens},
            creds=ProviderCreds(base_url=base_url, api_key=api_key or ""),
            state_dir=state_dir, extra_headers=extra_headers or {}, model_label=model_id, harness_mode=True,
            prebuilt_oai_body=body,
        )
    except Exception as e:
        return TurnResult(error=f"{type(e).__name__}: {e}")

    text_parts: list = []
    order: list = []
    blocks: dict = {}
    raw_json: dict = {}
    stop_reason = None
    error = None
    try:
        for ev in stream_completion(req):
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
    return TurnResult(text="".join(text_parts), tool_blocks=tool_blocks, stop_reason=stop_reason, error=error)
