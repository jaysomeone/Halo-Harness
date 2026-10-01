"""halo_harness.tools.webfetch -- the WebFetch tool (H2 scope A): stdlib
urllib only, 30s timeout, redirects followed only within the SAME host,
HTML converted to plain text, a 15-minute in-process cache. The brief's
"optional small-model summary" step is NOT implemented here (it would need
a model client wired into a tool, which risks an import cycle back into
agent/loop.py and is genuinely optional per its own wording) -- the model
driving the session sees the extracted page text directly and can summarize
it itself.
"""

from __future__ import annotations

import ipaddress
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

from halo_harness.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Fetches content from a URL and returns it as text (HTML is converted to readable plain text). "
    "Use this to read a web page, a raw file over HTTP(S), or an API's plain response.\n\n"
    "Usage notes:\n"
    "- The URL must be fully-formed (include the scheme); a bare http:// URL is upgraded to https:// "
    "automatically.\n"
    "- Only same-host redirects are followed automatically; a cross-host redirect is reported as an "
    "error instead of silently followed, naming the URL it would have gone to.\n"
    "- Results are cached for 15 minutes -- fetching the same URL again shortly after returns the "
    "cached text instead of re-fetching.\n"
    "- Responses are capped at 100KB of text; a longer page is truncated with a note."
)

_TIMEOUT_S = 30
# finding 14: the RAW read cap is generous (a few MB of markup) -- an HTML
# page is mostly tags/scripts/nav chrome, so capping the RAW bytes at 100KB
# before ever stripping tags threw away almost all of the readable text on
# any real page (verified: a 415KB page yielded 1,141 chars of navigation
# and no article body). The 100KB cap applies to the CONVERTED TEXT instead.
_MAX_RAW_BYTES = 5_000_000
_MAX_TEXT_CHARS = 100_000
_MAX_REDIRECTS = 5
_CACHE_TTL_S = 15 * 60
_CACHE: dict = {}  # url -> (fetched_at_monotonic, body_text)


def reset_cache() -> None:
    """Test seam: clear the module-level cache between tests."""
    _CACHE.clear()


def _is_local_host(hostname: str) -> bool:
    """localhost/loopback/private-range hosts are never auto-upgraded to
    https (below) -- a real internal service or a local dev server
    legitimately speaks plain http and has no TLS cert to upgrade to."""
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


class _CrossHostRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, original_host):
        super().__init__()
        self.original_host = original_host
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > _MAX_REDIRECTS:
            return None
        if urlparse(newurl).hostname != self.original_host:
            raise _CrossHostRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _TextExtractor(HTMLParser):
    """Minimal, dependency-free HTML->text: drops <script>/<style>/
    <noscript> content, inserts a newline at common block boundaries,
    keeps everything else as plain text."""
    _BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})
    _SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self):
        super().__init__()
        self.parts: list = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class WebFetchTool(Tool):
    name = "WebFetch"
    description = DESCRIPTION
    is_read_only = True
    result_cap = 100_000
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch content from"},
            "prompt": {"type": "string", "description": "What information you want to extract from the page"},
        },
        "required": ["url", "prompt"],
    }

    def summary(self, input: dict) -> str:
        return f"WebFetch({input.get('url', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("url", "") if isinstance(input, dict) else ""

    def _fetch_once(self, url: str):
        """One raw HTTP(S) attempt: returns `(raw_bytes, content_type,
        final_url)` on success, or a `ToolResult` (always `is_error=True`)
        describing the failure -- callers check `isinstance(result,
        ToolResult)` to tell the two apart."""
        parsed = urlparse(url)
        handler = _SameHostRedirectHandler(parsed.hostname)
        # 1.0.1 fixpass finding 8: WebFetch used urllib's own DEFAULT TLS
        # context (the plain build_opener(handler) below had no HTTPSHandler
        # of its own at all) -- the ONE `urlopen(` grep this hotfix's TLS
        # sweep relied on never caught this (it's `build_opener`, a
        # different call shape), so this stayed on urllib's stricter
        # default verification policy while every other HTTPS call in the
        # harness moved to default_tls_context() (VERIFY_X509_STRICT
        # cleared when present, the same CA-bundle env vars). Behind a
        # TLS-inspecting proxy whose CA cert has a non-critical
        # basicConstraints extension, WebFetch was the one thing failing on
        # Python 3.13 while `curl`/every other request in this same harness
        # worked. An explicit HTTPSHandler alongside the redirect handler --
        # build_opener accepts more than one -- fixes it with no change to
        # the redirect-following behavior above.
        from halo_harness.providers.http import default_tls_context
        opener = urllib.request.build_opener(handler, urllib.request.HTTPSHandler(context=default_tls_context()))
        req = urllib.request.Request(url, headers={"User-Agent": "halo/0.3 (+webfetch tool)"})
        try:
            with opener.open(req, timeout=_TIMEOUT_S) as resp:
                # finding 14: read a few MB of RAW bytes -- html_to_text
                # below strips markup down to a small fraction of that.
                raw = resp.read(_MAX_RAW_BYTES + 1)
                content_type = resp.headers.get("Content-Type", "") or ""
                final_url = resp.geturl()
            return raw, content_type, final_url
        except _CrossHostRedirect as e:
            return ToolResult(
                f"{url} redirected to a different host ({e.url}) -- refusing to follow automatically; "
                f"fetch that URL directly if you want it.",
                is_error=True,
            )
        except urllib.error.HTTPError as e:
            return ToolResult(f"HTTP error fetching {url}: {e.code} {e.reason}", is_error=True)
        except urllib.error.URLError as e:
            return ToolResult(f"Error fetching {url}: {e.reason}", is_error=True)
        except (OSError, ValueError, TimeoutError) as e:
            return ToolResult(f"Error fetching {url}: {e}", is_error=True)

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        url = input.get("url") if isinstance(input, dict) else None
        if not url or not isinstance(url, str):
            return ToolResult("The url parameter is required", is_error=True)
        parsed = urlparse(url)
        original_url = url
        upgraded = parsed.scheme == "http" and not _is_local_host(parsed.hostname or "")
        if upgraded:
            url = "https://" + url[len("http://"):]
            parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ToolResult(f"Invalid or unsupported URL: {url!r}", is_error=True)

        now = time.monotonic()
        cached = _CACHE.get(url)
        if cached and now - cached[0] < _CACHE_TTL_S:
            return ToolResult(cached[1])

        fetched = self._fetch_once(url)
        if isinstance(fetched, ToolResult):
            # finding 14: the https upgrade's CONNECTION itself failed (no
            # TLS listener, cert error, connection refused, ...) -- fall
            # back to the original plain http URL rather than giving up (a
            # genuine http-only host, e.g. an internal service or a CTF
            # box, must still be reachable).
            if not upgraded:
                return fetched
            fallback = self._fetch_once(original_url)
            if isinstance(fallback, ToolResult):
                return fallback
            fetched = fallback
            url = original_url

        raw, content_type, final_url = fetched
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
        if m:
            charset = m.group(1)
        try:
            text_raw = raw.decode(charset, errors="replace")
        except (LookupError, TypeError):
            text_raw = raw.decode("utf-8", errors="replace")

        looks_html = "html" in content_type.lower() or "<html" in text_raw[:1000].lower()
        text = html_to_text(text_raw) if looks_html else text_raw
        # finding 14: the 100KB cap applies to the CONVERTED text, not the
        # raw markup -- applied AFTER html_to_text so it never throws away
        # the readable content before it's even extracted.
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "\n... [response truncated at 100KB of text]"
        header = f"URL: {final_url}\n\n" if final_url != url else ""
        body = (header + text).strip() or "(empty response)"
        _CACHE[url] = (now, body)
        return ToolResult(body)
