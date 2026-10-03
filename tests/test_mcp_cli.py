"""tests.test_mcp_cli -- halo_harness/mcp_cli.py (H3 scope D): line format/
status vocabulary unit tests, and read-modify-write helpers exercised
in-process (faster than the subprocess round trips already covered in
tests/test_doctor_mcp_config_cli.py) -- add-json, project scope (.mcp.json),
remove's local->user->project search order, and byte-preservation of
unrelated `~/.claude.json` keys via a direct call (no subprocess).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness import mcp_cli as C

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


# ---- status_label / format_mcp_list_line (binary-facts sec.9, verbatim) ---

@test
def test_status_label_every_state(ctx: Ctx):
    cases = {
        "connected": "\u2714 Connected", "failed": "\u2717 Failed to connect",
        "needs_auth": "! Needs authentication", "pending_approval": "\u23f8 Pending approval",
        "disabled": "- Not configured", "pending": "- Not configured", "closed": "- Not configured",
    }
    for state, expected in cases.items():
        got = C.status_label({"state": state})
        ctx.check(f"{state} -> {expected!r}, got {got!r}", got == expected)


@test
def test_status_label_tools_fetch_failed(ctx: Ctx):
    got = C.status_label({"state": "connected", "tools_fetch_failed": True})
    ctx.check(f"special-cased even though state is 'connected', got {got!r}",
              got == "! Connected \u00b7 tools fetch failed")


@test
def test_status_label_unknown_state_is_connection_error(ctx: Ctx):
    ctx.check("an unrecognised state degrades to Connection error, never a crash",
              C.status_label({"state": "something-new"}) == "\u2717 Connection error")


@test
def test_format_mcp_list_line_stdio(ctx: Ctx):
    line = C.format_mcp_list_line({"name": "codriver", "type": "stdio", "command": "node",
                                     "args": ["C:\\x\\index.js"], "state": "connected"})
    ctx.check(f"binary-facts sec.9 example shape, got {line!r}",
              line == "codriver: node C:\\x\\index.js - \u2714 Connected")


@test
def test_format_mcp_list_line_http(ctx: Ctx):
    line = C.format_mcp_list_line({"name": "srv", "type": "http", "url": "https://x/mcp", "state": "failed"})
    ctx.check(f"HTTP label shape, got {line!r}", line == "srv: https://x/mcp (HTTP) - \u2717 Failed to connect")


@test
def test_format_mcp_list_line_sse(ctx: Ctx):
    line = C.format_mcp_list_line({"name": "srv", "type": "sse", "url": "https://x/sse", "state": "needs_auth"})
    ctx.check(f"SSE label shape, got {line!r}", line == "srv: https://x/sse (SSE) - ! Needs authentication")


# ---- read-modify-write (in-process, no subprocess) -------------------------

@test
def test_sniff_indent_detects_2_and_4_space(ctx: Ctx):
    ctx.check("2-space", C._sniff_indent('{\n  "a": 1\n}') == 2)
    ctx.check("4-space", C._sniff_indent('{\n    "a": 1\n}') == 4)
    ctx.check("no indented line -> defaults to 2", C._sniff_indent('{"a": 1}') == 2)


def _with_claude_json_path(fn):
    with tempfile.TemporaryDirectory() as td:
        old_env = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            return fn(Path(td))
        finally:
            if old_env is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old_env


@test
def test_store_entry_user_scope_preserves_unrelated_keys(ctx: Ctx):
    def _run(home):
        (home / ".claude.json").write_text(json.dumps({"other": {"x": 1}, "mcpServers": {"a": {"command": "a"}}}), encoding="utf-8")
        dest = C._store_entry(scope="user", name="b", entry={"type": "stdio", "command": "b"}, cwd=home)
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        ctx.check("unrelated key preserved", data["other"] == {"x": 1})
        ctx.check("existing server preserved", data["mcpServers"]["a"]["command"] == "a")
        ctx.check("new server added", data["mcpServers"]["b"]["command"] == "b")
        ctx.check(f"dest is the claude.json path, got {dest!r}", dest.endswith(".claude.json"))
    _with_claude_json_path(_run)


@test
def test_store_entry_local_scope_uses_normalized_cwd_key(ctx: Ctx):
    def _run(home):
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        cwd = home / "myproj"
        C._store_entry(scope="local", name="s", entry={"type": "stdio", "command": "c"}, cwd=cwd)
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        from halo_harness.config.paths import normalize_cwd
        key = normalize_cwd(cwd)
        ctx.check(f"stored under projects[{key}].mcpServers", data["projects"][key]["mcpServers"]["s"]["command"] == "c")
    _with_claude_json_path(_run)


@test
def test_store_entry_project_scope_writes_dot_mcp_json_not_claude_json(ctx: Ctx):
    def _run(home):
        claude_json_path = home / ".claude.json"
        claude_json_path.write_text(json.dumps({"untouched": True}), encoding="utf-8")
        cwd = home / "proj"
        cwd.mkdir()
        C._store_entry(scope="project", name="s", entry={"type": "stdio", "command": "c"}, cwd=cwd)
        ctx.check("claude.json completely untouched", json.loads(claude_json_path.read_text(encoding="utf-8")) == {"untouched": True})
        mcp_json = json.loads((cwd / ".mcp.json").read_text(encoding="utf-8"))
        ctx.check("server written to .mcp.json instead", mcp_json["mcpServers"]["s"]["command"] == "c")
    _with_claude_json_path(_run)


@test
def test_build_entry_stdio_shape(ctx: Ctx):
    entry = C._build_entry(transport="stdio", command_or_url="npx", extra_args=["-y", "pkg"],
                            env={"K": "V"}, headers={}, oauth=None)
    ctx.check("stdio entry shape", entry == {"type": "stdio", "command": "npx", "args": ["-y", "pkg"], "env": {"K": "V"}})


@test
def test_build_entry_http_shape_with_headers(ctx: Ctx):
    entry = C._build_entry(transport="http", command_or_url="https://x", extra_args=[],
                            env={}, headers={"Authorization": "Bearer t"}, oauth=None)
    ctx.check("http entry has url+headers, no command/args",
              entry == {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer t"}})


@test
def test_claude_json_add_remove_byte_identical_on_non_ascii_no_trailing_newline_fixture(ctx: Ctx):
    """finding 8, verified against the owner's real 66837-byte ~/.claude.json
    (non-ASCII text, no trailing newline): ensure_ascii=False, the
    source's own trailing-newline state is reproduced, and add+remove of
    the SAME entry restores the file byte for byte (byte compare, not
    parsed-dict compare -- a dict compare can't see \\uXXXX escaping or a
    missing/extra trailing newline)."""
    def _run(home):
        fixture = ('{\n  "greeting": "café — naïve résumé 你好 \U0001F600",\n'
                   '  "mcpServers": {}\n}')  # deliberately NO trailing newline
        (home / ".claude.json").write_bytes(fixture.encode("utf-8"))
        before = (home / ".claude.json").read_bytes()

        C._store_entry(scope="user", name="tmp-server", entry={"type": "stdio", "command": "c"}, cwd=home)
        after_add = (home / ".claude.json").read_bytes()
        ctx.check("non-ASCII characters are NOT re-escaped to \\uXXXX",
                  "café".encode("utf-8") in after_add and b"\\u00e9" not in after_add)

        data = json.loads(after_add.decode("utf-8"))
        del data["mcpServers"]["tmp-server"]
        indent = C._sniff_indent(after_add.decode("utf-8"))
        # simulate `mcp remove`'s own read-modify-write of the SAME state
        C._write_claude_json_raw(data, indent, trailing_newline=fixture.endswith("\n"), bom=False)
        after_remove = (home / ".claude.json").read_bytes()
        ctx.check(f"byte-identical to the original after add+remove, "
                  f"before_tail={before[-40:]!r} after_tail={after_remove[-40:]!r}",
                  after_remove == before)
    _with_claude_json_path(_run)


@test
def test_claude_json_write_preserves_trailing_newline_and_bom_state(ctx: Ctx):
    def _run(home):
        path = home / ".claude.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {}}).encode("utf-8"))  # BOM, no trailing \n
        C._store_entry(scope="user", name="s", entry={"type": "stdio", "command": "c"}, cwd=home)
        raw = path.read_bytes()
        ctx.check("BOM preserved", raw.startswith(b"\xef\xbb\xbf"))
        ctx.check(f"no trailing newline added (source had none), got tail={raw[-15:]!r}", not raw.endswith(b"\n"))
    _with_claude_json_path(_run)


@test
def test_claude_json_write_preserves_file_mode(ctx: Ctx):
    import stat as _stat
    def _run(home):
        path = home / ".claude.json"
        path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            raise SkipTestSentinel
        before_mode = _stat.S_IMODE(path.stat().st_mode)
        C._store_entry(scope="user", name="s", entry={"type": "stdio", "command": "c"}, cwd=home)
        after_mode = _stat.S_IMODE(path.stat().st_mode)
        ctx.check(f"file mode preserved across the replace, before={oct(before_mode)} after={oct(after_mode)}",
                  after_mode == before_mode)
    try:
        _with_claude_json_path(_run)
    except SkipTestSentinel:
        pass


class SkipTestSentinel(Exception):
    pass


@test
def test_parse_add_argv_dash_dash_separator(ctx: Ctx):
    """finding 9: `mcp add name -- cmd args` -- the FIRST bare `--` is
    consumed as a separator, never stored as the command itself."""
    opts, positionals = C._parse_add_argv(["airtable", "--", "npx", "-y", "airtable-mcp-server"])
    ctx.check(f"'--' itself never appears in positionals, got {positionals}", "--" not in positionals)
    ctx.check(f"name/command/args in the right order, got {positionals}",
              positionals == ["airtable", "npx", "-y", "airtable-mcp-server"])


@test
def test_parse_add_argv_inline_env_and_scope(ctx: Ctx):
    opts, positionals = C._parse_add_argv(["--env=API_KEY=abc=123", "--scope=project", "srv", "cmd"])
    ctx.check(f"--env=K=V inline form (value itself may contain '='), got {opts['env']}",
              opts["env"] == ["API_KEY=abc=123"])
    ctx.check(f"--scope=x inline form, got {opts['scope']!r}", opts["scope"] == "project")
    ctx.check(f"positionals unaffected, got {positionals}", positionals == ["srv", "cmd"])


@test
def test_parse_add_argv_flags_before_name_only(ctx: Ctx):
    opts, positionals = C._parse_add_argv(["-t", "http", "-s", "user", "srv", "https://x", "--extra-flag-for-server"])
    ctx.check("options captured", opts["transport"] == "http" and opts["scope"] == "user")
    ctx.check("everything from the name onward is positional (incl. dash-prefixed server args)",
              positionals == ["srv", "https://x", "--extra-flag-for-server"])


@test
def test_parse_kv_list(ctx: Ctx):
    ctx.check("env-shaped", C._parse_kv_list(["A=1", "B=2", "malformed"], "=") == {"A": "1", "B": "2"})
    ctx.check("header-shaped", C._parse_kv_list(["Authorization: Bearer x"], ":") == {"Authorization": "Bearer x"})


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
