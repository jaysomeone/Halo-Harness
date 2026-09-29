"""rolo_claude.work_matrix -- H14 scope H: `doctor --work --probe-all`. Sends
one short pong through every discovered chat-shaped Databricks endpoint on
its OWN chosen path (plus the anthropic gateway too, for Claude/GLM/Kimi,
with `--both`), records HTTP status/first tokens/latency/token-cost/tool-call
support, prints a table, and writes `~/.rolo-claude/work-matrix-<date>.json`
with endpoint names only -- never a host, never a token. Reuses the SAME
`stream_completion`/`stream_anthropic_completion` machinery a real turn
drives, so a green row here really means "a real turn on this route works".
Mock-verified only (the real workspace is behind an IP access list from this
box) -- see tests/test_dbx_work_matrix.py.
"""

from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

_PONG_PROMPT = "Reply with the single word pong."
_READ_TOOL = {"type": "function", "function": {
    "name": "Read", "description": "Read the contents of a local file.",
    "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
}}


@dataclass
class ProbeRow:
    name: str
    family: str
    path_type: str
    status: str = "?"
    first_tokens: str = ""
    latency_ms: Optional[float] = None
    tokens_out: Optional[int] = None
    tool_call_ok: Optional[bool] = None
    error: Optional[str] = None


def _openai_pong_body(with_tools: bool) -> dict:
    body = {"messages": [{"role": "user", "content": _PONG_PROMPT}], "max_tokens": 16, "stream": True}
    if with_tools:
        body["messages"] = [{"role": "user", "content": "Call the Read tool on pong.txt, then stop."}]
        body["tools"] = [_READ_TOOL]
    return body


def _anthropic_pong_body(model: str, with_tools: bool) -> dict:
    body = {"model": model, "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": _PONG_PROMPT}]}
    if with_tools:
        body["messages"] = [{"role": "user", "content": "Call the Read tool on pong.txt, then stop."}]
        body["tools"] = [{"name": "Read", "description": "Read a local file.",
                           "input_schema": _READ_TOOL["function"]["parameters"]}]
    return body


def _drive_openai(name: str, root: str, token: str, headers: dict, state_dir, *, with_tools: bool) -> ProbeRow:
    from rolo_claude.providers.databricks import dbx_cache_get_route
    from rolo_claude.providers.dbx_routing import chat_route_candidates
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_completion

    row = ProbeRow(name=name, family="", path_type="?")
    route = Route(provider="databricks", upstream_model=name, dialect="openai-chat")
    req = CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=root, api_key=token), state_dir=state_dir, extra_headers=headers,
        model_label=name, prebuilt_oai_body=_openai_pong_body(with_tools),
    )
    start = time.monotonic()
    text_parts, saw_tool_call, usage = [], False, {}
    try:
        for ev in stream_completion(req):
            if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
                saw_tool_call = True
            if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                text_parts.append(ev["delta"].get("text", ""))
            if ev.get("type") == "message_delta" and isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
        row.status = "200"
    except UpstreamError as e:
        row.status = str(e.status)
        row.error = e.message
    except Exception as e:
        row.status = "error"
        row.error = f"{type(e).__name__}: {e}"
    row.latency_ms = (time.monotonic() - start) * 1000
    row.first_tokens = "".join(text_parts)[:60]
    row.tokens_out = usage.get("output_tokens")
    if with_tools:
        row.tool_call_ok = saw_tool_call
    cands = chat_route_candidates(name, state_dir)
    used_idx = dbx_cache_get_route(name, state_dir)
    row.path_type = cands[used_idx].key if isinstance(used_idx, int) and 0 <= used_idx < len(cands) else "?"
    return row


def _drive_anthropic(name: str, root: str, token: str, headers: dict, state_dir, *, with_tools: bool) -> ProbeRow:
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_anthropic_completion

    row = ProbeRow(name=name, family="", path_type="anthropic")
    route = Route(provider="databricks", upstream_model=name, dialect="anthropic-passthrough")
    req = CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 200000, "max_output_tokens": 8192},
        creds=ProviderCreds(base_url=root, api_key=token), state_dir=state_dir, extra_headers=headers,
        model_label=name, prebuilt_anthropic_body=_anthropic_pong_body(name, with_tools),
    )
    start = time.monotonic()
    text_parts, saw_tool_call, usage = [], False, {}
    try:
        for ev in stream_anthropic_completion(req):
            if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
                saw_tool_call = True
            if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                text_parts.append(ev["delta"].get("text", ""))
            if ev.get("type") == "message_delta" and isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
        row.status = "200"
    except UpstreamError as e:
        row.status = str(e.status)
        row.error = e.message
    except Exception as e:
        row.status = "error"
        row.error = f"{type(e).__name__}: {e}"
    row.latency_ms = (time.monotonic() - start) * 1000
    row.first_tokens = "".join(text_parts)[:60]
    row.tokens_out = usage.get("output_tokens")
    if with_tools:
        row.tool_call_ok = saw_tool_call
    return row


def run_work_matrix(*, only: Optional[str] = None, both: bool = False, tools: bool = False,
                     state_dir=None) -> "tuple[list[ProbeRow], Path]":
    """Runs the probe over every cached chat-shaped endpoint (`--only`
    globs the endpoint name), writes the JSON report, and returns
    `(rows, report_path)`. Never raises: a Databricks-not-configured/
    unreachable box returns `([], report_path)` with the report itself
    naming the reason (still written, so a caller always has SOMETHING to
    look at)."""
    from rolo_claude.config.paths import bridge_home
    from rolo_claude.providers.config import (
        derive_workspace_root, merge_databricks_headers, resolve_databricks,
    )
    from rolo_claude.providers.databricks import load_dbx_endpoints_json
    from rolo_claude.providers.dbx_routing import classify_family, resolve_databricks_dialect

    state_dir = state_dir if state_dir is not None else bridge_home()
    report_path = state_dir / f"work-matrix-{date.today().isoformat()}.json"

    dbx = resolve_databricks()
    if dbx is None:
        _write_report(report_path, [], note="Databricks not configured")
        return [], report_path

    root = derive_workspace_root(dbx.host)
    headers = merge_databricks_headers(dbx.custom_headers)
    endpoints = load_dbx_endpoints_json(state_dir)
    names = sorted(endpoints)
    if only:
        names = [n for n in names if fnmatch.fnmatch(n, only)]

    rows: list = []
    for name in names:
        e = endpoints[name] if isinstance(endpoints[name], dict) else {}
        family = classify_family(name, foundation_model_name=e.get("foundation_model_name") or "",
                                  model_class=e.get("model_class") or "")
        if family == "non_chat":
            continue
        _clean, dialect = resolve_databricks_dialect(name, state_dir)
        if dialect == "anthropic-passthrough":
            row = _drive_anthropic(name, root, dbx.token, headers, state_dir, with_tools=tools)
        else:
            row = _drive_openai(name, root, dbx.token, headers, state_dir, with_tools=tools)
        row.family = family
        rows.append(row)
        if both and family in ("claude_foundation", "glm", "kimi") and dialect != "anthropic-passthrough":
            both_row = _drive_anthropic(name, root, dbx.token, headers, state_dir, with_tools=tools)
            both_row.family = family
            both_row.name = f"{name} (anthropic gateway)"
            rows.append(both_row)

    _write_report(report_path, rows)
    return rows, report_path


def _write_report(path: Path, rows: list, *, note: Optional[str] = None) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": [
            {"endpoint": r.name, "family": r.family, "path_type": r.path_type, "status": r.status,
             "first_tokens": r.first_tokens, "latency_ms": (round(r.latency_ms, 1) if r.latency_ms else None),
             "tokens_out": r.tokens_out, "tool_call_ok": r.tool_call_ok, "error": r.error}
            for r in rows
        ],
    }
    if note:
        payload["note"] = note
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def format_table(rows: list, *, tools: bool = False) -> str:
    header = f"{'endpoint':<44} {'family':<16} {'path':<12} {'status':>6} {'ms':>7} {'out-tok':>7}"
    if tools:
        header += f" {'tool-ok':>7}"
    header += "  first tokens"
    lines = [header]
    for r in rows:
        line = (f"{r.name:<44} {r.family:<16} {r.path_type:<12} {r.status:>6} "
                f"{(f'{r.latency_ms:.0f}' if r.latency_ms is not None else '?'):>7} "
                f"{(r.tokens_out if r.tokens_out is not None else '?'):>7}")
        if tools:
            line += f" {('yes' if r.tool_call_ok else ('no' if r.tool_call_ok is False else '?')):>7}"
        line += f"  {r.first_tokens or r.error or ''}"
        lines.append(line)
    return "\n".join(lines)
