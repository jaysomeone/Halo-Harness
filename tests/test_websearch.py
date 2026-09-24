"""tests.test_websearch -- H4 scope E: the WebSearch tool (tools/websearch.
py), mocked against tests/helpers/mock_openai.py's MockUpstream (a NEW,
non-streaming `send_json_response` scenario -- WebSearch's side call sets
`"stream": false` and expects one plain JSON chat-completions reply, unlike
every other scenario in that file, which is SSE-streamed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, send_json_response
from rolo_claude.providers.stream import ProviderCreds
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.websearch import WebSearchTool, build_websearch_tool, extract_url_citations

test, TESTS = new_registry()


def _scn_ok(h, body):
    send_json_response(h, 200, {
        "choices": [{"message": {
            "content": "DeepSeek V4 pricing is $0.28/$0.42 per million tokens.",
            "annotations": [
                {"type": "url_citation", "url_citation": {"url": "https://openrouter.ai/models/deepseek", "title": "DeepSeek on OpenRouter"}},
                {"type": "not_url_citation", "other": "ignored"},
            ],
        }}],
    })


def _scn_no_citations(h, body):
    send_json_response(h, 200, {"choices": [{"message": {"content": "plain answer, no sources"}}]})


def _scn_error(h, body):
    send_json_response(h, 200, {"error": {"message": "search backend unavailable"}})


def _scn_http_500(h, body):
    send_json_response(h, 500, {"error": {"message": "internal error"}})


SCENARIOS["websearch-ok"] = _scn_ok
SCENARIOS["websearch-no-citations"] = _scn_no_citations
SCENARIOS["websearch-error"] = _scn_error
SCENARIOS["websearch-500"] = _scn_http_500


def _tool_for(mock, scenario: str) -> WebSearchTool:
    return WebSearchTool(base_url=mock.base_url, api_key="test-key", model=f"mock/{scenario}")


@test
def test_websearch_returns_answer_and_citations(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        tool = _tool_for(mock, "websearch-ok")
        result = tool.run({"query": "current DeepSeek V4 pricing"}, ToolContext(cwd="."))
        ctx.check(f"not an error, got {result.content!r}", not result.is_error)
        ctx.check("answer text present", "DeepSeek V4 pricing" in result.content)
        ctx.check("citation URL present", "https://openrouter.ai/models/deepseek" in result.content)
        ctx.check("citation title present", "DeepSeek on OpenRouter" in result.content)
        ctx.check("request carried the web plugin", mock.requests[0]["body"]["plugins"] == [{"id": "web"}])
        ctx.check("request was non-streaming", mock.requests[0]["body"]["stream"] is False)
    finally:
        mock.stop()


@test
def test_websearch_no_citations_still_returns_answer(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        tool = _tool_for(mock, "websearch-no-citations")
        result = tool.run({"query": "anything"}, ToolContext(cwd="."))
        ctx.check(f"not an error, got {result.content!r}", not result.is_error)
        ctx.check("answer text present, no Sources section", result.content.strip() == "plain answer, no sources")
    finally:
        mock.stop()


@test
def test_websearch_upstream_error_field_is_reported(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        tool = _tool_for(mock, "websearch-error")
        result = tool.run({"query": "anything"}, ToolContext(cwd="."))
        ctx.check("is an error", result.is_error)
        ctx.check(f"names the backend error, got {result.content!r}", "search backend unavailable" in result.content)
    finally:
        mock.stop()


@test
def test_websearch_http_500_is_reported(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        tool = _tool_for(mock, "websearch-500")
        result = tool.run({"query": "anything"}, ToolContext(cwd="."))
        ctx.check("is an error", result.is_error)
        ctx.check(f"names HTTP 500, got {result.content!r}", "500" in result.content)
    finally:
        mock.stop()


@test
def test_websearch_missing_query_is_error(ctx: Ctx):
    tool = WebSearchTool(base_url="http://x", api_key="k", model="m")
    result = tool.run({}, ToolContext(cwd="."))
    ctx.check("missing query is an error", result.is_error)


@test
def test_extract_url_citations_filters_malformed_entries(ctx: Ctx):
    out = extract_url_citations([
        {"type": "url_citation", "url_citation": {"url": "https://a.example", "title": "A"}},
        {"type": "url_citation", "url_citation": {"title": "no url, skipped"}},
        "not a dict",
        None,
        {"type": "url_citation"},  # no url_citation sub-dict
    ])
    ctx.check(f"only the one valid entry survives, got {out}", out == [{"url": "https://a.example", "title": "A"}])


@test
def test_build_websearch_tool_only_for_openrouter_with_creds(ctx: Ctx):
    creds = ProviderCreds(base_url="http://x/api/v1", api_key="k")
    t1 = build_websearch_tool(main_provider="openrouter", creds=creds, small_model_raw="small/m", main_model_raw="main/m")
    ctx.check("built for openrouter+creds", t1 is not None and t1.model == "small/m")

    t2 = build_websearch_tool(main_provider="openrouter", creds=creds, small_model_raw=None, main_model_raw="main/m")
    ctx.check("falls back to main model when no small model", t2 is not None and t2.model == "main/m")

    t3 = build_websearch_tool(main_provider="databricks", creds=creds, small_model_raw="s", main_model_raw="m")
    ctx.check("absent for non-openrouter provider", t3 is None)

    t4 = build_websearch_tool(main_provider="openrouter", creds=None, small_model_raw="s", main_model_raw="m")
    ctx.check("absent when creds are unresolved", t4 is None)


@test
def test_websearch_summary_and_permission_content(ctx: Ctx):
    tool = WebSearchTool(base_url="http://x", api_key="k", model="m")
    ctx.check("summary", tool.summary({"query": "foo"}) == "WebSearch('foo')")
    ctx.check("permission_content", tool.permission_content({"query": "foo"}) == "foo")
    ctx.check("is_read_only", tool.is_read_only is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
