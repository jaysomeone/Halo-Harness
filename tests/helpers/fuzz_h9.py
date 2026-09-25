"""tests.helpers.fuzz_h9 -- H9 randomised bug-hunt fuzz harness.

Drives a REAL rolo_claude.agent.loop.Session against a deliberately
adversarial scripted mock upstream (malformed/truncated SSE chunks, huge
outputs, unicode edge cases incl. lone surrogates/RTL/ZWJ emoji, 429/5xx
storms, overflow errors, control characters in tool results) with a steer
or an abort (Esc) injected at a randomly chosen point in EVERY run, then
asserts the session-log invariants that must hold no matter what the
upstream or the user did.

Two modes of injecting the interrupt/steer, picked randomly per run:
  * "sync"  -- pulls session.turn()'s event generator one event at a time
    (test_steering.py's own technique) and fires after a random number of
    events -- lands on real yielded boundaries: message_start, content
    deltas, between two dispatched tool calls, etc.
  * "timer" -- runs the turn on a background thread and fires from the
    main thread after a random short delay -- the only way to land INSIDE
    a blocking wait the loop never yields control during (a hook
    subprocess, a retry sleep, a compaction summariser call).
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
from tests.helpers.mock_openai import (
    MockUpstream, SCENARIOS, _finish, start_sse, end_sse, abrupt_disconnect, _write_safe, send_json_response,
)
from rolo_claude.agent.assemble import SessionContext
from rolo_claude.agent.loop import Session
from rolo_claude.agent.derive import derive_request
from rolo_claude.agent.invariants import find_unpaired_tool_use_ids
from rolo_claude.model import ModelProfile, parse_model_ref
from rolo_claude.providers.stream import ProviderCreds
from rolo_claude.providers.request import prepare_anthropic_messages

_run_counter = itertools.count(1)

UNICODE_EDGE_SNIPPETS = [
    "plain ascii text",
    "café naïve résumé",
    "你好世界",
    "السلام عليكم rtl",
    "שלום rtl",
    "\U0001F600 emoji",
    "\U0001F468‍\U0001F469‍\U0001F467 zwj family",
    "lone surrogate ahead: \ud800 (end)",
    "control chars: \x00\x01\x1b[31mred\x1b[0m (end)",
]


class FuzzFailure(AssertionError):
    """Raised when a fuzz run violates one of the required invariants."""


def _role_chunk():
    return {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}


def _text_chunk(piece):
    return {"choices": [{"index": 0, "delta": {"content": piece}}]}


def _stop_chunk(reason="stop"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _tool_call_chunks(call_id, name, arguments):
    return [
        _role_chunk(),
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        _stop_chunk("tool_calls"),
    ]


def _sse_chunk_ascii_safe(handler, data) -> None:
    """Like mock_openai.write_sse_chunk but ensure_ascii=True, so a payload
    containing a LONE utf-16 surrogate (simulating a stream cut mid
    character) round-trips as a \\uXXXX escape instead of raising
    UnicodeEncodeError inside the mock server's own UTF-8 encode step."""
    body = json.dumps(data, ensure_ascii=True).encode("ascii")
    frame = b"data: " + body + b"\r\n\r\n"
    _write_safe(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def _raw_sse_garbage(handler, raw: bytes) -> None:
    """A syntactically-broken `data:` line -- not valid JSON at all."""
    frame = b"data: " + raw + b"\r\n\r\n"
    _write_safe(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def _finish_ascii_safe(handler, chunks, done=True) -> None:
    start_sse(handler)
    for c in chunks:
        _sse_chunk_ascii_safe(handler, c)
    if done:
        # bug fix (H9 fuzz bug hunt): this used to hardcode the chunk-size
        # prefix as the LITERAL byte "6", which is only the length of
        # "data: " alone -- the real frame ("data: [DONE]\r\n\r\n", the SAME
        # bytes mock_openai.write_sse_chunk's own "[DONE]" branch sends) is
        # 16 bytes (hex 10). The wrong prefix corrupted the HTTP chunked-
        # transfer framing for EVERY "text_unicode" fuzz act (any caller of
        # this function): the client's chunk decoder read only 6 bytes
        # ("data: ") as the chunk body, then choked on "[D" where it
        # expected the "\r\n" terminator, breaking the rest of the response
        # stream -- reproduced directly (see test_fuzz_h9.py's
        # test_pin_finish_ascii_safe_done_marker_has_correct_chunk_length):
        # session.turn() against this scenario hung well past a 30s
        # deadline instead of ever seeing the "[DONE]" marker. Computed
        # from the real frame length now, exactly like every other chunk
        # this file writes (_sse_chunk_ascii_safe/_raw_sse_garbage above).
        frame = b"data: [DONE]\r\n\r\n"
        _write_safe(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")
    end_sse(handler)


_ACTS_FAILUREISH = [
    "http_429", "http_5xx_500", "http_5xx_503", "malformed_chunk",
    "truncated_json", "disconnect_mid_stream", "no_done", "overflow_unfixable",
]
_ACTS_PRODUCTIVE = [
    "text", "text_unicode", "text_huge", "tool_call_read", "tool_call_bash",
    "tool_call_todo", "tool_call_bad_args", "tool_call_control_chars", "overflow_fixable",
]


def _gen_plan(rng: random.Random) -> list:
    n = rng.randint(1, 5)
    plan, fails_in_a_row = [], 0
    for _ in range(n):
        if fails_in_a_row < 3 and rng.random() < 0.4:
            plan.append(rng.choice(_ACTS_FAILUREISH))
            fails_in_a_row += 1
        else:
            plan.append(rng.choice(_ACTS_PRODUCTIVE))
            fails_in_a_row = 0
    plan.append("text")  # always end with one clean, terminal reply
    return plan


def _overflow_body(limit, requested_input, requested_output):
    total = requested_input + requested_output
    return {"error": {
        "message": (f"This endpoint's maximum context length is {limit} tokens. However, you requested "
                    f"about {total} tokens ({requested_input} of text input, {requested_output} in the output)."),
        "type": "invalid_request_error",
    }}


def _do_act(handler, body, act, rng, run_tmp: Path) -> None:
    if act == "text":
        _finish(handler, [_role_chunk(), _text_chunk(f"fuzz reply {rng.randint(0, 10_000)}"), _stop_chunk()])
    elif act == "text_unicode":
        snippet = rng.choice(UNICODE_EDGE_SNIPPETS)
        _finish_ascii_safe(handler, [_role_chunk(), _text_chunk(snippet), _stop_chunk()])
    elif act == "text_huge":
        size = rng.choice([200_000, 600_000, 1_500_000])
        _finish(handler, [_role_chunk(), _text_chunk("x" * size), _stop_chunk()])
    elif act == "tool_call_read":
        target = run_tmp / "benign.txt"
        target.write_text("benign fuzz file content\nline two\n", encoding="utf-8")
        _finish(handler, _tool_call_chunks(f"call_{rng.randint(0, 1_000_000)}", "Read", {"file_path": str(target)}))
    elif act == "tool_call_bash":
        _finish(handler, _tool_call_chunks(f"call_{rng.randint(0, 1_000_000)}", "Bash", {"command": "echo fuzz-ok"}))
    elif act == "tool_call_todo":
        todos = {"todos": [{"content": "fuzz item", "status": "pending", "activeForm": "Fuzzing"}]}
        _finish(handler, _tool_call_chunks(f"call_{rng.randint(0, 1_000_000)}", "TodoWrite", todos))
    elif act == "tool_call_bad_args":
        # deliberately malformed (missing required field) -- exercises the
        # repair/error-result path, never a crash.
        _finish(handler, _tool_call_chunks(f"call_{rng.randint(0, 1_000_000)}", "Read", {"not_a_real_field": 1}))
    elif act == "tool_call_control_chars":
        target = run_tmp / "control_chars.txt"
        target.write_bytes(bytes([0, 1, 2, 7, 8, 11, 12, 14, 27, 31]) + b"payload after control bytes\n")
        _finish(handler, _tool_call_chunks(f"call_{rng.randint(0, 1_000_000)}", "Read", {"file_path": str(target)}))
    elif act == "http_429":
        send_json_response(handler, 429, {"error": {"message": "rate limited", "type": "rate_limit"}}, {"Retry-After": "1"})
    elif act == "http_5xx_500":
        send_json_response(handler, 500, {"error": {"message": "internal error", "type": "server_error"}})
    elif act == "http_5xx_503":
        send_json_response(handler, 503, {"error": {"message": "overloaded", "type": "server_error"}})
    elif act == "malformed_chunk":
        start_sse(handler)
        _sse_chunk_ascii_safe(handler, _role_chunk())
        _raw_sse_garbage(handler, b'{"choices": [ this is not valid json ]')
        abrupt_disconnect(handler)
    elif act == "truncated_json":
        start_sse(handler)
        _sse_chunk_ascii_safe(handler, _role_chunk())
        _raw_sse_garbage(handler, b'{"choices":[{"index":0,"delta":{"content":"cut off mid-str')
        abrupt_disconnect(handler)
    elif act == "disconnect_mid_stream":
        start_sse(handler)
        _sse_chunk_ascii_safe(handler, _role_chunk())
        _sse_chunk_ascii_safe(handler, _text_chunk("partial before death"))
        abrupt_disconnect(handler)
    elif act == "no_done":
        _finish(handler, [_role_chunk(), _text_chunk("no done marker"), _stop_chunk()], done=False)
    elif act == "overflow_unfixable":
        send_json_response(handler, 400, _overflow_body(65536, 68000, 3000))
    elif act == "overflow_fixable":
        max_tokens = (body or {}).get("max_tokens")
        if isinstance(max_tokens, (int, float)) and max_tokens <= 13584:
            _finish(handler, [_role_chunk(), _text_chunk("fit after retry"), _stop_chunk()])
        else:
            send_json_response(handler, 400, _overflow_body(163840, 150000, 20000))
    else:
        raise AssertionError(f"unknown fuzz act: {act!r}")


class _RunScript:
    """Stateful scenario callable: returns the next act from `plan` on each
    call, repeating the LAST (always a clean "text") act forever once the
    plan is exhausted, so a run can never spin the adversarial part of its
    plan indefinitely."""

    def __init__(self, plan: list, run_tmp: Path, rng: random.Random):
        self.plan = plan
        self.run_tmp = run_tmp
        self.rng = rng
        self.call_index = 0
        self.calls_seen: list = []

    def __call__(self, handler, body) -> None:
        self.calls_seen.append(body)
        idx = min(self.call_index, len(self.plan) - 1)
        self.call_index += 1
        _do_act(handler, body, self.plan[idx], self.rng, self.run_tmp)


REPO_DIR = Path(__file__).resolve().parent.parent.parent
_HOOK_PROFILES = ["none", "none", "none", "none",  # weight: hooks in ~20% of runs
                  "stop_sleep", "posttooluse_sleep", "pretooluse_fast", "pretooluse_sleep",
                  "userpromptsubmit_sleep"]


def _build_hook_runner(fh, profile: str):
    import sys as _sys
    from rolo_claude.hooks import HookDef, HookRunner
    argv = [_sys.executable, "-m", "tests.helpers.hook_scripts"]
    if profile == "stop_sleep":
        hooks_by_event = {"Stop": [HookDef(type="command", args=argv + ["sleep"])]}
    elif profile == "posttooluse_sleep":
        hooks_by_event = {"PostToolUse": [HookDef(type="command", matcher="*", args=argv + ["sleep"])]}
    elif profile == "pretooluse_fast":
        hooks_by_event = {"PreToolUse": [HookDef(type="command", matcher="*", args=argv + ["json_allow"])]}
    elif profile == "pretooluse_sleep":
        # Must-do (review-findings-h4-h5-h3c AND h5b fuzz paragraphs, both
        # verbatim): steer/abort "during a hook run" names PreToolUse
        # explicitly -- pretooluse_fast alone never blocks long enough for
        # "timer" mode's action to land reliably inside its own wait.
        hooks_by_event = {"PreToolUse": [HookDef(type="command", matcher="*", args=argv + ["sleep"])]}
    elif profile == "userpromptsubmit_sleep":
        # Must-do (review-findings-h4-h5-h3c fuzz paragraph): steer/abort
        # during a hook run includes UserPromptSubmit -- turn()'s own
        # `_busy.set()` runs BEFORE this hook call, and the hook itself is
        # ONE blocking subprocess wait with no yield in between, so "timer"
        # mode (see run_one's own mode selection) is the only way to land
        # an action inside this specific window.
        hooks_by_event = {"UserPromptSubmit": [HookDef(type="command", args=argv + ["sleep"])]}
    else:
        return None
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    env.setdefault("HOOK_SLEEP_S", "1.2")
    return HookRunner(hooks_by_event, cwd=fh["proj"], session_id=f"fuzz-{next(_run_counter)}",
                       transcript_path=str(fh["proj"] / "transcript.jsonl"), effective_env=env)


try:
    import psutil as _psutil  # optional -- child-process leak check is best-effort
except ImportError:
    _psutil = None


def run_one(seed: int, mock: MockUpstream, *, timeout_s: float = 15.0, allow_slow_hooks: bool = True) -> dict:
    """Run exactly one randomised fuzz iteration. Returns a result dict;
    never raises (a hang is the only thing that can't be caught here --
    callers should run this under their own process-level ceiling too).
    `allow_slow_hooks=False` (the fast tests/run_all.py subset) excludes
    the ~1.2s sleep-hook profiles so a permanent CI-style run stays quick;
    the full manual/nightly sweep leaves it True."""
    rng = random.Random(seed)
    fh = build_fake_home()
    run_tmp = Path(tempfile.mkdtemp(prefix=f"fuzz-{seed}-"))
    plan = _gen_plan(rng)
    scenario_name = f"h9fuzz-{seed}-{next(_run_counter)}"
    SCENARIOS[scenario_name] = _RunScript(plan, run_tmp, rng)
    model = f"or:mock/{scenario_name}"

    hook_pool = _HOOK_PROFILES if allow_slow_hooks else [p for p in _HOOK_PROFILES if "sleep" not in p]
    hook_profile = rng.choice(hook_pool)
    hook_runner = _build_hook_runner(fh, hook_profile)
    mode = ("timer" if hook_profile in ("stop_sleep", "posttooluse_sleep", "pretooluse_sleep", "userpromptsubmit_sleep")
            else rng.choice(["sync", "sync", "timer"]))
    action = rng.choice(["abort", "steer", "none"])
    n_events_before_action = rng.randint(0, 6)
    delay_s = rng.uniform(0.05, 1.3)

    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)

    result = {"seed": seed, "plan": plan, "mode": mode, "action": action,
              "hook_profile": hook_profile, "ok": False, "error": None,
              "events": 0, "elapsed": 0.0}
    t0 = time.monotonic()
    before_idents = {t.ident for t in threading.enumerate()}
    children_before = len(_psutil.Process().children(recursive=True)) if _psutil else None

    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="fuzz-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=12,
        hook_runner=hook_runner,
    )

    thread_violations: list = []
    worker_ident_box = {"id": None}
    orig_append = session.log._append

    def _wrapped(node):
        ident = threading.current_thread().ident
        if session.busy:
            if worker_ident_box["id"] is None:
                worker_ident_box["id"] = ident
            elif worker_ident_box["id"] != ident:
                thread_violations.append((node.get("type"), ident, worker_ident_box["id"]))
        return orig_append(node)

    session.log._append = _wrapped

    drive_holder: dict = {}

    def _drive():
        try:
            count, kinds = 0, []
            for ev in session.turn(f"fuzz turn seed={seed}"):
                kinds.append(ev.kind)
                count += 1
                if mode == "sync" and action != "none" and count == n_events_before_action:
                    if action == "abort":
                        session.abort.set()
                    else:
                        session.steer(f"steer seed={seed}: change approach now")
            drive_holder["events"] = kinds
        except Exception as e:  # captured for the report, never swallowed
            drive_holder["exc"] = e

    try:
        th = threading.Thread(target=_drive, daemon=True, name=f"fuzz-drive-{seed}")
        th.start()
        if mode == "timer":
            time.sleep(delay_s)
            if action == "abort":
                session.abort.set()
            elif action == "steer":
                session.steer(f"steer seed={seed}: change approach now")
        th.join(timeout=timeout_s)
        if th.is_alive():
            raise AssertionError(f"seed={seed}: run hung past {timeout_s}s (mode={mode} action={action} plan={plan})")
        if "exc" in drive_holder:
            exc = drive_holder["exc"]
            raise AssertionError(f"seed={seed}: session.turn() raised {type(exc).__name__}: {exc} (mode={mode} action={action} plan={plan})")
        result["events"] = len(drive_holder.get("events", []))

        missing = find_unpaired_tool_use_ids(session.log)
        if missing:
            raise AssertionError(f"seed={seed}: unpaired tool_use ids {missing} (plan={plan}, mode={mode}, action={action})")
        # H9-brief.md Part C invariant 1 says "no unpaired calls, no
        # DUPLICATES" -- find_unpaired_tool_use_ids only ever checks the
        # first half (its own `answered` set silently de-dupes, by
        # construction never designed to catch a tool_use_id answered
        # TWICE), so that half is checked separately here.
        result_ids = [n.get("tool_use_id") for n in session.log.nodes() if n.get("type") == "tool_result"]
        dupes = sorted({tid for tid in result_ids if tid is not None and result_ids.count(tid) > 1})
        if dupes:
            raise AssertionError(f"seed={seed}: duplicate tool_result(s) for id(s) {dupes} (plan={plan}, mode={mode}, action={action})")
        if thread_violations:
            raise AssertionError(f"seed={seed}: log appended off-worker-thread while busy: {thread_violations[:3]} (mode={mode})")

        _, messages, _ = derive_request(session.log)
        prepared = prepare_anthropic_messages(messages)
        for i, m in enumerate(prepared):
            if m.get("role") == "assistant" and not m.get("content"):
                raise AssertionError(f"seed={seed}: empty assistant message at prepared[{i}] (plan={plan})")
            if i > 0 and m.get("role") == "user":
                prev = prepared[i - 1]
                prev_content = prev.get("content") if prev.get("role") == "assistant" else None
                if isinstance(prev_content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in prev_content):
                    content = m.get("content")
                    first = content[0] if isinstance(content, list) and content else None
                    if not (isinstance(first, dict) and first.get("type") == "tool_result"):
                        raise AssertionError(f"seed={seed}: tool_result not first in user[{i}] after tool_use (plan={plan})")

        if session.log.path.exists():
            for lineno, line in enumerate(session.log.path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    json.loads(line)  # raises json.JSONDecodeError on malformed lines

        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        SCENARIOS.pop(scenario_name, None)
        session.abort.set()  # in case anything is still winding down
        deadline = time.monotonic() + 2.0
        leaked = []
        while time.monotonic() < deadline:
            # `process_request_thread` is socketserver.ThreadingMixIn's OWN
            # per-connection worker inside the shared MockUpstream test
            # double (daemon_threads=True, one thread per keep-alive HTTP
            # connection) -- test infrastructure this harness reuses across
            # every run in a sweep, never something rolo_claude itself
            # spawns, so it is excluded from the leak check by name (Python
            # names a Thread(target=fn) "Thread-N (fn.__name__)" by default).
            leaked = [t for t in threading.enumerate()
                      if t.ident not in before_idents and t.is_alive()
                      and "process_request_thread" not in t.name]
            if not leaked:
                break
            time.sleep(0.1)
        if leaked and result["ok"]:
            result["ok"] = False
            result["error"] = f"seed={seed}: leaked thread(s) after teardown: {[t.name for t in leaked]}"
        if _psutil is not None and result["ok"]:
            time.sleep(0.2)
            children_after = len(_psutil.Process().children(recursive=True))
            if children_after > children_before:
                result["ok"] = False
                result["error"] = f"seed={seed}: leaked child process(es): {children_before} -> {children_after}"
        result["elapsed"] = time.monotonic() - t0
    return result


def run_sweep(n: int, *, base_seed: int = 0, timeout_s: float = 15.0, verbose: bool = False,
              allow_slow_hooks: bool = True) -> dict:
    """Run `n` fuzz iterations (seeds base_seed..base_seed+n-1) against one
    shared MockUpstream. Returns {"n", "passed", "failed", "failures":
    [(seed, error), ...], "elapsed"}."""
    mock = MockUpstream().start()
    t0 = time.monotonic()
    passed = failed = 0
    failures = []
    try:
        for i in range(n):
            seed = base_seed + i
            r = run_one(seed, mock, timeout_s=timeout_s, allow_slow_hooks=allow_slow_hooks)
            if r["ok"]:
                passed += 1
            else:
                failed += 1
                failures.append((seed, r["error"], r["plan"], r["mode"], r["action"]))
            if verbose:
                status = "OK" if r["ok"] else "FAIL"
                print(f"[{status}] seed={seed} mode={r['mode']} action={r['action']} "
                      f"hooks={r['hook_profile']} events={r['events']} {r['elapsed']:.2f}s"
                      + ("" if r["ok"] else f" :: {r['error']}"))
    finally:
        mock.stop()
    return {"n": n, "passed": passed, "failed": failed, "failures": failures,
            "elapsed": time.monotonic() - t0}


if __name__ == "__main__":
    import sys as _sys
    n = int(_sys.argv[1]) if len(_sys.argv) > 1 else 200
    seed0 = int(_sys.argv[2]) if len(_sys.argv) > 2 else 0
    summary = run_sweep(n, base_seed=seed0, verbose=True)
    print("-" * 74)
    print(f"H9 FUZZ SWEEP: {summary['n']} runs, {summary['passed']} passed, "
          f"{summary['failed']} failed, {summary['elapsed']:.1f}s total")
    for seed, err, plan, mode, action in summary["failures"]:
        print(f"  seed={seed} mode={mode} action={action} plan={plan}\n    {err}")
    _sys.exit(0 if summary["failed"] == 0 else 1)
