"""tests.test_history -- halo_harness/history.py: merges Claude Code's own
`~/.claude/history.jsonl` (read-only) with this harness's own
`~/.halo/history.jsonl`, project-filters across BOTH separator
forms, and appends only to our own file (U0 scope C).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness import history as h

test, TESTS = new_registry()


def _fresh_home():
    tmp = Path(tempfile.mkdtemp(prefix="halo-history-"))
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
        h.append_history_entry("from halo", str(tmp), timestamp=2)
        merged = h.load_merged_history()
        displays = [e["display"] for e in merged]
        ctx.check("both entries present", "from claude code" in displays and "from halo" in displays)
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


# ============================================================================
# W2c item 1: Claude Code's own history.jsonl stores `timestamp` in
# MILLISECONDS (verified live on the Kali VM: 1790623928405); halo's own
# file stored SECONDS (`time.time()`, e.g. 1790882425.67, the OLD default).
# Sorting by the raw value put every Claude Code entry after every halo
# entry regardless of real time -- Up always recalled the last `claude`
# prompt for that directory, never the one just typed in halo.
# ============================================================================

@test
def test_mixed_ms_and_seconds_timestamps_sort_by_real_time_not_raw_value(ctx: Ctx):
    """The exact bug: a ms Claude entry that is NEWER in real time than a
    seconds halo entry, and an older ms Claude entry too -- both units
    read back and interleaved correctly, oldest-first, by REAL time (never
    by raw magnitude, which would put every ms value after every seconds
    one)."""
    tmp, old = _fresh_home()
    try:
        older_claude_ms = 1790623928405  # real time ~1790623928.405s -- verified live
        halo_seconds = 1790882425.67  # real time ~1790882425.67s -- verified live, NEWER than the above
        newer_claude_ms = 1790999999000  # real time ~1790999999.0s -- NEWER than both
        _write_jsonl(h.claude_history_path(), [
            {"display": "older claude (ms)", "project": str(tmp), "timestamp": older_claude_ms},
            {"display": "newer claude (ms)", "project": str(tmp), "timestamp": newer_claude_ms},
        ])
        h.append_history_entry("halo entry (seconds)", str(tmp), timestamp=halo_seconds)
        merged = h.load_merged_history(cwd=str(tmp))
        displays = [e["display"] for e in merged]
        ctx.check(f"oldest-first by REAL time despite raw ms values dwarfing the raw seconds "
                  f"one, got {displays!r}",
                  displays == ["older claude (ms)", "halo entry (seconds)", "newer claude (ms)"])
    finally:
        _restore_home(old)


@test
def test_append_history_entry_now_default_writes_milliseconds(ctx: Ctx):
    """`append_history_entry`'s own "now" default (no explicit `timestamp`)
    now writes MILLISECONDS -- Claude Code's own schema -- not seconds. An
    explicit `timestamp` argument (every other test in this file) is
    untouched by this change; only the "now" default's unit moved."""
    import time as time_mod

    tmp, old = _fresh_home()
    try:
        before_ms = time_mod.time() * 1000.0
        entry = h.append_history_entry("now entry", str(tmp))
        after_ms = time_mod.time() * 1000.0
        ts = entry["timestamp"]
        ctx.check(f"the default timestamp is ms-shaped (> 1e11), got {ts!r}", ts > 1e11)
        ctx.check(f"it is genuinely 'now' in milliseconds, got {ts!r} not within "
                  f"[{before_ms!r}, {after_ms!r}]", before_ms <= ts <= after_ms + 1000)
        # An explicit timestamp (e.g. a seconds-shaped one, every other
        # fixture in this file) must still round-trip completely untouched.
        explicit = h.append_history_entry("explicit entry", str(tmp), timestamp=42.0)
        ctx.check(f"an explicit timestamp is never rescaled, got {explicit['timestamp']!r}",
                  explicit["timestamp"] == 42.0)
    finally:
        _restore_home(old)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
