"""tests.test_hotfix_101_fix_effort_retry -- 1.0.1 fixpass finding 10: the
gpt-6 "function tools + reasoning_effort" classifier no longer misfires on
a generic "...parameter... reasoning_effort..." 400 (operator precedence
made it match any message naming both "reasoning_effort" and a bare
"param"); the strip-the-field retry can still fire as an independent
fallback after a "none" retry that itself also fails, instead of being
permanently blocked by a single shared one-shot flag; a successful strip
retry resets `Session.effort` so later steps don't keep re-sending the
rejected value first.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_databricks import MockDatabricks
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


class _Env:
    """`Session.__init__` builds a real `SessionLog`, which opens/writes
    under `bridge_home()` the moment the Session exists -- NEVER derived
    from `fh["home"]`/`cwd` on its own. Without BRIDGE_TEST_HOME actually
    set, that falls back to the REAL ~/.halo/sessions on whatever
    machine runs the suite."""

    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
        os.environ["BRIDGE_TEST_HOME"] = str(self._fh["home"])
        os.environ.pop("BRIDGE_STATE_DIR", None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_dbx_session(fh, mock, *, model: str, effort=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-effort-retry-")), model_label=model,
        session_context=session_ctx, max_turns=6, effort=effort,
    )


# ---------------------------------------------------------------------------
# Classifier: narrowed to the real gpt-6 "function tool" wording only.
# ---------------------------------------------------------------------------

@test
def test_classifier_no_longer_matches_generic_parameter_wording(ctx: Ctx):
    from halo_harness.providers.errors import is_effort_with_tools_rejected_message
    # finding 10's own two examples -- both contain "reasoning_effort" and
    # the bare substring "param" but are NOT the gpt-6-specific message.
    ctx.check("does not match OpenAI's generic 'Unsupported parameter' wording",
              not is_effort_with_tools_rejected_message(
                  "Unsupported parameter: 'reasoning_effort' is not supported with this model."))
    ctx.check("does not match a generic 'Invalid value for parameter' wording",
              not is_effort_with_tools_rejected_message(
                  "Invalid value for parameter reasoning_effort: 'max'"))
    ctx.check("still matches the REAL gpt-6 wording", is_effort_with_tools_rejected_message(
        "Function tools with reasoning_effort are not supported for gpt-6-sol in /v1/chat/completions. "
        "To use function tools, use /v1/responses or set reasoning_effort to 'none'."))


@test
def test_general_classifier_still_matches_both_generic_examples(ctx: Ctx):
    """The generic strip-retry classifier is UNCHANGED and broad on
    purpose -- these messages must still be recognized as effort-related SO
    THE STRIP RETRY CAN HANDLE THEM, just not through the gpt-6 branch."""
    from halo_harness.providers.errors import is_effort_rejected_message
    ctx.check("matches 'Unsupported parameter' wording",
              is_effort_rejected_message("Unsupported parameter: 'reasoning_effort' is not supported with this model."))
    ctx.check("matches 'Invalid value for parameter' wording",
              is_effort_rejected_message("Invalid value for parameter reasoning_effort: 'max'"))


# ---------------------------------------------------------------------------
# End to end: the misclassification used to burn the ONE shared retry on a
# "none" attempt that could never work, failing the turn outright.
# ---------------------------------------------------------------------------

@test
def test_generic_param_wording_goes_straight_to_strip_and_succeeds(ctx: Ctx):
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            session = _new_dbx_session(
                fh, mock, model="dbx:databricks-gpt-5-reasoning-effort-param-reject-unless-stripped",
                effort="high")
            events_seen = list(session.turn("hello"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
            ctx.check(f"exactly two upstream attempts (no wasted 'none' attempt), got {len(mock.requests)}",
                      len(mock.requests) == 2)
            first_body, second_body = mock.requests[0]["body"] or {}, mock.requests[1]["body"] or {}
            ctx.check(f"first attempt carried the effort field, got {first_body.get('reasoning_effort')!r}",
                      first_body.get("reasoning_effort") == "high")
            ctx.check(f"retry STRIPPED the field entirely (not set to 'none'), got {second_body!r}",
                      "reasoning_effort" not in second_body)
            ctx.check(f"self.effort reset after the successful strip, got {session.effort!r}",
                      session.effort is None)
        finally:
            mock.stop()


@test
def test_strip_retry_fires_as_a_second_fallback_after_a_failed_none_retry(ctx: Ctx):
    """The independent-flags fix: a route that genuinely triggers the gpt-6
    "none" retry (correctly classified) but STILL rejects reasoning_effort
    even as "none" must fall through to the general strip retry instead of
    failing outright."""
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            session = _new_dbx_session(
                fh, mock, model="dbx:databricks-gpt-5-reasoning-effort-tools-reject-even-with-none",
                effort="high")
            events_seen = list(session.turn("read a file"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
            ctx.check(f"exactly THREE upstream attempts (original, none-retry, strip-retry), got "
                      f"{len(mock.requests)}", len(mock.requests) == 3)
            bodies = [r["body"] or {} for r in mock.requests]
            ctx.check(f"attempt 1 sent the requested effort, got {bodies[0].get('reasoning_effort')!r}",
                      bodies[0].get("reasoning_effort") == "high")
            ctx.check(f"attempt 2 (the none-retry) set it to 'none', got {bodies[1].get('reasoning_effort')!r}",
                      bodies[1].get("reasoning_effort") == "none")
            ctx.check(f"attempt 3 (the strip-retry) removed it entirely, got {bodies[2]!r}",
                      "reasoning_effort" not in bodies[2])
            ctx.check(f"self.effort reset after the successful strip, got {session.effort!r}",
                      session.effort is None)
        finally:
            mock.stop()


# ---------------------------------------------------------------------------
# 1.0.1 part 2 fixpass finding 13: the "none" rule must be learned only when
# the "none" retry ITSELF succeeded -- not when it also 400'd and the
# separate strip-retry is what actually rescued the turn (the exact scenario
# `test_strip_retry_fires_as_a_second_fallback_after_a_failed_none_retry`
# above already drives: 3 attempts, "none" rejected, strip succeeds).
# ---------------------------------------------------------------------------

@test
def test_rule_is_not_learned_when_the_none_retry_itself_failed(ctx: Ctx):
    """Before this fix: `effort_none_retried` alone gated the learn, so this
    exact 3-attempt sequence (original rejected, "none" ALSO rejected, the
    independent strip-retry succeeds) wrongly recorded "none" as this
    endpoint's permanent rule -- every later tool step on this route would
    then pay one guaranteed-to-fail "none" request before the strip retry
    rescued it, forever, and `/effort` would falsely show "none (tools)"."""
    from halo_harness.providers.learned_rules import learned_reasoning_effort_with_tools
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            session = _new_dbx_session(
                fh, mock, model="dbx:databricks-gpt-5-reasoning-effort-tools-reject-even-with-none",
                effort="high")
            events_seen = list(session.turn("read a file"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
            ctx.check(f"3 attempts as before (original, none-retry, strip-retry), got {len(mock.requests)}",
                      len(mock.requests) == 3)
            ctx.check(f"the 'none' rule was NOT learned on the live session profile, got "
                      f"{session.provider_profile.reasoning_effort_with_tools!r}",
                      session.provider_profile.reasoning_effort_with_tools != "none")
            ctx.check("nothing was persisted to the on-disk learned-rules cache either",
                      learned_reasoning_effort_with_tools(session.state_dir, "databricks",
                                                           session.model_ref.model) is None)
        finally:
            mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
