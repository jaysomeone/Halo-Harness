"""tests.test_escalation -- Halo 2.0.3 round 5e: hybrid escalation as an
explicit policy (`routing.escalation`). Pure-function coverage for
`agent.escalation` (policy loading, the per-role override, the tool-
failure count, the judge-reply parse, decision formatting) plus real
`Session._maybe_escalate` integration against a mock Ollama upstream for
the `tool_failures`/`context_overflow` triggers in "ask" mode (no network
needed -- "ask" never calls the escalation target) and the `low_confidence`
trigger's real judge call (against the SAME mock, scripted to answer
CONFIDENT/UNSURE).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_ollama import MockUpstream

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)
    os.environ.pop("BRIDGE_TEST_HOME", None)
    os.environ.pop("OLLAMA_HOST", None)


# ---------------------------------------------------------------------------
# agent.escalation -- pure functions
# ---------------------------------------------------------------------------

@test
def test_load_escalation_policy_none_when_unset(ctx: Ctx):
    from halo_harness.agent.escalation import load_escalation_policy
    _fresh_state_dir("esc-unset-")
    try:
        ctx.check("no policy configured -> None", load_escalation_policy() is None)
    finally:
        _clear_env()


@test
def test_load_escalation_policy_parses_a_valid_dict(ctx: Ctx):
    from halo_harness.agent.escalation import load_escalation_policy
    from halo_harness.theme import set_config_value
    _fresh_state_dir("esc-valid-")
    try:
        set_config_value("routing.escalation", {
            "to": "or:anthropic/claude-haiku-4.5", "when": ["low_confidence", "tool_failures", "nonsense"],
            "ask": False,
        })
        policy = load_escalation_policy()
        ctx.check("policy resolved", policy is not None)
        ctx.check(f"to, got {policy.to!r}", policy.to == "or:anthropic/claude-haiku-4.5")
        ctx.check(f"unrecognized trigger dropped, got {policy.when}",
                  policy.when == ("low_confidence", "tool_failures"))
        ctx.check(f"ask is False, got {policy.ask!r}", policy.ask is False)
    finally:
        _clear_env()


@test
def test_load_escalation_policy_rejects_no_to_or_no_recognized_when(ctx: Ctx):
    from halo_harness.agent.escalation import load_escalation_policy
    from halo_harness.theme import set_config_value
    _fresh_state_dir("esc-bad-")
    try:
        set_config_value("routing.escalation", {"when": ["low_confidence"]})  # no "to"
        ctx.check("no to -> None", load_escalation_policy() is None)
        set_config_value("routing.escalation", {"to": "or:x/y", "when": ["nonsense"]})
        ctx.check("no recognized trigger -> None", load_escalation_policy() is None)
        set_config_value("routing.escalation", {"to": "or:x/y", "when": "tool_failures"})
        policy = load_escalation_policy()
        ctx.check(f"a bare string 'when' normalizes to a one-item tuple, got {policy.when if policy else None}",
                  policy is not None and policy.when == ("tool_failures",))
    finally:
        _clear_env()


@test
def test_role_escalation_enabled(ctx: Ctx):
    from halo_harness.agent.escalation import role_escalation_enabled
    ctx.check("no role name -> enabled", role_escalation_enabled(None, {"researcher": {"escalation": False}}))
    ctx.check("no table -> enabled", role_escalation_enabled("researcher", None))
    ctx.check("role not in table -> enabled", role_escalation_enabled("researcher", {}))
    ctx.check("a bare model string entry -> enabled", role_escalation_enabled("researcher", {"researcher": "ol:x"}))
    ctx.check("entry with no escalation key -> enabled",
              role_escalation_enabled("researcher", {"researcher": {"model": "ol:x"}}))
    ctx.check("escalation: false -> disabled",
              not role_escalation_enabled("researcher", {"researcher": {"model": "ol:x", "escalation": False}}))
    ctx.check("escalation: true -> enabled",
              role_escalation_enabled("researcher", {"researcher": {"model": "ol:x", "escalation": True}}))


@test
def test_count_tool_failures_since(ctx: Ctx):
    from halo_harness.agent.escalation import count_tool_failures_since
    nodes = [
        {"type": "user"},
        {"type": "tool_result", "is_error": True},   # before start_index -- never counted
        {"type": "tool_result", "is_error": True},
        {"type": "tool_result", "is_error": False},
        {"type": "tool_result", "is_error": True},
        {"type": "text"},
    ]
    ctx.check(f"2 failures from index 2 on, got {count_tool_failures_since(nodes, 2)}",
              count_tool_failures_since(nodes, 2) == 2)
    ctx.check(f"3 failures from index 0 on, got {count_tool_failures_since(nodes, 0)}",
              count_tool_failures_since(nodes, 0) == 3)
    ctx.check("0 from the end", count_tool_failures_since(nodes, len(nodes)) == 0)


@test
def test_judge_says_confident(ctx: Ctx):
    from halo_harness.agent.escalation import judge_says_confident
    ctx.check("CONFIDENT -> True", judge_says_confident("CONFIDENT"))
    ctx.check("confident, lowercase -> True", judge_says_confident("confident"))
    ctx.check("UNSURE -> False", not judge_says_confident("UNSURE"))
    ctx.check("'Unsure.' with punctuation -> False", not judge_says_confident("Unsure."))
    ctx.check("empty -> True (benefit of the doubt)", judge_says_confident(""))
    ctx.check("None -> True (benefit of the doubt)", judge_says_confident(None))
    ctx.check("unparseable garbage -> True (benefit of the doubt)", judge_says_confident("¯\\_(ツ)_/¯"))


@test
def test_format_decision_line(ctx: Ctx):
    from halo_harness.agent.escalation import EscalationDecision, format_decision_line
    asked = EscalationDecision(turn=3, trigger="tool_failures", to="or:x/y", action="asked")
    escalated = EscalationDecision(turn=4, trigger="low_confidence", to="or:x/y", action="escalated")
    ctx.check(f"asked phrasing, got {format_decision_line(asked)!r}",
              format_decision_line(asked) == "turn 3: asked about escalating to or:x/y (tool_failures)")
    ctx.check(f"escalated phrasing, got {format_decision_line(escalated)!r}",
              format_decision_line(escalated) == "turn 4: escalated to or:x/y (low_confidence)")


# ---------------------------------------------------------------------------
# Session._maybe_escalate -- real integration, no network for "ask" mode
# ---------------------------------------------------------------------------

def _new_ollama_session(fh, mock, *, model="ol:plain-text"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["OLLAMA_HOST"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    return Session(
        cwd=fh["proj"], model_ref=parse_model_ref(model),
        model_profile=ModelProfile(context_tokens=10_000, max_output_tokens=1_000),
        creds=ProviderCreds(base_url=mock.base_url, api_key=""),
        state_dir=Path(tempfile.mkdtemp(prefix="esc-session-")), model_label=model,
        session_context=session_ctx, max_turns=10,
    )


@test
def test_maybe_escalate_ask_mode_tool_failures_notifies_and_stays_local(ctx: Ctx):
    from halo_harness.theme import set_config_value
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        # BRIDGE_TEST_HOME MUST be set before the very first get_config_
        # value/set_config_value call of this test -- bridge_home() reads
        # the env var at CALL time, and set here too late once already hit
        # the REAL ~/.halo (caught live during this round's own test run;
        # see the hand-back report).
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        set_config_value("routing.escalation", {"to": "or:anthropic/claude-haiku-4.5",
                                                 "when": ["tool_failures"], "ask": True})
        session = _new_ollama_session(fh, mock)
        before_ref = session.model_ref
        session._turn_log_start_idx = len(session.log.nodes())
        session.log.append_tool_result(tool_use_id="x1", content="boom", is_error=True)
        session.log.append_tool_result(tool_use_id="x2", content="boom again", is_error=True)
        events_out = list(session._maybe_escalate(1))
        ctx.check(f"exactly one notification, got {events_out}", len(events_out) == 1)
        text = events_out[0].data.get("text", "")
        ctx.check(f"names the trigger, got {text!r}", "tool_failures" in text)
        ctx.check(f"names the target, got {text!r}", "or:anthropic/claude-haiku-4.5" in text)
        ctx.check(f"stayed on the local model, got {session.model_ref.raw!r}", session.model_ref is before_ref)
        ctx.check(f"one 'asked' decision recorded, got {session._escalation_decisions}",
                  len(session._escalation_decisions) == 1 and session._escalation_decisions[0].action == "asked")
        chats = [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/") == "/api/chat"]
        ctx.check(f"ask mode never calls the escalation target (no judge/main call made either): "
                  f"got {len(chats)} /api/chat calls", len(chats) == 0)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("OLLAMA_HOST", None)


@test
def test_maybe_escalate_auto_mode_context_overflow_switches_model(ctx: Ctx):
    from halo_harness.theme import set_config_value
    from tests.helpers.mock_openai import MockUpstream as MockOpenAI
    fh = build_fake_home()
    mock = MockUpstream().start()
    cloud = MockOpenAI().start()
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])  # see the matching comment above -- order matters
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = cloud.base_url
        set_config_value("routing.escalation", {"to": "or:deepseek/deepseek-v3.2",
                                                 "when": ["context_overflow"], "ask": False})
        session = _new_ollama_session(fh, mock)
        session._turn_context_overflow_count = 1
        events_out = list(session._maybe_escalate(2))
        ctx.check(f"exactly one notification, got {events_out}", len(events_out) == 1)
        ctx.check(f"exact wording, got {events_out[0].data.get('text')!r}",
                  events_out[0].data.get("text") == "escalated to or:deepseek/deepseek-v3.2: context_overflow")
        ctx.check(f"model actually switched, got {session.model_ref.raw!r}",
                  session.model_ref.raw == "or:deepseek/deepseek-v3.2")
        ctx.check(f"one 'escalated' decision recorded, got {session._escalation_decisions}",
                  len(session._escalation_decisions) == 1
                  and session._escalation_decisions[0].action == "escalated"
                  and session._escalation_decisions[0].trigger == "context_overflow")
    finally:
        mock.stop()
        cloud.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("OLLAMA_HOST", None)
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_maybe_escalate_low_confidence_uses_real_judge_call(ctx: Ctx):
    from halo_harness.theme import set_config_value
    fh = build_fake_home()
    mock = MockUpstream().start()

    def _scn_unsure(h, body):
        from tests.helpers.mock_ollama import _finish_chat
        _finish_chat(h, [{"message": {"role": "assistant", "content": "UNSURE"}, "done": True,
                           "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 1}])

    mock.scenarios["plain-text"] = _scn_unsure
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])  # see the matching comment above -- order matters
        set_config_value("routing.escalation", {"to": "or:anthropic/claude-haiku-4.5",
                                                 "when": ["low_confidence"], "ask": True})
        session = _new_ollama_session(fh, mock)
        events_out = list(session._maybe_escalate(1, final_text="I'm not totally sure about this."))
        ctx.check(f"exactly one notification (the judge said UNSURE), got {events_out}", len(events_out) == 1)
        ctx.check(f"names low_confidence, got {events_out[0].data.get('text')!r}",
                  "low_confidence" in events_out[0].data.get("text", ""))
        chats = [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/") == "/api/chat"]
        ctx.check(f"exactly one real judge call made, got {len(chats)}", len(chats) == 1)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("OLLAMA_HOST", None)


@test
def test_maybe_escalate_is_a_noop_on_a_cloud_model_session(ctx: Ctx):
    """Local first: never on a cloud-model session, even with a policy
    configured and a trigger that would otherwise fire."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    from halo_harness.theme import set_config_value
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        set_config_value("routing.escalation", {"to": "or:anthropic/claude-haiku-4.5",
                                                 "when": ["context_overflow"], "ask": True})
        session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref("or:mock/model"), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url="http://127.0.0.1:1", api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="esc-cloud-")), model_label="or:mock/model",
            session_context=session_ctx, max_turns=6,
        )
        session._turn_context_overflow_count = 5
        events_out = list(session._maybe_escalate(1))
        ctx.check(f"no events on a cloud-model session, got {events_out}", events_out == [])
        ctx.check("no decisions recorded", session._escalation_decisions == [])
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)


# ---------------------------------------------------------------------------
# /escalation (commands.builtins._cmd_escalation)
# ---------------------------------------------------------------------------

@test
def test_cmd_escalation_shows_policy_and_decisions(ctx: Ctx):
    from halo_harness.agent.escalation import EscalationDecision
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_escalation
    from halo_harness.theme import set_config_value
    _fresh_state_dir("esc-cmd-")
    try:
        no_policy = _cmd_escalation("", HeadlessFacade(cwd=Path(".")))
        ctx.check(f"no policy configured, got {no_policy!r}", "No hybrid-escalation policy configured" in no_policy)
        set_config_value("routing.escalation", {"to": "or:anthropic/claude-haiku-4.5",
                                                 "when": ["low_confidence", "tool_failures"], "ask": True})

        class _FakeSession:
            pass

        session = _FakeSession()
        session._escalation_decisions = [
            EscalationDecision(turn=1, trigger="tool_failures", to="or:anthropic/claude-haiku-4.5", action="asked"),
        ]
        out = _cmd_escalation("", HeadlessFacade(cwd=Path("."), session=session))
        ctx.check(f"policy line present, got {out!r}", "Escalation policy: to or:anthropic/claude-haiku-4.5" in out)
        ctx.check("decision line present", "turn 1: asked about escalating to" in out)
    finally:
        _clear_env()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
