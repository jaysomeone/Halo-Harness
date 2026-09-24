"""rolo_claude.providers.errors -- upstream error-body parsing, the
context-overflow detector, and the OpenAI/Databricks -> Anthropic error
mapping table. Moved out of bridge.py unchanged in the H0 package split; see
wip/SIGNATURES.md's "0.2.1 fixes" section for upstream_error_text/
parse_context_overflow's robustness history.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

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
    if status == 400 and _OVERFLOW_TAXONOMY_RE.search(message):
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

