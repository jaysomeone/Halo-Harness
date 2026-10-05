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

Round 5i (after round 5f's `hf:mlx`/shared `providers.huggingface_send`):
`send_turn_for` below is the gym's OWN provider dispatcher -- Ollama stays
`send_turn` unchanged; an `hf:local/*`/`hf:mlx/*` ref goes through
`providers.huggingface_send.send_hf_turn` (round 5f's shared sender,
reused here AS IS, never re-implemented a third time -- that module's own
docstring is explicit about this). Every gym task function calls `send_
turn_for` (never `send_turn` directly) so the battery's own logic stays
provider-agnostic; only this function (and `send_repair`) knows two
senders exist."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TurnResult:
    text: str = ""
    tool_blocks: "list" = field(default_factory=list)  # [{"name": str, "input": dict}, ...]
    stop_reason: Optional[str] = None
    timing_ns: dict = field(default_factory=dict)
    error: Optional[str] = None
    # Fix pass (2026-10-04 live-run finding): `text` is built ONLY from
    # `content_block_delta`/`text_delta` events below -- `providers.
    # ollama_stream.OllamaStreamToAnthropic` never turns `message.thinking`
    # into one of those (it only ever accumulates into `harness_meta
    # ["reasoning_text"]`, display-only) -- so `text`/`thinking` can never
    # be confused with one another on THIS side. Captured here, separate
    # from `text`, purely for diagnosability (`gym.format_card`'s/`halo
    # gym --show-replies`'s own samples) -- the real live-run bug turned
    # out to be `requested_max_tokens` being too small for a thinking-by-
    # default model to clear its own reasoning before the budget ran out
    # (`text` came back genuinely EMPTY, `stop_reason="max_tokens"`, never
    # thinking text masquerading as the answer) -- see `gym_reply_tasks.py`
    # for the budget fix; `thinking` here is what makes a repeat of that
    # failure mode immediately visible instead of a bare, unexplained 0%.
    thinking: str = ""


@dataclass(frozen=True)
class HFHost:
    """Round 5i: the gym's own duck-typed stand-in for an `ollama.
    OllamaHost`, same shape as `doctor_local.py`'s private `_HFHost` --
    NOT imported from there (that name is underscore-private to that
    module, and the fix/extension rounds for `gym*.py`/`doctor_local.py`
    run concurrently per the coordinator's own file-ownership split) --
    just enough (`base_url`/`api_key`/`name`, `url` aliasing `base_url`
    for `name`-only display text) for `send_turn_for`'s huggingface
    branch and `gym_run.py`'s result-file bookkeeping."""
    base_url: str
    api_key: Optional[str]
    name: str

    @property
    def url(self) -> str:
        return self.base_url


@dataclass(frozen=True)
class HFContextDecision:
    """Round 5i: the huggingface branch's own minimal stand-in for
    `providers.ollama_hw.OllamaContextDecision` -- every gym task function
    reads only `decision.num_ctx` (grepped: `gym_reply_tasks.py`'s own
    `_build_recall_prompt` is the ONE call site outside this module), so
    this is the one field that needs a real value; `max_output_tokens`
    is consulted by `send_turn_for` alone, for `send_hf_turn`'s own
    `max_output_tokens` knob (the openai-chat dialect has no context-
    ownership rule of its own to derive one from)."""
    num_ctx: int = 128000
    max_output_tokens: int = 16384


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
    thinking = ""
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
                if isinstance(meta, dict) and isinstance(meta.get("reasoning_text"), str):
                    thinking = meta["reasoning_text"]
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
                       timing_ns=timing_ns, error=error, thinking=thinking)


def send_turn_for(*, host, route, profile, decision, **kwargs) -> TurnResult:
    """The gym's provider dispatcher (module docstring) -- `kwargs` are
    exactly `send_turn`'s own `system_text`/`messages`/`tools`/
    `requested_max_tokens`/`force_format`/`state_dir`, which `providers.
    huggingface_send.send_hf_turn` also accepts verbatim (its own
    signature is a deliberate superset). `timing_ns` for the huggingface
    branch is wall-clock ONLY (that sender exposes no server-side `usage`/
    per-phase timing to a caller outside it, unlike Ollama's native
    `eval_duration`/`prompt_eval_duration`) -- `eval_count` is the SAME
    `len(text)//4` chars-per-token heuristic `providers.ollama_fit.
    estimate_catalog_prompt_tokens` already uses elsewhere, `eval_duration`
    is the measured wall-clock call time; `prompt_eval_duration` is left
    OUT entirely (never guessed), so `gym_run._average_throughput` --
    unchanged, reads this same dict shape -- naturally reports a real
    `tokens_per_second` but `prefill_seconds=None` for an hf: card,
    documented as a real limitation rather than a faked number."""
    if route.provider == "huggingface":
        from halo_harness.providers.huggingface_send import send_hf_turn
        t0 = time.monotonic()
        hf_result = send_hf_turn(
            base_url=host.base_url, api_key=host.api_key, model_id=route.upstream_model,
            context_tokens=getattr(decision, "num_ctx", None) or 8192,
            max_output_tokens=getattr(decision, "max_output_tokens", None) or 4096,
            **kwargs,
        )
        elapsed = time.monotonic() - t0
        timing_ns = {}
        if not hf_result.error and elapsed > 0:
            timing_ns = {"eval_count": max(1, len(hf_result.text) // 4), "eval_duration": elapsed * 1_000_000_000.0}
        return TurnResult(text=hf_result.text, tool_blocks=hf_result.tool_blocks,
                           stop_reason=hf_result.stop_reason, error=hf_result.error, timing_ns=timing_ns)
    return send_turn(host=host, route=route, profile=profile, decision=decision, **kwargs)


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
    from halo_harness.providers.tool_call_schema import repair_prompt_for, supports_constrained_tool_calls
    if route.provider == "huggingface":
        # Round 5i: an hf:local/*/hf:mlx/* ref resolved by the gym is
        # ALWAYS "local" in this gate's sense (never the router/a
        # dedicated endpoint) -- same reasoning `doctor_local.py`'s own
        # `_structured_output` step already documents for this exact
        # provider/dialect pair, never `ollama_hw.is_local_host` (an
        # Ollama-host-shaped hostname check that would not even apply to
        # an `HFHost`).
        constrained = supports_constrained_tool_calls(provider="huggingface", dialect="openai-chat", local=True)
    else:
        from halo_harness.providers.ollama_hw import is_local_host
        constrained = supports_constrained_tool_calls(provider="ollama", dialect="ollama", local=is_local_host(host))
    system_text, user_text = repair_prompt_for(
        tool_name=tool_name, schema=schema, error_message=error_message, raw_input=raw_input)
    messages = [{"role": "user", "content": [{"type": "text", "text": user_text}]}]
    result = send_turn_for(host=host, route=route, profile=profile, decision=decision, system_text=system_text,
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
