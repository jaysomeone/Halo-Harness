"""rolo_claude.stats_cli -- `rolo-claude stats` subcommand (H8 scope D,
extended H10 Part A): a headless, cross-session version of the TUI's own
`/stats` (which only ever covers the ONE live session it's running in).

The ORIGINAL (no `--models`/`--tools`) path is UNCHANGED byte-for-byte --
it reads `meta.model` via `controller.compute_session_stats`, so it keeps
working on a session log recorded before H10 (no `usage.model`/`.provider`
fields at all). `--models`/`--tools` are a NEW, additive path over
`rolo_claude.telemetry`'s richer per-(model,provider)/per-tool aggregation,
which needs those H10 fields to attribute anything -- a pre-H10 log simply
contributes nothing to those two tables (it still counts under `--json`'s
plain `sessions`/`tool_counts` as before).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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


def _session_jsonl_files(cwd: Path, *, all_projects: bool, session_id: "str | None" = None) -> list:
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
    native ones. H10 Part A: `session_id`, when given, scopes to exactly
    that one session's own file (`--session ID`)."""
    from rolo_claude.agent.sessions import sessions_dir
    from rolo_claude.config.paths import bridge_home
    if session_id:
        root = bridge_home() / "sessions"
        pattern = f"*/{session_id}.jsonl" if all_projects else None
        if pattern is not None:
            return sorted(root.glob(pattern)) if root.is_dir() else []
        d = sessions_dir(cwd)
        f = d / f"{session_id}.jsonl"
        return [f] if f.exists() else []
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


def _rich_console(*, width: "int | None" = None, file=None):
    """Honours `NO_COLOR` (https://no-color.org) the same way every other
    rich-rendered surface in this codebase does; `rich` is a core
    dependency (pyproject.toml), never optional, so this import is safe
    from a plain headless CLI module too. `width`, when given, PINS the
    console's width instead of auto-detecting (and forces `force_terminal`
    so a non-stdout `file` -- a test's own StringIO -- still renders as
    one) -- see `_print_table`'s own comment on why the rich-table render
    path needs this. `file` defaults to rich's own default (`sys.stdout`)."""
    from rich.console import Console
    kwargs = {"no_color": bool(os.environ.get("NO_COLOR")), "highlight": False}
    if width is not None:
        kwargs["width"] = width
        kwargs["force_terminal"] = True
    if file is not None:
        kwargs["file"] = file
    return Console(**kwargs)


def _terminal_width() -> int:
    """H10b defect 2: the width a table is fit to -- `RC_TERM_WIDTH` (a
    dedicated test seam) wins if set; else `shutil.get_terminal_size()`,
    which already honours the standard `COLUMNS` env var itself before
    falling back to a real terminal query / its own (80, 24) default.
    Never below 80 (brief: "min 80 cols") -- a narrower terminal still
    gets the same sane compact layout rather than shrinking further."""
    override = os.environ.get("RC_TERM_WIDTH")
    if override:
        try:
            return max(80, int(override))
        except ValueError:
            pass
    return max(80, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _repair_hit_str(r: dict) -> str:
    top = max(r["repair_hit_pct"].items(), key=lambda kv: kv[1], default=("none", 0.0))
    return f"{top[1]:.0f}% {top[0]}" if top[1] else "0%"


def _ms_str(v) -> str:
    return f"{v:.0f}ms" if v is not None else "-"


# H10b defect 2: MOST -> least important, left to right. The compact
# default (`stats --models`) keeps the first 14 (exactly the brief's own
# list: model, provider, sessions, calls, tokens in/out, cost, avg
# latency, tool calls, tool err %, repair %, edit fail %, steers,
# compactions, loop trips); `--wide` keeps all of them. Either set is then
# further trimmed, right to left, to fit `_terminal_width()` -- see
# `_fit_columns`.
_MODEL_COLUMNS = [
    ("model", lambda r: r["model"]),
    ("provider", lambda r: r["provider"] or "-"),
    ("sessions", lambda r: str(r["sessions"])),
    ("calls", lambda r: str(r["calls"])),
    ("tok in/out", lambda r: f"{r['tokens_in']}/{r['tokens_out']}"),
    ("cost", lambda r: f"${r['cost_usd']:.4f}"),
    ("avg lat", lambda r: _ms_str(r["avg_latency_ms"])),
    ("tool calls", lambda r: str(r["tool_calls"])),
    ("tool err%", lambda r: f"{r['tool_error_pct']:.0f}%"),
    ("repair%", _repair_hit_str),
    ("edit fail%", lambda r: f"{r['edit_failure_pct']:.0f}%"),
    ("steers", lambda r: str(r["steers"])),
    ("compactions", lambda r: str(r["compactions"])),
    ("loop trips", lambda r: str(r["loop_breaker_trips"])),
    # -- wide-only from here down --
    ("route", lambda r: r["route"] or "-"),
    ("tok cached", lambda r: str(r["tokens_cached"])),
    ("avg ttft", lambda r: _ms_str(r["avg_ttft_ms"])),
    ("finish=len%", lambda r: f"{r['finish_length_pct']:.0f}%"),
    ("retries", lambda r: str(r["retries"])),
    ("overflow", lambda r: str(r["overflows"])),
    ("interrupt", lambda r: str(r["interrupts"])),
]
_MODEL_COLUMNS_COMPACT_N = 14

_TOOL_COLUMNS = [
    ("tool", lambda r: r["tool"]),
    ("calls", lambda r: str(r["calls"])),
    ("error%", lambda r: f"{r['error_pct']:.0f}%"),
    ("avg ms", lambda r: r["avg_ms"] is not None and f"{r['avg_ms']:.0f}" or "-"),
    ("avg bytes", lambda r: r["avg_bytes"] is not None and f"{r['avg_bytes']:.0f}" or "-"),
    ("spilled", lambda r: str(r["spilled"])),
    ("top error class", lambda r: (max(r["error_classes"].items(), key=lambda kv: kv[1], default=(None, 0))[0] or "-")),
]


def _estimate_width(columns: list, rows: list) -> int:
    total = 1  # left border
    for header, fmt in columns:
        w = len(header)
        for r in rows:
            w = max(w, len(fmt(r)))
        total += w + 3  # 1 padding space each side + 1 separator/border char
    return total


def _fit_columns(columns: list, rows: list, target_width: int) -> list:
    """Drops columns from the right (least important) until the estimated
    rendered width fits `target_width` -- never drops the last remaining
    column, so there's always something to show."""
    cols = list(columns)
    while len(cols) > 1 and _estimate_width(cols, rows) > target_width:
        cols.pop()
    return cols


def _print_plain_table(title: str, columns: list, rows: list) -> None:
    """H10b defect 2: the non-TTY path -- plain space-aligned text via the
    builtin `print` (never `Console.print`, which word-wraps a long line to
    its own detected width by default -- exactly the kind of silent
    reflow/truncation that would break a wide, deliberately-un-dropped
    plain table), no box drawing, no width-based column dropping, and no
    per-cell truncation, so piping `stats --models` to a file always
    carries every requested column's FULL value on one line."""
    print(title)
    widths = [max(len(header), max((len(fmt(r)) for r in rows), default=0)) for header, fmt in columns]
    print("  ".join(header.ljust(w) for (header, _), w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(fmt(r).ljust(w) for (_, fmt), w in zip(columns, widths)))


def _print_table(console, title: str, columns: list, rows: list) -> None:
    if not rows:
        empty = f"(no {title.lower()} activity in this window)"
        if console.is_terminal:
            console.print(empty)
        else:
            print(empty)
        return
    if not console.is_terminal:
        _print_plain_table(title, columns, rows)
        return
    from rich.table import Table

    target_width = _terminal_width()
    cols = _fit_columns(columns, rows, target_width)
    # A FRESH Console pinned to the exact width `_fit_columns` sized
    # against, writing to the SAME underlying file as `console` -- reusing
    # the auto-detecting `console` as-is here could let rich's own width
    # autodetection disagree with ours (e.g. `RC_TERM_WIDTH` set but not
    # the `COLUMNS` env var rich itself reads), silently bringing back the
    # "collapses to ..." bug this whole rewrite exists to fix.
    render_console = _rich_console(width=target_width, file=console.file)
    table = Table(title=title)
    for header, _ in cols:
        # `overflow="fold"` wraps an unexpectedly long value onto another
        # line inside its own cell instead of ever eliding it to "..." --
        # `_fit_columns` already sized the table to the terminal, so this
        # is a pure safety net, never the normal case.
        table.add_column(header, overflow="fold")
    for r in rows:
        table.add_row(*[fmt(r) for _, fmt in cols])
    render_console.print(table)


def _print_models_table(rows: list, *, wide: bool = False) -> None:
    console = _rich_console()
    columns = _MODEL_COLUMNS if wide else _MODEL_COLUMNS[:_MODEL_COLUMNS_COMPACT_N]
    _print_table(console, "Per model / provider", columns, rows)


def _print_tools_table(rows: list) -> None:
    console = _rich_console()
    _print_table(console, "Per tool", _TOOL_COLUMNS, rows)


def _cmd_stats_telemetry(args) -> int:
    """The `--models`/`--tools` path -- `rolo_claude.telemetry`'s richer
    per-(model,provider)/per-tool aggregation, scoped by `--since`/
    `--all-projects`/`--session`."""
    from rolo_claude import telemetry

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    slug = None
    if not args.all_projects:
        from rolo_claude.config.paths import project_slug
        slug = project_slug(cwd)
    summaries = telemetry.scan(since=args.since, slug=slug, all_projects=args.all_projects,
                                session_id=args.session)
    model_rows = telemetry.aggregate_by_model(summaries) if args.models else []
    tool_rows = telemetry.aggregate_by_tool(summaries) if args.tools else []

    if args.json:
        print(json.dumps({
            "sessions": len(summaries), "since": args.since,
            "models": model_rows, "tools": tool_rows,
            "top_error_classes": telemetry.top_error_classes(summaries),
        }, ensure_ascii=False))
        return 0

    scope = "all projects" if args.all_projects else str(cwd)
    # H10b defect 2: reflects whichever of --models/--tools was actually
    # asked for -- used to hardcode "--models" even for a bare --tools run.
    flags = " ".join(f for f, on in (("--models", args.models), ("--tools", args.tools)) if on)
    print(f"rolo-claude stats {flags} ({scope}, since {args.since}, {len(summaries)} session(s)):")
    if args.models:
        _print_models_table(model_rows, wide=args.wide)
    if args.tools:
        _print_tools_table(tool_rows)
    return 0


def cmd_stats(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude stats", add_help=True,
                                      description="Aggregate tokens/cost/tool-calls across session logs (headless /stats).")
    parser.add_argument("--all", action="store_true", dest="all_projects",
                         help="Aggregate every project's sessions, not just the current directory's")
    parser.add_argument("--all-projects", action="store_true", dest="all_projects",
                         help="Alias for --all")
    parser.add_argument("--cwd", default=None, metavar="DIR",
                         help="Project directory to aggregate (default: the current directory; ignored with --all)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a text report")
    parser.add_argument("--models", action="store_true",
                         help="Show the richer per-(model,provider) telemetry table (repairs, edit failures, ttft/latency, ...)")
    parser.add_argument("--tools", action="store_true", help="Show the per-tool telemetry table")
    parser.add_argument("--wide", action="store_true",
                         help="Show every --models column instead of the terminal-fit compact default")
    parser.add_argument("--since", default="7d", choices=("7d", "30d", "all"),
                         help="Time window for --models/--tools (default 7d; ignored by the plain report below)")
    parser.add_argument("--session", default=None, metavar="ID", help="Scope to one session id")
    args = parser.parse_args(argv)

    if args.models or args.tools:
        return _cmd_stats_telemetry(args)

    from rolo_claude.controller import compute_session_stats, format_cache_tokens_suffix

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    files = _session_jsonl_files(cwd, all_projects=args.all_projects, session_id=args.session)

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
