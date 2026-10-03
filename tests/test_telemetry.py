"""tests.test_telemetry -- halo_harness/telemetry.py (H10 Part A): scan +
per-model/per-tool aggregation on fixtures with known counts, corrupt-line
handling, cache invalidation on mtime, doctor's sessions-count/cache-age
helpers. Fixtures live under tests/fixtures/telemetry/ (Linux-shaped paths,
generated via the real SessionLog class -- see the scratch generator script
referenced in the H10 report, not hand-typed JSON).
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness import telemetry

test, TESTS = new_registry()

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "telemetry"


def _fresh_sessions_dir() -> Path:
    """Copy the checked-in fixtures into a fresh tempdir with mtimes set to
    NOW (fixtures on disk may be arbitrarily old after a git checkout,
    which would make a `since="7d"` filter -- or the mtime-based cache --
    behave unpredictably from one checkout to the next)."""
    tmp = Path(tempfile.mkdtemp(prefix="telemetry-test-"))
    sessions_dir = tmp / "sessions"
    shutil.copytree(FIXTURES, sessions_dir)
    now = time.time()
    for p in sessions_dir.glob("*/*.jsonl"):
        os.utime(p, (now, now))
    os.environ["BRIDGE_TEST_HOME"] = str(tmp / "home")
    return sessions_dir


@test
def test_scan_finds_every_fixture_session(ctx: Ctx):
    d = _fresh_sessions_dir()
    summaries = telemetry.scan(d, since="all", all_projects=True, use_cache=False)
    ctx.check(f"3 sessions scanned, got {len(summaries)}", len(summaries) == 3)
    ids = {s.session_id for s in summaries}
    ctx.check("session A present", "sessA0000000000000000000000001" in ids)
    ctx.check("session B present", "sessB0000000000000000000000002" in ids)
    ctx.check("session C present", "sessC0000000000000000000000003" in ids)


@test
def test_scan_scopes_by_slug(ctx: Ctx):
    d = _fresh_sessions_dir()
    proj = telemetry.scan(d, since="all", slug="-home-user-proj", use_cache=False)
    ctx.check(f"2 sessions in -home-user-proj, got {len(proj)}", len(proj) == 2)
    other = telemetry.scan(d, since="all", slug="-home-user-other-proj", use_cache=False)
    ctx.check(f"1 session in -home-user-other-proj, got {len(other)}", len(other) == 1)


@test
def test_scan_session_id_filter(ctx: Ctx):
    d = _fresh_sessions_dir()
    rows = telemetry.scan(d, since="all", all_projects=True, session_id="sessA0000000000000000000000001",
                           use_cache=False)
    ctx.check(f"exactly 1 session for --session filter, got {len(rows)}", len(rows) == 1)
    ctx.check("it's session A", rows[0].session_id == "sessA0000000000000000000000001")


@test
def test_corrupt_line_skipped_and_counted(ctx: Ctx):
    d = _fresh_sessions_dir()
    rows = telemetry.scan(d, since="all", all_projects=True, session_id="sessB0000000000000000000000002",
                           use_cache=False)
    ctx.check("found session B", len(rows) == 1)
    ctx.check(f"1 corrupt line counted, got {rows[0].corrupt_lines}", rows[0].corrupt_lines == 1)
    # The corrupt line must not have crashed the scan or lost the REST of
    # the file's real nodes -- session B's own usage/tool_result data is
    # still there.
    ctx.check("session B still has real model data despite the trailing corrupt line",
              any(k.startswith("dbx:databricks-claude-sonnet-4") for k in rows[0].models))


@test
def test_aggregate_by_model_known_counts(ctx: Ctx):
    d = _fresh_sessions_dir()
    summaries = telemetry.scan(d, since="all", all_projects=True, use_cache=False)
    rows = telemetry.aggregate_by_model(summaries)
    by_model = {r["model"]: r for r in rows}
    ctx.check("deepseek row present", "or:deepseek/deepseek-v4-flash" in by_model)
    ds = by_model["or:deepseek/deepseek-v4-flash"]
    ctx.check(f"deepseek: 3 calls, got {ds['calls']}", ds["calls"] == 3)
    ctx.check(f"deepseek: 1 retry total, got {ds['retries']}", ds["retries"] == 1)
    ctx.check(f"deepseek: 1 overflow, got {ds['overflows']}", ds["overflows"] == 1)
    ctx.check(f"deepseek: 1 compaction, got {ds['compactions']}", ds["compactions"] == 1)
    ctx.check(f"deepseek: 1 steer, got {ds['steers']}", ds["steers"] == 1)
    ctx.check(f"deepseek: route 'or', got {ds['route']}", ds["route"] == "or")
    ctx.check(f"deepseek: provider DeepInfra, got {ds['provider']}", ds["provider"] == "DeepInfra")
    ctx.check(f"deepseek: 4 tool calls, got {ds['tool_calls']}", ds["tool_calls"] == 4)
    ctx.check(f"deepseek: tool_error_pct 50.0, got {ds['tool_error_pct']}", ds["tool_error_pct"] == 50.0)
    ctx.check(f"deepseek: edit_failure_pct 100.0 (1/1 Edit call failed), got {ds['edit_failure_pct']}",
              ds["edit_failure_pct"] == 100.0)
    ctx.check(f"deepseek: rename repair-hit% == 25.0 (1 of 4 tool_use), got {ds['repair_hit_pct']['rename']}",
              ds["repair_hit_pct"]["rename"] == 25.0)
    ctx.check(f"deepseek: finish_length_pct 33.3 (1 of 3 calls), got {ds['finish_length_pct']}",
              ds["finish_length_pct"] == 33.3)

    ctx.check("dbx row present", "dbx:databricks-claude-sonnet-4" in by_model)
    dbx = by_model["dbx:databricks-claude-sonnet-4"]
    ctx.check(f"dbx: 2 calls merged into ONE (model,provider) row despite the aborted "
              f"call having no provider, got {dbx['calls']}", dbx["calls"] == 2)
    ctx.check(f"dbx: 1 aborted status, got {dbx['status_counts']['aborted']}", dbx["status_counts"]["aborted"] == 1)
    ctx.check(f"dbx: 1 loop_breaker trip, got {dbx['loop_breaker_trips']}", dbx["loop_breaker_trips"] == 1)

    ctx.check("glm row present", "or:z-ai/glm-5.3-flash" in by_model)


@test
def test_w4a_per_model_turns_counts_distinct_turns_not_calls(ctx: Ctx):
    """W4a misc ("stats --models per-model turns real"): a turn with two
    model calls to the SAME model (a retry/tool-loop) must count as ONE
    turn for that model, not two -- `calls` still counts both. Built
    directly against `_summarize_nodes` (no on-disk fixture needed) so the
    exact turn/call shape is unambiguous."""
    nodes = [
        {"type": "meta", "model": "or:test/model-x"},
        {"type": "user", "kind": None},  # turn 1
        {"type": "usage", "model": "or:test/model-x", "provider": "TestProv", "usage": {}},
        {"type": "usage", "model": "or:test/model-x", "provider": "TestProv", "usage": {}},  # same turn, 2nd call (retry)
        {"type": "user", "kind": None},  # turn 2
        {"type": "usage", "model": "or:test/model-x", "provider": "TestProv", "usage": {}},
        {"type": "user", "kind": "steer"},  # never increments s.turns -- must not look like a 3rd turn
        {"type": "usage", "model": "or:test/model-x", "provider": "TestProv", "usage": {}},
    ]
    summary = telemetry._summarize_nodes(session_id="s1", slug="sl", path="p", mtime=0.0, size=0,
                                          nodes=nodes, corrupt_lines=0)
    rows = telemetry.aggregate_by_model([summary])
    row = next(r for r in rows if r["model"] == "or:test/model-x")
    ctx.check(f"4 calls total, got {row['calls']}", row["calls"] == 4)
    ctx.check(f"only 2 distinct turns (the steer's own call doesn't start a 3rd), got {row['turns']}",
              row["turns"] == 2)


@test
def test_aggregate_by_tool_known_counts(ctx: Ctx):
    d = _fresh_sessions_dir()
    summaries = telemetry.scan(d, since="all", all_projects=True, use_cache=False)
    rows = telemetry.aggregate_by_tool(summaries)
    by_tool = {r["tool"]: r for r in rows}
    ctx.check(f"Bash: 3 calls, got {by_tool['Bash']['calls']}", by_tool["Bash"]["calls"] == 3)
    ctx.check(f"Edit: 1 call, 100% error, got {by_tool['Edit']['error_pct']}", by_tool["Edit"]["error_pct"] == 100.0)
    ctx.check("mcp__kb__kb_search shows spilled=1", by_tool["mcp__kb__kb_search"]["spilled"] == 1)


@test
def test_top_error_classes_has_examples(ctx: Ctx):
    d = _fresh_sessions_dir()
    summaries = telemetry.scan(d, since="all", all_projects=True, use_cache=False)
    rows = telemetry.top_error_classes(summaries, n=10)
    classes = {r["error_class"] for r in rows}
    for expected in ("not_found", "schema_invalid", "denied_by_rule", "loop_breaker"):
        ctx.check(f"{expected} present in top_error_classes", expected in classes)
    for r in rows:
        ctx.check(f"{r['error_class']} has a session#seq example", "#" in (r["example"] or ""))


@test
def test_cache_invalidates_on_mtime(ctx: Ctx):
    d = _fresh_sessions_dir()
    first = telemetry.scan(d, since="all", all_projects=True, use_cache=True)
    first_by_id = {s.session_id: s for s in first}
    cache_file = telemetry.cache_path()
    ctx.check("cache file written", cache_file.exists())
    mtime_after_first = cache_file.stat().st_mtime

    # A second scan with NOTHING changed must be a pure cache hit -- no
    # re-parse, no cache re-write (mtime stays identical).
    time.sleep(0.05)
    second = telemetry.scan(d, since="all", all_projects=True, use_cache=True)
    ctx.check("cache file untouched on an unchanged re-scan",
              cache_file.stat().st_mtime == mtime_after_first)
    ctx.check("same session count on cache-hit re-scan", len(second) == len(first))

    # Now mutate session C on disk (append a fresh usage node) and touch its
    # mtime forward -- the cache must invalidate for THAT file only.
    sess_c = next(p for p in d.glob("*/*.jsonl") if "sessC" in p.name)
    with open(sess_c, "a", encoding="utf-8") as f:
        f.write('{"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}, '
                '"cost_usd": 0.0001, "model": "or:z-ai/glm-5.3-flash", "route": "or", '
                '"provider": "Novita", "seq": 999, "ts": 0}\n')
    future = time.time() + 5
    os.utime(sess_c, (future, future))

    third = telemetry.scan(d, since="all", all_projects=True, use_cache=True)
    third_by_id = {s.session_id: s for s in third}
    c_before = first_by_id["sessC0000000000000000000000003"]
    c_after = third_by_id["sessC0000000000000000000000003"]
    calls_before = c_after_calls = None
    for key, bucket in c_before.models.items():
        calls_before = bucket["calls"]
    for key, bucket in c_after.models.items():
        c_after_calls = bucket["calls"]
    ctx.check(f"session C's call count grew after the on-disk change was picked up "
              f"(before={calls_before}, after={c_after_calls})",
              c_after_calls is not None and calls_before is not None and c_after_calls > calls_before)
    # Session A/B, untouched, must still be served from cache unchanged.
    ctx.check("session A unaffected by session C's cache invalidation",
              third_by_id["sessA0000000000000000000000001"].turns
              == first_by_id["sessA0000000000000000000000001"].turns)


@test
def test_total_sessions_count_and_cache_age(ctx: Ctx):
    d = _fresh_sessions_dir()
    n = telemetry.total_sessions_count(d)
    ctx.check(f"total_sessions_count == 3, got {n}", n == 3)
    telemetry.scan(d, since="all", all_projects=True, use_cache=True)
    age = telemetry.cache_age_seconds()
    ctx.check("cache_age_seconds is a small non-negative number right after a scan",
              age is not None and 0 <= age < 30)


def _independent_count(paths: "list[Path]"):
    """H10b defect 1+3 acceptance: the brief's own ~20-line independent
    count, over the SAME raw JSONL files, never calling into telemetry.py
    -- `(total tool_result calls, unknown-tool calls, {model: usage calls})`."""
    tool_calls = unknown_tool = 0
    model_calls: dict = {}
    for p in paths:
        tool_use_index: dict = {}
        current_model = None
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            n = json.loads(line)
            if n["type"] == "meta" and n.get("model"):
                current_model = n["model"]
            elif n["type"] == "assistant":
                for b in n.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_use_index[b["id"]] = b.get("name") or "?"
            elif n["type"] == "tool_result":
                tool_calls += 1
                tool = n.get("tool") or tool_use_index.get(n.get("tool_use_id")) or "?"
                if tool == "?":
                    unknown_tool += 1
            elif n["type"] == "usage":
                model = n.get("model") or current_model
                if model:
                    model_calls[model] = model_calls.get(model, 0) + 1
    return tool_calls, unknown_tool, model_calls


@test
def test_pre_h10_shape_tool_and_model_attribution(ctx: Ctx):
    """H10b defect 1+3: a session log written in the PRE-H10 shape (no
    `tool` on a `tool_result`, no `model`/`provider`/`route` on `usage`, no
    `tool_meta` on `assistant`, no `error_class` even on an `is_error`
    result -- exactly what every one of the owner's real pre-H10 sessions looks
    like) must still attribute every tool call to its real tool (joining
    `tool_use_id` against the preceding assistant node's own `tool_use`
    blocks) and every usage node to its real model (from the session's
    `meta` node), cross-checked against an INDEPENDENT count computed
    directly from the raw JSONL, never through telemetry.py's own logic.
    An H10-shaped session in the same scan proves the fix changes nothing
    for a node that already carries its own metadata."""
    tmp = Path(tempfile.mkdtemp(prefix="telemetry-pre-h10-"))
    sessions_dir = tmp / "sessions"
    proj = sessions_dir / "-home-user-legacy-proj"
    proj.mkdir(parents=True)
    os.environ["BRIDGE_TEST_HOME"] = str(tmp / "home")

    pre_h10_lines = [
        {"type": "meta", "model": "or:deepseek/deepseek-v4-flash", "tools": [], "ts": 0, "seq": 0},
        {"type": "system", "text": "You are halo.", "ts": 0, "seq": 1},
        {"type": "user", "content": [{"type": "text", "text": "list files"}], "ts": 0, "seq": 2},
        # PRE-H10 usage: no model/provider/route at all.
        {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 10}, "cost_usd": 0.001, "ts": 0, "seq": 3},
        # PRE-H10 assistant: no tool_meta.
        {"type": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
         "stop_reason": "tool_use", "ts": 0, "seq": 4},
        # PRE-H10 tool_result: no `tool`, no `error_class`.
        {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt\n", "is_error": False, "ts": 0, "seq": 5},
        {"type": "user", "content": [{"type": "text", "text": "edit main.py"}], "ts": 0, "seq": 6},
        {"type": "usage", "usage": {"input_tokens": 200, "output_tokens": 20}, "cost_usd": 0.002, "ts": 0, "seq": 7},
        {"type": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {}}],
         "stop_reason": "tool_use", "ts": 0, "seq": 8},
        {"type": "tool_result", "tool_use_id": "t2",
         "content": "String to replace not found in file /x/main.py.", "is_error": True, "ts": 0, "seq": 9},
        {"type": "user", "content": [{"type": "text", "text": "grep TODO"}], "ts": 0, "seq": 10},
        {"type": "usage", "usage": {"input_tokens": 50, "output_tokens": 5}, "cost_usd": 0.0005, "ts": 0, "seq": 11},
        # A legacy repaired flag: a node-level {tool_use_id: bool} map --
        # tool_meta's own precursor, predating repair_kind entirely.
        {"type": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "Grep", "input": {}}],
         "stop_reason": "tool_use", "repaired": {"t3": True}, "ts": 0, "seq": 12},
        {"type": "tool_result", "tool_use_id": "t3", "content": "no matches", "is_error": False, "ts": 0, "seq": 13},
    ]
    legacy_path = proj / "legacyA0000000000000000000000001.jsonl"
    with open(legacy_path, "w", encoding="utf-8") as f:
        for line in pre_h10_lines:
            f.write(json.dumps(line) + "\n")

    h10_lines = [
        {"type": "meta", "model": "or:deepseek/deepseek-v4-flash", "tools": [], "ts": 0, "seq": 0},
        {"type": "system", "text": "You are halo.", "ts": 0, "seq": 1},
        {"type": "user", "content": [{"type": "text", "text": "read file"}], "ts": 0, "seq": 2},
        {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 1}, "cost_usd": 0.0001,
         "model": "or:deepseek/deepseek-v4-flash", "route": "or", "provider": "DeepInfra", "ts": 0, "seq": 3},
        {"type": "assistant", "content": [{"type": "tool_use", "id": "u1", "name": "Read", "input": {}}],
         "stop_reason": "tool_use", "tool_meta": {"u1": {"repaired": False, "repair_kind": "none"}}, "ts": 0, "seq": 4},
        {"type": "tool_result", "tool_use_id": "u1", "content": "ok", "is_error": False, "ok": True,
         "tool": "Read", "ts": 0, "seq": 5},
    ]
    h10_path = proj / "h10B00000000000000000000000002.jsonl"
    with open(h10_path, "w", encoding="utf-8") as f:
        for line in h10_lines:
            f.write(json.dumps(line) + "\n")

    ind_tool_calls, ind_unknown, ind_model_calls = _independent_count([legacy_path, h10_path])

    summaries = telemetry.scan(sessions_dir, since="all", all_projects=True, use_cache=False)
    tool_rows = telemetry.aggregate_by_tool(summaries)
    model_rows = telemetry.aggregate_by_model(summaries)
    by_tool = {r["tool"]: r for r in tool_rows}

    ctx.check("Bash resolved via the tool_use_id join, 1 call", by_tool.get("Bash", {}).get("calls") == 1)
    ctx.check("Edit resolved via the join, 1 call", by_tool.get("Edit", {}).get("calls") == 1)
    ctx.check("Grep resolved via the join, 1 call", by_tool.get("Grep", {}).get("calls") == 1)
    ctx.check("Read (H10-shaped) still resolves, 1 call", by_tool.get("Read", {}).get("calls") == 1)

    total_calls = sum(r["calls"] for r in tool_rows)
    unknown_calls = by_tool.get("?", {}).get("calls", 0)
    ctx.check(f"total tool_result calls matches the independent count: {ind_tool_calls} vs {total_calls}",
              total_calls == ind_tool_calls)
    ctx.check(f"unknown-tool ('?') calls matches the independent count: {ind_unknown} vs {unknown_calls}",
              unknown_calls == ind_unknown)
    ctx.check("no unknown-tool calls at all in this fixture (every tool_result joins)", unknown_calls == 0)

    ctx.check("Edit's legacy (no error_class) tool_result is classified 'not_found' from its own text",
              by_tool["Edit"]["error_classes"].get("not_found") == 1)

    model_calls_seen = sum(r["calls"] for r in model_rows if r["model"] == "or:deepseek/deepseek-v4-flash")
    ctx.check(f"per-model usage-call count matches the independent count: "
              f"{ind_model_calls['or:deepseek/deepseek-v4-flash']} vs {model_calls_seen}",
              model_calls_seen == ind_model_calls["or:deepseek/deepseek-v4-flash"])
    ctx.check("legacy usage nodes (no model of their own) attributed via the meta node -- "
              "a real (model, provider) row exists, not just the H10-shaped one",
              any(r["model"] == "or:deepseek/deepseek-v4-flash" and r["provider"] is None for r in model_rows))
    ctx.check("legacy node-level {tool_use_id: bool} repaired map counted as a repair hit (kind 'none')",
              any(r["model"] == "or:deepseek/deepseek-v4-flash" and r["repair_hit_pct"].get("none", 0) > 0
                  for r in model_rows))


@test
def test_tokens_out_counts_reasoning_tokens_reported_separately(ctx: Ctx):
    """Since the 1.0.1 fix pass `map_usage` reports reasoning tokens in
    their own field (so cost is never double-billed); `stats` must still
    count them as generated output, or reasoning-heavy models under-report
    their output volume."""
    tmp = Path(tempfile.mkdtemp(prefix="telemetry-reasoning-"))
    sessions_dir = tmp / "sessions"
    proj = sessions_dir / "-home-user-reasoning-proj"
    proj.mkdir(parents=True)
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(tmp / "home")
    try:
        lines = [
            {"type": "meta", "model": "dbx:databricks-deepseek-v4-1-flash", "tools": [], "ts": 0, "seq": 0},
            {"type": "user", "content": [{"type": "text", "text": "think hard"}], "ts": 0, "seq": 1},
            {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 40, "reasoning_tokens": 60},
             "cost_usd": 0.0, "model": "dbx:databricks-deepseek-v4-1-flash", "route": "databricks",
             "provider": "databricks", "ts": 0, "seq": 2},
            {"type": "assistant", "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
             "ts": 0, "seq": 3},
        ]
        path = proj / "reasoningA000000000000000000000001.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        summaries = telemetry.scan(sessions_dir, since="all", all_projects=True, use_cache=False)
        rows = telemetry.aggregate_by_model(summaries)
        total_out = sum(int(r.get("tokens_out") or 0) for r in rows)
        ctx.check(f"tokens_out = output + reasoning (100 expected), got {total_out} from {rows}", total_out == 100)
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
