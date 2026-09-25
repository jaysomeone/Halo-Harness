"""rolo_claude.stats_cli -- `rolo-claude stats` subcommand (H8 scope D): a
headless, cross-session version of the TUI's own `/stats` (which only ever
covers the ONE live session it's running in). Aggregates every session
JSONL log for a project directory (or, with `--all`, every project this
`~/.rolo-claude/sessions/` has ever seen) into per-model token/cost totals
and per-tool call counts, via the SAME `controller.compute_session_stats`
the live `/stats` command uses on one session's own nodes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _merge_stats(total: dict, part: dict) -> None:
    total["turns"] += part["turns"]
    total["total_cost_usd"] += part["total_cost_usd"]
    for model, bucket in part["per_model"].items():
        dest = total["per_model"].setdefault(model, {
            "input_tokens": 0, "output_tokens": 0,
            # H9 whole-tree review finding 29: same fields controller.
            # compute_session_stats itself now tracks -- see its own comment.
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "cost_usd": 0.0, "calls": 0,
        })
        dest["input_tokens"] += bucket["input_tokens"]
        dest["output_tokens"] += bucket["output_tokens"]
        dest["cache_read_input_tokens"] += bucket.get("cache_read_input_tokens", 0)
        dest["cache_creation_input_tokens"] += bucket.get("cache_creation_input_tokens", 0)
        dest["cost_usd"] += bucket["cost_usd"]
        dest["calls"] += bucket["calls"]
    for name, n in part["tool_counts"].items():
        total["tool_counts"][name] = total["tool_counts"].get(name, 0) + n


def _session_jsonl_files(cwd: Path, *, all_projects: bool) -> list:
    """H9 whole-tree review finding 13: deliberately does NOT also glob each
    session's own `<session_id>/subagents/*.jsonl` files (agent/subagent.py's
    `_child_log_paths`) -- a tempting-looking fix that would DOUBLE-COUNT.
    `agent/subagent.py`'s `_rollup_child_cost_into_parent` now appends one
    rolled-up "usage" node (tagged `agent_id`) to the PARENT's own top-level
    `*.jsonl` every time a child finishes, specifically so a single glob of
    the ordinary per-session files (unchanged below) already includes every
    sub-agent's spend -- both for THIS headless aggregator and for the
    live TUI's own `/stats` (`Controller.session_stats`, which reads only
    `self.session.log.nodes()`, never touching `subagents/*.jsonl` at all).
    Additionally globbing the child files here would sum the SAME dollars
    twice: once from the parent's rolled-up node, once from the child's own
    native ones."""
    from rolo_claude.agent.sessions import sessions_dir
    from rolo_claude.config.paths import bridge_home
    if all_projects:
        root = bridge_home() / "sessions"
        if not root.is_dir():
            return []
        return sorted(root.glob("*/*.jsonl"))
    d = sessions_dir(cwd)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"))


def _read_nodes(path: Path) -> list:
    nodes = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    nodes.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return nodes


def cmd_stats(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude stats", add_help=True,
                                      description="Aggregate tokens/cost/tool-calls across session logs (headless /stats).")
    parser.add_argument("--all", action="store_true", dest="all_projects",
                         help="Aggregate every project's sessions, not just the current directory's")
    parser.add_argument("--cwd", default=None, metavar="DIR",
                         help="Project directory to aggregate (default: the current directory; ignored with --all)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a text report")
    args = parser.parse_args(argv)

    from rolo_claude.controller import compute_session_stats, format_cache_tokens_suffix

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    files = _session_jsonl_files(cwd, all_projects=args.all_projects)

    total = {"turns": 0, "total_cost_usd": 0.0, "per_model": {}, "tool_counts": {}}
    for path in files:
        _merge_stats(total, compute_session_stats(_read_nodes(path)))

    if args.json:
        print(json.dumps({"sessions": len(files), **total}, ensure_ascii=False))
        return 0

    scope = "all projects" if args.all_projects else str(cwd)
    print(f"rolo-claude stats ({scope}, {len(files)} session(s)):")
    print(f"  Turns: {total['turns']}")
    print(f"  Total cost: ${total['total_cost_usd']:.4f}")
    if total["per_model"]:
        print("  Per model:")
        for model, bucket in sorted(total["per_model"].items()):
            print(f"    {model}: {bucket['calls']} call(s), "
                  f"{bucket['input_tokens']}in/{bucket['output_tokens']}out tok"
                  f"{format_cache_tokens_suffix(bucket)}, ${bucket['cost_usd']:.4f}")
    if total["tool_counts"]:
        print("  Tool calls:")
        for name, n in sorted(total["tool_counts"].items()):
            print(f"    {name}: {n}")
    return 0
