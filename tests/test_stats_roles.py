"""tests.test_stats_roles -- V2c (H15): `stats --roles`'s per-role telemetry
aggregation -- `telemetry.py`'s `_summarize_nodes` tagging a rolled-up
sub-agent usage node's own `role` field, `aggregate_by_role` summing it
across sessions, and the real `halo stats --roles` CLI end to end.
Split out of tests/test_roles.py to keep each file under the brief's own
"<= 250 lines per Write" rule; roles.py's own table resolution and `/roles`
rendering live there instead.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


@test
def test_aggregate_by_role_sums_across_sessions(ctx: Ctx):
    from halo_harness.telemetry import SessionSummary, aggregate_by_role
    s1 = SessionSummary(session_id="s1", slug="p", path="s1.jsonl", mtime=0, size=0,
                         roles={"researcher": {"calls": 2, "tokens_in": 100, "tokens_out": 50,
                                                "tokens_cached": 0, "cost_usd": 0.01}})
    s2 = SessionSummary(session_id="s2", slug="p", path="s2.jsonl", mtime=0, size=0,
                         roles={"researcher": {"calls": 1, "tokens_in": 10, "tokens_out": 5,
                                                "tokens_cached": 0, "cost_usd": 0.001},
                                "coder": {"calls": 1, "tokens_in": 200, "tokens_out": 100,
                                          "tokens_cached": 0, "cost_usd": 0.02}})
    rows = aggregate_by_role([s1, s2])
    ctx.check(f"two roles, got {[r['role'] for r in rows]}", [r["role"] for r in rows] == ["coder", "researcher"])
    researcher = next(r for r in rows if r["role"] == "researcher")
    ctx.check(f"calls summed across sessions, got {researcher}", researcher["calls"] == 3)
    ctx.check(f"sessions counted, got {researcher['sessions']}", researcher["sessions"] == 2)
    ctx.check(f"cost summed, got {researcher['cost_usd']}", abs(researcher["cost_usd"] - 0.011) < 1e-9)


@test
def test_summarize_nodes_tags_role_from_rolled_up_usage_node(ctx: Ctx):
    from halo_harness.telemetry import _summarize_nodes
    nodes = [
        # A rolled-up sub-agent usage node carries no "model" of its own
        # (agent/subagent.py's `_rollup_child_cost_into_parent`) -- it's
        # attributed to whatever the PARENT's own last "meta" model-switch
        # node said, exactly like any other pre-H10-shaped usage node.
        {"type": "meta", "model": "or:vendor/parent"},
        {"type": "usage", "usage": {"input_tokens": 40, "output_tokens": 10}, "cost_usd": 0.005,
         "agent_id": "ag1", "role": "reviewer"},
        {"type": "usage", "usage": {"input_tokens": 5, "output_tokens": 1}, "cost_usd": 0.0001},
    ]
    s = _summarize_nodes(session_id="s1", slug="p", path="s1.jsonl", mtime=0, size=0, nodes=nodes, corrupt_lines=0)
    ctx.check(f"only the role-tagged node counted, got {s.roles}", s.roles == {
        "reviewer": {"calls": 1, "tokens_in": 40, "tokens_out": 10, "tokens_cached": 0, "cost_usd": 0.005},
    })


def _run_cli(argv, home: Path, cwd: Path, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


@test
def test_stats_roles_end_to_end_json(ctx: Ctx):
    """The real `halo stats --roles --json` CLI surface, over a
    hand-written session log carrying one rolled-up, role-tagged usage
    node -- proves the whole pipe (log -> telemetry.scan -> aggregate_by_role
    -> stats_cli's own JSON output) end to end."""
    home = Path(tempfile.mkdtemp(prefix="stats-roles-home-"))
    sessions_dir = home / ".halo" / "sessions" / "testproj"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "model": "or:vendor/parent"},
        {"type": "usage", "usage": {"input_tokens": 300, "output_tokens": 80}, "cost_usd": 0.02,
         "agent_id": "ag1", "role": "coder"},
    ]
    p = sessions_dir / "sess1.jsonl"
    p.write_text("\n".join(json.dumps(n) for n in lines) + "\n", encoding="utf-8")
    now = time.time()
    os.utime(p, (now, now))
    cwd = Path(tempfile.mkdtemp(prefix="stats-roles-cwd-"))
    result = _run_cli(["stats", "--roles", "--since", "all", "--all-projects", "--json"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    obj = json.loads(result.stdout)
    ctx.check("'roles' key present", "roles" in obj)
    ctx.check(f"the coder role shows up, got {obj['roles']}", any(r["role"] == "coder" for r in obj["roles"]))
    coder = next(r for r in obj["roles"] if r["role"] == "coder")
    ctx.check(f"cost carried through, got {coder}", abs(coder["cost_usd"] - 0.02) < 1e-9)

    text_result = _run_cli(["stats", "--roles", "--since", "all", "--all-projects"], home, cwd)
    ctx.check(f"plain-text run also exits 0, got {text_result.returncode}", text_result.returncode == 0)
    ctx.check("plain text mentions the role", "coder" in text_result.stdout)


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
