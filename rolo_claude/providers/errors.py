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

