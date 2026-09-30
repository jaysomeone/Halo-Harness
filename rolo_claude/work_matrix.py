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
import sys
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
    # V2a open question 2 ("route split per endpoint from the cache"): the
    # candidate key `~/.rolo-claude/routes-cache.json` already named for this
    # model BEFORE this probe ran, vs. `path_type` (what this run actually
    # used/re-cached) -- a mismatch is a visible route split. Always None on
    # the anthropic dialect (it has no candidate cache of its own).
    cached_path_type: Optional[str] = None
    # V2a open question 1 ("reasoning replay after a tool call per family"):
    # None when not applicable (no --tools, or the first turn never produced
    # a tool_use to replay), else whether a REAL second turn -- built through
    # the exact same providers.request builder + hooks.reasoning_echo a live
    # session uses -- replaying the captured reasoning/thinking plus the tool
    # result was accepted by the upstream.
    reasoning_replay_ok: Optional[bool] = None


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


def _cached_candidate_key(name: str, state_dir) -> Optional[str]:
    """The candidate key `~/.rolo-claude/routes-cache.json` names for `name`
    RIGHT NOW (before/after a probe runs) -- None when nothing is cached yet."""
    from rolo_claude.providers.databricks import dbx_cache_get_route
    from rolo_claude.providers.dbx_routing import chat_route_candidates
    cands = chat_route_candidates(name, state_dir)
    idx = dbx_cache_get_route(name, state_dir)
    return cands[idx].key if isinstance(idx, int) and 0 <= idx < len(cands) else None


def _accumulate_blocks(events: list) -> "tuple[list, dict]":
    """Reconstruct the harness's own internal Anthropic-shaped content
    blocks (text/thinking-with-signature/tool_use-with-parsed-input) plus
    the terminal `harness_meta` dict from a `stream_completion`/
    `stream_anthropic_completion` event iterator -- both dialects yield the
    SAME Anthropic-shaped event vocabulary (that translation is the whole
    point of `stream_completion`), so one accumulator drives a faithful
    second-turn replay for either one. A `thinking` block's content is kept
    under `"text"` (never `"thinking"`) -- the harness's own logged-node
    convention `providers.request.prepare_anthropic_messages` expects."""
    blocks: dict = {}
    order: list = []
    raw_json: dict = {}
    harness_meta: dict = {}
    for ev in events:
        et = ev.get("type")
        idx = ev.get("index")
        if et == "content_block_start":
            order.append(idx)
            blocks[idx] = dict(ev.get("content_block") or {})
            raw_json[idx] = ""
        elif et == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                blocks[idx]["text"] = blocks[idx].get("text", "") + delta.get("text", "")
            elif dtype == "thinking_delta":
                blocks[idx]["text"] = blocks[idx].get("text", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                blocks[idx]["signature"] = blocks[idx].get("signature", "") + delta.get("signature", "")
            elif dtype == "input_json_delta":
                raw_json[idx] += delta.get("partial_json", "")
        elif et == "content_block_stop" and blocks.get(idx, {}).get("type") == "tool_use":
            try:
                blocks[idx]["input"] = json.loads(raw_json.get(idx) or "{}")
            except (json.JSONDecodeError, ValueError):
                blocks[idx]["input"] = {}
        elif et == "message_delta" and isinstance(ev.get("harness_meta"), dict):
            harness_meta = ev["harness_meta"]
    return [blocks[i] for i in order], harness_meta


_REPLAY_TOOL_PROMPT = "Call the Read tool on pong.txt, then stop."


def _turn2_messages(blocks: list, harness_meta: dict) -> "tuple[list, Optional[str]]":
    """Turn 1's accumulated blocks (+ any captured reasoning) -> a synthetic
    turn-2 transcript in the harness's own internal (Anthropic-shaped) log
    convention: the original prompt, the assistant's thinking/text/tool_use
    reply (with its logged `reasoning` sibling field for the openai-chat
    dialect, which never turns reasoning into a content block), then a user
    turn carrying the tool_result -- exactly what `derive_request()` would
    hand the real request builders for the very next turn. Returns
    `(messages, tool_use_id)`; `tool_use_id` is None if turn 1 never
    produced one (callers only reach here when it did)."""
    assistant_content = []
    tool_use_id = None
    for b in blocks:
        bt = b.get("type")
        if bt == "thinking" and b.get("signature"):
            assistant_content.append({"type": "thinking", "text": b.get("text", ""), "signature": b["signature"]})
        elif bt == "text" and b.get("text"):
            assistant_content.append({"type": "text", "text": b["text"]})
        elif bt == "tool_use":
            tool_use_id = b.get("id")
            assistant_content.append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                                       "input": b.get("input") or {}})
    assistant_msg: dict = {"role": "assistant", "content": assistant_content}
    if harness_meta.get("reasoning_text") or harness_meta.get("reasoning_details"):
        assistant_msg["reasoning"] = {"text": harness_meta.get("reasoning_text") or "",
                                       "details": harness_meta.get("reasoning_details")}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _REPLAY_TOOL_PROMPT}]},
        assistant_msg,
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "pong"}]},
    ]
    return messages, tool_use_id


_REPLAY_TOOL_ANTHROPIC = {"name": "Read", "description": "Read a local file.",
                           "input_schema": _READ_TOOL["function"]["parameters"]}


def _probe_reasoning_replay_openai(name: str, route, profile, blocks: list, harness_meta: dict,
                                    root: str, token: str, headers: dict, state_dir) -> bool:
    """V2a open question 1 (mlflow/cursor/invocations side): build turn 2
    through the REAL `providers.request.build_request_body` (so
    `hooks.reasoning_echo` applies the exact per-family replay rule a live
    session would) and send it -- True iff the upstream accepted it (a
    clean stream with no error event), False on any error/exception."""
    from rolo_claude.providers.request import build_request_body
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, stream_completion
    messages, tool_use_id = _turn2_messages(blocks, harness_meta)
    if tool_use_id is None:
        return False
    body2 = build_request_body(system_text="", messages=messages, tools=[_REPLAY_TOOL_ANTHROPIC], route=route,
                                profile=profile, context_tokens=128000, prompt_estimate=100, requested_max_tokens=16)
    req2 = CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=root, api_key=token), state_dir=state_dir, extra_headers=headers,
        model_label=name, prebuilt_oai_body=body2, harness_mode=True,
    )
    try:
        return not any(ev.get("type") == "error" for ev in stream_completion(req2))
    except Exception:
        return False


def _drive_openai(name: str, root: str, token: str, headers: dict, state_dir, *, with_tools: bool) -> ProbeRow:
    from rolo_claude.providers.profiles import resolve_profile
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_completion

    row = ProbeRow(name=name, family="", path_type="?")
    route = Route(provider="databricks", upstream_model=name, dialect="openai-chat")
    row.cached_path_type = _cached_candidate_key(name, state_dir)
    profile = resolve_profile(route)
    req = CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=root, api_key=token), state_dir=state_dir, extra_headers=headers,
        model_label=name, prebuilt_oai_body=_openai_pong_body(with_tools), harness_mode=True,
    )
    start = time.monotonic()
    text_parts, saw_tool_call, usage, events_list = [], False, {}, []
    try:
        for ev in stream_completion(req):
            events_list.append(ev)
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
        if saw_tool_call and row.status == "200":
            blocks, harness_meta = _accumulate_blocks(events_list)
            row.reasoning_replay_ok = _probe_reasoning_replay_openai(
                name, route, profile, blocks, harness_meta, root, token, headers, state_dir)
    row.path_type = _cached_candidate_key(name, state_dir) or "?"
    return row


def _probe_reasoning_replay_anthropic(name: str, route, profile, blocks: list,
                                       root: str, token: str, headers: dict, state_dir) -> bool:
    """V2a open question 1 (anthropic/v1/messages side): build turn 2
    through the REAL `providers.request.build_anthropic_request_body` (so a
    genuinely signed `thinking` block, when turn 1 produced one, is replayed
    exactly as `prepare_anthropic_messages` would for a live session) --
    True iff the upstream accepted it, False on any error/exception."""
    from rolo_claude.providers.request import build_anthropic_request_body
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, stream_anthropic_completion
    messages, tool_use_id = _turn2_messages(blocks, {})
    if tool_use_id is None:
        return False
    body2 = build_anthropic_request_body(system_text="", messages=messages, tools=[_REPLAY_TOOL_ANTHROPIC],
                                          route=route, profile=profile, requested_max_tokens=16)
    req2 = CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 200000, "max_output_tokens": 8192},
        creds=ProviderCreds(base_url=root, api_key=token), state_dir=state_dir, extra_headers=headers,
        model_label=name, prebuilt_anthropic_body=body2, harness_mode=True,
    )
    try:
        return not any(ev.get("type") == "error" for ev in stream_anthropic_completion(req2))
    except Exception:
        return False


def _drive_anthropic(name: str, root: str, token: str, headers: dict, state_dir, *, with_tools: bool) -> ProbeRow:
    from rolo_claude.providers.profiles import resolve_profile
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_anthropic_completion

    row = ProbeRow(name=name, family="", path_type="anthropic")
    route = Route(provider="databricks", upstream_model=name, dialect="anthropic-passthrough")
    profile = resolve_profile(route)
    req = CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 200000, "max_output_tokens": 8192},
        creds=ProviderCreds(base_url=root, api_key=token), state_dir=state_dir, extra_headers=headers,
        model_label=name, prebuilt_anthropic_body=_anthropic_pong_body(name, with_tools), harness_mode=True,
    )
    start = time.monotonic()
    text_parts, saw_tool_call, usage, events_list = [], False, {}, []
    try:
        for ev in stream_anthropic_completion(req):
            events_list.append(ev)
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
        if saw_tool_call and row.status == "200":
            blocks, _harness_meta = _accumulate_blocks(events_list)
            row.reasoning_replay_ok = _probe_reasoning_replay_anthropic(
                name, route, profile, blocks, root, token, headers, state_dir)
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
             "tokens_out": r.tokens_out, "tool_call_ok": r.tool_call_ok, "error": r.error,
             # V2a open questions, per endpoint: (1) "cached_path_type" vs.
             # "path_type" being different names a real route split; (2)
             # "reasoning_replay_ok" is None ("not tested this run") unless
             # --tools produced a tool_use to actually replay.
             "cached_path_type": r.cached_path_type, "reasoning_replay_ok": r.reasoning_replay_ok}
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
        header += f" {'tool-ok':>7} {'replay':>7}"
    header += "  first tokens"
    lines = [header]
    for r in rows:
        # V2a: a visible "(was: X)" flags open question 2 (route split) --
        # the on-disk cache said X before this run, but this run actually
        # used/re-cached a DIFFERENT path.
        path_col = r.path_type
        if r.cached_path_type and r.cached_path_type != r.path_type:
            path_col = f"{r.path_type}(was:{r.cached_path_type})"
        line = (f"{r.name:<44} {r.family:<16} {path_col:<12} {r.status:>6} "
                f"{(f'{r.latency_ms:.0f}' if r.latency_ms is not None else '?'):>7} "
                f"{(r.tokens_out if r.tokens_out is not None else '?'):>7}")
        if tools:
            line += f" {('yes' if r.tool_call_ok else ('no' if r.tool_call_ok is False else '?')):>7}"
            # V2a open question 1: reasoning replay after a tool call --
            # "?" when --tools never actually produced a tool_use to replay.
            replay = "yes" if r.reasoning_replay_ok else ("no" if r.reasoning_replay_ok is False else "?")
            line += f" {replay:>7}"
        line += f"  {r.first_tokens or r.error or ''}"
        lines.append(line)
    return "\n".join(lines)


# =============================================================================
# V2b -- matrix-driven fixes tooling: `rolo-claude work-matrix show/apply`.
# Reads a `doctor --work --probe-all` JSON report (see `_write_report` above
# -- endpoint names only, never a host or a token) and turns each FAILING
# row into a suggested action, per the V2 brief's own rules:
#   - 403               -> off the VPN (Databricks' IP access list).
#   - any other non-200, with a "<name> (anthropic gateway)" companion row
#     (only ever present when `--both` probed it) that itself answered 200
#     -> the endpoint's DEFAULT dialect is the wrong path; the ONE real,
#     already-implemented per-endpoint knob that can fix it is
#     `databricks.gateway.<endpoint>: anthropic` (`providers.dbx_routing.
#     resolve_databricks_dialect`) -- the only failure class `apply` ever
#     writes anything for.
#   - any other non-200 (no working companion to point at)
#     -> genuinely unknown from this data alone; report it.
#   - 200, but `tool_call_ok is False` (only meaningful when `--tools` ran)
#     -> a FAMILY-wide rule (model_table.json), never a per-endpoint
#     config.json key -- reported, never written by `apply`.
#   - 200, but `reasoning_replay_ok is False` (only meaningful when
#     `--tools` produced a tool_use to replay) -> reported (no existing
#     per-endpoint "disable thinking for tool loops" config knob to write).
# A clean 200 row with neither flag set is not a failure at all (classify_
# row returns None for it).
# =============================================================================

def load_report(path) -> dict:
    """Raises OSError/ValueError on a missing/malformed file -- callers
    (`cmd_work_matrix`) turn that into a clean exit-2 message, never a
    traceback."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def classify_row(row: dict, rows_by_name: dict) -> Optional[dict]:
    """`row` is one entry of the report's own "rows" list; `rows_by_name`
    maps every row's own `endpoint` name to itself (including a `--both`
    companion row, named `"<name> (anthropic gateway)"`) so a companion
    lookup never needs a second pass over the report. Returns `None` for a
    row that isn't a failure at all (a plain 200 with neither tool/replay
    flag set to False); otherwise `{"issue", "action", "config_key",
    "config_value"}` -- the last two are `None` unless this failure maps
    onto a REAL, already-implemented config.json override (only the
    wrong-path/anthropic-gateway-companion case does)."""
    name = row.get("endpoint", "")
    status = str(row.get("status", ""))
    error = (row.get("error") or "").strip()
    if status == "403":
        return {"issue": "403 (Databricks IP access list)", "action": "run this on the VPN, then re-probe",
                "config_key": None, "config_value": None}
    if status != "200":
        companion = rows_by_name.get(f"{name} (anthropic gateway)")
        if companion is not None and str(companion.get("status")) == "200":
            return {
                "issue": f"wrong path -- the default dialect failed (status {status})",
                "action": f"set databricks.gateway.{name} = anthropic (the anthropic gateway answered correctly)",
                "config_key": f"databricks.gateway.{name}", "config_value": "anthropic",
            }
        issue = f"upstream error (status {status})" + (f": {error[:80]}" if error else "")
        return {"issue": issue, "action": "unknown -- report this endpoint for investigation",
                "config_key": None, "config_value": None}
    if row.get("tool_call_ok") is False:
        return {"issue": "no tool call produced",
                "action": "change this family's tool-call rule (tool_choice_required_supported / schema "
                           "simplifier) in model_table.json",
                "config_key": None, "config_value": None}
    if row.get("reasoning_replay_ok") is False:
        return {"issue": "reasoning replay rejected after a tool call",
                "action": f"disable thinking for tool loops on {name}",
                "config_key": None, "config_value": None}
    return None


def render_show_table(report: dict) -> str:
    """`rolo-claude work-matrix show`'s own rendering -- endpoint/status/
    issue/suggested-action, one line per FAILING row only (a report where
    every probed endpoint is clean says so plainly instead of an empty
    table)."""
    rows = report.get("rows") or []
    if report.get("note") and not rows:
        return f"(nothing probed: {report['note']})"
    rows_by_name = {r.get("endpoint"): r for r in rows if isinstance(r, dict)}
    header = f"{'endpoint':<46} {'status':>6}  {'issue':<48} suggested action"
    lines = [header, "-" * len(header)]
    any_failure = False
    for row in rows:
        classified = classify_row(row, rows_by_name)
        if classified is None:
            continue
        any_failure = True
        name = row.get("endpoint", "?")
        status = str(row.get("status", "?"))
        lines.append(f"{name:<46} {status:>6}  {classified['issue']:<48} {classified['action']}")
    if not any_failure:
        lines.append("(every probed endpoint is green -- nothing to fix)")
    return "\n".join(lines)


def writable_overrides(report: dict) -> "list[tuple[str, str]]":
    """Every `(config_key, config_value)` pair `show` would suggest AND
    that maps onto a real config.json knob -- `apply`'s own input, in
    report order, de-duplicated (last write for a given key wins, same as
    `rolo-claude config set` would if run twice)."""
    rows = report.get("rows") or []
    rows_by_name = {r.get("endpoint"): r for r in rows if isinstance(r, dict)}
    out: dict = {}
    for row in rows:
        classified = classify_row(row, rows_by_name)
        if classified and classified.get("config_key"):
            out[classified["config_key"]] = classified["config_value"]
    return list(out.items())


def cmd_work_matrix(argv: list) -> int:
    """`rolo-claude work-matrix show|apply <report.json>` -- never touches
    Claude Code's own files; `apply` writes only to `~/.rolo-claude/
    config.json`, and only the `databricks.gateway.<endpoint>` overrides
    `writable_overrides` names, after printing them and asking for
    confirmation (`--yes` skips the prompt, for scripting/CI)."""
    import argparse

    parser = argparse.ArgumentParser(prog="rolo-claude work-matrix", add_help=True,
                                      description="Interpret a `doctor --work --probe-all` JSON report.")
    sub = parser.add_subparsers(dest="action")
    p_show = sub.add_parser("show", help="Render the report as a table with a suggested action per failure")
    p_show.add_argument("report", metavar="REPORT.JSON")
    p_apply = sub.add_parser("apply", help="Write the report's suggested per-endpoint overrides into config.json")
    p_apply.add_argument("report", metavar="REPORT.JSON")
    p_apply.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args(argv)
    if args.action not in ("show", "apply"):
        parser.print_help()
        return 2

    try:
        report = load_report(args.report)
    except (OSError, ValueError) as e:
        print(f"rolo-claude work-matrix: could not read {args.report!r}: {e}", file=sys.stderr)
        return 2
    if not isinstance(report, dict):
        print(f"rolo-claude work-matrix: {args.report!r} is not a JSON object", file=sys.stderr)
        return 2

    if args.action == "show":
        print(render_show_table(report))
        return 0

    overrides = writable_overrides(report)
    if not overrides:
        print("rolo-claude work-matrix apply: nothing actionable in this report (no writable overrides).")
        return 0
    print("The following overrides would be written to ~/.rolo-claude/config.json:")
    for key, value in overrides:
        print(f"  {key} = {value!r}")
    if not args.yes:
        try:
            answer = input("Apply these overrides? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted -- nothing written.")
            return 1
    from rolo_claude.theme import set_config_value
    for key, value in overrides:
        set_config_value(key, value)
    print(f"Wrote {len(overrides)} override(s) to ~/.rolo-claude/config.json.")
    return 0
