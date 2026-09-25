"""tests.test_retry_classifier -- providers/errors.py H5 scope F items 2-4:
the merged context-overflow regex list (Appendix A), the message-pattern
retry classifier + jittered backoff (Appendix B), and the SSE chunk-idle
watchdog constants (item 3)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.providers.errors import (
    RETRY_INITIAL_DELAY_MS, RETRY_MAX_DELAY_NO_HEADERS_MS, RETRY_MAX_RETRIES, SSE_CHUNK_IDLE_TIMEOUT_S,
    SSE_HEADER_TIMEOUT_S, is_context_overflow_message, is_retryable_message, retry_delay_ms,
)

test, TESTS = new_registry()

# ---------------------------------------------------------------------------
# Overflow classifier merge (Appendix A)
# ---------------------------------------------------------------------------

_OVERFLOW_WORDINGS = [
    "prompt is too long: 5000 tokens > 4000 maximum",                       # Anthropic
    "This model's maximum context length is 8192 tokens.",                   # OpenAI/vLLM
    "request_too_large: reduce the size of your request",                    # OpenCode verbatim
    "exceeds the context window of this model",                              # OpenCode verbatim
    "context_length_exceeded",                                               # OpenCode verbatim (snake_case)
    "too many tokens in the request",                                        # OpenCode verbatim
    "token limit exceeded for this model",                                   # OpenCode verbatim
    "model_context_window_exceeded",                                        # OpenCode verbatim
    "exceeded model token limit: 4096",                                      # Kimi (rolo-claude existing)
    "input token count (500000) exceeds the maximum",                        # Gemini
    "context window exceeds limit",                                         # MiniMax 2013
    "quota_limit_reached",                                                  # DeepSeek terse
]


@test
def test_overflow_patterns_all_recognized(ctx: Ctx):
    for wording in _OVERFLOW_WORDINGS:
        ctx.check(f"recognized as overflow: {wording!r}", is_context_overflow_message(400, wording) is True)


@test
def test_overflow_status_413_always_overflow(ctx: Ctx):
    ctx.check("HTTP 413 is always overflow regardless of message text",
              is_context_overflow_message(413, "irrelevant body text") is True)


@test
def test_overflow_error_code_context_length_exceeded(ctx: Ctx):
    body = {"error": {"code": "context_length_exceeded", "message": "something else entirely"}}
    ctx.check("error.code == context_length_exceeded is always overflow",
              is_context_overflow_message(400, "unrelated text", body) is True)


@test
def test_overflow_exclusions_win(ctx: Ctx):
    ctx.check("a rate-limit message is never classified as overflow",
              is_context_overflow_message(400, "rate limit exceeded on context window endpoint") is False)
    ctx.check("a too-many-requests message is never overflow",
              is_context_overflow_message(400, "too many requests, please slow down (context limit noted)") is False)


@test
def test_ordinary_message_not_overflow(ctx: Ctx):
    ctx.check("an unrelated error is not overflow", is_context_overflow_message(400, "invalid API key") is False)


# ---------------------------------------------------------------------------
# Retry classifier (Appendix B)
# ---------------------------------------------------------------------------

@test
def test_retryable_status_ge_500(ctx: Ctx):
    for status in (500, 502, 503, 504, 529):
        ctx.check(f"status {status} is retryable", is_retryable_message(status, "server error") is True)


@test
def test_retryable_429(ctx: Ctx):
    ctx.check("429 is retryable", is_retryable_message(429, "rate limited") is True)


@test
def test_overflow_never_retryable_even_with_5xx_wording(ctx: Ctx):
    ctx.check("context overflow is NEVER retried, even at 400 with overflow wording",
              is_retryable_message(400, "prompt is too long: 9000 tokens > 8000 maximum") is False)


@test
def test_openai_404_retryable_only_for_openai_host(ctx: Ctx):
    ctx.check("openai 404 IS retryable (spurious 404 for a real model)",
              is_retryable_message(404, "model not found", host="openai") is True)
    ctx.check("the SAME 404 on a different host is NOT specially retryable",
              is_retryable_message(404, "model not found", host="openrouter") is False)


@test
def test_network_error_finish_reason_always_retryable(ctx: Ctx):
    ctx.check("raw_finish_reason=network_error is always retryable",
              is_retryable_message(200, "", raw_finish_reason="network_error") is True)


@test
def test_watchdog_kinds_always_retryable(ctx: Ctx):
    ctx.check("header_timeout kind is retryable", is_retryable_message(None, "", kind="header_timeout") is True)
    ctx.check("sse_read_timeout kind is retryable", is_retryable_message(None, "", kind="sse_read_timeout") is True)


@test
def test_retryable_message_pattern_table(ctx: Ctx):
    cases = [
        "the connection was terminated unexpectedly",
        "fetch failed: network error",
        "ECONNRESET: socket hang up",
        "request timeout after 30s",
        "resource_exhausted: please try again",
        "the service is temporarily at capacity",
        "please try your request again",
        "rate limit exceeded, back off",
        "the model is currently overloaded",
    ]
    for msg in cases:
        ctx.check(f"retryable by message pattern: {msg!r}", is_retryable_message(400, msg) is True)


@test
def test_non_retryable_ordinary_400(ctx: Ctx):
    ctx.check("an ordinary validation 400 is not retryable",
              is_retryable_message(400, "invalid request: missing required field 'model'") is False)


@test
def test_retryable_checks_body_text_too(ctx: Ctx):
    ctx.check("a retryable pattern found only in body_text still counts",
              is_retryable_message(400, "", body_text="upstream terminated the connection") is True)


# ---------------------------------------------------------------------------
# Backoff / jitter (Appendix B `delay_ms`)
# ---------------------------------------------------------------------------

@test
def test_delay_ms_exponential_growth_without_headers(ctx: Ctx):
    d1 = retry_delay_ms(1, {})
    d2 = retry_delay_ms(2, {})
    d3 = retry_delay_ms(3, {})
    # base sequence (pre-jitter): 2000, 4000, 8000 -- jitter adds 0-25%.
    ctx.check(f"attempt 1 base ~2000ms (+0-25% jitter), got {d1}", RETRY_INITIAL_DELAY_MS <= d1 <= RETRY_INITIAL_DELAY_MS * 1.25)
    ctx.check(f"attempt 2 base ~4000ms (+0-25% jitter), got {d2}", 4000 <= d2 <= 5000)
    ctx.check(f"attempt 3 base ~8000ms (+0-25% jitter), got {d3}", 8000 <= d3 <= 10000)


@test
def test_delay_ms_capped_at_30s_without_headers(ctx: Ctx):
    d = retry_delay_ms(20, {})  # would be enormous uncapped
    ctx.check(f"capped at {RETRY_MAX_DELAY_NO_HEADERS_MS}ms without headers, got {d}", d <= RETRY_MAX_DELAY_NO_HEADERS_MS)


@test
def test_delay_ms_retry_after_ms_header_wins(ctx: Ctx):
    ctx.check("retry-after-ms is used verbatim (ms)", retry_delay_ms(1, {"retry-after-ms": "1234"}) == 1234)
    ctx.check("retry-after-ms can exceed the no-header 30s cap (a header IS present)",
              retry_delay_ms(1, {"retry-after-ms": "45000"}) == 45000)


@test
def test_delay_ms_retry_after_seconds_header(ctx: Ctx):
    ctx.check("retry-after in seconds -> ms", retry_delay_ms(1, {"retry-after": "5"}) == 5000)


@test
def test_delay_ms_retry_after_ms_beats_retry_after_seconds(ctx: Ctx):
    d = retry_delay_ms(1, {"retry-after-ms": "700", "retry-after": "9"})
    ctx.check(f"retry-after-ms takes precedence over retry-after, got {d}", d == 700)


@test
def test_delay_ms_header_keys_case_insensitive(ctx: Ctx):
    ctx.check("header lookups are case-insensitive", retry_delay_ms(1, {"Retry-After-Ms": "111"}) == 111)


@test
def test_max_retries_constant(ctx: Ctx):
    ctx.check("Appendix B's max retries is 5", RETRY_MAX_RETRIES == 5)


@test
def test_h8_retry_after_rfc3339_anthropic_ratelimit_reset_header(ctx: Ctx):
    """H8 cheap must-do: `anthropic-ratelimit-*-reset` is an RFC 3339
    timestamp ("2024-01-01T00:00:05Z"-shaped) copied into the SAME
    `Retry-After` slot an ordinary HTTP-date header would use (see
    providers/stream.py's 429 handling) -- `parsedate_to_datetime` (RFC
    2822 HTTP-date) rejects that format outright, so it must fall back to
    `datetime.fromisoformat`."""
    import datetime as _dt
    future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=5)
    rfc3339 = future.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    d = retry_delay_ms(1, {"retry-after": rfc3339})
    ctx.check(f"RFC 3339 'Z' timestamp parsed to ~5000ms, got {d}", 3500 <= d <= 6500)

    rfc3339_offset = future.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    d2 = retry_delay_ms(1, {"retry-after": rfc3339_offset})
    ctx.check(f"RFC 3339 explicit-offset timestamp also parsed, got {d2}", 3500 <= d2 <= 6500)

    ctx.check("an ordinary HTTP-date Retry-After still works (regression check)",
              retry_delay_ms(1, {"retry-after": "5"}) == 5000)

    ctx.check("a genuinely unparseable value falls back to the exponential-backoff default, not a crash",
              retry_delay_ms(1, {"retry-after": "not-a-date-at-all"}) > 0)


# ---------------------------------------------------------------------------
# SSE watchdog constants
# ---------------------------------------------------------------------------

@test
def test_sse_watchdog_constants(ctx: Ctx):
    ctx.check("300s header timeout (OpenCode default)", SSE_HEADER_TIMEOUT_S == 300.0)
    ctx.check("300s chunk-idle timeout (OpenCode default)", SSE_CHUNK_IDLE_TIMEOUT_S == 300.0)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
