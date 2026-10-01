"""tests.test_hotfix_101_fix_thinking_budget -- 1.0.1 fixpass finding 11:
the Anthropic "high" default only applies to ADAPTIVE-capable models (never
a non-adaptive Haiku 4.5/Sonnet 4.5-or-older route, which used to get a
budget_tokens value nearly equal to max_tokens); the version regex parses
hyphenated ids (`claude-sonnet-4-6`) correctly instead of misreading them as
pre-4.6; a non-adaptive model's budget is capped at max_tokens // 2 (never
below Anthropic's own 1,024 floor).
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
    set, that falls back to the REAL ~/.rolo-claude/sessions on whatever
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
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-thinking-budget-")), model_label=model,
        session_context=session_ctx, max_turns=6, effort=effort,
    )


# ---------------------------------------------------------------------------
# _anthropic_model_supports_adaptive_thinking: hyphenated version parsing.
# ---------------------------------------------------------------------------

@test
def test_hyphenated_version_parses_as_a_decimal(ctx: Ctx):
    from rolo_claude.providers.request import _anthropic_model_supports_adaptive_thinking
    ctx.check("claude-sonnet-4-6 (hyphenated) is adaptive-capable",
              _anthropic_model_supports_adaptive_thinking("claude-sonnet-4-6"))
    ctx.check("databricks-claude-sonnet-4-6 (Databricks id shape) is adaptive-capable",
              _anthropic_model_supports_adaptive_thinking("databricks-claude-sonnet-4-6"))
    ctx.check("dotted claude-sonnet-4.6 is still recognised (unchanged)",
              _anthropic_model_supports_adaptive_thinking("claude-sonnet-4.6"))
    for v in ("4-5", "5-5", "4-8"):
        model_id = f"claude-sonnet-{v}"
        expected = float(v.replace("-", ".")) >= 4.6
        ctx.check(f"claude-sonnet-{v} -> adaptive={expected}, got "
                  f"{_anthropic_model_supports_adaptive_thinking(model_id)}",
                  _anthropic_model_supports_adaptive_thinking(model_id) == expected)


@test
def test_hyphenated_pre_4_6_sonnet_stays_non_adaptive(ctx: Ctx):
    from rolo_claude.providers.request import _anthropic_model_supports_adaptive_thinking
    ctx.check("claude-sonnet-4-5 (older, hyphenated) is NOT adaptive-capable",
              not _anthropic_model_supports_adaptive_thinking("claude-sonnet-4-5"))


@test
def test_snapshot_date_never_misread_as_a_fake_minor_version(ctx: Ctx):
    """A snapshot-dated id with no real minor version at all
    (`claude-sonnet-4-20250514`) must fall back to bare major "4" (pre-4.6,
    not adaptive) -- never misread the date's leading digits as a minor
    version."""
    from rolo_claude.providers.request import _anthropic_model_supports_adaptive_thinking
    ctx.check("claude-sonnet-4-20250514 is NOT adaptive (reads as bare major 4, not 4.20...)",
              not _anthropic_model_supports_adaptive_thinking("claude-sonnet-4-20250514"))
    ctx.check("claude-sonnet-5-20250514 (bare major 5) IS adaptive",
              _anthropic_model_supports_adaptive_thinking("claude-sonnet-5-20250514"))


# ---------------------------------------------------------------------------
# map_effort_anthropic: non-adaptive budget capped at max_tokens // 2.
# ---------------------------------------------------------------------------

@test
def test_non_adaptive_budget_capped_at_half_max_tokens(ctx: Ctx):
    from rolo_claude.providers.request import map_effort_anthropic
    result = map_effort_anthropic("high", "claude-sonnet-4-5", max_tokens=10000)
    ctx.check(f"budget is max_tokens // 2 (5000), not max_tokens - 1 (9999), got {result}",
              result == {"thinking": {"type": "enabled", "budget_tokens": 5000}})


@test
def test_non_adaptive_budget_never_below_the_1024_floor(ctx: Ctx):
    from rolo_claude.providers.request import map_effort_anthropic
    # max_tokens=1500 -> half is 750, below the 1,024 floor -- floored up,
    # still strictly less than max_tokens (1500).
    result = map_effort_anthropic("low", "claude-sonnet-4-5", max_tokens=1500)
    ctx.check(f"floored at 1024, got {result}", result == {"thinking": {"type": "enabled", "budget_tokens": 1024}})
    ctx.check("1024 < max_tokens (the real Anthropic wire constraint)", 1024 < 1500)


@test
def test_adaptive_model_budget_cap_does_not_apply(ctx: Ctx):
    """The half-max_tokens cap is only relevant to the budget_tokens shape
    -- an adaptive-capable model's {"type": "adaptive"} body has no
    budget_tokens field for it to apply to at all."""
    from rolo_claude.providers.request import map_effort_anthropic
    result = map_effort_anthropic("high", "claude-sonnet-4-6", max_tokens=10000)
    ctx.check(f"adaptive shape, no budget_tokens anywhere, got {result}",
              result == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})


# ---------------------------------------------------------------------------
# Session-level: the "high" default is scoped to adaptive-capable models.
# ---------------------------------------------------------------------------

@test
def test_non_adaptive_anthropic_family_leaves_effort_unset_by_default(ctx: Ctx):
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            session = _new_dbx_session(fh, mock, model="dbx:databricks-claude-sonnet-4-5@anthropic", effort=None)
            ctx.check(f"effort stays None (non-adaptive route keeps 'omit -> provider default'), "
                      f"got {session.effort!r}", session.effort is None)
        finally:
            mock.stop()


@test
def test_adaptive_hyphenated_anthropic_family_still_defaults_to_high(ctx: Ctx):
    """The fix narrows the default -- it must not also break the case it's
    supposed to keep working (a genuinely adaptive-capable model, named
    with the real hyphenated version shape)."""
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            session = _new_dbx_session(fh, mock, model="dbx:databricks-claude-sonnet-4-6@anthropic", effort=None)
            ctx.check(f"effort defaults to high (adaptive, hyphenated 4-6 parsed correctly), "
                      f"got {session.effort!r}", session.effort == "high")
            ctx.check(f"source labeled default, got {session.effort_source!r}", session.effort_source == "default")
        finally:
            mock.stop()


@test
def test_set_model_mid_session_respects_the_same_adaptive_gate(ctx: Ctx):
    """Switching INTO a non-adaptive Anthropic-family route mid-session
    (from a chat-dialect model with no effort set) must NOT pick up the
    "high" default either."""
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            from rolo_claude.model import parse_model_ref
            from rolo_claude.model import ModelProfile as MP

            session = _new_dbx_session(fh, mock, model="or:mock/model", effort=None)
            ctx.check("starting on a chat-dialect route, effort stays unset", session.effort is None)
            new_ref = parse_model_ref("dbx:databricks-claude-sonnet-4-5@anthropic")
            session.set_model(new_ref, MP())
            ctx.check(f"switching to a NON-adaptive Anthropic route still leaves effort unset, "
                      f"got {session.effort!r}", session.effort is None)
        finally:
            mock.stop()


@test
def test_opus_is_version_gated_like_sonnet(ctx: Ctx):
    """Opus 4.1 and 4.5 are both served on Databricks and take
    `budget_tokens`; adaptive starts at 4.6 for opus as for sonnet. Every
    opus id used to be treated as adaptive, which would have sent
    `thinking: adaptive` + `effort: high` by default and failed every turn
    on those two endpoints."""
    from rolo_claude.providers.request import _anthropic_model_supports_adaptive_thinking as adaptive
    for mid, want in (("databricks-claude-opus-4-1", False), ("databricks-claude-opus-4-5", False),
                      ("databricks-claude-opus-4-6", True), ("databricks-claude-opus-5", True),
                      ("databricks-claude-opus-5-5", True), ("claude-fable-5-1", True)):
        ctx.check(f"{mid}: adaptive={want}", adaptive(mid) is want)


@test
def test_version_before_family_bedrock_ids_parse_their_real_version(ctx: Ctx):
    """External Bedrock endpoints name the version BEFORE the family
    (`us-anthropic-claude-3-7-sonnet-20250219-v1-0`); the snapshot date
    after the family used to parse as a huge major version and classify
    Claude 3.x as adaptive."""
    from rolo_claude.providers.request import _anthropic_model_supports_adaptive_thinking as adaptive
    for mid, want in (("us-anthropic-claude-3-7-sonnet-20250219-v1-0", False),
                      ("us-anthropic-claude-3-5-sonnet-20241022-v2-0", False),
                      ("us-anthropic-claude-sonnet-4-20250514-v1-0", False),
                      ("us-anthropic-claude-sonnet-4-5-20250929-v1-0", False)):
        ctx.check(f"{mid}: adaptive={want}", adaptive(mid) is want)


@test
def test_otpm_tracker_counts_output_plus_reasoning_tokens(ctx: Ctx):
    """The Databricks OTPM budget is charged for every generated token;
    since `map_usage` reports reasoning separately, the tracker feed must
    be their sum (review re-check: a reasoning-heavy reply under-counted)."""
    from rolo_claude.agent.loop import generated_tokens_for_otpm
    ctx.check("output + reasoning", generated_tokens_for_otpm({"output_tokens": 500, "reasoning_tokens": 1500}) == 2000)
    ctx.check("output only", generated_tokens_for_otpm({"output_tokens": 500}) == 500)
    ctx.check("reasoning only", generated_tokens_for_otpm({"reasoning_tokens": 7}) == 7)
    ctx.check("neither reported -> None", generated_tokens_for_otpm({"input_tokens": 3}) is None)
    ctx.check("non-dict -> None", generated_tokens_for_otpm(None) is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
