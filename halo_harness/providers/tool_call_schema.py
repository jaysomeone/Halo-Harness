"""halo_harness.providers.tool_call_schema -- Halo 2.0.3 round 5b part 2
("Reliable tool calls from small models"): the dialect-agnostic pieces
shared by the `ollama` request builder (providers/ollama_request.py), the
`openai-chat` request builder's `hf:local` branch (providers/request.py),
and `agent/loop.py`'s own repair round (`Session._attempt_tool_repair`) --
the constrained-output schema shape, the gate for who may use it, and the
one-shot local repair-round prompt, so none of the three call sites can
quietly disagree about any of them.

**Fix pass (2026-10-04 live-run finding) -- constrained decoding repairs, it
never forces:** this module used to ALSO carry an `expected_to_call_tool`
rule ("right after a tool result, mid-task") that an ORDINARY turn's own
request builder consulted to decide whether to send the tool-call schema
as an output CONSTRAINT on that turn -- i.e. to try to FORCE a well-formed
call before one was even attempted. On a real local box that forced the
model into the tool-call shape on every post-tool-result turn with no way
to just answer in prose; once the model ran out of anything useful to
call, it emitted one meaningless call (`TaskStop` on a task that didn't
exist) over and over, and because EVERY following turn was ALSO right
after a tool result, EVERY retry was ALSO constrained -- the session
looped for 25 minutes (175 requests) until killed by hand. `expected_to_
call_tool` is deleted outright, not merely unwired: there is no other
correct use for "constrain an ordinary turn" left in this design, and
keeping a tested-but-uncalled rule around invites exactly this bug's
return. The schema/gate functions below now have exactly ONE caller each:
`agent/loop.py`'s `Session._attempt_tool_repair`, reactively, AFTER a real
tool call already came back malformed -- never proactively on a turn that
hasn't even tried yet. See `docs/MODELS.md`'s "Reliable tool calls" section
for the current (corrected) rule in full, and `agent/loop.py`'s own
`_identical_call_guard_key`/`_count` for the new, stricter backstop this
fix pass adds against a model stuck repeating one call for any OTHER
reason.
"""

from __future__ import annotations

from typing import Optional


def tool_call_output_schema(tool_names: "list[str]") -> dict:
    """The generic "one well-formed call to one of the offered tools"
    schema sent as the output constraint (brief: "sends the tool-call
    schema") -- `arguments` is deliberately left as a bare `object` with no
    per-tool sub-schema (never a `oneOf` across every offered tool's own
    schema): a combinatorial schema across N tools risks exceeding what a
    local grammar/structured-output engine can compile, or rejecting a
    valid call over a sub-schema mismatch the engine resolves differently
    than expected -- the existing, separate `providers.repair.validate_and_
    coerce` step already re-validates the ACTUAL arguments against the
    real tool's schema once the call lands, so this constraint only needs
    to guarantee "valid JSON, with a real-looking name and an object of
    arguments", not re-implement full per-tool validation a second time
    inside the model's own decoding."""
    names = [n for n in (tool_names or []) if isinstance(n, str) and n]
    schema: dict = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "required": ["name", "arguments"],
    }
    if names:
        schema["properties"]["name"]["enum"] = names
    return schema


_RESPONSE_FORMAT_SCHEMA_NAME = "tool_call"


def openai_response_format_for_schema(schema: dict, *, name: str = _RESPONSE_FORMAT_SCHEMA_NAME) -> dict:
    """The OpenAI `response_format` `json_schema` shape llama-server (and
    every other OpenAI-compatible local server documented to honour
    `--json-schema`/structured output) accepts -- `strict: False`: the
    schema above never declares `additionalProperties: false`, so asking
    for strict mode would 400 on a server that enforces that pairing
    rather than silently relaxing it."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": False}}


def supports_constrained_tool_calls(*, provider: str, dialect: str, local: bool) -> bool:
    """The shared gate items 1 AND 2 both consult (brief: "Both behind the
    `ollama`/`huggingface` profiles only; every other dialect is
    untouched"). `local` means: for `ollama`, `providers.ollama_hw.
    is_local_host(host)` (research doc: "Ollama's Cloud currently does not
    support structured outputs" -- a LAN `ollama.hosts[]` entry is still a
    real local-network Ollama daemon, same support as loopback, so `local`
    here means "not the `ollama.com` cloud route", which `is_local_host`
    already distinguishes correctly -- see that function's own docstring);
    for `huggingface`, `model.ModelRef.local` (an `hf:local/*` ref -- never
    the router or a dedicated Inference Endpoint, which this round's
    research never confirmed support `response_format` the same way)."""
    if dialect == "ollama":
        return bool(local)
    if provider == "huggingface" and dialect == "openai-chat":
        return bool(local)
    return False


def repair_prompt_for(*, tool_name: str, schema: "Optional[dict]", error_message: str, raw_input) -> "tuple[str, str]":
    """`(system_text, user_text)` for item 2's ONE local repair round --
    deliberately a tiny, SELF-CONTAINED exchange (no prior conversation,
    no other tools offered) so the ONLY thing constrained decoding has to
    produce is a corrected arguments object, never a second attempt at
    picking a tool. `schema=None` (an unresolved/unknown tool name) still
    gets a usable prompt -- see `agent.repair_round`'s own docstring for
    why that case is NOT actually wired to a network round trip this
    round (no single schema exists to constrain against); kept general
    here so a future round that DOES resolve a short candidate list can
    reuse this builder unchanged."""
    import json
    schema_text = json.dumps(schema, ensure_ascii=False) if isinstance(schema, dict) else "(no schema available)"
    system_text = (
        "You are repairing exactly one malformed tool call. Reply with ONLY the corrected "
        "arguments as a single JSON object matching the given schema -- no prose, no markdown "
        "fence, no explanation, nothing before or after the JSON object."
    )
    user_text = (
        f"Tool: {tool_name}\n"
        f"Schema for the arguments object: {schema_text}\n"
        f"Your previous call's arguments: {raw_input!r}\n"
        f"Error: {error_message}\n"
        f"Reply with only the corrected arguments object."
    )
    return system_text, user_text
