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
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish

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
        from rolo_claude.hooks import HookOutcome
        self.calls.append({"event": event, "payload": payload, "matched": matched})
        return HookOutcome()

    def run_session_end(self, reason: str) -> None:
        self.calls.append({"event": "SessionEnd", "reason": reason})


def _new_session(fh, mock, *, model, hook_runner=None, max_turns=10):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(context_tokens=200_000, max_output_tokens=8192),
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
        from rolo_claude.agent.derive import derive_request
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
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_compact

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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
