"""halo_harness.telemetry -- L0 telemetry (H10 Part A): scans session logs
under `~/.halo/sessions/<slug>/*.jsonl` and aggregates per-model and
per-tool counters for `halo stats --models/--tools`, `/stats
--models`, and `/improve`'s own evidence clustering
(`halo_harness/improve/evidence.py`). Derives EVERYTHING from non-wire
metadata already on the log nodes (`agent/log.py`'s `append_usage`/
`append_tool_result`/`append_assistant(tool_meta=...)`, all wired in
`agent/loop.py`) -- never a new model-visible field; see
`tests/test_telemetry_byte_identical.py` for the proof that adding it
never changes a derived request.

POSIX HOME first (`config.paths.bridge_home`); no Windows assumptions
anywhere in this module.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from halo_harness.config.paths import bridge_home

ERROR_CLASSES = (
    "schema_invalid", "not_found", "multiple_matches", "read_before_edit",
    "timeout", "denied_by_rule", "interrupted", "loop_breaker", "mcp_error", "other",
)
REPAIR_KINDS = ("leak_parser", "lenient_json", "rename", "args_repair", "none")
STATUS_VALUES = ("ok", "429", "5xx", "overflow", "aborted", "connect_error")

# Mirrors controller.py's own `_NON_PROMPT_USER_KINDS` (agent/log.py's
# `append_user(kind=...)` tags) -- a "user" node with one of these `kind`
# values is not a genuine new turn.
_NON_PROMPT_USER_KINDS = frozenset({
    "compaction_summary", "compaction_tail", "continuation", "agent_notice", "job_notice", "steer",
})

_SINCE_DAYS = {"7d": 7, "30d": 30, "all": None}


def _new_model_counters() -> dict:
    return {
        "route": "", "calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_usd": 0.0,
        "ttft_sum_ms": 0.0, "ttft_n": 0, "latency_sum_ms": 0.0, "latency_n": 0,
        "finish_length": 0, "retries": 0, "status_counts": {s: 0 for s in STATUS_VALUES},
        "tool_calls": 0, "tool_errors": 0, "tool_use_total": 0,
        "repair_hits": {k: 0 for k in REPAIR_KINDS},
        "edit_calls": 0, "edit_failures": 0,
        "steers": 0, "interrupts": 0, "compactions": 0, "overflows": 0,
        "error_classes": {c: 0 for c in ERROR_CLASSES},
    }


def _new_role_counters() -> dict:
    """V2c (H15) `stats --roles`: a rolled-up sub-agent usage node
    (`agent/log.py::append_usage(role=...)`) carries no per-turn tool/
    repair/latency breakdown of its own (that detail lives in the CHILD's
    own, separately-globbed `subagents/*.jsonl`, deliberately never
    double-counted here -- see `stats_cli.py`'s own docstring) -- just the
    spend `_rollup_child_cost_into_parent` already summed for that one
    Agent-tool call."""
    return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_usd": 0.0}


def _new_tool_counters() -> dict:
    return {"calls": 0, "errors": 0, "ms_sum": 0.0, "ms_n": 0, "bytes_sum": 0, "spilled": 0,
            "error_classes": {c: 0 for c in ERROR_CLASSES}}


def _content_text(content) -> str:
    """Best-effort plain text from a `tool_result` node's `content` --
    almost always a plain string, but defensively handles the block-list
    shape too (an image/mixed result), for `_legacy_error_class` below."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts)
    return "" if content is None else str(content)


def _legacy_error_class(tool: Optional[str], content) -> str:
    """H10b defect 1: best-effort `error_class` for a `tool_result` node
    written before H10 (the field didn't exist at all) -- a small,
    self-contained mirror of agent/loop.py's own `_classify_tool_error_text`
    PLUS the structural rejections that function deliberately leaves to its
    own call sites (permission denial, loop breaker, schema_invalid),
    reconstructed here from THIS codebase's own stable, first-party-authored
    message text (tools/edit.py, tools/read.py, agent/loop.py's
    `_resolve_tool_call`/loop-breaker/abort wording -- never model or file
    content, so this is telemetry categorization, not a content classifier).
    Deliberately NOT imported from `agent.loop`: that module pulls in the
    full provider/tool/hook import graph (~130ms measured, vs. ~35ms for
    this module alone), which every `stats`/`doctor` invocation would
    otherwise pay even when nothing needs classifying."""
    text = _content_text(content)
    if "has not been read yet" in text or "Read it again before editing" in text:
        return "read_before_edit"
    if "Found multiple matches" in text:
        return "multiple_matches"
    if "ABORTED_BEFORE_DISPATCH" in text or "interrupted by user" in text or "turn was interrupted" in text:
        return "interrupted"
    if "Loop breaker:" in text:
        return "loop_breaker"
    if "Permission denied" in text or "matches a deny rule" in text:
        return "denied_by_rule"
    if ("not valid JSON" in text or "Invalid arguments for" in text or "missing required parameter" in text
            or "must be an absolute path" in text):
        return "schema_invalid"
    if "not found" in text or "does not exist" in text:
        return "not_found"
    if "timed out" in text or "timeout" in text.lower():
        return "timeout"
    if tool and tool.startswith("mcp__"):
        return "mcp_error"
    return "other"


def _route_from_model(model: Optional[str]) -> Optional[str]:
    """`or:`/`dbx:`/`ant:` -- the same prefix `model.parse_model_ref` reads
    as the route, and the identical value H10's own `usage.route` field
    already carries (the fixtures show `"route": "or"`, no host suffix
    either) -- free to derive for a pre-H10 `usage` node that has a `model`
    but no `route` of its own."""
    if not model or ":" not in model:
        return None
    return model.split(":", 1)[0] or None


@dataclass
class SessionSummary:
    """One session's pre-aggregated counters -- small enough to round-trip
    through `~/.halo/stats-cache.json` so an unchanged file never
    needs re-parsing. `models` keys are `"<model>\\x1f<provider>"`."""
    session_id: str
    slug: str
    path: str
    mtime: float
    size: int
    corrupt_lines: int = 0
    turns: int = 0
    models: dict = field(default_factory=dict)
    # V2c (H15): role name -> `_new_role_counters()` -- `stats --roles`'s
    # own per-role spend aggregation. Absent from a pre-V2c cached entry
    # (`from_dict`'s "known fields" filter tolerates that already); a
    # session with no role-bearing sub-agent call simply leaves this `{}`.
    roles: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    error_examples: dict = field(default_factory=dict)  # error_class -> "sid#seq"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionSummary":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def _read_session_lines(path: Path) -> "tuple[list, int]":
    """`(nodes, corrupt_line_count)` -- a corrupt JSONL line is skipped and
    counted, never a crash (brief: "a corrupt line is skipped and counted,
    never a crash")."""
    nodes: list = []
    corrupt = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    nodes.append(json.loads(line))
                except json.JSONDecodeError:
                    corrupt += 1
    except OSError:
        pass
    return nodes, corrupt


def _summarize_nodes(*, session_id: str, slug: str, path: str, mtime: float, size: int,
                      nodes: list, corrupt_lines: int) -> SessionSummary:
    s = SessionSummary(session_id=session_id, slug=slug, path=path, mtime=mtime, size=size,
                        corrupt_lines=corrupt_lines)
    current_key: Optional[str] = None
    # H10b defect 3: tracks the session's active model from `meta`/model-
    # change nodes (agent/loop.py appends a fresh `meta` node with `model=`
    # on EVERY model switch, same shape as the session-start one -- see
    # `_switch_model`'s own comment) -- a pre-H10 `usage` node never carried
    # its own `model`/`provider` at all, so without this fallback its
    # tokens/cost/etc. were silently dropped entirely (nothing to key
    # `s.models` by), which is why `stats --models` over rolo's real,
    # nearly-all-pre-H10 logs used to show almost nothing.
    current_model: Optional[str] = None
    # H10b defect 1: `tool_use_id -> tool name`, built up as assistant
    # nodes stream by in log order (an assistant node carrying a
    # `tool_use` block always precedes the matching `tool_result` node in
    # the same session file) -- the join a pre-H10 `tool_result` node (no
    # `tool` field of its own) needs to attribute itself correctly instead
    # of falling into the aggregator's "?" bucket.
    tool_use_index: dict = {}
    # A failed/aborted call (agent/loop.py's own `_log_call_failure`) logs
    # a usage node with NO `provider` (nothing ever responded to name one)
    # -- falling back to the LAST real (model, provider) bucket this same
    # model already used in this session, rather than fragmenting into its
    # own "(model, '')" row, keeps `aggregate_by_model` from splitting one
    # model's stats across two rows just because one of its calls failed.
    last_key_for_model: dict = {}

    def model_bucket() -> Optional[dict]:
        if current_key is None:
            return None
        return s.models.setdefault(current_key, _new_model_counters())

    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype == "meta":
            if node.get("model"):
                current_model = node["model"]
                # H11b finding 18: a cc: turn logs its tool_use/tool_result
                # nodes BEFORE its own (single, end-of-turn) usage node, so
                # `current_key` -- otherwise only ever touched by a "usage"
                # node -- was still whatever the PREVIOUS turn (or route,
                # after an or:->cc: switch) left it at; every tool call
                # this turn was mis-attributed (or, on a session's first
                # turn ever, silently dropped: model_bucket() returned
                # None). `Session.__init__`/`clear()`/`set_model()` all log
                # a `model=` meta node before any tool call can happen, so
                # this alone fixes both cases -- a bare `tools=`-only
                # catalog-growth meta write (agent/loop.py's `_on_catalog_
                # grow`) never has a `model` key, so it never reaches here.
                # The FALLBACK provider guess matters too: `cc:` is always
                # deterministically provider "cc" (no per-call responding-
                # provider/failover concept applies to it at all), so a
                # session's very first cc: turn's tool calls land in the
                # SAME bucket its usage node will use a moment later,
                # rather than splitting into a same-model/no-provider row
                # of their own (`_route_from_model` covers or:/dbx:/ant:
                # reasonably, though those CAN legitimately fail over to a
                # different responding provider than their own prefix).
                guessed_provider = "cc" if current_model.startswith("cc:") else (_route_from_model(current_model) or "")
                current_key = last_key_for_model.get(current_model, f"{current_model}\x1f{guessed_provider}")
        elif ntype == "user":
            kind = node.get("kind")
            if kind not in _NON_PROMPT_USER_KINDS:
                s.turns += 1
            elif kind == "steer":
                b = model_bucket()
                if b is not None:
                    b["steers"] += 1
        elif ntype == "usage":
            # H10b defect 3: fall back to the session's tracked
            # `current_model` (from `meta`) when this usage node predates
            # H10 and never logged its own `model` -- see `current_model`'s
            # own comment above for why this is the fix for the "almost no
            # model rows" bug.
            model = node.get("model") or current_model
            if model:
                provider = node.get("provider")
                if provider:
                    current_key = f"{model}\x1f{provider}"
                    last_key_for_model[model] = current_key
                else:
                    current_key = last_key_for_model.get(model, f"{model}\x1f")
                b = model_bucket()
                route = node.get("route") or _route_from_model(model)
                if route:
                    b["route"] = route
                usage = node.get("usage") or {}
                b["calls"] += 1
                b["tokens_in"] += int(usage.get("input_tokens") or 0)
                b["tokens_out"] += int(usage.get("output_tokens") or 0) + int(usage.get("reasoning_tokens") or 0)
                b["tokens_cached"] += (int(usage.get("cache_read_input_tokens") or 0)
                                        + int(usage.get("cache_creation_input_tokens") or 0))
                cost = node.get("cost_usd")
                if isinstance(cost, (int, float)):
                    b["cost_usd"] += cost
                if isinstance(node.get("ttft_ms"), (int, float)):
                    b["ttft_sum_ms"] += node["ttft_ms"]
                    b["ttft_n"] += 1
                if isinstance(node.get("latency_ms"), (int, float)):
                    b["latency_sum_ms"] += node["latency_ms"]
                    b["latency_n"] += 1
                if node.get("finish_reason") in ("max_tokens", "length"):
                    b["finish_length"] += 1
                b["retries"] += int(node.get("retries") or 0)
                status = node.get("status") or "ok"
                if status in b["status_counts"]:
                    b["status_counts"][status] += 1
                # V2c (H15): a sub-agent rollup's own `role` tag (absent on
                # every ordinary direct-model-call usage node, and on any
                # pre-V2c log) -- `stats --roles`'s own per-role spend.
                role = node.get("role")
                if role:
                    rb = s.roles.setdefault(role, _new_role_counters())
                    rb["calls"] += 1
                    rb["tokens_in"] += int(usage.get("input_tokens") or 0)
                    rb["tokens_out"] += int(usage.get("output_tokens") or 0) + int(usage.get("reasoning_tokens") or 0)
                    rb["tokens_cached"] += (int(usage.get("cache_read_input_tokens") or 0)
                                             + int(usage.get("cache_creation_input_tokens") or 0))
                    if isinstance(cost, (int, float)):
                        rb["cost_usd"] += cost
        elif ntype == "assistant":
            if node.get("stop_reason") == "interrupted":
                b = model_bucket()
                if b is not None:
                    b["interrupts"] += 1
            tool_meta = node.get("tool_meta") or {}
            # H10b defect 1: a pre-H10 assistant node never had `tool_meta`
            # at all -- when a `repaired` flag was logged pre-H10, it lived
            # directly on the node as a `{tool_use_id: bool}` map (tool_
            # meta's own precursor) or, failing that, on the block itself.
            # Checked ONLY as a fallback when `tool_meta` has nothing for
            # this id, so an H10-shaped node's behavior is unchanged.
            legacy_repaired_map = node.get("repaired") if isinstance(node.get("repaired"), dict) else None
            for block in node.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_id = block.get("id")
                    if tool_use_id:
                        # H10b defect 1: indexed regardless of whether a
                        # model bucket exists yet -- the join a `tool_
                        # result` node needs must never depend on model
                        # attribution succeeding first.
                        tool_use_index[tool_use_id] = block.get("name") or "?"
                    b = model_bucket()
                    if b is None:
                        continue
                    b["tool_use_total"] += 1
                    meta = tool_meta.get(tool_use_id)
                    if meta is not None:
                        repaired = bool(meta.get("repaired"))
                        kind = meta.get("repair_kind") or "none"
                    else:
                        legacy = legacy_repaired_map.get(tool_use_id) if legacy_repaired_map else None
                        if legacy is None and isinstance(block.get("repaired"), bool):
                            legacy = block.get("repaired")
                        repaired, kind = bool(legacy), "none"
                    if repaired and kind in b["repair_hits"]:
                        b["repair_hits"][kind] += 1
        elif ntype == "tool_result":
            # H10b defect 1: a pre-H10 tool_result node never carried
            # `tool` -- joined against `tool_use_index` (built from the
            # preceding assistant node's own tool_use block in THIS same
            # session, always logged first) before ever falling back to
            # "?", which is now reserved for the genuinely unresolvable
            # case (no matching tool_use anywhere in the file).
            tool = node.get("tool") or tool_use_index.get(node.get("tool_use_id")) or "?"
            is_error = bool(node.get("is_error"))
            if is_error:
                error_class = node.get("error_class") or _legacy_error_class(tool, node.get("content"))
            else:
                error_class = None
            tc = s.tools.setdefault(tool, _new_tool_counters())
            tc["calls"] += 1
            if is_error:
                tc["errors"] += 1
                if error_class in tc["error_classes"]:
                    tc["error_classes"][error_class] += 1
                s.error_examples.setdefault(error_class, f"{session_id}#{node.get('seq')}")
            if isinstance(node.get("ms"), (int, float)):
                tc["ms_sum"] += node["ms"]
                tc["ms_n"] += 1
            if isinstance(node.get("bytes"), (int, float)):
                tc["bytes_sum"] += node["bytes"]
            if node.get("spilled"):
                tc["spilled"] += 1
            b = model_bucket()
            if b is not None:
                b["tool_calls"] += 1
                if is_error:
                    b["tool_errors"] += 1
                    if error_class in b["error_classes"]:
                        b["error_classes"][error_class] += 1
                if tool == "Edit":
                    b["edit_calls"] += 1
                    if error_class in ("not_found", "multiple_matches"):
                        b["edit_failures"] += 1
        elif ntype == "compacted":
            b = model_bucket()
            if b is not None:
                b["compactions"] += 1
                if node.get("trigger") == "overflow":
                    b["overflows"] += 1
    return s


# ---------------------------------------------------------------------------
# Cache: ~/.halo/stats-cache.json keyed by (path, size, mtime).
# ---------------------------------------------------------------------------

def cache_path() -> Path:
    return bridge_home() / "stats-cache.json"


def _load_cache() -> dict:
    p = cache_path()
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass  # best-effort -- a failed cache write never fails a scan


def cache_age_seconds() -> Optional[float]:
    """`doctor`'s own "cache age" line; None if the cache has never been
    written."""
    p = cache_path()
    try:
        # Clamp at 0: on Windows a just-written file's mtime can land a few
        # milliseconds after time.time(), which would read as a negative age.
        return max(0.0, time.time() - p.stat().st_mtime)
    except OSError:
        return None


def _since_cutoff(since: str) -> Optional[float]:
    """"7d"/"30d"/"all" are the documented vocabulary, but any "<N>d"
    string (`halo_harness.improve.config.since_str` can produce one from a
    custom `improve.since_days`) parses too; anything else falls back to
    7 days rather than raising on a hand-edited config value."""
    if since == "all":
        return None
    days = _SINCE_DAYS.get(since)
    if days is None and isinstance(since, str) and since.endswith("d"):
        try:
            days = int(since[:-1])
        except ValueError:
            days = None
    if days is None:
        days = 7
    return time.time() - days * 86400


def _candidate_files(sessions_dir: Path, *, slug: Optional[str], all_projects: bool,
                      session_id: Optional[str]) -> list:
    if not sessions_dir.is_dir():
        return []
    if session_id:
        pattern = f"*/{session_id}.jsonl" if all_projects or not slug else f"{slug}/{session_id}.jsonl"
        return sorted(sessions_dir.glob(pattern))
    if all_projects or not slug:
        return sorted(sessions_dir.glob("*/*.jsonl"))
    d = sessions_dir / slug
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


# ---------------------------------------------------------------------------
# Public re-exports for halo_harness.improve.evidence, which needs the same
# candidate-file/since-window/line-reading logic but a RAW node pass (for
# excerpt text), not the pre-aggregated SessionSummary counters above.
# ---------------------------------------------------------------------------

def candidate_session_files(sessions_dir: Optional[Path] = None, *, since: str = "7d",
                             slug: Optional[str] = None, all_projects: bool = False,
                             session_id: Optional[str] = None) -> "list[Path]":
    sessions_dir = Path(sessions_dir) if sessions_dir is not None else (bridge_home() / "sessions")
    cutoff = _since_cutoff(since)
    out = []
    for path in _candidate_files(sessions_dir, slug=slug, all_projects=all_projects, session_id=session_id):
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        out.append(path)
    return out


def read_session_lines(path: Path) -> "tuple[list, int]":
    return _read_session_lines(path)


def scan(sessions_dir: Optional[Path] = None, *, since: str = "7d", slug: Optional[str] = None,
         all_projects: bool = False, session_id: Optional[str] = None,
         use_cache: bool = True) -> "list[SessionSummary]":
    """Scan every matching `*.jsonl` under `sessions_dir` (default
    `bridge_home()/"sessions"`), returning one `SessionSummary` per file.
    `since` is "7d"|"30d"|"all", applied to the file's own mtime (a fast,
    good-enough proxy for session recency that lets an out-of-window file
    be skipped WITHOUT ever opening it). `slug` scopes to one project
    (ignored when `all_projects`); neither given scans every project, same
    as `all_projects=True` -- callers that want "this project only" must
    pass `slug`."""
    sessions_dir = Path(sessions_dir) if sessions_dir is not None else (bridge_home() / "sessions")
    cutoff = _since_cutoff(since)
    cache = _load_cache() if use_cache else {}
    cache_dirty = False
    out: list = []
    for path in _candidate_files(sessions_dir, slug=slug, all_projects=all_projects, session_id=session_id):
        try:
            st = path.stat()
        except OSError:
            continue
        if cutoff is not None and st.st_mtime < cutoff:
            continue
        key = str(path)
        cached = cache.get(key)
        if (use_cache and isinstance(cached, dict) and cached.get("size") == st.st_size
                and cached.get("mtime") == st.st_mtime and isinstance(cached.get("summary"), dict)):
            out.append(SessionSummary.from_dict(cached["summary"]))
            continue
        nodes, corrupt = _read_session_lines(path)
        this_slug = path.parent.name
        summary = _summarize_nodes(session_id=path.stem, slug=this_slug, path=key, mtime=st.st_mtime,
                                    size=st.st_size, nodes=nodes, corrupt_lines=corrupt)
        out.append(summary)
        cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "summary": summary.to_dict()}
        cache_dirty = True
    if cache_dirty and use_cache:
        _save_cache(cache)
    return out


# ---------------------------------------------------------------------------
# Aggregation.
# ---------------------------------------------------------------------------

def _pct(numer: int, denom: int) -> float:
    return round(100.0 * numer / denom, 1) if denom else 0.0


def _avg(total: float, n: int) -> Optional[float]:
    return round(total / n, 1) if n else None


def aggregate_by_model(summaries: "list[SessionSummary]") -> "list[dict]":
    """Rows per (model, provider), sorted by (model, provider) for
    deterministic output: sessions, turns, model calls, tokens in/out/
    cached, cost, avg ttft/latency, finish=length %, retries/429s,
    overflows, tool calls, tool error %, repair-hit % by kind, edit
    failures (not_found + multiple_matches) %, steers, interrupts,
    compactions, loop-breaker trips."""
    merged: dict = {}
    sessions_per_key: dict = {}
    for s in summaries:
        for key, b in s.models.items():
            model, _, provider = key.partition("\x1f")
            dest = merged.setdefault(key, _new_model_counters())
            sessions_per_key.setdefault(key, set()).add(s.session_id)
            dest["route"] = b.get("route") or dest["route"]
            for field_name in ("calls", "tokens_in", "tokens_out", "tokens_cached", "cost_usd",
                                "ttft_sum_ms", "ttft_n", "latency_sum_ms", "latency_n", "finish_length",
                                "retries", "tool_calls", "tool_errors", "tool_use_total",
                                "edit_calls", "edit_failures", "steers", "interrupts", "compactions", "overflows"):
                dest[field_name] += b.get(field_name, 0)
            for status, n in (b.get("status_counts") or {}).items():
                dest["status_counts"][status] = dest["status_counts"].get(status, 0) + n
            for kind, n in (b.get("repair_hits") or {}).items():
                dest["repair_hits"][kind] = dest["repair_hits"].get(kind, 0) + n
            for cls, n in (b.get("error_classes") or {}).items():
                dest["error_classes"][cls] = dest["error_classes"].get(cls, 0) + n

    rows = []
    for key in sorted(merged):
        model, _, provider = key.partition("\x1f")
        c = merged[key]
        rows.append({
            "model": model, "provider": provider or None, "route": c["route"] or None,
            "sessions": len(sessions_per_key.get(key, ())), "turns": None,  # turns is session-scoped, not model-scoped
            "calls": c["calls"], "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"],
            "tokens_cached": c["tokens_cached"], "cost_usd": round(c["cost_usd"], 4),
            "avg_ttft_ms": _avg(c["ttft_sum_ms"], c["ttft_n"]), "avg_latency_ms": _avg(c["latency_sum_ms"], c["latency_n"]),
            "finish_length_pct": _pct(c["finish_length"], c["calls"]),
            "retries": c["retries"], "status_counts": c["status_counts"], "overflows": c["overflows"],
            "tool_calls": c["tool_calls"], "tool_error_pct": _pct(c["tool_errors"], c["tool_calls"]),
            "repair_hit_pct": {k: _pct(n, c["tool_use_total"]) for k, n in c["repair_hits"].items()},
            "edit_failure_pct": _pct(c["edit_failures"], c["edit_calls"]),
            "steers": c["steers"], "interrupts": c["interrupts"], "compactions": c["compactions"],
            "loop_breaker_trips": c["error_classes"].get("loop_breaker", 0),
        })
    # `turns` is a session-level counter (one session may talk to several
    # models); reported once per model as "sessions this model was used in"
    # already covers the useful cross-session signal, so `turns` above is
    # intentionally left as a per-model placeholder (None) rather than
    # double-counting a session's turns once per model it happened to use.
    return rows


def aggregate_by_role(summaries: "list[SessionSummary]") -> "list[dict]":
    """V2c (H15) `stats --roles`: rows per role name, sorted, sessions/
    calls/tokens/cost -- summed straight from each session's own `s.roles`
    (populated only from a rolled-up sub-agent usage node's `role` tag;
    see `_summarize_nodes`)."""
    merged: dict = {}
    sessions_per_role: dict = {}
    for s in summaries:
        for role, b in s.roles.items():
            dest = merged.setdefault(role, _new_role_counters())
            sessions_per_role.setdefault(role, set()).add(s.session_id)
            for field_name in ("calls", "tokens_in", "tokens_out", "tokens_cached", "cost_usd"):
                dest[field_name] += b.get(field_name, 0)
    rows = []
    for role in sorted(merged):
        c = merged[role]
        rows.append({
            "role": role, "sessions": len(sessions_per_role.get(role, ())), "calls": c["calls"],
            "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"], "tokens_cached": c["tokens_cached"],
            "cost_usd": round(c["cost_usd"], 4),
        })
    return rows


def aggregate_by_tool(summaries: "list[SessionSummary]") -> "list[dict]":
    merged: dict = {}
    for s in summaries:
        for name, tc in s.tools.items():
            dest = merged.setdefault(name, _new_tool_counters())
            dest["calls"] += tc.get("calls", 0)
            dest["errors"] += tc.get("errors", 0)
            dest["ms_sum"] += tc.get("ms_sum", 0.0)
            dest["ms_n"] += tc.get("ms_n", 0)
            dest["bytes_sum"] += tc.get("bytes_sum", 0)
            dest["spilled"] += tc.get("spilled", 0)
            for cls, n in (tc.get("error_classes") or {}).items():
                dest["error_classes"][cls] = dest["error_classes"].get(cls, 0) + n
    rows = []
    for name in sorted(merged):
        c = merged[name]
        rows.append({
            "tool": name, "calls": c["calls"], "error_pct": _pct(c["errors"], c["calls"]),
            "avg_ms": _avg(c["ms_sum"], c["ms_n"]), "avg_bytes": _avg(c["bytes_sum"], c["ms_n"] or c["calls"]),
            "spilled": c["spilled"], "error_classes": {k: v for k, v in c["error_classes"].items() if v},
        })
    return rows


def top_error_classes(summaries: "list[SessionSummary]", *, n: int = 5) -> "list[dict]":
    """Global error-class counts across every scanned session's tool
    results (`s.tools[*]["error_classes"]`, the authoritative per-class
    tally), each with ONE example `session#seq` (`s.error_examples`, the
    first `_summarize_nodes` saw of that class in that session)."""
    counts: dict = {}
    example: dict = {}
    for s in summaries:
        for tc in s.tools.values():
            for cls, c in (tc.get("error_classes") or {}).items():
                if c:
                    counts[cls] = counts.get(cls, 0) + c
        for cls, ex in s.error_examples.items():
            example.setdefault(cls, ex)
    rows = [{"error_class": cls, "count": cnt, "example": example.get(cls)}
            for cls, cnt in counts.items() if cnt > 0]
    rows.sort(key=lambda r: (-r["count"], r["error_class"]))
    return rows[:n]


def total_sessions_count(sessions_dir: Optional[Path] = None) -> int:
    """`doctor`'s own "sessions" line -- a cheap glob count, no parsing."""
    sessions_dir = Path(sessions_dir) if sessions_dir is not None else (bridge_home() / "sessions")
    if not sessions_dir.is_dir():
        return 0
    return sum(1 for _ in sessions_dir.glob("*/*.jsonl"))
