"""tests.test_errors_taxonomy -- providers/errors.py scope D additions:
overflow wordings (DeepSeek/OpenAI/OpenRouter/Databricks/Kimi) map to the
correct taxonomy category, the DeepSeek reasoning-replay 400 is named
distinctly, the backoff ladder, and Databricks 429 field extraction.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.providers.errors import (
    AUTH, CONTEXT_WINDOW_EXCEEDED, MAX_RETRIES, PROVIDER_FAILURE, RATE_LIMIT,
    backoff_delay, classify_error_category, is_reasoning_replay_bug,
    parse_context_overflow, parse_databricks_rate_limit,
)

test, TESTS = new_registry()

_OVERFLOW_WORDINGS = {
    "openai": "This model's maximum context length is 128000 tokens. However, you requested 130000 tokens (129000 in the messages, 1000 in the completion).",
    "deepseek": "This model's maximum context length is 1048576 tokens. However, you requested 1787370 tokens (1403370 in the messages, 384000 in the completion)",
    "deepseek_terse": "Input token exceed the limit 65536",
    "openrouter": "This endpoint's maximum context length is 65536 tokens. However, you requested about 71000 tokens (68000 of text input, 3000 in the output).",
    "databricks": "Bad Request: exceed context limit: 6000 + 4096 > 8192",
    "kimi": "Your request exceeded model token limit: 262144 (requested: 269030)",
}


@test
def test_overflow_wordings_classify_as_context_window_exceeded(ctx: Ctx):
    for name, message in _OVERFLOW_WORDINGS.items():
        category = classify_error_category(400, message)
        ctx.check(f"{name!r} wording classifies as CONTEXT_WINDOW_EXCEEDED, got {category!r}", category == CONTEXT_WINDOW_EXCEEDED)


@test
def test_kimi_and_deepseek_wordings_parse_numerically_too(ctx: Ctx):
    """The NUMERIC extraction pipeline (parse_context_overflow, used for
    the fixable-retry decision) also recognizes the new wordings, not just
    the taxonomy classifier."""
    kimi = parse_context_overflow(400, _OVERFLOW_WORDINGS["kimi"], None, requested_max_tokens=8000)
    ctx.check(f"Kimi limit parsed, got {kimi.limit if kimi else None}", kimi is not None and kimi.limit == 262144)
    ctx.check(f"Kimi total (requested) parsed, got {kimi.total if kimi else None}", kimi is not None and kimi.total == 269030)

    deepseek = parse_context_overflow(400, _OVERFLOW_WORDINGS["deepseek"], None)
    ctx.check(f"DeepSeek limit parsed, got {deepseek.limit if deepseek else None}", deepseek is not None and deepseek.limit == 1048576)
    ctx.check(f"DeepSeek prompt_tokens parsed, got {deepseek.prompt_tokens if deepseek else None}", deepseek is not None and deepseek.prompt_tokens == 1403370)


@test
def test_auth_and_rate_limit_classification(ctx: Ctx):
    ctx.check("401 -> AUTH", classify_error_category(401, "invalid api key") == AUTH)
    ctx.check("403 -> AUTH", classify_error_category(403, "forbidden") == AUTH)
    ctx.check("429 -> RATE_LIMIT", classify_error_category(429, "rate limit exceeded") == RATE_LIMIT)
    ctx.check("500 -> PROVIDER_FAILURE", classify_error_category(500, "internal error") == PROVIDER_FAILURE)


@test
def test_reasoning_replay_bug_named_distinctly(ctx: Ctx):
    deepseek_chat_wording = "The `reasoning_content` in the thinking mode must be passed back to the API."
    deepseek_anthropic_wording = "The `content[].thinking` in the thinking mode must be passed back to the API."
    ctx.check("DeepSeek Chat Completions wording detected", is_reasoning_replay_bug(deepseek_chat_wording) is True)
    ctx.check("DeepSeek Anthropic-route wording detected", is_reasoning_replay_bug(deepseek_anthropic_wording) is True)
    ctx.check("an ordinary error is NOT a reasoning-replay bug", is_reasoning_replay_bug("rate limit exceeded") is False)
    ctx.check("classify_error_category also recognizes it (as PROVIDER_FAILURE, never CONTEXT_WINDOW_EXCEEDED)",
              classify_error_category(400, deepseek_chat_wording) == PROVIDER_FAILURE)


@test
def test_backoff_ladder(ctx: Ctx):
    ctx.check("attempt 0 -> 1s", backoff_delay(0) == 1.0)
    ctx.check("attempt 1 -> 2s", backoff_delay(1) == 2.0)
    ctx.check("attempt 2 -> 4s", backoff_delay(2) == 4.0)
    ctx.check("attempt 3 -> 8s", backoff_delay(3) == 8.0)
    ctx.check("attempt 4 -> 16s", backoff_delay(4) == 16.0)
    ctx.check("attempt 99 clamps to the last rung (16s), never grows unbounded", backoff_delay(99) == 16.0)
    ctx.check("retry_after is ADDED on top of the ladder", backoff_delay(0, retry_after=5) == 6.0)
    ctx.check("a garbage retry_after is ignored, not a crash", backoff_delay(0, retry_after="not-a-number") == 1.0)
    ctx.check("MAX_RETRIES is 5", MAX_RETRIES == 5)


@test
def test_parse_databricks_rate_limit(ctx: Ctx):
    body = {"error": {"message": "Rate limit exceeded", "type": "rate_limit_exceeded", "code": 429,
                       "limit_type": "input_tokens_per_minute", "limit": 200000, "current": 200150, "retry_after": 15}}
    parsed = parse_databricks_rate_limit(body)
    ctx.check(f"limit_type extracted, got {parsed.get('limit_type')!r}", parsed.get("limit_type") == "input_tokens_per_minute")
    ctx.check(f"retry_after extracted, got {parsed.get('retry_after')!r}", parsed.get("retry_after") == 15)
    ctx.check("a non-matching shape returns {}", parse_databricks_rate_limit({"error_code": "BAD_REQUEST"}) == {})
    ctx.check("a bare string body never raises", parse_databricks_rate_limit("not a dict") == {})


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
