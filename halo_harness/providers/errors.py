"""halo_harness.providers.errors -- upstream error-body parsing, the
context-overflow detector, and the OpenAI/Databricks -> Anthropic error
mapping table. Moved out of bridge.py unchanged in the H0 package split; see
wip/SIGNATURES.md's "0.2.1 fixes" section for upstream_error_text/
parse_context_overflow's robustness history.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("bridge")

@dataclass
class OverflowInfo:
    limit: int
    prompt_tokens: int | None
    fixable: bool
    total: int | None


def _coerce_text(value) -> str:
    """Best-effort str() that never raises, for a JSON field (an error
    message, error.metadata.raw, a text content-block's "text") that a
    misbehaving upstream may send as the wrong type -- a dict, null, a
    number -- instead of the expected string (finding 6)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def upstream_error_text(body) -> str:
    """Best-effort upstream error message, checked in order: OpenAI/OpenRouter
    error.message -> top-level message (a Databricks serving-endpoint 400 is
    shaped {"error_code":...,"message":...}, with NO nested "error" object at
    all) -> a bare string "error" field -> str(body). Accepts a dict, bytes,
    or str and never raises regardless of shape, e.g. a bare {"error":"boom"}
    used to crash every caller that assumed error was always a dict
    (findings 2 and 6)."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return body.decode("utf-8", "replace")
    if not isinstance(body, dict):
        return _coerce_text(body)
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            return msg
    msg = body.get("message")
    if isinstance(msg, str) and msg:
        return msg
    if isinstance(err, str) and err:
        return err
    return str(body)


def parse_context_overflow(status: int, err_msg: str, raw_meta: str | None,
                            requested_max_tokens: int | None = None) -> OverflowInfo | None:
    """Parse an upstream context-overflow error from OpenAI/vLLM, OpenRouter,
    error.metadata.raw, or Databricks wording. None if status != 400 or no
    limit found. `requested_max_tokens` (the max_tokens actually sent on the
    failing request) feeds two fixes: (a) finding 4's fallback derivation of
    the prompt-token count A = T - requested_max_tokens when a wording only
    ever states a total T with no per-part breakdown; (b) finding 1's floor
    below which a mathematically-fixable retry (A <= L) is still treated as
    unfixable, because the resulting retry budget (L - A - 256) would be too
    small to be a useful response (e.g. near/below zero when A sits close to
    L, or ~1 when a huge inlined image inflated A) -- better to let Claude
    Code compact than burn a round trip on a near-empty reply."""
    if status != 400:
        return None
    err_msg = _coerce_text(err_msg)
    raw_meta_text = _coerce_text(raw_meta)
    text = err_msg + ((" " + raw_meta_text) if raw_meta_text else "")

    limit = None
    prompt_tokens = None
    total = None

    # Databricks real wording: "... exceed context limit: A + B > L" -- A, B,
    # L all in one match. Tried FIRST: the older, looser "context limit ...
    # (\d+)" pattern below (kept as a fallback for other Databricks wordings)
    # matched A (6000) instead of L (8192) against this exact wording
    # (finding 3) since "max_tokens" and a number both appear right after
    # "context limit" in the sentence.
    dbx_full = re.search(r"exceed context limit:?\s*(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)", text)
    if dbx_full:
        prompt_tokens = int(dbx_full.group(1))
        limit = int(dbx_full.group(3))
    else:
        # Anthropic's native Messages API wording (H5 scope C): "prompt is
        # too long: N tokens > M maximum" -- N is the PROMPT total, M is
        # the limit; tried before the looser patterns below so it can't be
        # shadowed by e.g. "context limit ... (\d+)" matching the wrong
        # number out of the same sentence.
        anthropic_m = re.search(r"prompt is too long:\s*(\d+)\s*tokens\s*>\s*(\d+)\s*maximum", text)
        if anthropic_m:
            total = int(anthropic_m.group(1))
            limit = int(anthropic_m.group(2))
            return OverflowInfo(limit, total, False, total)  # Anthropic's own message already IS the total; never silently clamp-retried

        for pattern in (
            r"maximum context length is (\d+)",           # OpenAI/vLLM and OpenRouter (superset phrase)
            r"exceeded model token limit:\s*(\d+)",        # Kimi (coordinator research, deepseek_kimi_adapters.md)
            r"context limit(?: of)?[^\d]{0,20}?(\d+)",     # looser Databricks fallback wording
        ):
            m = re.search(pattern, text)
            if m:
                limit = int(m.group(1))
                break
        if limit is None:
            return None

        prompt_match = re.search(r"\((\d+) in the messages", text)
        if not prompt_match:
            prompt_match = re.search(r"\((\d+) of text input", text)  # OpenRouter (finding 4)
        if prompt_match:
            prompt_tokens = int(prompt_match.group(1))
        else:
            dbx_match = re.search(r"(\d+)\s*input tokens\s*\+\s*(\d+)\s*max_tokens", text)
            prompt_tokens = int(dbx_match.group(1)) if dbx_match else None

        total_match = re.search(r"requested about (\d+)", text)
        if not total_match:
            total_match = re.search(r"\(requested:\s*(\d+)\)", text)  # Kimi wording
        total = int(total_match.group(1)) if total_match else None

        # Fallback (finding 4): a wording that only ever states a total T with
        # no per-part breakdown -- derive A = T - requested max_tokens so a
        # fixable overflow is still recognized instead of an unconditional
        # compaction rewrite.
        if prompt_tokens is None and total is not None and isinstance(requested_max_tokens, int):
            prompt_tokens = total - requested_max_tokens

    # Only fixable (worth a silent max_tokens-clamped retry) if the PROMPT
    # itself still fits under the limit (a prompt that alone exceeds it can
    # never be fixed by shrinking max_tokens) AND the resulting retry budget
    # clears a sane floor (finding 1) -- min(requested_max_tokens, 4096), or
    # a flat 4096 if the original request's max_tokens isn't known.
    fixable = False
    if prompt_tokens is not None and prompt_tokens <= limit:
        retry_budget = limit - prompt_tokens - 256
        if isinstance(requested_max_tokens, int) and requested_max_tokens > 0:
            floor = min(requested_max_tokens, 4096)
        else:
            floor = 4096
        fixable = retry_budget >= floor
    return OverflowInfo(limit, prompt_tokens, fixable, total)


def build_prompt_too_long_message(total: int, limit: int) -> str:
    """Build prompt too long error message."""
    return f"prompt is too long: {max(total, limit + 1)} tokens > {limit} maximum"


def map_upstream_error(status: int, body: dict | bytes | str, provider: str,
                       resp_headers: dict | None = None) -> tuple[int, dict, dict]:
    """Map upstream error to client error."""
    # Parse message (finding 2/6: never assume body["error"] is a dict --
    # Databricks has no nested "error" object at all, and a bare
    # {"error": "boom"} used to crash this with AttributeError).
    msg = upstream_error_text(body)
    # Table mapping: (upstream_status, error_type, client_status, should_retry)
    table = [
        (401, "authentication_error", 401, False),
        (402, "permission_error", 402, False),
        (403, "permission_error", 403, False),
        (404, "not_found_error", 404, False),
        (400, "invalid_request_error", 400, False),
        # V2a fix: 413 had no row here, so it fell through to this
        # function's own "anything else" default of should_retry=True --
        # that default WINS at the caller (agent/loop.py's `e.retryable or
        # is_retryable_message(...)` short-circuits on a True left side),
        # so `is_context_overflow_message`'s own unconditional
        # `status == 413` -> overflow rule (never retryable) was never
        # actually reachable: a real 413 (a request too large to ever
        # succeed unmodified) was retried up to MAX_RETRIES times against
        # the identical failing body instead. 413 is never retryable in
        # place, matching every other 4xx client-error row above.
        (413, "invalid_request_error", 413, False),
        (429, "rate_limit_error", 429, True),
        (500, "api_error", 500, True),
        (502, "overloaded_error", 529, True),
        (503, "overloaded_error", 529, True),
        (504, "overloaded_error", 529, True),
    ]
    err_type = "api_error"
    client_status = status
    should_retry = True
    for st, et, cs, retry in table:
        if status == st:
            err_type, client_status, should_retry = et, cs, retry
            break
    else:
        if status >= 500:
            err_type, client_status, should_retry = "overloaded_error", 529, True
    # Build response
    extra_headers = {"x-should-retry": "true" if should_retry else "false"}
    if resp_headers:
        for k, v in resp_headers.items():
            if k.lower() == "retry-after":
                extra_headers["Retry-After"] = v
                break
    json_body = {"error": {"type": err_type, "message": msg}}
    return client_status, json_body, extra_headers


# ---- H1 scope D: error taxonomy, backoff ladder, DeepSeek reasoning-replay
# bug detector -- ALL additive (new names only, nothing above this line is
# touched), used by the harness's request/loop layer, never by the proxy.

AUTH = "AUTH"
RATE_LIMIT = "RATE_LIMIT"
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
STREAM_CLOSED = "STREAM_CLOSED"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
PROVIDER_FAILURE = "PROVIDER_FAILURE"

# Wordings that mean "this really is a context/quota overflow" even when no
# clean limit/prompt-token numbers can be pulled out of them (so
# `parse_context_overflow` -- which requires numbers -- returns None on
# them): DeepSeek's terse quota_limit_reached body, Kimi's two prose
# variants (coordinator research, deepseek_kimi_adapters.md rule 4).
_OVERFLOW_TAXONOMY_RE = re.compile(
    r"maximum context length is \d+"
    r"|exceeded model token limit"
    r"|input token exceed the limit"
    r"|input token length too long"
    r"|prompt tokens \+ max_tokens exceeds"
    r"|context limit"
    r"|input token count \(\d+\) exceeds the maximum"       # Gemini
    r"|maximum prompt length is \d+"                          # xAI
    r"|context window exceeds limit"                          # MiniMax (error 2013)
    r"|range of input length should be",                      # Qwen/DashScope
    re.IGNORECASE,
)

# Named hooks for behaviours the coordinator's per-family research flags as
# NOT data-driven (a row's "code_branch" key names one of these). Only the
# families H1 actually targets (DeepSeek/Kimi/GLM/Qwen/MiniMax) have real
# logic anywhere in this codebase; every other branch name is intentionally
# a documented no-op that logs once, per the coordinator's own scoping
# ("leave the rest as documented no-op branches that log") -- Gemini 3
# thought-signature replay, Mistral's `^[a-zA-Z0-9]{9}$` tool-id
# constraint, gpt-oss harmony reasoning rules, Grok encrypted reasoning,
# and Gemma's system-role/no-native-tools quirks all land here later.
_CODE_BRANCH_LOGGED: set = set()


def run_code_branch(name: Optional[str], **context) -> None:
    """Look up `name` in a (currently empty) registry of real per-family
    hooks; if unregistered, log once per process and return -- never
    raises, so an unimplemented branch degrades to "do nothing extra",
    never a crash."""
    if not name:
        return
    if name not in _CODE_BRANCH_LOGGED:
        _CODE_BRANCH_LOGGED.add(name)
        log.debug("code_branch %r has no implementation yet (documented no-op): %r", name, context)

_REASONING_REPLAY_BUG_RE = re.compile(
    r"reasoning_content.{0,60}must be passed back"
    r"|content\[\]\.thinking.{0,60}must be passed back",
    re.IGNORECASE | re.DOTALL,
)


class ReasoningReplayBug(Exception):
    """The upstream 400'd specifically because we failed to replay
    `reasoning_content` (DeepSeek exact wording: "The reasoning_content in
    the thinking mode must be passed back to the API."). This ALWAYS means
    a bug in OUR OWN request-builder's replay logic (providers/request.py),
    never a transient condition -- it must surface loudly, never be
    swallowed or silently retried like an ordinary 400."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def is_reasoning_replay_bug(message: str) -> bool:
    return bool(_REASONING_REPLAY_BUG_RE.search(message or ""))


def is_effort_with_tools_rejected_message(message: str) -> bool:
    """1.0.1 hotfix 22: the gpt-6 family's own live wording -- "Function
    tools with reasoning_effort are not supported for gpt-6-sol in
    /v1/chat/completions... set reasoning_effort to 'none'" -- a MORE
    SPECIFIC case than `is_effort_rejected_message` below: dropping the
    field entirely (that function's own retry) is not enough here, since
    the endpoint's own default is not `none` either; the retry for THIS
    message sets `reasoning_effort: "none"` explicitly instead. Checked
    before the more general function, which still owns every other effort-
    rejection wording (e.g. `output_config.effort`)."""
    text = message or ""
    # 1.0.1 fixpass finding 10: operator precedence made the old check
    # (`"reasoning_effort" in text and ("function tool" in text.lower() or
    # "param" in text and "reasoning_effort" in text)`) collapse to
    # `"reasoning_effort" in text and ("function tool" in text.lower() or
    # "param" in text)` -- ANY 400 naming both "reasoning_effort" and the
    # bare substring "param" (OpenAI's own generic "Unsupported parameter:
    # 'reasoning_effort'..." or "Invalid value for parameter reasoning_
    # effort: 'max'") matched this gpt-6-only check. The retry it triggers
    # (`reasoning_effort: "none"`) is invalid on those routes too, and
    # shared the SAME one-shot flag as the general strip-the-field retry
    # that would have worked -- so the turn failed outright instead of ever
    # trying that. Narrowed to require the real gpt-6 wording ("function
    # tool") -- never a bare "param".
    return "reasoning_effort" in text and "function tool" in text.lower()


_TOOLS_REJECTED_RE = re.compile(
    r'unknown field\s*"tools"'                                    # Databricks strict-allowlist 400
    r"|no endpoints found that support tool use"                   # OpenRouter routing 404
    r"|does not support (function|tool) calling",                  # generic provider wording
    re.IGNORECASE,
)


def is_tools_rejected_message(message: str) -> bool:
    """Halo 2.0.2 round 5 (Qwen-at-work brief, item 1/4): the upstream
    flatly refused `tools` rather than anything inside a schema/argument
    -- Databricks' own strict-body-allowlist wording for a field it never
    expected at all (`json: unknown field "tools"`, confirmed today
    against the real gateway's 400 shape -- see `profiles.
    DATABRICKS_BODY_ALLOWLIST`'s own docstring) or OpenRouter's documented
    `404 No endpoints found that support tool use` when routing lands on a
    tool-less provider (docs/harness/QWEN-RESEARCH.md §3h, via
    claude-code-router#409). Distinct from `ToolCatalogTooLarge` (a COUNT
    problem this harness catches before ever sending the request) and from
    any argument/schema-shaped 400 -- this is specifically "this endpoint
    does not do tool calling at all," the live-400 twin of a decision-only
    row's `capabilities.decision_only`/`ProviderProfile.tools_supported`
    for an endpoint that had no row (or pattern match) to tell Halo that
    up front. `agent/loop.py`'s `_step` calls `providers.learned_rules.
    learn_tools_rejected` the first time this fires for a Databricks
    endpoint, so the NEXT request against it never pays for the same
    round trip -- `resolve_profile` consults that cache via `tools_
    supported` before a request is even built."""
    return bool(_TOOLS_REJECTED_RE.search(message or ""))


def is_effort_rejected_message(message: str) -> bool:
    """1.0.1 hotfix 19.3: the upstream 400'd specifically on the effort
    field this harness put in the body -- verified wording on a Databricks
    Claude foundation endpoint: `output_config.effort: Input should be
    'low', 'medium', 'high' or 'max'` (an `xhigh` that reached the wire
    despite `clamp_effort`, e.g. a route whose accepted set this harness
    hasn't modeled correctly yet) -- a chat-dialect route's own
    `reasoning_effort` field can 400 the same way on a value it doesn't
    recognize. `agent/loop.py::_step`'s wire_error branch retries ONCE with
    every effort-related field stripped from the body when this matches,
    rather than failing the turn outright over a field that's genuinely
    optional on every dialect (omitting it always falls back to the
    provider's own default).

    1.0.1 fixpass finding 13: two more shapes, neither naming the field the
    same way the wordings above do -- (a) OpenRouter routes the value
    through a nested `reasoning.effort` path (a dot, not the flat
    `reasoning_effort` this check already looks for -- "reasoning.effort"
    is NOT a substring of "reasoning_effort" or vice versa); (b) a plain
    OpenAI-style enum-validation 400 for a value this harness's own
    `effort_values_supported` let through anyway (finding 13's own root
    cause: a chat-dialect profile used to accept "max", which is an
    Anthropic-only level) commonly reads "Invalid value: 'max'. Supported
    values are: 'low', 'medium', and 'high'." with no field name in the
    message text at all (`upstream_error_text` only ever keeps "message",
    dropping the response's separate "param" field) -- matched here by
    "invalid value" together with one of the two out-of-range values this
    harness itself could have sent ('max'/'xhigh'), never a bare "invalid
    value" alone (which would misfire on an unrelated 400, e.g. a bad
    max_tokens or model name)."""
    text = message or ""
    low = text.lower()
    if "output_config.effort" in text or "reasoning_effort" in text or "reasoning.effort" in low:
        return True
    return "invalid value" in low and ("'max'" in low or "'xhigh'" in low)


def classify_error_category(status: int, message: str) -> str:
    """Map a wire failure to the dsh-style taxonomy (scope D). Pure
    classification -- never raises, never mutates the message; distinct
    from (and doesn't replace) `map_upstream_error`'s client-response
    shape, which the proxy still owns unchanged."""
    message = message or ""
    if is_reasoning_replay_bug(message):
        return PROVIDER_FAILURE  # named separately via ReasoningReplayBug at the raise site
    if status in (401, 403):
        return AUTH
    if status == 429:
        return RATE_LIMIT
    # V2a: a literal 413 is unconditionally an overflow (matching
    # `is_context_overflow_message`'s own `status == 413` rule) -- it never
    # needs a wording match the way a 400 does, since "the payload itself
    # was too large" is the whole meaning of that status code.
    if status == 413 or (status == 400 and _OVERFLOW_TAXONOMY_RE.search(message)):
        return CONTEXT_WINDOW_EXCEEDED
    if status >= 500:
        return PROVIDER_FAILURE
    return MALFORMED_RESPONSE if status == 400 else PROVIDER_FAILURE


def parse_databricks_rate_limit(body) -> dict:
    """Extract {limit_type, retry_after, limit, current} from a Databricks
    429 body `{"error":{"message","type","code","limit_type","limit",
    "current","retry_after"}}`. {} if `body` doesn't have this shape."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return {}
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return {}
    return {k: err[k] for k in ("limit_type", "retry_after", "limit", "current") if k in err}


_BACKOFF_LADDER = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_RETRIES = 5


def backoff_delay(attempt: int, retry_after=None) -> float:
    """1-2-4-8-16s ladder (scope D), `attempt` is 0-based and clamps to the
    ladder's last rung past 5 attempts; a provider-supplied `retry_after`
    (seconds, string or number) is ADDED on top rather than replacing the
    ladder, so a 429 with a short Retry-After still backs off at least as
    much as an ordinary failure would."""
    base = _BACKOFF_LADDER[max(0, min(attempt, len(_BACKOFF_LADDER) - 1))]
    extra = 0.0
    if retry_after is not None:
        try:
            extra = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            extra = 0.0
    return base + extra


# ---------------------------------------------------------------------------
# H5 scope F items 2/4 (OpenCode Appendix A/B, reports/OpenCode harness deep
# review.md) -- the message-pattern retry classifier and the merged
# context-overflow regex list, ADDITIVE to everything above (classify_error_
# category/backoff_delay/_OVERFLOW_TAXONOMY_RE are unchanged and still used
# exactly as before by every existing caller).
# ---------------------------------------------------------------------------

# Appendix A, verbatim (8 of OpenCode's own ~27, captured in the review)
# unioned with halo's existing _OVERFLOW_TAXONOMY_RE patterns and the
# adapter-rules report's per-host regexes -- every pattern that means "this
# is a context/quota overflow", from every source, in one place.
OVERFLOW_PATTERNS = [
    # OpenCode provider-error.ts (verbatim, 8 of ~27)
    r"prompt is too long", r"request_too_large", r"exceeds the context window",
    r"maximum context length is \d+ tokens", r"context[_ ]length[_ ]exceeded",
    r"too many tokens", r"token limit exceeded", r"model_context_window_exceeded",
    # halo errors.py (existing _OVERFLOW_TAXONOMY_RE, unioned in)
    r"maximum context length is \d+", r"exceeded model token limit",
    r"input token exceed the limit", r"input token length too long",
    r"prompt tokens \+ max_tokens exceeds", r"context limit",
    r"input token count \(\d+\) exceeds the maximum",      # Gemini
    r"maximum prompt length is \d+",                         # xAI
    r"context window exceeds limit",                         # MiniMax 2013
    r"range of input length should be",                      # DashScope
    # adapter-rules report (per host)
    r"maximum context length is (\d+) tokens\. However, you requested about (\d+) tokens",  # OpenRouter
    r"maximum context length is (\d+) tokens\. However, you requested (\d+) tokens",        # vLLM / DeepSeek
    r"quota_limit_reached", r"\"code\"\s*:\s*\"?1261\"?", r"Prompt too long",              # DeepSeek terse, Z.ai
    r"prompt is too long: (\d+) tokens > (\d+) maximum",                                     # Anthropic
]
OVERFLOW_EXCLUSIONS = [r"rate limit", r"too many requests", r"^(throttling error|service unavailable):"]

MERGED_OVERFLOW_RE = re.compile("|".join(OVERFLOW_PATTERNS), re.IGNORECASE)
_OVERFLOW_EXCLUSIONS_RE = re.compile("|".join(OVERFLOW_EXCLUSIONS), re.IGNORECASE)


def is_context_overflow_message(status: int, message: str, body: Optional[dict] = None) -> bool:
    """Appendix A's rule, verbatim: classify as overflow if `status == 413`,
    or `body.error.code == "context_length_exceeded"`, or any pattern
    matches AND no exclusion matches. Never retry this class -- hand it to
    compaction (see `retryable` below, which checks this FIRST)."""
    if status == 413:
        return True
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code") == "context_length_exceeded":
            return True
    message = message or ""
    if _OVERFLOW_EXCLUSIONS_RE.search(message):
        return False
    return bool(MERGED_OVERFLOW_RE.search(message))


# Appendix B, verbatim.
RETRY_INITIAL_DELAY_MS = 2000
RETRY_BACKOFF_FACTOR = 2
RETRY_JITTER_FACTOR = 0.25
RETRY_MAX_DELAY_NO_HEADERS_MS = 30_000
RETRY_MAX_DELAY_MS = 2_147_483_647          # only reachable via a Retry-After header
RETRY_MAX_RETRIES = 5

RETRYABLE_MESSAGE_PATTERNS = [               # all case-insensitive
    r"429|500|502|503|504|524",
    r"rate increased too quickly|rate limit|rate-limit|rate_limit|too many requests",
    r"overloaded|service unavailable|internal error|provider returned error|provider_returned_error",
    r"terminated|fetch failed|socket hang up|connection refused|econnrefused|econnreset|etimedout",
    r"^timeout$|\b(?:request|response|connection|network|stream|read) (?:timeout|timed out|time out)\b",
    r"try your request again|retry your request|resource exhausted|resource_exhausted",
    r"\btry again (?:later|in\b)|\b(?:currently|temporarily) at capacity\b",
]
_RETRYABLE_MESSAGE_RE = re.compile("|".join(RETRYABLE_MESSAGE_PATTERNS), re.IGNORECASE)


def is_retryable_message(status: Optional[int], message: str, *, body_text: str = "",
                          host: Optional[str] = None, raw_finish_reason: Optional[str] = None,
                          kind: Optional[str] = None) -> bool:
    """`retryable()`, Appendix B: never retries CONTEXT_WINDOW_EXCEEDED;
    always retries >=500 or 429; OpenAI's own occasional spurious 404
    ("sometimes returns 404 for models that are actually available") is
    retryable only for that host; `raw_finish_reason == "network_error"`
    (an SDK-level signal, not an HTTP status) and `kind` in
    ("header_timeout", "sse_read_timeout") (the watchdog below) are always
    retryable; otherwise falls through to the message-pattern list."""
    if is_context_overflow_message(status or 0, message):
        return False
    if isinstance(status, int) and (status >= 500 or status == 429):
        return True
    if status == 404 and host == "openai":
        return True
    if raw_finish_reason == "network_error":
        return True
    if kind in ("header_timeout", "sse_read_timeout"):
        return True
    text = f"{message or ''} {body_text or ''}"
    return bool(_RETRYABLE_MESSAGE_RE.search(text))


def _parse_retry_after_seconds(value) -> Optional[float]:
    """H8 cheap must-do: `anthropic-ratelimit-*-reset` is an RFC 3339
    timestamp (e.g. "2024-01-01T00:00:00Z"), which `email.utils.
    parsedate_to_datetime` (RFC 2822 HTTP-date, e.g. "Wed, 21 Oct 2015
    07:28:00 GMT" -- the ordinary `Retry-After` header's own format)
    rejects outright -- `stream.py`'s own 429 handling copies that header's
    raw value into the SAME `Retry-After` slot this function parses
    whenever the response used the Anthropic-specific header instead of
    (or in addition to) the standard one, so both shapes have to work
    here. Tried as a fallback, never instead of, the HTTP-date parse
    (which stays tried first since it's what the vast majority of real
    `Retry-After` headers actually send)."""
    from email.utils import parsedate_to_datetime
    import datetime as _dt
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    dt = None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            # `datetime.fromisoformat` doesn't accept a bare trailing "Z"
            # on every Python version this harness supports -- normalized
            # to the equivalent explicit "+00:00" offset first.
            iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
            dt = _dt.datetime.fromisoformat(iso_text)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds())


def retry_delay_ms(attempt: int, headers: Optional[dict] = None) -> int:
    """Appendix B `delay_ms`, verbatim precedence: `retry-after-ms` (any
    case) first, then `retry-after` (seconds, or an HTTP date), else
    `2000 * 2^(attempt-1)` with 25% jitter, capped at 30s when no header is
    present. `attempt` is 1-based. A header-driven delay is capped only by
    the effectively-unbounded `RETRY_MAX_DELAY_MS`, never by the 30s no-
    header cap -- a provider that asks for a longer wait is honoured."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if "retry-after-ms" in headers:
        try:
            return min(int(float(headers["retry-after-ms"])), RETRY_MAX_DELAY_MS)
        except (TypeError, ValueError):
            pass
    if "retry-after" in headers:
        secs = _parse_retry_after_seconds(headers["retry-after"])
        if secs is not None:
            return min(int(secs * 1000), RETRY_MAX_DELAY_MS)
    base = RETRY_INITIAL_DELAY_MS * (RETRY_BACKOFF_FACTOR ** max(0, attempt - 1))
    jitter = base * RETRY_JITTER_FACTOR * random.random()
    return min(int(base + jitter), RETRY_MAX_DELAY_NO_HEADERS_MS)


# ---------------------------------------------------------------------------
# H5 scope F item 3: SSE chunk-idle watchdog constants (OpenCode: 300s header
# timeout, 300s between-chunk timeout on a text/event-stream body). The
# watchdog ITSELF lives in providers/stream.py (it needs the live socket/
# queue), these are its shared, testable constants + the classification it
# raises for `is_retryable_message`'s `kind=` parameter above.
# ---------------------------------------------------------------------------

SSE_HEADER_TIMEOUT_S = 300.0
SSE_CHUNK_IDLE_TIMEOUT_S = 300.0


class SSEChunkIdleTimeout(Exception):
    """Raised when no SSE chunk arrives for `SSE_CHUNK_IDLE_TIMEOUT_S`
    seconds -- classified `kind="sse_read_timeout"`, always retryable per
    `is_retryable_message` above."""


def flatten_content_parts(content) -> str:
    """Flatten a Databricks-style list of {'type':'text'|'reasoning',...} content parts into plain text; reasoning parts are logged at DEBUG with their length and never emitted."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            # finding 6: a malformed upstream can send {"type":"text","text":null}
            # -- part.get("text", "") only substitutes the default when the KEY
            # is missing, not when it's present-but-null, and "".join() on a
            # None item raises TypeError inside this stream loop.
            texts.append(_coerce_text(part.get("text")))
        elif ptype == "reasoning":
            for s in part.get("summary") or []:
                if isinstance(s, dict):
                    rtext = s.get("text", "")
                    log.debug("databricks reasoning part (not emitted), length=%d", len(rtext))
    return "".join(texts)

