"""tests.test_history -- rolo_claude/history.py: merges Claude Code's own
`~/.claude/history.jsonl` (read-only) with this harness's own
`~/.rolo-claude/history.jsonl`, project-filters across BOTH separator
forms, and appends only to our own file (U0 scope C).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude import history as h

test, TESTS = new_registry()


def _fresh_home():
    tmp = Path(tempfile.mkdtemp(prefix="rolo-claude-history-"))
    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(tmp)
    return tmp, old


def _restore_home(old):
    if old is None:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    else:
        os.environ["BRIDGE_TEST_HOME"] = old


def _write_jsonl(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@test
def test_merges_both_files(ctx: Ctx):
    tmp, old = _fresh_home()
    try:
        _write_jsonl(h.claude_history_path(), [{"display": "from claude code", "project": str(tmp), "timestamp": 1}])
        h.append_history_entry("from rolo-claude", str(tmp), timestamp=2)
        merged = h.load_merged_history()
        displays = [e["display"] for e in merged]
        ctx.check("both entries present", "from claude code" in displays and "from rolo-claude" in displays)
    finally:
        _restore_home(old)


@test
def test_append_never_writes_claude_codes_own_file(ctx: Ctx):
    tmp, old = _fresh_home()
    try:
        h.append_history_entry("hello", str(tmp))
        ctx.check("Claude Code's own history.jsonl was never created", not h.claude_history_path().exists())
        ctx.check("our own history.jsonl was created", h.rolo_history_path().exists())
    finally:
        _restore_home(old)


@test
def test_project_filter_matches_both_separator_forms(ctx: Ctx):
    """`normalize_cwd` pattern-detects a Windows-drive-shaped string
    (`C:\\...`/`C:/...`) on ANY host OS (config/paths.py's own docstring:
    "detected by pattern, never by host os.name") -- fixed literal Windows
    paths keep this test OS-neutral instead of mangling the real (possibly
    POSIX) temp dir a backslash-swap would turn into garbage on Linux."""
    tmp, old = _fresh_home()
    try:
        forward = "C:/Users/example/proj"
        backward = "C:\\Users\\example\\proj"
        _write_jsonl(h.claude_history_path(), [
            {"display": "forward-form entry", "project": forward, "timestamp": 1},
            {"display": "backward-form entry", "project": backward, "timestamp": 2},
            {"display": "unrelated project entry", "project": "C:/Users/example/other", "timestamp": 3},
        ])
        merged = h.load_merged_history(cwd=forward)
        displays = {e["display"] for e in merged}
        ctx.check("forward-form matched", "forward-form entry" in displays)
        ctx.check("backward-form matched", "backward-form entry" in displays)
        ctx.check("unrelated project excluded", "unrelated project entry" not in displays)
    finally:
        _restore_home(old)


@test
def test_sorted_oldest_first(ctx: Ctx):
    tmp, old = _fresh_home()
    try:
        _write_jsonl(h.claude_history_path(), [
            {"display": "third", "project": str(tmp), "timestamp": 30},
            {"display": "first", "project": str(tmp), "timestamp": 10},
            {"display": "second", "project": str(tmp), "timestamp": 20},
        ])
        merged = h.load_merged_history(cwd=str(tmp))
        ctx.check(f"sorted oldest-first, got {[e['display'] for e in merged]!r}",
                  [e["display"] for e in merged] == ["first", "second", "third"])
    finally:
        _restore_home(old)


@test
def test_limit_returns_most_recent(ctx: Ctx):
    tmp, old = _fresh_home()
    try:
        for i in range(5):
            h.append_history_entry(f"entry-{i}", str(tmp), timestamp=float(i))
        merged = h.load_merged_history(cwd=str(tmp), limit=2)
        ctx.check(f"only the last 2, got {[e['display'] for e in merged]!r}",
                  [e["display"] for e in merged] == ["entry-3", "entry-4"])
    finally:
        _restore_home(old)


@test
def test_malformed_line_skipped_not_fatal(ctx: Ctx):
    tmp, old = _fresh_home()
    try:
        path = h.claude_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"display": "ok one", "project": "x", "timestamp": 1}\nnot json at all\n', encoding="utf-8")
        merged = h.load_merged_history()
        ctx.check("well-formed line survives, malformed one skipped", len(merged) == 1 and merged[0]["display"] == "ok one")
    finally:
        _restore_home(old)


@test
def test_schema_keys_present_on_appended_entry(ctx: Ctx):
    tmp, old = _fresh_home()
    try:
        entry = h.append_history_entry("hi", str(tmp), session_id="abc123", pasted_contents={"0": {"foo": 1}})
        for key in h.HISTORY_SCHEMA_KEYS:
            ctx.check(f"appended entry has key {key!r}", key in entry)
        ctx.check("sessionId round-trips", entry["sessionId"] == "abc123")
    finally:
        _restore_home(old)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
