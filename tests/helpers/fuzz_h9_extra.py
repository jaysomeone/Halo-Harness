"""tests.helpers.fuzz_h9_extra -- H9 fuzz bug hunt: three more engines
alongside tests.helpers.fuzz_h9.run_one, each covering a fuzz-surface
bullet that engine cannot reach because it never happens WHILE
session.turn()'s single generator is being pulled/timed the way run_one
drives it:

  * run_one_compaction  -- a steer/abort injected DURING auto-compaction's
    own summariser call (Session._run_compaction, driven directly --
    see its own docstring for why organically triggering the 80% gate
    would add run time without adding coverage).
  * run_one_permission  -- a steer/abort injected WHILE a permission
    decision is pending (an `ask` rule, interactive session) -- fuzzes
    every possible resolution (allow/deny/abort-while-pending/steer-then-
    resolve) instead of test_steering.py's one fixed case.
  * run_one_anthropic_native -- the SAME class of malformed/huge/unicode/
    control-char/retry-storm/overflow wire behavior as fuzz_h9.run_one,
    but over the NATIVE Anthropic SSE event vocabulary (message_start,
    content_block_start/delta/stop, message_delta, message_stop) via
    tests.helpers.mock_anthropic, since that dialect's own translation
    layer (providers/stream.stream_anthropic_completion) is otherwise
    never exercised by the OpenAI-dialect-only engine.

Kept in a SEPARATE file (not appended to fuzz_h9.py) because that file is
being independently extended by the central H9 worker concurrently with
this fuzz-bug-hunt slice -- new engines here avoid any risk of a
conflicting edit to shared functions there. Everything reusable is
imported from fuzz_h9/mock_openai/mock_anthropic rather than duplicated.
Every `run_one_*` here returns the SAME result-dict shape (seed, engine,
ok, error, events, elapsed) so tests/test_fuzz_h9.py can drive all four
engines uniformly.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import tempfile
import threading
import time
from pathlib import Path

from tests.helpers.fake_home import build_fake_home
from tests.helpers.fuzz_h9 import (
    UNICODE_EDGE_SNIPPETS, _finish_ascii_safe, _role_chunk, run_one as _run_one_general,
    _sse_chunk_ascii_safe, _raw_sse_garbage, _stop_chunk, _text_chunk, _tool_call_chunks,
)
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish, abrupt_disconnect, send_json_response, start_sse
from tests.helpers.mock_anthropic import (
    MockAnthropic, SCENARIOS as ANTHROPIC_SCENARIOS,
    _end_sse as _a_end_sse, _finish as _a_finish, _send_json as _a_send_json,
    _start_sse as _a_start_sse, _usage as _a_usage, _write as _a_write,
)
from halo_harness.agent.assemble import SessionContext
from halo_harness.agent.derive import derive_request
from halo_harness.agent.invariants import find_unpaired_tool_use_ids
from halo_harness.agent.loop import Session
from halo_harness.model import ModelProfile, parse_model_ref
from halo_harness.permissions import Decision, PermissionEngine
from halo_harness.providers.request import prepare_anthropic_messages
from halo_harness.providers.stream import ProviderCreds

_run_counter = itertools.count(1)

try:
    import psutil as _psutil  # optional -- same best-effort child-process check as fuzz_h9.py
except ImportError:
    _psutil = None


def _check_common_invariants(session, *, seed: int, label: str) -> None:
    """Invariants 1/2/3/6 from H9-brief.md Part C, shared by every engine
    in this file (thread-leak/no-hang are each caller's own job, since
    they need the outer thread/timing context)."""
    missing = find_unpaired_tool_use_ids(session.log)
    if missing:
        raise AssertionError(f"seed={seed} [{label}]: unpaired tool_use ids {missing}")
    # invariant 1's other half -- see fuzz_h9.run_one's matching comment:
    # find_unpaired_tool_use_ids' own `answered` set silently de-dupes, so
    # a tool_use_id answered TWICE is checked separately here.
    result_ids = [n.get("tool_use_id") for n in session.log.nodes() if n.get("type") == "tool_result"]
    dupes = sorted({tid for tid in result_ids if tid is not None and result_ids.count(tid) > 1})
    if dupes:
        raise AssertionError(f"seed={seed} [{label}]: duplicate tool_result(s) for id(s) {dupes}")
    _, messages, _ = derive_request(session.log)
    prepared = prepare_anthropic_messages(messages)
    for i, m in enumerate(prepared):
        if m.get("role") == "assistant" and not m.get("content"):
            raise AssertionError(f"seed={seed} [{label}]: empty assistant message at prepared[{i}]")
        if i > 0 and m.get("role") == "user":
            prev = prepared[i - 1]
            prev_content = prev.get("content") if prev.get("role") == "assistant" else None
            if isinstance(prev_content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in prev_content):
                content = m.get("content")
                first = content[0] if isinstance(content, list) and content else None
                if not (isinstance(first, dict) and first.get("type") == "tool_result"):
                    raise AssertionError(f"seed={seed} [{label}]: tool_result not first in user[{i}] after tool_use")
    if session.log.path.exists():
        for line in session.log.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)  # raises json.JSONDecodeError on a malformed log line


def _teardown_and_leak_check(session, *, seed: int, label: str, before_idents: set, result: dict,
                              children_before) -> None:
    session.abort.set()
    for cid in list(getattr(session, "_permission_waiters", {}) or {}):
        try:
            session.resolve_permission(cid, Decision("deny", "fuzz teardown"))
        except Exception:
            pass
    deadline = time.monotonic() + 2.0
    leaked = []
    while time.monotonic() < deadline:
        leaked = [t for t in threading.enumerate() if t.ident not in before_idents and t.is_alive()
                  and "process_request_thread" not in t.name]
        if not leaked:
            break
        time.sleep(0.1)
    if leaked and result["ok"]:
        result["ok"] = False
        result["error"] = f"seed={seed} [{label}]: leaked thread(s) after teardown: {[t.name for t in leaked]}"
    if _psutil is not None and result["ok"] and children_before is not None:
        time.sleep(0.2)
        children_after = len(_psutil.Process().children(recursive=True))
        if children_after > children_before:
            result["ok"] = False
            result["error"] = f"seed={seed} [{label}]: leaked child process(es): {children_before} -> {children_after}"


_CLEAN_SUMMARY_TEXT = (
    "<summary>\n## Primary Request and Intent\nfuzz\n## Key Technical Concepts\nfuzz\n"
    "## Files and Code\nfuzz\n## Errors and Fixes\nfuzz\n## Pending Jobs\nfuzz\n"
    "## Current Work\nfuzz\n## Next Step\nfuzz\n## Critical Context\nfuzz\n</summary>"
)


def run_one_compaction(seed: int, mock: MockUpstream, *, timeout_s: float = 15.0) -> dict:
    """Must-do (review-findings-h4-h5-h3c AND h5b fuzz paragraphs, both
    verbatim): "steer or Esc ... during ... auto-compaction". Runs one real
    turn first (something to actually compact), then drives
    Session._run_compaction directly (see module docstring) with a steer
    or abort injected after a random number of its own yielded events, on
    a background thread so an external join(timeout=) enforces the
    no-hang invariant from OUTSIDE exactly like fuzz_h9.run_one does."""
    rng = random.Random(seed)
    fh = build_fake_home()
    scenario_name = f"h9fuzz-compact-{seed}-{next(_run_counter)}"
    SCENARIOS[scenario_name] = lambda h, b: _finish(h, [_role_chunk(), _text_chunk("some real prior work happened"), _stop_chunk()])
    model = f"or:mock/{scenario_name}"
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    session = Session(
        cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="fuzz-compact-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=6,
    )
    result = {"seed": seed, "engine": "compaction", "ok": False, "error": None, "events": 0, "elapsed": 0.0,
              "summary_act": None, "action": None}
    t0 = time.monotonic()
    before_idents = {t.ident for t in threading.enumerate()}
    children_before = len(_psutil.Process().children(recursive=True)) if _psutil else None
    thread_violations: list = []
    drive_holder: dict = {}

    summary_act = rng.choice(["clean", "malformed", "huge_then_clean", "unicode", "http_429_then_clean"])
    action = rng.choice(["abort", "steer", "none"])
    n_events_before_action = rng.randint(0, 4)
    result["summary_act"], result["action"] = summary_act, action

    def _summary_scn(h, b):
        if summary_act == "malformed" and _summary_scn.calls == 0:
            _summary_scn.calls += 1
            start_sse(h)
            _sse_chunk_ascii_safe(h, _role_chunk())
            _raw_sse_garbage(h, b'{"choices": [ broken mid-object')
            abrupt_disconnect(h)
            return
        if summary_act == "http_429_then_clean" and _summary_scn.calls == 0:
            _summary_scn.calls += 1
            send_json_response(h, 429, {"error": {"message": "rate limited", "type": "rate_limit"}}, {"Retry-After": "1"})
            return
        if summary_act == "huge_then_clean" and _summary_scn.calls == 0:
            _summary_scn.calls += 1
            _finish(h, [_role_chunk(), _text_chunk("y" * 400_000), _stop_chunk()])
            return
        if summary_act == "unicode" and _summary_scn.calls == 0:
            _summary_scn.calls += 1
            _finish_ascii_safe(h, [_role_chunk(), _text_chunk(rng.choice(UNICODE_EDGE_SNIPPETS)), _stop_chunk()])
            return
        _summary_scn.calls += 1
        _finish(h, [_role_chunk(), _text_chunk(_CLEAN_SUMMARY_TEXT), _stop_chunk()])

    _summary_scn.calls = 0

    def _drive():
        try:
            list(session.turn(f"do some real work seed={seed}"))  # something to compact
            SCENARIOS[scenario_name] = _summary_scn
            session._busy.set()  # simulate turn()'s own invariant context (see _wrapped below)
            orig_append = session.log._append
            worker_ident_box = {"id": None}

            def _wrapped(node):
                ident = threading.current_thread().ident
                if worker_ident_box["id"] is None:
                    worker_ident_box["id"] = ident
                elif worker_ident_box["id"] != ident:
                    thread_violations.append((node.get("type"), ident, worker_ident_box["id"]))
                return orig_append(node)

            session.log._append = _wrapped
            count, kinds = 0, []
            trigger = rng.choice(["manual", "auto", "overflow"])
            for ev in session._run_compaction(session.turn_count or 1, trigger=trigger):
                kinds.append(ev.kind)
                count += 1
                if action != "none" and count == n_events_before_action:
                    if action == "abort":
                        session.abort.set()
                    else:
                        session.steer(f"steer seed={seed} during compaction")
            session._busy.clear()
            drive_holder["events"] = kinds
        except Exception as e:
            drive_holder["exc"] = e

    try:
        th = threading.Thread(target=_drive, daemon=True, name=f"fuzz-compact-drive-{seed}")
        th.start()
        th.join(timeout=timeout_s)
        if th.is_alive():
            raise AssertionError(f"seed={seed} [compaction]: hung past {timeout_s}s (summary_act={summary_act} action={action})")
        if "exc" in drive_holder:
            exc = drive_holder["exc"]
            raise AssertionError(f"seed={seed} [compaction]: raised {type(exc).__name__}: {exc} (summary_act={summary_act} action={action})")
        result["events"] = len(drive_holder.get("events", []))
        if thread_violations:
            raise AssertionError(f"seed={seed} [compaction]: log appended off-worker-thread while busy: {thread_violations[:3]}")
        _check_common_invariants(session, seed=seed, label="compaction")
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        SCENARIOS.pop(scenario_name, None)
        _teardown_and_leak_check(session, seed=seed, label="compaction", before_idents=before_idents,
                                  result=result, children_before=children_before)
        result["elapsed"] = time.monotonic() - t0
    return result


def run_one_permission(seed: int, mock: MockUpstream, *, timeout_s: float = 15.0) -> dict:
    """Must-do (H9-brief Part C + review-findings-h5b fuzz paragraph):
    "while a permission decision is pending" -- test_steering.py's own
    test_steer_during_a_pending_card_does_not_answer_the_card pattern,
    fuzzed over all four resolutions (allow/deny/abort-while-pending/
    steer-then-resolve) instead of exercising just one fixed case."""
    fh = build_fake_home()
    target = fh["proj"] / f"perm_fuzz_{seed}.txt"
    scenario_name = f"h9fuzz-perm-{seed}-{next(_run_counter)}"

    def _scn(h, body):
        messages = (body or {}).get("messages") or []
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
            _finish(h, [_role_chunk(), _text_chunk("done"), _stop_chunk()])
        else:
            _finish(h, _tool_call_chunks(f"call_perm_{seed}", "Write", {"file_path": str(target), "content": "hi\n"}))

    SCENARIOS[scenario_name] = _scn
    model = f"or:mock/{scenario_name}"
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    engine = PermissionEngine(mode="default", cwd=fh["proj"])  # Write under cwd -> "ask" in default mode
    session = Session(
        cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="fuzz-perm-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=6,
        permission_engine=engine,
    )
    session.interactive = True

    rng = random.Random(seed)
    action = rng.choice(["allow", "deny", "abort_while_pending", "steer_then_allow"])
    result = {"seed": seed, "engine": "permission", "ok": False, "error": None, "events": 0, "elapsed": 0.0,
              "action": action}
    t0 = time.monotonic()
    before_idents = {t.ident for t in threading.enumerate()}
    children_before = len(_psutil.Process().children(recursive=True)) if _psutil else None
    events_seen: list = []
    drive_holder: dict = {}

    def _drive():
        try:
            for ev in session.turn(f"write the fuzz file seed={seed}"):
                events_seen.append(ev)
        except Exception as e:
            drive_holder["exc"] = e

    try:
        th = threading.Thread(target=_drive, daemon=True, name=f"fuzz-perm-drive-{seed}")
        th.start()
        deadline = time.monotonic() + timeout_s
        while not any(e.kind == "permission_request" for e in events_seen) and th.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not any(e.kind == "permission_request" for e in events_seen):
            raise AssertionError(f"seed={seed} [permission]: no permission_request seen before timeout (action={action})")
        pending_ids = list(session._permission_waiters.keys())
        call_id = pending_ids[0] if pending_ids else f"call_perm_{seed}"

        if action == "abort_while_pending":
            session.abort.set()
        elif action == "steer_then_allow":
            queued = session.steer(f"steer seed={seed} during pending card")
            time.sleep(0.15)
            if call_id not in session._permission_waiters:
                raise AssertionError(f"seed={seed} [permission]: steer answered the card by itself (queued={queued})")
            session.resolve_permission(call_id, Decision("allow", "fuzz allow after steer"))
        elif action == "allow":
            session.resolve_permission(call_id, Decision("allow", "fuzz allow"))
        else:
            session.resolve_permission(call_id, Decision("deny", "fuzz deny"))

        th.join(timeout=timeout_s)
        if th.is_alive():
            raise AssertionError(f"seed={seed} [permission]: hung past {timeout_s}s (action={action})")
        if "exc" in drive_holder:
            exc = drive_holder["exc"]
            raise AssertionError(f"seed={seed} [permission]: raised {type(exc).__name__}: {exc} (action={action})")
        result["events"] = len(events_seen)
        _check_common_invariants(session, seed=seed, label="permission")
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        SCENARIOS.pop(scenario_name, None)
        _teardown_and_leak_check(session, seed=seed, label="permission", before_idents=before_idents,
                                  result=result, children_before=children_before)
        result["elapsed"] = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Native Anthropic-dialect fuzzing (mock_anthropic, `ant:` route) -- the
# message_start/content_block_start/delta/stop/message_delta/message_stop
# vocabulary named explicitly in H9-brief.md Part C, which run_one's
# OpenAI-chat-dialect mock never produces on the wire (stream_completion
# translates it into the same shape internally, but stream_anthropic_
# completion's OWN parsing of these events on the wire is otherwise never
# exercised at all).
# ---------------------------------------------------------------------------

def _a_event_ascii_safe(handler, event: dict) -> None:
    """Like mock_anthropic._sse_event but ensure_ascii=True -- see
    fuzz_h9._sse_chunk_ascii_safe's own docstring for why."""
    frame = f"event: {event['type']}\r\ndata: {json.dumps(event, ensure_ascii=True)}\r\n\r\n".encode("ascii")
    _a_write(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def _a_raw_garbage(handler, raw: bytes) -> None:
    """A malformed SSE line -- no `event:` line, no `data: ` prefix, or
    stray blank lines, depending on `raw`. The CHUNK framing itself is
    always correct (len(raw) computed here, never hardcoded -- see the
    _finish_ascii_safe bug this module's sibling fuzz_h9.py had) so only
    the SSE-level content is what's malformed, not the HTTP transport."""
    _a_write(handler, b"%x\r\n" % len(raw) + raw + b"\r\n")


_ANTHROPIC_ACTS = [
    "clean_text", "clean_tool_use", "thinking_and_signature", "truncated_tool_json",
    "malformed_sse_no_data_prefix", "malformed_sse_stray_blank_lines", "huge_text",
    "unicode_text", "control_chars_tool_result", "rate_limit_429", "overflow_400", "mid_stream_error",
]


def _do_anthropic_act(h, body, act, rng, run_tmp: Path) -> None:
    mid = f"msg_{rng.randint(0, 10**9)}"
    start_ev = {"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant",
                "content": [], "model": body.get("model", "claude"), "usage": _a_usage(output_tokens=0)}}
    if act == "clean_text":
        _a_finish(h, [
            start_ev,
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": f"fuzz reply {rng.randint(0, 10000)}"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()},
            {"type": "message_stop"},
        ])
    elif act == "clean_tool_use":
        target = run_tmp / "benign_ant.txt"
        target.write_text("benign anthropic fuzz content\n", encoding="utf-8")
        _a_finish(h, [
            start_ev,
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": f"toolu_{rng.randint(0, 10**9)}", "name": "Read", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps({"file_path": str(target)})}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": _a_usage()},
            {"type": "message_stop"},
        ])
    elif act == "thinking_and_signature":
        _a_finish(h, [
            start_ev,
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "considering the fuzz input"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": f"sig_{rng.randint(0, 10**9)}"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "fuzz answer after thinking"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()},
            {"type": "message_stop"},
        ])
    elif act == "truncated_tool_json":
        _a_start_sse(h)
        _a_event_ascii_safe(h, start_ev)
        _a_event_ascii_safe(h, {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": f"toolu_{rng.randint(0, 10**9)}", "name": "Read", "input": {}}})
        cut = rng.choice([
            '{"file_path": "C:\\\\Users\\\\some\\\\path\\\\that-never-clo',
            '{"file_path": "ends with a backslash escape\\\\',
            '{"file_path": "a", "extra": {"nested": [1, 2,',
        ])
        _a_event_ascii_safe(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": cut}})
        if rng.random() < 0.5:
            _a_event_ascii_safe(h, {"type": "content_block_stop", "index": 0})
            _a_event_ascii_safe(h, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": _a_usage()})
            _a_event_ascii_safe(h, {"type": "message_stop"})
            _a_end_sse(h)
        else:
            abrupt_disconnect(h)
    elif act == "malformed_sse_no_data_prefix":
        _a_start_sse(h)
        _a_event_ascii_safe(h, start_ev)
        _a_raw_garbage(h, b"this line has no data: prefix at all\r\n\r\n")
        _a_event_ascii_safe(h, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        _a_event_ascii_safe(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "recovered anyway"}})
        _a_event_ascii_safe(h, {"type": "content_block_stop", "index": 0})
        _a_event_ascii_safe(h, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()})
        _a_event_ascii_safe(h, {"type": "message_stop"})
        _a_end_sse(h)
    elif act == "malformed_sse_stray_blank_lines":
        _a_start_sse(h)
        _a_raw_garbage(h, b"\r\n\r\n\r\n")
        _a_event_ascii_safe(h, start_ev)
        _a_raw_garbage(h, b"\r\n\r\n")
        _a_event_ascii_safe(h, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        _a_event_ascii_safe(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "still here"}})
        _a_event_ascii_safe(h, {"type": "content_block_stop", "index": 0})
        _a_event_ascii_safe(h, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()})
        _a_event_ascii_safe(h, {"type": "message_stop"})
        _a_end_sse(h)
    elif act == "huge_text":
        size = rng.choice([200_000, 600_000, 1_200_000])
        _a_finish(h, [
            start_ev,
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "z" * size}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()},
            {"type": "message_stop"},
        ])
    elif act == "unicode_text":
        snippet = rng.choice(UNICODE_EDGE_SNIPPETS)
        _a_start_sse(h)
        _a_event_ascii_safe(h, start_ev)
        _a_event_ascii_safe(h, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        _a_event_ascii_safe(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": snippet}})
        _a_event_ascii_safe(h, {"type": "content_block_stop", "index": 0})
        _a_event_ascii_safe(h, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()})
        _a_event_ascii_safe(h, {"type": "message_stop"})
        _a_end_sse(h)
    elif act == "control_chars_tool_result":
        target = run_tmp / "control_chars_ant.txt"
        target.write_bytes(bytes([0, 1, 2, 7, 8, 11, 12, 14, 27, 31]) + b"payload after control bytes\n")
        _a_finish(h, [
            start_ev,
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": f"toolu_{rng.randint(0, 10**9)}", "name": "Read", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps({"file_path": str(target)})}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": _a_usage()},
            {"type": "message_stop"},
        ])
    elif act == "rate_limit_429":
        _a_send_json(h, 429, {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}},
                      extra_headers={"retry-after": "1"})
    elif act == "overflow_400":
        _a_send_json(h, 400, {"type": "error", "error": {
            "type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum"}})
    elif act == "mid_stream_error":
        _a_start_sse(h)
        _a_event_ascii_safe(h, start_ev)
        _a_event_ascii_safe(h, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        _a_event_ascii_safe(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial "}})
        _a_event_ascii_safe(h, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        _a_end_sse(h)
    else:
        raise AssertionError(f"unknown anthropic fuzz act: {act!r}")


class _AnthropicRunScript:
    """Plays `act` on the FIRST request; any follow-up call (e.g. after a
    tool_use's own result comes back) always gets a clean wrap-up reply,
    so a run can never spin the adversarial act indefinitely."""

    def __init__(self, act: str, run_tmp: Path, rng: random.Random):
        self.act, self.run_tmp, self.rng, self.call_index = act, run_tmp, rng, 0

    def __call__(self, handler, body) -> None:
        if self.call_index == 0:
            self.call_index += 1
            _do_anthropic_act(handler, body, self.act, self.rng, self.run_tmp)
            return
        _a_finish(handler, [
            {"type": "message_start", "message": {"id": f"msg_wrapup_{self.rng.randint(0, 10**9)}", "type": "message",
             "role": "assistant", "content": [], "model": body.get("model", "claude"), "usage": _a_usage(output_tokens=0)}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "wrapped up"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _a_usage()},
            {"type": "message_stop"},
        ])


def run_one_anthropic_native(seed: int, mock: MockAnthropic, *, timeout_s: float = 15.0) -> dict:
    """The same fuzz-surface bullets as fuzz_h9.run_one, over the NATIVE
    Anthropic SSE event vocabulary (mock_anthropic, `ant:` route) instead
    of the OpenAI-chat dialect -- exercises providers/stream.
    stream_anthropic_completion's own translation layer, which run_one
    never touches at all."""
    rng = random.Random(seed)
    fh = build_fake_home()
    # H10b: this call site was the ONE engine in this file that never set
    # BRIDGE_TEST_HOME before constructing a real Session -- every sibling
    # engine (run_one_general/_run_one_compaction/_run_one_permission,
    # fuzz_h9.run_one) does. Without it, SessionLog falls through to the
    # REAL `~/.halo/sessions` (config/paths.bridge_home's own
    # documented fallback), which is exactly how `ant:claude-h9fuzz-ant-*`
    # sessions leaked into the owner's real session history (H10b report).
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    run_tmp = Path(tempfile.mkdtemp(prefix=f"fuzz-ant-{seed}-"))
    act = rng.choice(_ANTHROPIC_ACTS)
    scenario_name = f"h9fuzz-ant-{seed}-{next(_run_counter)}"
    ANTHROPIC_SCENARIOS[scenario_name] = _AnthropicRunScript(act, run_tmp, rng)
    model = f"ant:claude-{scenario_name}"

    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    session = Session(
        cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="fuzz-ant-state-")), model_label=model,
        session_context=session_ctx, max_turns=6,
    )

    action = rng.choice(["abort", "steer", "none"])
    n_events_before_action = rng.randint(0, 6)
    result = {"seed": seed, "engine": "anthropic_native", "ok": False, "error": None, "events": 0,
              "elapsed": 0.0, "act": act, "action": action}
    t0 = time.monotonic()
    before_idents = {t.ident for t in threading.enumerate()}
    children_before = len(_psutil.Process().children(recursive=True)) if _psutil else None
    drive_holder: dict = {}

    def _drive():
        try:
            count, kinds = 0, []
            for ev in session.turn(f"fuzz anthropic native turn seed={seed}"):
                kinds.append(ev.kind)
                count += 1
                if action != "none" and count == n_events_before_action:
                    if action == "abort":
                        session.abort.set()
                    else:
                        session.steer(f"steer seed={seed}: change approach now")
            drive_holder["events"] = kinds
        except Exception as e:
            drive_holder["exc"] = e

    try:
        th = threading.Thread(target=_drive, daemon=True, name=f"fuzz-ant-drive-{seed}")
        th.start()
        th.join(timeout=timeout_s)
        if th.is_alive():
            raise AssertionError(f"seed={seed} [anthropic_native]: hung past {timeout_s}s (act={act} action={action})")
        if "exc" in drive_holder:
            exc = drive_holder["exc"]
            raise AssertionError(f"seed={seed} [anthropic_native]: raised {type(exc).__name__}: {exc} (act={act} action={action})")
        result["events"] = len(drive_holder.get("events", []))
        _check_common_invariants(session, seed=seed, label="anthropic_native")
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        ANTHROPIC_SCENARIOS.pop(scenario_name, None)
        _teardown_and_leak_check(session, seed=seed, label="anthropic_native", before_idents=before_idents,
                                  result=result, children_before=children_before)
        result["elapsed"] = time.monotonic() - t0
    return result


_ENGINE_WEIGHTS = [("general", 50), ("compaction", 16), ("permission", 17), ("anthropic_native", 17)]


def run_sweep_all(n: int, *, base_seed: int = 0, timeout_s: float = 15.0, verbose: bool = False,
                   allow_slow_hooks: bool = True) -> dict:
    """Distributes `n` seeds (base_seed..base_seed+n-1) across all four
    engines (fuzz_h9.run_one plus the three in this module), each seed
    deterministically mapped to exactly one engine (keyed off the seed
    itself, independent of iteration order) so a failing seed is
    reproducible by re-running JUST that one engine with that seed."""
    mock_oai = MockUpstream().start()
    mock_ant = MockAnthropic().start()
    t0 = time.monotonic()
    passed = failed = 0
    failures = []
    try:
        for i in range(n):
            seed = base_seed + i
            engine = random.Random(f"engine-pick:{seed}").choices(
                [nm for nm, _ in _ENGINE_WEIGHTS], weights=[w for _, w in _ENGINE_WEIGHTS], k=1)[0]
            if engine == "general":
                r = _run_one_general(seed, mock_oai, timeout_s=timeout_s, allow_slow_hooks=allow_slow_hooks)
                r.setdefault("engine", "general")
            elif engine == "compaction":
                r = run_one_compaction(seed, mock_oai, timeout_s=timeout_s)
            elif engine == "permission":
                r = run_one_permission(seed, mock_oai, timeout_s=timeout_s)
            else:
                r = run_one_anthropic_native(seed, mock_ant, timeout_s=timeout_s)
            if r["ok"]:
                passed += 1
            else:
                failed += 1
                failures.append((seed, engine, r.get("error")))
            if verbose:
                status = "OK" if r["ok"] else "FAIL"
                print(f"[{status}] seed={seed} engine={engine} events={r.get('events')} {r.get('elapsed', 0):.2f}s"
                      + ("" if r["ok"] else f" :: {r.get('error')}"))
    finally:
        mock_oai.stop()
        mock_ant.stop()
    return {"n": n, "passed": passed, "failed": failed, "failures": failures, "elapsed": time.monotonic() - t0}


if __name__ == "__main__":
    import sys as _sys
    n = int(_sys.argv[1]) if len(_sys.argv) > 1 else 200
    seed0 = int(_sys.argv[2]) if len(_sys.argv) > 2 else 0
    summary = run_sweep_all(n, base_seed=seed0, verbose=True)
    print("-" * 74)
    print(f"H9 FUZZ EXTRA SWEEP: {summary['n']} runs, {summary['passed']} passed, "
          f"{summary['failed']} failed, {summary['elapsed']:.1f}s total")
    for seed, engine, err in summary["failures"]:
        print(f"  seed={seed} engine={engine}\n    {err}")
    _sys.exit(0 if summary["failed"] == 0 else 1)
