"""rolo_claude.providers.hooks -- H2 must-do 3: the eight named code
branches the research report (`reports/Open weight model adapter rules.md`)
says stay in code while their PARAMETERS live in `model_table.json`:
`reasoning_echo`, `tool_id_normalize`, `system_normalize`, `leak_parser` +
`think_tag_strip` + `args_repair`, `stream_aggregate`, `max_tokens_budget` +
`overflow_classifier`, `loop_guards`, `host_allowlist`. Before H2 these were
an empty `run_code_branch` registry in `providers/errors.py` that nothing
invoked; every function here is REAL and is actually called from
`providers/oai_stream.py`, `providers/request.py`, or `agent/loop.py` (see
each function's docstring for its call site) -- `tests/test_hooks.py` has
at least one test per function.

Dependency direction is one-way (`oai_stream.py`/`request.py`/`loop.py` ->
this module -> `providers/errors.py` only) so nothing here risks a import
cycle with the modules that call it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

from rolo_claude.providers.errors import classify_error_category, parse_context_overflow

log = logging.getLogger("bridge")


# ---------------------------------------------------------------------------
# 1. reasoning_echo -- called from providers/request.py's build_request_body.
# ---------------------------------------------------------------------------

def reasoning_echo(oai_messages: list, messages: list, profile, *, tools_present: bool) -> None:
    """Mutates `oai_messages` in place: splice each assistant message's
    logged `reasoning` node ({"text": str, "details": list|None} -- see
    agent/loop.py's `_step`) back onto the matching OpenAI-dialect assistant
    proto, per `profile.reasoning_replay`.

    Finding 2 (two bugs): (a) `reasoning_details` streamed deltas are now
    MERGED by (type,index) at capture time (oai_stream.py's `feed_chunk`),
    so the list handed to us here is already complete/ordered, never a
    single last fragment; (b) `profile.reasoning_dual_field` (DeepSeek V4
    rows: `reasoning.replay == "echo_required_400"`) means the OpenRouter
    "details" branch ALSO sends `reasoning_content` (text, "" when absent)
    beside the verbatim `reasoning_details` array -- DeepSeek 400s
    ("reasoning_content ... must be passed back") if that field is missing,
    regardless of whether `reasoning_details` is also present.

    Finding 8 (alignment): positions here line up 1:1 with `messages`'
    assistant entries because the CALLER (`request.py`) guarantees
    `_flatten_messages` never silently drops an empty/whitespace assistant
    node before calling this -- see `request.py`'s `_flatten_no_drop`.
    "empty" (Qwen3-thinking etc, must NOT see prior reasoning) and
    "thinking" (native Anthropic passthrough never reaches this builder)
    are both no-ops.
    """
    if profile.reasoning_replay not in ("text", "details"):
        return
    reasoning_list = [m.get("reasoning") if isinstance(m, dict) else None
                       for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
    idx = 0
    for proto in oai_messages:
        if proto.get("role") != "assistant":
            continue
        raw = reasoning_list[idx] if idx < len(reasoning_list) else None
        idx += 1
        text = raw.get("text") if isinstance(raw, dict) else None
        details = raw.get("details") if isinstance(raw, dict) else None
        if profile.reasoning_replay == "text":
            if not tools_present:
                continue
            proto["reasoning_content"] = text if isinstance(text, str) else ""
        elif profile.reasoning_replay == "details":
            if isinstance(details, list):
                proto["reasoning_details"] = details
            if profile.reasoning_dual_field and tools_present:
                proto["reasoning_content"] = text if isinstance(text, str) else ""


# ---------------------------------------------------------------------------
# 2. tool_id_normalize -- called from providers/oai_stream.py's _finalize.
# ---------------------------------------------------------------------------

_ALNUM9_RE = re.compile(r"[^a-zA-Z0-9]")
_KIMI_NATIVE_ID_RE = re.compile(r"^functions\.[^:]+:\d+$")


def normalize_tool_id(raw_id: Optional[str], *, name: str, tool_id_format: str, counter) -> str:
    """Decide the id a `tool_use` block is actually emitted (and later
    logged/replayed) with. `counter` is a single-element list `[n]` the
    caller owns (one per stream) -- mutated in place so successive calls in
    the SAME stream advance it; only consulted for `kimi_functions_idx`.

    - "preserve"/"minimax_preserve": the upstream id verbatim, whatever it
      is (finding 1's core fix -- Kimi/MiniMax ids must round-trip
      byte-for-byte; a synthesized id drops Kimi's tool-loop pass rate to
      20% and MiniMax rejects one outright with error 2013).
    - "alnum9" (Mistral): `[^a-zA-Z0-9]` stripped, truncated/padded to
      exactly 9 chars -- Mistral's tokenizer-level id constraint, applied
      identically whether the raw id already looked mistral-shaped or not.
    - "kimi_functions_idx": Kimi's own native shape is already
      `functions.{name}:{idx}` on the wire -- left untouched when it
      already matches; only an id that DOESN'T (empty, or one carried over
      from importing non-Kimi history) gets renamed to that shape using the
      per-stream counter.
    """
    if not raw_id:
        raw_id = None
    if tool_id_format == "mint":
        # The proxy's own PERMANENT default (real Claude Code clients only
        # care about ids being valid and unique WITHIN their own
        # request/response cycle -- reusing a duplicate/colliding upstream
        # id, as some hosts genuinely send, breaks that pairing). Never
        # preserve verbatim; always fall through to the caller's toolu_
        # mint. This is what keeps oai_stream.py's default byte-for-byte
        # unchanged for every caller that doesn't explicitly ask for
        # id preservation (i.e. everyone except agent/loop.py).
        return ""
    if tool_id_format == "alnum9":
        base = _ALNUM9_RE.sub("", raw_id or "")
        return (base[:9] or "0").ljust(9, "0")
    if tool_id_format == "kimi_functions_idx":
        if raw_id and _KIMI_NATIVE_ID_RE.match(raw_id):
            return raw_id
        n = counter[0]
        counter[0] += 1
        return f"functions.{name}:{n}"
    # "preserve" / "minimax_preserve" / anything unrecognized: verbatim,
    # minted by the caller only when raw_id is falsy (oai_stream.py already
    # does this -- see _finalize's `tool_id = tc's id or f"toolu_{...}"`).
    return raw_id or ""


# ---------------------------------------------------------------------------
# 3. system_normalize -- called from providers/request.py's build_request_body.
# ---------------------------------------------------------------------------

def system_normalize(system_text: str, messages: list, profile) -> "tuple[str, list]":
    """Returns a (system_text, messages) pair, NOT mutated in place (a
    system prompt this harness's own derive_request() ALWAYS carries as a
    separate string, never as a `role: "system"` entry inside `messages` --
    see agent/derive.py -- so there is no in-`messages` system entry to
    scan for; the single-leading-system-message rule for the common case
    is already correct: `_flatten_messages` emits exactly one leading
    `system` proto from this string and `derive_request` never emits more
    than one system node to begin with).

    What's NEW here is `profile.system_placement ==
    "fold_into_first_user"` (Gemma 3: no system role at all; DeepSeek R1:
    "no system prompt", both from `model_table.json`): fold `system_text`
    into the FRONT of the first `role: "user"` message instead, and return
    `""` as the system text, so `_flatten_messages` never emits a system
    proto at all for these rows. A no-op (`(system_text, messages)`
    unchanged) for every other row (`system_placement == "first"`, the
    default, and whenever there's no text to fold or no user message to
    fold it into)."""
    if profile.system_placement != "fold_into_first_user" or not system_text or not messages:
        return system_text, messages
    out: list = []
    folded = False
    prefix = [{"type": "text", "text": system_text}]
    for msg in messages:
        if not folded and isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                msg = {**msg, "content": prefix + content}
            elif isinstance(content, str):
                msg = {**msg, "content": prefix + [{"type": "text", "text": content}]}
            else:
                msg = {**msg, "content": prefix}
            folded = True
        out.append(msg)
    return ("" if folded else system_text), out


# ---------------------------------------------------------------------------
# 4/5/6. leak_parser + think_tag_strip + args_repair -- H2's minimal repair
# layer (the full repair layer, incl. schema validation and a retry with
# tool_choice=required, is H2b's job per the brief; these three exist and
# are called now so the review's "make it a real hook" requirement is met).
# ---------------------------------------------------------------------------

_LEAK_PATTERNS = {
    # name -> (regex, group->field mapping). Deliberately covers only the
    # handful of concrete shapes cited by the review/report -- see
    # tool_leak_patterns in model_table.json for which row expects which.
    "hermes_tool_call": re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL),
    "glm_arg_key": re.compile(r"<tool_call>\s*([A-Za-z0-9_.\-]+)\s*((?:<arg_key>.*?</arg_value>\s*)+)", re.DOTALL),
    "kimi_section_tokens": re.compile(
        r"<\|tool_call_begin\|>\s*([A-Za-z0-9_.\-]+):\d+\s*<\|tool_call_argument_begin\|>\s*(\{.*?\})\s*<\|tool_call_end\|>",
        re.DOTALL,
    ),
}
_GLM_ARG_RE = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.DOTALL)

# H2b repair layer (brief scope B): the remaining leak shapes the brief's
# own format list names -- DSML (DeepSeek V3.2+/V4:
# research_notes/.../deepseek_kimi_adapters.md:35), Qwen3-Coder/Qwen3.5's
# `<function=...><parameter=...>` XML (glm_qwen_minimax_adapters.md:44),
# MiniMax M2.x's `<minimax:tool_call><invoke>` and M1's plain
# `<tool_calls>{json}</tool_calls>` (glm_qwen_minimax_adapters.md:53/56),
# and fenced JSON with a "name" key. Each needs MORE than the flat
# (name_group, args_group) shape the dict above handles (nested tag
# extraction, or scanning several fenced blocks) -- registered as
# EXTRACTOR FUNCTIONS instead, tried by the SAME `profile.tool_leak_patterns`
# name lookup in `leak_parser` below, so a profile row never has to know
# which of the two internal mechanisms serves its own pattern name.
_DSML_INVOKE_RE = re.compile(r"<｜DSML｜invoke\s+name=\"([^\"]+)\"\s*>(.*?)</｜DSML｜invoke>", re.DOTALL)
_DSML_PARAM_RE = re.compile(r"<｜DSML｜parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</｜DSML｜parameter>", re.DOTALL)
_QWEN_FUNCTION_RE = re.compile(r"<function=([^>]+)>(.*?)</function>", re.DOTALL)
_QWEN_PARAMETER_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
_MINIMAX_INVOKE_RE = re.compile(
    r"<minimax:tool_call>\s*<invoke\s+name=\"([^\"]+)\"\s*>(.*?)</invoke>\s*</minimax:tool_call>", re.DOTALL,
)
_MINIMAX_PARAM_RE = re.compile(r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>", re.DOTALL)
_MINIMAX_M1_RE = re.compile(r"<tool_calls>\s*(\{.*?\})\s*</tool_calls>", re.DOTALL)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _tag_params(blob: str, param_re) -> dict:
    return {k.strip(): v.strip() for k, v in param_re.findall(blob)}


def _extract_dsml(text: str) -> Optional[dict]:
    m = _DSML_INVOKE_RE.search(text)
    if not m:
        return None
    return {"name": m.group(1).strip(), "arguments": _tag_params(m.group(2), _DSML_PARAM_RE)}


def _extract_qwen3_coder_xml(text: str) -> Optional[dict]:
    m = _QWEN_FUNCTION_RE.search(text)
    if not m:
        return None
    return {"name": m.group(1).strip(), "arguments": _tag_params(m.group(2), _QWEN_PARAMETER_RE)}


def _extract_minimax_invoke_xml(text: str) -> Optional[dict]:
    m = _MINIMAX_INVOKE_RE.search(text)
    if not m:
        return None
    return {"name": m.group(1).strip(), "arguments": _tag_params(m.group(2), _MINIMAX_PARAM_RE)}


def _extract_minimax_m1_tool_calls(text: str) -> Optional[dict]:
    m = _MINIMAX_M1_RE.search(text)
    if not m:
        return None
    parsed = args_repair(m.group(1))
    if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
        return {"name": parsed["name"], "arguments": parsed.get("arguments") or {}}
    return None


def _extract_fenced_json(text: str) -> Optional[dict]:
    for m in _FENCED_JSON_RE.finditer(text):
        parsed = args_repair(m.group(1))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("name"), str):
            continue
        args = parsed.get("arguments")
        if not isinstance(args, dict):
            args = parsed.get("input") if isinstance(parsed.get("input"), dict) else parsed.get("parameters")
        return {"name": parsed["name"], "arguments": args if isinstance(args, dict) else {}}
    return None


# name -> extractor(text) -> {"name","arguments"} | None. Tried BEFORE
# `_LEAK_PATTERNS` in `leak_parser` (a name is never in both dicts).
_LEAK_EXTRACTORS = {
    "dsml": _extract_dsml,
    "qwen3_coder_xml": _extract_qwen3_coder_xml,
    "minimax_invoke_xml": _extract_minimax_invoke_xml,
    "minimax_m1_tool_calls": _extract_minimax_m1_tool_calls,
    # model_table.json uses both names for a fenced-JSON-with-name shape
    # (no row distinguishes them further) -- same extractor for both.
    "json_text_call": _extract_fenced_json,
    "name_json_text": _extract_fenced_json,
}


def leak_parser(text: str, profile) -> Optional[dict]:
    """Called from agent/loop.py's `_turn_body` ONLY when a tool call was
    expected (tools were present in the request) and the turn ended without
    one (`stop_reason != "tool_use"`) -- narrowly scoped per the report's
    own rule ("only when a tool call was expected... then retry once"),
    never run against ordinary prose. Returns {"name", "arguments"} (a
    plain dict, arguments already parsed) on a match, else None. Only the
    patterns named in `profile.tool_leak_patterns` are tried."""
    if not text or not profile.tool_leak_patterns:
        return None
    for pattern_name in profile.tool_leak_patterns:
        extractor = _LEAK_EXTRACTORS.get(pattern_name)
        if extractor is not None:
            result = extractor(text)
            if result is not None:
                return result
            continue
        rx = _LEAK_PATTERNS.get(pattern_name)
        if rx is None:
            continue
        m = rx.search(text)
        if not m:
            continue
        if pattern_name == "glm_arg_key":
            name = m.group(1).strip()
            args = {k.strip(): v.strip() for k, v in _GLM_ARG_RE.findall(m.group(2))}
            return {"name": name, "arguments": args}
        name_or_json = m.group(1)
        args_raw = m.group(2) if m.lastindex and m.lastindex >= 2 else None
        if args_raw is not None:
            parsed = args_repair(args_raw)
            if parsed is not None:
                return {"name": name_or_json.strip(), "arguments": parsed}
            continue
        parsed = args_repair(name_or_json)
        if isinstance(parsed, dict) and "name" in parsed:
            return {"name": parsed["name"], "arguments": parsed.get("arguments") or parsed.get("input") or {}}
    return None


def think_tag_strip(text: str) -> str:
    """Strip a leading `<think>...</think>` block and stray
    end-of-sentence sentinel tokens from DISPLAYED text (never from what's
    logged). The canonical implementation lives here now; `oai_stream.
    strip_display_artifacts` re-exports it so existing callers/imports are
    unaffected. Called from `rolo_claude/output.py` (finding 15)."""
    if not text:
        return text
    think_re = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
    sentinel_re = re.compile(r"<｜end▁of▁sentence｜>|<\|end_of_sentence\|>")
    return sentinel_re.sub("", think_re.sub("", text, count=1))


def args_repair(raw: str) -> Optional[dict]:
    """Lenient JSON repair for tool-call arguments: strip code fences,
    drop trailing commas, swap a Python-repr'd dict's single quotes /
    True|False|None for JSON's, balance an unterminated trailing brace.
    Returns a parsed dict, or None if nothing recognizable came out.
    Called from `oai_stream.py`'s `_finalize` as the SECOND attempt (after
    a plain `json.loads`) before a tool call is tagged malformed."""
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", candidate)
        candidate = re.sub(r"```\s*$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)  # trailing commas
    if "'" in repaired and '"' not in repaired:
        repaired = repaired.replace('"', '\\"').replace("'", '"')
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    opens = repaired.count("{") - repaired.count("}")
    if opens > 0:
        repaired += "}" * opens
    try:
        result = json.loads(repaired)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 7. stream_aggregate -- called from providers/oai_stream.py's feed_chunk.
# ---------------------------------------------------------------------------

def stream_aggregate_key(idx: Optional[int], id_: Optional[str], *, id_to_key: dict,
                          next_auto: list, last_key: list) -> tuple:
    """Stable key for one tool-call delta. `id_to_key`/`next_auto`/`last_key`
    are the CALLER's own per-stream mutable state (`next_auto`/`last_key`
    passed as single-element lists so this pure function can advance them
    without owning any state itself) -- kept on `OpenAIStreamToAnthropic`
    instances exactly as before finding 11.

    Finding 11 rule 3: an INDEX-LESS delta (idx is None) always continues
    the CURRENT call (`last_key`) regardless of what id -- if any -- it
    carries; only an INDEXED delta can open a genuinely new key. This is
    what stops a GLM-style mid-stream id change (`chatcmpl-tool-...` ->
    `toolcall0` on the SAME index-less continuation) from splitting one
    call into two malformed ones."""
    if idx is not None:
        key = ("idx", idx)
        last_key[0] = key
        return key
    if last_key[0] is not None:
        return last_key[0]
    if id_ is not None:
        if id_ not in id_to_key:
            id_to_key[id_] = ("auto", next_auto[0])
            next_auto[0] += 1
        key = id_to_key[id_]
    else:
        key = ("auto", next_auto[0])
        next_auto[0] += 1
    last_key[0] = key
    return key


def stream_aggregate_apply(buf: dict, func: dict) -> int:
    """Apply one tool-call delta's `function` fragment to `buf`
    ({"name","args"}) in place; returns how many argument chars were
    appended (0 if none), for the caller's output-size accounting.

    Finding 11 rules 1-2, null-safe: `"name": null` never overwrites an
    already-captured name (only a non-empty STRING name is accepted), and
    `"arguments": null` is treated as "" instead of raising TypeError on
    `buf["args"] += None`."""
    name = func.get("name")
    if isinstance(name, str) and name:
        buf["name"] = name
    if "arguments" not in func:
        return 0
    args = func["arguments"]
    if not isinstance(args, str):
        return 0
    buf["args"] += args
    return len(args)


# ---------------------------------------------------------------------------
# 8. max_tokens_budget + overflow_classifier -- called from agent/loop.py.
# ---------------------------------------------------------------------------

_OTPM_LOCK = threading.Lock()
_OTPM_HISTORY: dict = {}  # model_key -> list[(monotonic_ts, tokens)]
_OTPM_WINDOW_S = 60.0
_OTPM_MARGIN = 256
_OTPM_FLOOR = 512
_OTPM_MAX_WAIT_S = 65.0


def reset_databricks_otpm_history() -> None:
    """Test seam: clear the rolling-window state between tests."""
    with _OTPM_LOCK:
        _OTPM_HISTORY.clear()


def record_databricks_output_tokens(model_key: str, tokens) -> None:
    """Record one response's actual output-token spend against the rolling
    60s OTPM window; called from agent/loop.py right after a Databricks
    response's usage lands (`Session._turn_body`, after `cost_meter.
    add_usage`). A no-op for a non-positive/non-int token count (a response
    whose usage was never reported)."""
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        return
    with _OTPM_LOCK:
        _OTPM_HISTORY.setdefault(model_key, []).append((time.monotonic(), tokens))


def _otpm_used(model_key: str, now: float) -> int:
    with _OTPM_LOCK:
        hist = _OTPM_HISTORY.get(model_key) or []
        hist[:] = [(t, n) for (t, n) in hist if now - t < _OTPM_WINDOW_S]
        return sum(n for _, n in hist)


def max_tokens_budget(profile, *, model_key: str, requested: Optional[int] = None, abort=None) -> int:
    """Called from agent/loop.py's `_derive_and_build`, BEFORE
    `providers.request.build_request_body` (which still applies its own
    static context-headroom clamp on top of whatever this returns) -- a
    no-op passthrough (`min(cap, want)`) for any row without `profile.
    databricks_rate_limits` (every OpenRouter row, and the Databricks rows
    the report has no published OTPM for).

    Finding 6: Databricks pre-admits `max_tokens` against the OTPM budget
    with a 429 -- sending the WHOLE budget on every call, with no
    accounting for output already spent in the trailing 60s, means the
    second step of a tool loop inside that window gets an immediate 429.
    This keeps a per-model rolling 60s window of actually-spent output
    tokens and budgets `min(cap, otpm - used - margin)`; when that
    collapses below a floor, it waits (bounded at `_OTPM_MAX_WAIT_S`,
    abort-aware) for the oldest window entry to age out rather than
    sending a request that's virtually guaranteed to 429 anyway."""
    cap = profile.max_tokens_cap or profile.max_tokens_default or 16384
    want = requested if isinstance(requested, int) and requested > 0 else (profile.max_tokens_default or cap)
    rl = profile.databricks_rate_limits
    if not isinstance(rl, dict) or not isinstance(rl.get("otpm"), int):
        return max(1, min(want, cap))

    otpm = rl["otpm"]
    deadline = time.monotonic() + _OTPM_MAX_WAIT_S
    while True:
        used = _otpm_used(model_key, time.monotonic())
        budget = otpm - used - _OTPM_MARGIN
        if budget >= _OTPM_FLOOR or time.monotonic() >= deadline:
            return max(1, min(want, cap, max(budget, 1)))
        if abort is not None and abort.is_set():
            return max(1, min(want, cap, max(budget, 1)))
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def overflow_classifier(status: int, message: str) -> str:
    """Thin named wrapper around `providers.errors.classify_error_category`
    -- called from agent/loop.py wherever a wire/phase-1 failure needs the
    dsh-style taxonomy category (AUTH/RATE_LIMIT/CONTEXT_WINDOW_EXCEEDED/
    PROVIDER_FAILURE/MALFORMED_RESPONSE). `classify_error_category` and
    `parse_context_overflow` (the numeric extraction used for the
    fixable-retry decision) are already correct and well-tested
    (test_errors_taxonomy.py) -- this hook exists so the review's "named
    hook, actually called" requirement is met without duplicating that
    per-host regex work a second time."""
    return classify_error_category(status, message)


# ---------------------------------------------------------------------------
# 9. loop_guards -- called from agent/loop.py's _turn_body/_dispatch_tools.
# ---------------------------------------------------------------------------

def is_retryable_empty_completion(*, stop_reason: Optional[str], text: str, tool_use_count: int,
                                   tools_present: bool) -> bool:
    """finding 7 (`EMPTY_RESPONSE` was defined but never used) + the
    report's "tool_calls-with-no-parseable-call" guard, which collapse to
    the same observable state in this codebase (see oai_stream.py's
    `decide_stop_reason`: no captured tool_calls means `has_tool_calls` is
    False regardless of what the wire `finish_reason` said, so both cases
    surface as an ordinary `end_turn` with empty content). DeepSeek V4
    Pro's documented empty completion after tool results (22 of 46 turns)
    is exactly this: `stop_reason != "tool_use"`, no text, no tool_use,
    even though tools were offered. The loop retries this ONCE, then lowers
    effort / re-pins (never retries in place a second time)."""
    return bool(tools_present and stop_reason != "tool_use" and not (text or "").strip() and tool_use_count == 0)


def classify_length_tool_call(flags: dict) -> str:
    """finding 3: what to do with ONE tool_use block's `harness_meta.
    tool_call_flags` entry (from oai_stream.py's strict_tool_json capture)
    -- "length" (max_tokens cut the call off mid-argument: write an
    `is_error` result telling the model to split the operation, never
    `invalid`), "malformed" (a COMPLETE but invalid-JSON call: quote the
    JSON error), or "ok" (nothing flagged)."""
    if flags.get("truncated_by_length"):
        return "length"
    if flags.get("malformed_json"):
        return "malformed"
    return "ok"


# ---------------------------------------------------------------------------
# 10. host_allowlist -- called from providers/request.py's build_request_body.
# ---------------------------------------------------------------------------

def host_allowlist(body: dict, profile, *, tools_present: bool) -> None:
    """Mutates `body` in place; called right before `build_request_body`
    returns. Finding 5: merges the row's OpenRouter pin (`order`,
    `allow_fallbacks`, `only`, `ignore`, `quantizations`) into `provider`
    on EVERY OpenRouter request (not just an empty `{"require_parameters":
    true}`), and forces `require_parameters: true` whenever tools are
    present -- every seeded `openrouter_pin` was previously dead data.
    Databricks' strict field set is already enforced by `profile.
    body_allowlist` filtering elsewhere in `build_request_body`; this hook
    only owns the OpenRouter-specific piece (`must-do 6` layers the
    `x-databricks-use-coding-agent-mode` HEADER on separately, in
    agent/loop.py/headless.py -- a header is not a body field)."""
    if not profile.host_specific_fields:
        return
    provider: dict = dict(body.get("provider") or {})
    if isinstance(profile.openrouter_pin, dict):
        for key in ("order", "allow_fallbacks", "only", "ignore", "quantizations"):
            if key in profile.openrouter_pin:
                provider[key] = profile.openrouter_pin[key]
    if tools_present:
        provider["require_parameters"] = True
    if provider:
        body["provider"] = provider
