"""rolo_claude.tools.websearch -- the WebSearch tool (H4 scope E). Only
ever constructed/registered when the session's MAIN provider is OpenRouter
(D-CFG: "registered only when the main provider is OpenRouter ... on other
providers the tool is absent -- Claude Code's own WebSearch is server-side;
we document the difference" -- see `build_websearch_tool` below, called
from the session-construction call sites in headless.py/tui/bootstrap.py).
Implemented as a SIDE call (a completely separate HTTP request, never
touching the main conversation's own streamed request/response) to
OpenRouter's `/chat/completions` endpoint with the `web` search plugin
enabled, on the model this tool was constructed with (the session's small
model when one is configured, else falling back to the main model) --
returns the answer text plus every `url_citation` annotation OpenRouter
attaches to the reply.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Search the web for current information (news, prices, documentation, anything the model's "
    "own training data might be missing or outdated on) and return an answer grounded in real "
    "search results, with source URLs. Only available when the configured model provider is "
    "OpenRouter (web search runs through OpenRouter's own search plugin, on a side call)."
)

_TIMEOUT_S = 30


class WebSearchTool(Tool):
    name = "WebSearch"
    description = DESCRIPTION
    is_read_only = True
    result_cap = 25_000
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
        },
        "required": ["query"],
    }

    def __init__(self, *, base_url: str, api_key: str, model: str, extra_headers: Optional[dict] = None):
        """`base_url`/`api_key` -- the session's own OpenRouter
        ProviderCreds; `model` -- the BARE upstream model id (no `or:`
        prefix) this tool's own side calls run on."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = dict(extra_headers or {})

    def summary(self, input: dict) -> str:
        return f"WebSearch({input.get('query', '')!r})"

    def permission_content(self, input: dict) -> str:
        return input.get("query", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        query = input.get("query") if isinstance(input, dict) else None
        if not query or not isinstance(query, str):
            return ToolResult("The query parameter is required", is_error=True)

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "plugins": [{"id": "web"}],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/rolo-claude/rolo-claude",
            "X-Title": "rolo-claude",
        }
        headers.update(self.extra_headers)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            # 1.0.1 hotfix 11: urlopen_tls -- see providers/http.py's own docstring.
            from rolo_claude.providers.http import urlopen_tls
            with urlopen_tls(req, timeout=_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            return ToolResult(f"WebSearch failed: HTTP {e.code} {e.reason} {detail[:500]}", is_error=True)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return ToolResult(f"WebSearch failed: {e}", is_error=True)

        try:
            data = json.loads(raw)
        except ValueError:
            return ToolResult(f"WebSearch: upstream returned a non-JSON response: {raw[:500]}", is_error=True)

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                return ToolResult(f"WebSearch failed: {error.get('message', error)}", is_error=True)
            return ToolResult("WebSearch: upstream returned no answer", is_error=True)

        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        message = message if isinstance(message, dict) else {}
        answer = message.get("content") or ""
        citations = extract_url_citations(message.get("annotations") or [])

        text = str(answer).strip()
        if citations:
            text += "\n\nSources:\n" + "\n".join(
                f"- {c['url']}" + (f" -- {c['title']}" if c.get("title") else "") for c in citations
            )
        return ToolResult(text or "(no answer text returned)")


def extract_url_citations(annotations: list) -> list:
    """`[{"url":..., "title":...}, ...]` from an OpenAI/OpenRouter-shaped
    `annotations[]` list (`{"type": "url_citation", "url_citation": {...}}`
    each) -- tolerant of a missing/malformed entry, never raises."""
    out: list = []
    for a in annotations or []:
        if not isinstance(a, dict):
            continue
        uc = a.get("url_citation")
        if not isinstance(uc, dict):
            continue
        url = uc.get("url")
        if isinstance(url, str) and url:
            out.append({"url": url, "title": uc.get("title") or ""})
    return out


def build_websearch_tool(*, main_provider: str, creds, small_model_raw: Optional[str], main_model_raw: str,
                          extra_headers: Optional[dict] = None) -> Optional[WebSearchTool]:
    """`None` when the session's main provider isn't OpenRouter (D-CFG) or
    OpenRouter creds aren't resolved -- the CALLER (headless.py/tui/
    bootstrap.py's session construction) only adds this tool to the frozen
    registry when it gets a real instance back, so the tool is genuinely
    ABSENT (not just non-functional) on every other provider.
    `small_model_raw`/`main_model_raw` are BARE upstream model ids (no
    `or:` prefix); the small model is preferred when configured."""
    if main_provider != "openrouter" or creds is None:
        return None
    model = small_model_raw or main_model_raw
    return WebSearchTool(base_url=creds.base_url, api_key=creds.api_key, model=model, extra_headers=extra_headers)
