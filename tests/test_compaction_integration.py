"""tests.test_compaction_integration -- agent/compact.py + agent/loop.py's
Session._run_compaction END TO END against a real (mocked-upstream)
Session: the log shows a `compacted` node, the next derived request is
smaller, PreCompact/PostCompact/SessionStart(compact) hook payloads are
correct, and /compact (commands/builtins.py) reaches a live session.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish, send_json_response
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps every `or:mock/...` ref below resolving exactly as it did
# before parse_model_ref started refusing an auto-detected-disabled
# provider; each test here already scopes its OWN BRIDGE_TEST_HOME.
ensure_default_provider_credentials()

test, TESTS = new_registry()

_HEADINGS = (
    "Primary Request and Intent", "Key Technical Concepts", "Files and Code",
    "Errors and Fixes", "Pending Jobs", "Current Work", "Next Step", "Critical Context",
)
_GOOD_SUMMARY = "\n".join(f"## {h}\nsome content for {h}." for h in _HEADINGS)


def _scn_good_summary(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": _GOOD_SUMMARY}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


SCENARIOS["compaction-good-summary"] = _scn_good_summary

# finding 4: an Anthropic-shaped "prompt is too long" wording is ALWAYS
# unfixable (providers/errors.py's own parse_context_overflow hard-codes
# `fixable=False` for this wording), so it raises ContextOverflow on the
# very first attempt with no max_tokens-clamped retry in between -- the
# simplest reliable way to script a summarisation call that overflows.
_OVERFLOW_400 = {"error": {"message": "prompt is too long: 999999 tokens > 100000 maximum",
                            "type": "invalid_request_error"}}


def _scn_overflow_on_exact_prefix_then_flatten_succeeds(h, body):
    """The exact-prefix summarisation attempt (this session's own frozen
    tool catalog still attached as `tools`) always overflows; the
    OpenCode-style flattened fallback (finding 4: `tools: []`, no
    catalog) succeeds with a good summary."""
    if (body or {}).get("tools"):
        send_json_response(h, 400, _OVERFLOW_400)
        return
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": _GOOD_SUMMARY}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


SCENARIOS["compaction-overflow-then-flatten"] = _scn_overflow_on_exact_prefix_then_flatten_succeeds


def _scn_overflow_always(h, body):
    """Every summarisation attempt overflows, including the flattened
    fallback (finding 4's total-failure path: no marker may be written)."""
    send_json_response(h, 400, _OVERFLOW_400)


SCENARIOS["compaction-overflow-always"] = _scn_overflow_always


def _scn_429_once_then_good_summary(h, body):
    """finding 4: the summarisation call reuses `_step`'s OWN transport
    retry ladder -- a single retryable 429 must be retried (with backoff),
    never treated as a content-validation failure. Keyed on a module-level
    counter since MockUpstream's own per-scenario state doesn't otherwise
    track call count within one test run."""
    _scn_429_once_then_good_summary.calls += 1
    if _scn_429_once_then_good_summary.calls == 1:
        send_json_response(h, 429, {"error": {"message": "rate limited, try again"}}, {"Retry-After": "0"})
        return
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": _GOOD_SUMMARY}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


_scn_429_once_then_good_summary.calls = 0
SCENARIOS["compaction-429-then-good"] = _scn_429_once_then_good_summary


class _FakeHookRunner:
    """Records every payload()/run() call so a test can assert the EXACT
    fields _run_compaction/_fire_session_start hand it, without spawning a
    real subprocess per call (the real HookRunner's subprocess mechanics
    are already covered elsewhere, e.g. test_hooks_loop_integration.py)."""

    def __init__(self):
        self.calls: list = []

    def has_hooks(self, event: str) -> bool:
        return True

    def payload(self, event: str, *, extra=None, **kw):
        # Mirrors hooks.build_payload's own contract: `extra` fields merge
        # at the TOP level of the returned dict (not nested under "extra"),
        # alongside standard fields like hook_event_name.
        out = {"hook_event_name": event, **kw}
        if extra:
            out.update(extra)
        return out

    def run(self, event: str, payload, matched: str = "", **kw):
        from halo_harness.hooks import HookOutcome
        self.calls.append({"event": event, "payload": payload, "matched": matched})
        return HookOutcome()

    def run_session_end(self, reason: str) -> None:
        self.calls.append({"event": "SessionEnd", "reason": reason})


def _new_session(fh, mock, *, model, hook_runner=None, max_turns=10, model_profile=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref,
        model_profile=model_profile or ModelProfile(context_tokens=200_000, max_output_tokens=8192),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="compact-int-")), model_label=model, session_context=session_ctx,
        openrouter_base_url=mock.base_url, max_turns=max_turns,
        permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]),
        hook_runner=hook_runner,
    )


@test
def test_run_compaction_end_to_end_shrinks_the_log_and_logs_a_compacted_node(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        hooks = _FakeHookRunner()
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary", hook_runner=hooks)
        # A small profile (tail-retention budget = min(15k, max(2k, 25% of
        # ~8.6k usable)) = 2,000 tokens = ~8,000 chars) against a MUCH
        # bigger synthetic history, so shadowing genuinely drops content --
        # the tiny-profile-vs-tiny-history case (everything fits in the
        # tail budget, nothing shrinks) is a real, EXPECTED property of
        # the formula, not what this test is about.
        session.model_profile = session.model_profile.__class__(context_tokens=10_000, max_output_tokens=1_000)

        # Build up some fake history directly on the log (cheaper/more
        # deterministic than driving several real turns through the mock).
        from halo_harness.agent.derive import derive_request
        filler = "x" * 400
        for i in range(60):
            session.log.append_user([{"type": "text", "text": f"question number {i}, please look at file_{i}.py -- {filler}"}])
            session.log.append_assistant(content=[{"type": "text", "text": f"answer number {i}: did the thing. {filler}"}], stop_reason="end_turn")

        before_system, before_messages, _ = derive_request(session.log, tools=None)
        before_len = len(before_system) + sum(len(json.dumps(m)) for m in before_messages)

        events_seen = list(session._run_compaction(1, trigger="auto"))

        ctx.check("a 'start' compaction event was emitted", any(e.kind == "compaction" and e.data["phase"] == "start" for e in events_seen))
        ctx.check("a 'done' compaction event was emitted", any(e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen))

        nodes = session.log.nodes()
        compacted_nodes = [n for n in nodes if n.get("type") == "compacted"]
        ctx.check(f"exactly one 'compacted' node logged, got {len(compacted_nodes)}", len(compacted_nodes) == 1)
        ctx.check("the compacted node carries surface_op=replace", compacted_nodes[0]["surface_op"] == "replace")
        ctx.check("the compacted node carries the trigger", compacted_nodes[0]["trigger"] == "auto")

        after_system, after_messages, _ = derive_request(session.log, tools=None)
        after_len = len(after_system) + sum(len(json.dumps(m)) for m in after_messages)
        ctx.check(f"the derived request shrank (before={before_len} after={after_len})", after_len < before_len)
        ctx.check("system text is untouched (never shadowed)", after_system == before_system)

        all_text = " ".join(b.get("text", "") for m in after_messages for b in m["content"] if isinstance(b, dict))
        ctx.check("the wrapped compacted-summary made it into the derived transcript", "<compacted-summary>" in all_text)
        ctx.check("early question content is gone (shadowed)", "question number 0" not in all_text)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5c_f16_compaction_strips_thinking_blocks_from_the_reappended_tail(ctx: Ctx):
    """H5c finding 16: a thinking block's `signature` is cryptographically
    bound to the EXACT prefix that preceded it when the model produced it
    (Anthropic's "preserved thinking" check). Compaction re-appends the
    verbatim tail right after a brand-new `<compacted-summary>` node -- a
    completely different prefix -- so any signature surviving in that tail
    is now stale and would fail signature validation on the very next
    request. The thinking block must be STRIPPED when the tail is
    re-appended; the tail's own real text/tool_use content must survive
    untouched."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary")
        session.model_profile = session.model_profile.__class__(context_tokens=10_000, max_output_tokens=1_000)

        filler = "x" * 400
        for i in range(60):
            session.log.append_user([{"type": "text", "text": f"question number {i} -- {filler}"}])
            session.log.append_assistant(content=[{"type": "text", "text": f"answer number {i}. {filler}"}], stop_reason="end_turn")
        # The LAST exchange -- guaranteed to survive in the verbatim tail
        # (select_verbatim_tail always keeps at least the last unit) --
        # carries a real, signed thinking block.
        session.log.append_user([{"type": "text", "text": "one final question"}])
        session.log.append_assistant(content=[
            {"type": "thinking", "text": "reasoning about the final question", "signature": "sig-bound-to-old-prefix"},
            {"type": "text", "text": "the final answer"},
        ], stop_reason="end_turn")

        events_seen = list(session._run_compaction(1, trigger="auto"))
        ctx.check("compaction completed", any(e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen))

        from halo_harness.agent.derive import derive_request
        _system, messages, _tools = derive_request(session.log, tools=None)
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        ctx.check(f"the final answer's assistant message survived in the tail, got {assistant_msgs}",
                  any(any(b.get("type") == "text" and "the final answer" in (b.get("text") or "") for b in m["content"])
                      for m in assistant_msgs))
        thinking_blocks = [b for m in assistant_msgs for b in m["content"]
                           if isinstance(b, dict) and b.get("type") == "thinking"]
        ctx.check(f"NO thinking block survived the re-append (stale signature), got {thinking_blocks}",
                  thinking_blocks == [])
        ctx.check("the stale signature text is nowhere in the derived transcript",
                  "sig-bound-to-old-prefix" not in json.dumps(messages))
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_run_compaction_fires_precompact_and_postcompact_hooks_with_correct_payloads(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        hooks = _FakeHookRunner()
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary", hook_runner=hooks)
        session.log.append_user([{"type": "text", "text": "do something"}])
        session.log.append_assistant(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

        list(session._run_compaction(1, trigger="manual", custom_instructions="focus on file names"))

        pre = next((c for c in hooks.calls if c["event"] == "PreCompact"), None)
        post = next((c for c in hooks.calls if c["event"] == "PostCompact"), None)
        # SessionStart fires once at session construction (source="startup")
        # AND again here (source="compact") -- find the COMPACT one
        # specifically, not just the first SessionStart call recorded.
        session_start = next((c for c in hooks.calls
                               if c["event"] == "SessionStart" and c["payload"].get("source") == "compact"), None)

        ctx.check("PreCompact fired", pre is not None)
        ctx.check(f"PreCompact payload carries trigger=manual, got {pre and pre['payload']}",
                  pre is not None and pre["payload"].get("trigger") == "manual")
        ctx.check("PreCompact payload carries the custom_instructions",
                  pre is not None and pre["payload"].get("custom_instructions") == "focus on file names")
        ctx.check("PreCompact's matched== trigger (manual|auto), per D-CFG", pre is not None and pre["matched"] == "manual")

        ctx.check("PostCompact fired", post is not None)
        ctx.check("PostCompact payload carries the compact_summary text",
                  post is not None and "Primary Request and Intent" in (post["payload"].get("compact_summary") or ""))

        ctx.check("SessionStart(compact) fired", session_start is not None)
        ctx.check("SessionStart payload's source is 'compact'",
                  session_start is not None and session_start["payload"].get("source") == "compact")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_compact_command_runs_a_real_compaction_through_the_facade(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_compact

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary")
        session.log.append_user([{"type": "text", "text": "some history to summarize"}])
        session.log.append_assistant(content=[{"type": "text", "text": "an answer"}], stop_reason="end_turn")

        facade = HeadlessFacade(cwd=fh["proj"], session=session)
        out = _cmd_compact("focus on file names", facade)
        ctx.check(f"/compact reports a result, got {out!r}", "Compacted the conversation" in out)
        ctx.check("a compacted node landed in the log via the command",
                  any(n.get("type") == "compacted" for n in session.log.nodes()))
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


def _fill_history(session, n=60):
    filler = "x" * 400
    for i in range(n):
        session.log.append_user([{"type": "text", "text": f"question number {i}, look at file_{i}.py -- {filler}"}])
        session.log.append_assistant(content=[{"type": "text", "text": f"answer number {i}: did the thing. {filler}"}],
                                      stop_reason="end_turn")


@test
def test_h5b_f04_overflow_on_exact_prefix_falls_back_to_flattened_serialisation(ctx: Ctx):
    """finding 4: when the EXACT structured prefix itself overflows on the
    summarisation call, compaction retries ONCE with OpenCode's flattened,
    tool-less plain-text serialisation instead of giving up -- a real
    Session, a real (mocked) overflow, driven through the real
    _run_compaction generator."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/compaction-overflow-then-flatten")
        session.model_profile = session.model_profile.__class__(context_tokens=10_000, max_output_tokens=1_000)
        _fill_history(session)

        events_seen = list(session._run_compaction(1, trigger="auto"))
        ctx.check("compaction still reports 'done' after falling back", any(
            e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen))
        ctx.check("no 'failed' phase was emitted", not any(
            e.kind == "compaction" and e.data["phase"] == "failed" for e in events_seen))
        nodes = session.log.nodes()
        ctx.check("a compacted marker WAS written (the fallback succeeded)",
                  any(n.get("type") == "compacted" for n in nodes))
        # The flattened fallback request is scenario-verifiable: exactly
        # one request had `tools` present (the failed exact-prefix
        # attempt) and at least one had `tools` absent/empty (the
        # successful flattened retry).
        tool_less_calls = [r for r in mock.requests if not (r.get("body") or {}).get("tools")]
        ctx.check(f"at least one call went out with no tools (the flattened fallback), got {len(tool_less_calls)}",
                  len(tool_less_calls) >= 1)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5c_f22_serialize_transcript_for_summary_bounds_total_size_keeping_the_head(ctx: Ctx):
    """H5c finding 22: the overflow fallback used to flatten the WHOLE
    (already-pruned) transcript with NO bound on the total size -- each
    individual tool_result was capped, but hundreds of turns could still
    add up to more than the fallback call's own small context window,
    overflowing a SECOND time with no further retry left. `max_chars`
    bounds the TOTAL string, keeping the HEAD (never splitting a message's
    own group of lines) and marking the cut."""
    from halo_harness.agent.loop import _SUMMARY_FALLBACK_TRUNCATED_MARKER, _serialize_transcript_for_summary

    messages = []
    for i in range(500):
        messages.append({"role": "user", "content": [{"type": "text", "text": f"user turn number {i}"}]})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"assistant reply number {i}"}]})

    unbounded = _serialize_transcript_for_summary(messages)
    ctx.check(f"sanity: the unbounded serialisation really is large, got {len(unbounded)} chars",
              len(unbounded) > 20_000)

    bounded = _serialize_transcript_for_summary(messages, max_chars=5_000)
    ctx.check(f"bounded output stays within a small overhead of the cap, got {len(bounded)} chars",
              len(bounded) <= 5_000 + len(_SUMMARY_FALLBACK_TRUNCATED_MARKER) + 200)
    ctx.check("the truncation marker is present", bounded.endswith(_SUMMARY_FALLBACK_TRUNCATED_MARKER))
    ctx.check("the HEAD (earliest turns) is kept, not the tail", "[User]: user turn number 0" in bounded)
    ctx.check("the tail (latest turns) was dropped", "[User]: user turn number 499" not in bounded)

    # A single message-group bigger than the whole budget is still kept in
    # full (never an empty fallback body) -- "always keep at least the
    # first non-empty group".
    huge_first = [{"role": "user", "content": [{"type": "text", "text": "x" * 10_000}]}]
    single = _serialize_transcript_for_summary(huge_first, max_chars=100)
    ctx.check(f"a single oversized group is kept whole rather than producing an empty body, got {len(single)} chars",
              len(single) >= 10_000)

    # No cap given at all -- unchanged, unbounded behaviour (existing
    # per-tool_result-only capping still applies, just no TOTAL cap).
    ctx.check("max_chars=None means no total cap (back-compatible default)",
              _serialize_transcript_for_summary(messages, max_chars=None) == unbounded)


@test
def test_h5b_f04_total_failure_never_writes_the_compacted_marker(ctx: Ctx):
    """finding 4 (critical): a compaction that fails completely (overflow
    even after the flattened-serialisation fallback) must NEVER write the
    `compacted` marker with "(summary unavailable)" -- the old bug. The
    session log must be byte-for-byte unchanged and a phase="failed"
    compaction event with a human reason must be emitted instead."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/compaction-overflow-always")
        session.model_profile = session.model_profile.__class__(context_tokens=10_000, max_output_tokens=1_000)
        _fill_history(session)
        nodes_before = session.log.nodes()

        events_seen = list(session._run_compaction(1, trigger="auto"))

        failed = [e for e in events_seen if e.kind == "compaction" and e.data["phase"] == "failed"]
        ctx.check(f"a 'failed' compaction event was emitted, got phases={[e.data['phase'] for e in events_seen if e.kind == 'compaction']}",
                  len(failed) == 1)
        ctx.check("the failure reason is a real string", bool(failed[0].data.get("reason")))
        ctx.check("no 'done' phase was ever emitted", not any(
            e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen))

        nodes_after = session.log.nodes()
        ctx.check(f"NO compacted marker was written, got {[n.get('type') for n in nodes_after if n.get('type') == 'compacted']}",
                  not any(n.get("type") == "compacted" for n in nodes_after))
        ctx.check("the log is completely unchanged (same node count)", len(nodes_after) == len(nodes_before))
        ctx.check("nothing mentions the old '(summary unavailable)' placeholder anywhere in the log",
                  not any("summary unavailable" in json.dumps(n) for n in nodes_after))
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5b_f04_retryable_429_reuses_the_step_retry_ladder(ctx: Ctx):
    """finding 4: a single retryable 429 during summarisation is retried
    (with backoff via `_step`'s own ladder), never treated as a content-
    validation failure -- the summary still succeeds and the marker is
    written, with no "(summary unavailable)" fallback text."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        _scn_429_once_then_good_summary.calls = 0  # test isolation
        session = _new_session(fh, mock, model="or:mock/compaction-429-then-good")
        session.log.append_user([{"type": "text", "text": "hello"}])
        session.log.append_assistant(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")

        events_seen = list(session._run_compaction(1, trigger="manual"))
        ctx.check("compaction succeeded despite the one 429", any(
            e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen))
        ctx.check(f"the upstream really was called twice (429 then success), got {_scn_429_once_then_good_summary.calls}",
                  _scn_429_once_then_good_summary.calls == 2)
        nodes = session.log.nodes()
        ctx.check("a compacted marker was written", any(n.get("type") == "compacted" for n in nodes))
        ctx.check("no '(summary unavailable)' placeholder anywhere",
                  not any("summary unavailable" in json.dumps(n) for n in nodes))
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5b_f04_prune_is_wired_into_every_ordinary_step(ctx: Ctx):
    """finding 4: agent/prune.py used to be wired ONLY into `/context` --
    `_derive_and_build` (every real model call) must ALSO prune tool
    results that fall OUTSIDE the 40k-token protection window before they
    go out over the wire."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        from halo_harness.agent.prune import OLD_TOOL_RESULT_CLEARED
        session = _new_session(fh, mock, model="or:mock/model")
        session.log.append_user([{"type": "text", "text": "run the old command"}])
        session.log.append_assistant(
            content=[{"type": "tool_use", "id": "call_old", "name": "Bash", "input": {"command": "old"}}],
            stop_reason="tool_use",
        )
        session.log.append_tool_result(tool_use_id="call_old", content="the old result", is_error=False)
        # Push "call_old" outside the 40k-token protection window with
        # enough newer filler content.
        for i in range(6):
            tid = f"call_{i}"
            session.log.append_user([{"type": "text", "text": f"q{i}"}])
            session.log.append_assistant(
                content=[{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "x"}}],
                stop_reason="tool_use",
            )
            session.log.append_tool_result(tool_use_id=tid, content="F" * 45000, is_error=False)

        _system_text, messages, _tools, body = session._derive_and_build()
        wire_blob = json.dumps(body.get("messages") or [])
        ctx.check("the old, now-outside-the-window tool_result was stubbed before hitting the wire",
                  OLD_TOOL_RESULT_CLEARED in wire_blob)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5b_f01_44kb_read_result_reaches_the_next_request_byte_for_byte(ctx: Ctx):
    """H5b finding 1 (critical), exactly as specified: a 44 KB tool result
    (a Read, in this repro) sitting INSIDE the protection window must
    appear in the NEXT request's wire body byte-for-byte, never head/tail-
    truncated by the old blanket "over 8,192 chars" rule."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model")
        session.log.append_user([{"type": "text", "text": "read the big file"}])
        session.log.append_assistant(
            content=[{"type": "tool_use", "id": "call_read", "name": "Read", "input": {"file_path": "/big.txt"}}],
            stop_reason="tool_use",
        )
        big_read_result = "line content here\n" * 2500  # ~44,500 chars, matches the review's own repro size
        ctx.check(f"fixture really is ~44KB, got {len(big_read_result)}", 40_000 < len(big_read_result) < 50_000)
        session.log.append_tool_result(tool_use_id="call_read", content=big_read_result, is_error=False)

        # `messages` (the 2nd return) is the Anthropic-shaped, ALREADY-
        # PRUNED list `_derive_and_build` actually passes to the wire
        # translation -- pruning's own concern is precisely THIS shape;
        # `body["messages"]` would be OpenAI-chat-translated (a "tool"-role
        # message keyed by `tool_call_id`, not `tool_use_id`) for this
        # mock/openrouter session, which is a wire-format detail orthogonal
        # to what this finding is about.
        _system_text, messages, _tools, _body = session._derive_and_build()
        found = None
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("tool_use_id") == "call_read":
                        found = block.get("content")
        ctx.check("the Read result is present in the very next request", found is not None)
        ctx.check(f"...and byte-for-byte identical to what the tool actually returned, got {len(found or '')} chars",
                  found == big_read_result)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5b_f02_two_consecutive_requests_share_an_identical_prefix(ctx: Ctx):
    """H5b finding 2: after a large step, two CONSECUTIVE `_derive_and_build`
    calls must produce wire message lists that agree byte-for-byte on their
    shared prefix -- the stub boundary must not slide forward between them
    just because one more (small) turn was appended in between."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model")
        session.log.append_user([{"type": "text", "text": "q0"}])
        session.log.append_assistant(
            content=[{"type": "tool_use", "id": "call_old", "name": "Bash", "input": {"command": "old"}}],
            stop_reason="tool_use",
        )
        session.log.append_tool_result(tool_use_id="call_old", content="old but important content " * 5,
                                        is_error=False)
        for i in range(6):
            tid = f"call_{i}"
            session.log.append_user([{"type": "text", "text": f"q{i}"}])
            session.log.append_assistant(
                content=[{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "x"}}],
                stop_reason="tool_use",
            )
            session.log.append_tool_result(tool_use_id=tid, content="F" * 45000, is_error=False)

        _s1, _m1, _t1, body1 = session._derive_and_build()
        wire1 = body1.get("messages") or []

        # One more (small) turn -- the STABLE case: this alone must not
        # advance the committed stub boundary (it takes a full 20k-token
        # batch to do that -- see agent/prune.py's own module docstring).
        session.log.append_user([{"type": "text", "text": "q6"}])
        session.log.append_assistant(
            content=[{"type": "tool_use", "id": "call_6", "name": "Bash", "input": {"command": "y"}}],
            stop_reason="tool_use",
        )
        session.log.append_tool_result(tool_use_id="call_6", content="small new result", is_error=False)
        _s2, _m2, _t2, body2 = session._derive_and_build()
        wire2 = body2.get("messages") or []

        shared = min(len(wire1), len(wire2))
        ctx.check(f"every shared message is byte-identical across the two requests, "
                  f"got {shared} shared of {len(wire1)}/{len(wire2)}",
                  all(wire1[i] == wire2[i] for i in range(shared)))
        ctx.check("the second request has exactly the new turn appended", len(wire2) > len(wire1))
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5b_f18_environment_and_deferred_tools_and_plan_reinjected_after_compaction(ctx: Ctx):
    """finding 18: compaction used to re-inject only CLAUDE.md/memory/
    files-read -- the environment snapshot, the deferred-tool-names
    reminder, and (when this session is in plan mode) the active plan
    file/note all vanished from the model's view after the FIRST
    compaction. All three must now be re-attached."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary")
        session.model_profile = session.model_profile.__class__(context_tokens=10_000, max_output_tokens=1_000)

        # A fake SessionCatalog with a deferred tool name, and plan mode
        # active with a real plan file -- both re-injection paths exercised
        # in one pass.
        class _FakeCatalog:
            deferred = {"mcp__kb__kb_search"}
        session.session_catalog = _FakeCatalog()
        session.permission_engine.mode = "plan"
        plan_path = Path(tempfile.mkdtemp(prefix="compact-plan-")) / "plan.md"
        plan_path.write_text("## Step 1\nDo the thing.\n", encoding="utf-8")
        session.permission_engine.set_plan_file(plan_path)

        _fill_history(session)
        list(session._run_compaction(1, trigger="auto"))

        from halo_harness.agent.derive import derive_request
        _system, messages, _tools = derive_request(session.log, tools=None)
        all_text = " ".join(b.get("text", "") for m in messages for b in (m.get("content") or [])
                             if isinstance(b, dict) and b.get("type") == "text")
        ctx.check("environment snapshot re-injected (cwd/OS/git block)", "cwd" in all_text.lower() or "working directory" in all_text.lower())
        ctx.check("deferred-tool-names reminder re-injected", "mcp__kb__kb_search" in all_text)
        ctx.check("the active plan note is re-attached", "Plan mode is active" in all_text)
        ctx.check("the plan file's own content is re-attached", "Do the thing." in all_text)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5c_f03_no_back_to_back_auto_compaction_driven_through_real_gate(ctx: Ctx):
    """H5b finding 1: `_maybe_auto_compact` must never run a SECOND
    compaction immediately after one that just ran, with no real step in
    between. Unlike the wrong-reason test this replaces (which hand-set
    `session._just_compacted = True` to FAKE "a compaction just ran"
    instead of driving it through a real `_maybe_auto_compact` call), this
    drives an ACTUAL compaction first, then proves the very next call is
    skipped (non-failure phase), and a fresh one is allowed again once the
    prompt genuinely drops below trigger."""
    from halo_harness.agent.compact import compaction_trigger_tokens
    from halo_harness.model import ModelProfile

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        profile = ModelProfile(context_tokens=200_000, max_output_tokens=8192)
        trigger = compaction_trigger_tokens(profile.context_tokens, profile.max_output_tokens)
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary", model_profile=profile)
        session.log.append_user([{"type": "text", "text": "hello"}])
        session.log.append_assistant(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")

        # Step 1: a REAL compaction runs (this profile's tail easily fits
        # under its own trigger once compacted, so no backoff gets armed).
        session._last_prompt_tokens = trigger + 5000
        step1 = list(session._maybe_auto_compact(1))
        ctx.check("step 1: a real compaction ran", any(
            e.kind == "compaction" and e.data["phase"] == "start" for e in step1))
        ctx.check("step 1: _just_compacted set after a successful compaction", session._just_compacted is True)
        ctx.check("step 1: no backoff armed (this compaction WAS effective)", session._compaction_backoff_remaining == 0)

        # Step 2: even though the caller reports the SAME still-over-trigger
        # size again (as if nothing had changed), the back-to-back guard
        # skips it -- non-failure phase, no second compacted node.
        session._last_prompt_tokens = trigger + 5000
        step2 = list(session._maybe_auto_compact(2))
        ctx.check("step 2: no compaction ran (back-to-back)", not any(
            e.kind == "compaction" and e.data["phase"] == "start" for e in step2))
        ctx.check("step 2: a non-failure 'skipped' event explains it (not 'failed')", any(
            e.kind == "compaction" and e.data["phase"] == "skipped" for e in step2))
        ctx.check("step 2: _just_compacted cleared after the one skip", session._just_compacted is False)
        ctx.check("only ONE compacted node was ever written", sum(
            1 for n in session.log.nodes() if n.get("type") == "compacted") == 1)

        # Step 3: a genuinely NEW real step's usage still reports over
        # trigger (real new content, not "the same failed attempt") -- a
        # FRESH compaction is correctly allowed (this is NOT "back-to-back
        # with no real step between": steps 1 and 3 have step 2's real
        # decision point between them, and finding 1 only forbids
        # compacting twice with NOTHING in between).
        session._last_prompt_tokens = trigger + 7000
        step3 = list(session._maybe_auto_compact(3))
        ctx.check("step 3: a fresh compaction runs again for genuinely new growth", any(
            e.kind == "compaction" and e.data["phase"] == "start" for e in step3))
        ctx.check("two compacted nodes total now (step 1 and step 3)", sum(
            1 for n in session.log.nodes() if n.get("type") == "compacted") == 2)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5c_f03_ineffective_compaction_backs_off_instead_of_retrying_every_other_step(ctx: Ctx):
    """H5b/H5c finding 3: "skip when the post-compaction estimate is still
    at or above the trigger" -- with the finding's own 32,768/29,491
    small-window profile and a trailing exchange too big to ever fit under
    that window's tiny trigger even after compacting (the verbatim tail
    alone still exceeds it), the OLD code's one-shot-only guard re-ran a
    full summarisation call every OTHER step forever (verified: a 5-step
    Read turn made 3 summariser calls, at steps 1, 3 and 5). Proves a
    compaction that runs but achieves nothing useful now backs off for
    `_COMPACTION_FAILURE_BACKOFF_STEPS` steps, the same as an outright
    failure, instead of retrying next-available-step forever."""
    from halo_harness.agent.compact import compaction_trigger_tokens
    from halo_harness.agent.loop import _COMPACTION_FAILURE_BACKOFF_STEPS
    from halo_harness.model import ModelProfile

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        profile = ModelProfile(context_tokens=32_768, max_output_tokens=29_491)
        trigger = compaction_trigger_tokens(profile.context_tokens, profile.max_output_tokens)
        session = _new_session(fh, mock, model="or:mock/compaction-good-summary", model_profile=profile)
        session.log.append_user([{"type": "text", "text": "read a huge file"}])
        # A single trailing exchange bigger than the trigger itself (by
        # chars/4) -- `select_verbatim_tail` always keeps at least the last
        # unit even when it alone exceeds the tail budget, so the retained
        # tail alone guarantees the post-compaction estimate stays >= trigger.
        huge_text = "x" * (trigger * 4 + 40_000)
        session.log.append_assistant(content=[{"type": "text", "text": huge_text}], stop_reason="end_turn")
        session._last_prompt_tokens = trigger + 5000

        step1 = list(session._maybe_auto_compact(1))
        ctx.check("step 1: a real compaction ran", any(
            e.kind == "compaction" and e.data["phase"] == "start" for e in step1))
        ctx.check("step 1: it did NOT fail outright (real summary text was produced)", not any(
            e.kind == "compaction" and e.data["phase"] == "failed" for e in step1))
        ctx.check(f"step 1: backoff armed because the retained tail alone is still >= trigger, "
                  f"got {session._compaction_backoff_remaining}",
                  session._compaction_backoff_remaining == _COMPACTION_FAILURE_BACKOFF_STEPS)

        # Step 2: the ordinary one-shot back-to-back guard fires FIRST (as
        # in the test above) -- the backoff counter is not even consulted
        # yet, so it has not ticked down.
        step2 = list(session._maybe_auto_compact(2))
        ctx.check("step 2: skipped (back-to-back), not a fresh attempt", not any(
            e.kind == "compaction" and e.data["phase"] == "start" for e in step2))
        ctx.check(f"step 2: backoff counter untouched by the one-shot guard, got {session._compaction_backoff_remaining}",
                  session._compaction_backoff_remaining == _COMPACTION_FAILURE_BACKOFF_STEPS)

        # Steps 3..(2+N): the backoff itself ticks down, one skip per step,
        # with NO further summarisation call attempted at all.
        for i in range(_COMPACTION_FAILURE_BACKOFF_STEPS):
            step = list(session._maybe_auto_compact(3 + i))
            ctx.check(f"backoff step {i}: no compaction attempted", not any(
                e.kind == "compaction" and e.data["phase"] in ("start", "failed") for e in step))
            ctx.check(f"backoff step {i}: a 'backing off' skip explains it", any(
                e.kind == "compaction" and e.data["phase"] == "skipped"
                and "backing off" in e.data.get("reason", "") for e in step))
        ctx.check("backoff counter reached zero", session._compaction_backoff_remaining == 0)
        ctx.check("still only ONE compacted node written across the whole backoff window", sum(
            1 for n in session.log.nodes() if n.get("type") == "compacted") == 1)

        # Once exhausted, a fresh attempt is allowed again (and, since the
        # huge tail is still there, re-arms the backoff once more).
        step_final = list(session._maybe_auto_compact(999))
        ctx.check("after the backoff window, a real attempt runs again", any(
            e.kind == "compaction" and e.data["phase"] == "start" for e in step_final))
        ctx.check("backoff re-armed after the new (still ineffective) attempt",
                  session._compaction_backoff_remaining == _COMPACTION_FAILURE_BACKOFF_STEPS)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h5c_f22_failed_auto_compaction_backs_off_instead_of_retrying_every_step(ctx: Ctx):
    """H5c finding 22: after a FAILED auto-compaction attempt (the
    summarisation call fails outright, even after the overflow fallback),
    the very next steps must NOT immediately retry the whole (potentially
    slow) attempt again -- back off for `_COMPACTION_FAILURE_BACKOFF_STEPS`
    steps first."""
    from halo_harness.agent.compact import compaction_trigger_tokens
    from halo_harness.agent.loop import _COMPACTION_FAILURE_BACKOFF_STEPS
    from halo_harness.model import ModelProfile

    fh = build_fake_home()
    mock = MockUpstream().start()

    def _scn_always_overflow(h, body):
        send_json_response(h, 400, _OVERFLOW_400)

    SCENARIOS["compaction-always-overflow"] = _scn_always_overflow
    try:
        profile = ModelProfile(context_tokens=200_000, max_output_tokens=8192)
        trigger = compaction_trigger_tokens(profile.context_tokens, profile.max_output_tokens)
        session = _new_session(fh, mock, model="or:mock/compaction-always-overflow", model_profile=profile)
        session.log.append_user([{"type": "text", "text": "hello"}])
        session.log.append_assistant(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")
        session._last_prompt_tokens = trigger + 5000

        step1 = list(session._maybe_auto_compact(1))
        ctx.check("step 1: the attempt genuinely failed (phase=failed)", any(
            e.kind == "compaction" and e.data["phase"] == "failed" for e in step1))
        ctx.check("step 1: _just_compacted is false after a failure", session._just_compacted is False)
        ctx.check(f"backoff armed for {_COMPACTION_FAILURE_BACKOFF_STEPS} steps, got {session._compaction_backoff_remaining}",
                  session._compaction_backoff_remaining == _COMPACTION_FAILURE_BACKOFF_STEPS)

        # Every step during the backoff window skips WITHOUT attempting a
        # new (doomed) compaction call.
        for i in range(_COMPACTION_FAILURE_BACKOFF_STEPS):
            step = list(session._maybe_auto_compact(2 + i))
            ctx.check(f"backoff step {i}: no compaction attempted (no start/failed event)", not any(
                e.kind == "compaction" and e.data["phase"] in ("start", "failed") for e in step))
            ctx.check(f"backoff step {i}: a 'skipped' backoff event explains it", any(
                e.kind == "compaction" and e.data["phase"] == "skipped" and "backing off" in e.data.get("reason", "")
                for e in step))
        ctx.check("backoff counter reached zero", session._compaction_backoff_remaining == 0)

        # Once the backoff is exhausted, a real attempt runs again (and
        # fails again, re-arming the backoff).
        step_final = list(session._maybe_auto_compact(999))
        ctx.check("after the backoff window, a real attempt runs again", any(
            e.kind == "compaction" and e.data["phase"] == "failed" for e in step_final))
        ctx.check("backoff re-armed after the new failure", session._compaction_backoff_remaining == _COMPACTION_FAILURE_BACKOFF_STEPS)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h8_compaction_model_actually_used_by_the_summariser(ctx: Ctx):
    """H8 cheap must-do: `compactionModel` (settings.json/config.json,
    resolved by agent/compact.resolve_knobs) is actually used to route the
    summarisation call, instead of being resolved and never read. Both
    models are on the SAME mock provider/creds (the documented same-
    provider-only limitation) but are DISTINCT model ids the mock can
    tell apart."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model")
        session.log.append_user([{"type": "text", "text": "hello"}])
        session.log.append_assistant(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")

        from halo_harness.agent.compact import CompactionKnobs
        session._compaction_knobs = CompactionKnobs(compaction_model="or:mock/compaction-good-summary")

        list(session._run_compaction(1, trigger="manual"))
        models_called = {r.get("body", {}).get("model") for r in mock.requests}
        ctx.check(f"the summarisation call used compactionModel, got {models_called}",
                  "mock/compaction-good-summary" in models_called)
        ctx.check("the session's own model_ref is restored after the summary call",
                  session.model_ref.raw == "or:mock/model")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h202_compaction_role_is_the_rung_after_compaction_model(ctx: Ctx):
    """Halo 2.0.2 brief A.1: "`compaction` is the rung after
    `compactionModel`" -- consulted ONLY when `compactionModel` itself
    resolved to nothing."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model")
        session.log.append_user([{"type": "text", "text": "hello"}])
        session.log.append_assistant(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")

        from halo_harness.agent.compact import CompactionKnobs
        session._compaction_knobs = CompactionKnobs(compaction_model=None)  # nothing set at the compactionModel rung
        session.roles = {"compaction": "or:mock/compaction-from-role"}

        list(session._run_compaction(1, trigger="manual"))
        models_called = {r.get("body", {}).get("model") for r in mock.requests}
        ctx.check(f"the summarisation call used the compaction ROLE's model, got {models_called}",
                  "mock/compaction-from-role" in models_called)
        ctx.check("the session's own model_ref is restored after the summary call",
                  session.model_ref.raw == "or:mock/model")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h202_compaction_model_still_beats_the_compaction_role(ctx: Ctx):
    """`compactionModel` stays the higher-precedence rung -- a configured
    `compaction` role must never override it."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model")
        session.log.append_user([{"type": "text", "text": "hello"}])
        session.log.append_assistant(content=[{"type": "text", "text": "hi"}], stop_reason="end_turn")

        from halo_harness.agent.compact import CompactionKnobs
        session._compaction_knobs = CompactionKnobs(compaction_model="or:mock/compaction-good-summary")
        session.roles = {"compaction": "or:mock/compaction-from-role"}

        list(session._run_compaction(1, trigger="manual"))
        models_called = {r.get("body", {}).get("model") for r in mock.requests}
        ctx.check(f"compactionModel still wins, got {models_called}",
                  "mock/compaction-good-summary" in models_called
                  and "mock/compaction-from-role" not in models_called)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_h202_compaction_role_effort_is_applied_and_restored(ctx: Ctx):
    """2.0.2 review finding 12 (major), second half: the `compaction`
    role's own `effort` (a `{"model", "effort"}` table value) used to be
    unpacked (`raw, _compaction_effort = role_value_parts(...)`) and then
    dropped outright -- the summary call ran on the swapped-in model but
    the SESSION's own ordinary effort, never a role-specific one."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model")
        from halo_harness.agent.compact import CompactionKnobs
        session._compaction_knobs = CompactionKnobs(compaction_model=None)
        session.roles = {"compaction": {"model": "or:mock/compaction-from-role", "effort": "high"}}
        session.effort = "low"  # the session's own ordinary effort, before any swap

        saved = session._compaction_model_override()
        ctx.check(f"the role's own effort is applied for the summary call, got {session.effort!r}",
                  session.effort == "high")
        session._restore_compaction_model(saved)
        ctx.check(f"the session's own ordinary effort is restored afterward, got {session.effort!r}",
                  session.effort == "low")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
