"""tests.test_tools_misc -- WebFetch (against a real local HTTP server),
TodoWrite, ToolSearch, AskUserQuestion, Skill (stub), and the shared
result-truncation-to-disk helper (tools/truncate.py).
"""
import http.server
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools.ask_user_question import AskUserQuestionTool
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.registry import ToolRegistry
from rolo_claude.tools.skill import SkillTool
from rolo_claude.tools.todowrite import TodoWriteTool
from rolo_claude.tools.tool_search import ToolSearchTool
from rolo_claude.tools.webfetch import WebFetchTool

test, TESTS = new_registry()


# ---- a tiny local HTTP server for WebFetch --------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/page.html":
            body = b"<html><body><h1>Title</h1><p>Hello <b>world</b></p><script>evil()</script></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-same-host":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/page.html")
            self.end_headers()
        elif self.path == "/redirect-cross-host":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/page.html")
            self.end_headers()
        elif self.path == "/plain":
            body = b"just plain text\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


class _LocalServer:
    def __enter__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"


# ---- WebFetch ---------------------------------------------------------

@test
def test_webfetch_converts_html_to_text(ctx: Ctx):
    from rolo_claude.tools import webfetch as wf
    wf.reset_cache()
    with _LocalServer() as srv:
        result = WebFetchTool().run({"url": f"{srv.base_url}/page.html", "prompt": "extract the title"}, ToolContext(cwd=Path(".")))
    ctx.check("no error", result.is_error is False)
    ctx.check("visible text present", "Title" in result.content and "Hello" in result.content and "world" in result.content)
    ctx.check("script content stripped", "evil()" not in result.content)
    ctx.check("no raw HTML tags leaked", "<h1>" not in result.content and "<script>" not in result.content)


@test
def test_webfetch_plain_text_passthrough(ctx: Ctx):
    from rolo_claude.tools import webfetch as wf
    wf.reset_cache()
    with _LocalServer() as srv:
        result = WebFetchTool().run({"url": f"{srv.base_url}/plain", "prompt": "read it"}, ToolContext(cwd=Path(".")))
    ctx.check("plain text passed through untouched", "just plain text" in result.content)


@test
def test_webfetch_follows_same_host_redirect(ctx: Ctx):
    from rolo_claude.tools import webfetch as wf
    wf.reset_cache()
    with _LocalServer() as srv:
        result = WebFetchTool().run({"url": f"{srv.base_url}/redirect-same-host", "prompt": "x"}, ToolContext(cwd=Path(".")))
    ctx.check(f"same-host redirect followed, got {result.content!r}", result.is_error is False and "Title" in result.content)


@test
def test_webfetch_refuses_cross_host_redirect(ctx: Ctx):
    from rolo_claude.tools import webfetch as wf
    wf.reset_cache()
    with _LocalServer() as srv:
        result = WebFetchTool().run({"url": f"{srv.base_url}/redirect-cross-host", "prompt": "x"}, ToolContext(cwd=Path(".")))
    ctx.check("cross-host redirect is refused, not silently followed", result.is_error is True)
    ctx.check("error names the redirect", "redirect" in result.content.lower())


@test
def test_webfetch_caches_for_15_minutes(ctx: Ctx):
    from rolo_claude.tools import webfetch as wf
    wf.reset_cache()
    with _LocalServer() as srv:
        url = f"{srv.base_url}/plain"
        r1 = WebFetchTool().run({"url": url, "prompt": "x"}, ToolContext(cwd=Path(".")))
        # Kill the server, then fetch again -- a cache hit must NOT need the network.
    r2 = WebFetchTool().run({"url": url, "prompt": "x"}, ToolContext(cwd=Path(".")))
    ctx.check("second fetch (server now dead) served from cache", r2.is_error is False)
    ctx.check("cached content identical", r1.content == r2.content)


@test
def test_webfetch_missing_url_is_error(ctx: Ctx):
    result = WebFetchTool().run({"prompt": "x"}, ToolContext(cwd=Path(".")))
    ctx.check("missing url is an error", result.is_error is True)


@test
def test_webfetch_rejects_unsupported_scheme(ctx: Ctx):
    result = WebFetchTool().run({"url": "ftp://example.invalid/file", "prompt": "x"}, ToolContext(cwd=Path(".")))
    ctx.check("ftp:// is rejected", result.is_error is True)


# ---- TodoWrite ----------------------------------------------------------

@test
def test_todowrite_accepts_a_valid_list(ctx: Ctx):
    todos = [{"content": "do thing", "status": "in_progress", "activeForm": "Doing thing"}]
    result = TodoWriteTool().run({"todos": todos}, ToolContext(cwd=Path(".")))
    ctx.check("valid list accepted", result.is_error is False)
    ctx.check("content echoed back", "do thing" in result.content)


@test
def test_todowrite_rejects_two_in_progress(ctx: Ctx):
    todos = [
        {"content": "a", "status": "in_progress", "activeForm": "Doing a"},
        {"content": "b", "status": "in_progress", "activeForm": "Doing b"},
    ]
    result = TodoWriteTool().run({"todos": todos}, ToolContext(cwd=Path(".")))
    ctx.check("two in_progress items rejected", result.is_error is True)


@test
def test_todowrite_rejects_bad_status(ctx: Ctx):
    todos = [{"content": "a", "status": "done", "activeForm": "Doing a"}]
    result = TodoWriteTool().run({"todos": todos}, ToolContext(cwd=Path(".")))
    ctx.check("invalid status rejected", result.is_error is True)


# ---- ToolSearch -----------------------------------------------------------

@test
def test_toolsearch_select_exact_names(ctx: Ctx):
    reg = ToolRegistry()
    result = ToolSearchTool().run({"query": "select:Read,Bash"}, ToolContext(cwd=Path("."), registry=reg))
    ctx.check("no error", result.is_error is False)
    ctx.check('"Read" present', '"Read"' in result.content)
    ctx.check('"Bash" present', '"Bash"' in result.content)
    ctx.check("Grep not included (not selected)", '"Grep"' not in result.content)


@test
def test_toolsearch_keyword_search(ctx: Ctx):
    reg = ToolRegistry()
    result = ToolSearchTool().run({"query": "file pattern matching"}, ToolContext(cwd=Path("."), registry=reg))
    ctx.check(f"Glob ranks highly for this query, got {result.content[:200]!r}", '"Glob"' in result.content)


@test
def test_toolsearch_no_registry_is_error(ctx: Ctx):
    result = ToolSearchTool().run({"query": "select:Read"}, ToolContext(cwd=Path(".")))
    ctx.check("no registry wired -> error, not a crash", result.is_error is True)


# ---- AskUserQuestion / Skill stub -----------------------------------------

@test
def test_ask_user_question_errors_in_print_mode(ctx: Ctx):
    result = AskUserQuestionTool().run({"question": "which one?"}, ToolContext(cwd=Path(".")))
    ctx.check("AskUserQuestion errors (no interactive UI yet)", result.is_error is True)


@test
def test_skill_stub_always_errors_honestly(ctx: Ctx):
    result = SkillTool().run({"skill": "deploy"}, ToolContext(cwd=Path(".")))
    ctx.check("Skill stub errors", result.is_error is True)
    ctx.check("names the requested skill", "deploy" in result.content)


# ---- truncation spill -----------------------------------------------------

@test
def test_truncate_spills_full_content_and_shows_head_tail(ctx: Ctx):
    from rolo_claude.tools.truncate import spill_and_truncate
    d = Path(tempfile.mkdtemp(prefix="truncate-"))
    content = "".join(f"line{i}\n" for i in range(5000))
    cap = 1000
    shown = spill_and_truncate(content, cap=cap, session_dir=d, tool_use_id="call_1")
    ctx.check(f"shown output is near the cap, got {len(shown)} chars", len(shown) < len(content))
    ctx.check("head of the original content present", "line0\n" in shown)
    ctx.check("tail of the original content present", "line4999" in shown)
    ctx.check("a pointer note is present", "omitted" in shown)
    spill_path = d / "tool-results" / "call_1.txt"
    ctx.check("full content spilled to disk", spill_path.exists())
    ctx.check("spilled file has the FULL content", spill_path.read_text(encoding="utf-8") == content)


@test
def test_truncate_is_a_noop_under_the_cap(ctx: Ctx):
    from rolo_claude.tools.truncate import spill_and_truncate
    shown = spill_and_truncate("short", cap=1000, session_dir=None, tool_use_id="call_2")
    ctx.check("content under the cap is returned unchanged", shown == "short")


@test
def test_truncate_none_cap_means_no_truncation(ctx: Ctx):
    from rolo_claude.tools.truncate import spill_and_truncate
    long_content = "x" * 1_000_000
    shown = spill_and_truncate(long_content, cap=None, session_dir=None, tool_use_id="call_3")
    ctx.check("cap=None never truncates", shown == long_content)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
