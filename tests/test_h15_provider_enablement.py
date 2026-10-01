"""tests.test_h15_provider_enablement -- H15 item 21, rule REPLACED by the
H15 part 2 addendum: detected credentials/a real claude.ai login now
AUTO-enable a provider; the `providers` block in config.json stores
OVERRIDES only (an explicit `enabled: true`/`false` always wins). Covers
`halo_harness.providers.enablement`, the `Controller.list_models()`/
`model.parse_model_ref` gates, the now-permanently-no-op migration, and the
`halo providers`/`/providers` surfaces.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "BRIDGE_ANTHROPIC_BASE_URL",
    "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
)


class _Env:
    """Snapshots/restores every provider variable this module (or a test
    within it) touches, plus BRIDGE_TEST_HOME/BRIDGE_STATE_DIR/BRIDGE_ENV_
    FILE -- the real `~/.halo` is never written.

    H15 part 2 addendum: `claude_subscription`'s auto-detection shells out
    to the REAL `claude auth status` whenever `BRIDGE_TEST_CC_AUTH_STATUS`
    is simply absent -- merely popping it (the old behaviour here) made
    every "nothing detected" test here depend on whether THIS machine
    happens to have a real claude.ai login (true on the owner's own dev
    box). Defaults it to an explicit not-logged-in shape instead; any test
    that wants a real login still overrides it after entering, same as
    before."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="h15-provider-enablement-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# is_enabled / enable / disable -- H15 part 2 addendum: auto-detected from
# real credentials/login by default; the `providers` block stores
# OVERRIDES only.
# ---------------------------------------------------------------------------

@test
def test_is_enabled_false_with_no_block_and_no_credentials(ctx: Ctx):
    """Nothing configured at all (no block, no key, no login) -> every
    provider reads as disabled -- auto-detection found nothing to turn on."""
    from halo_harness.providers.enablement import is_enabled
    with _Env():
        for name in ("databricks", "openrouter", "anthropic", "claude_subscription", "typesafe"):
            ctx.check(f"{name} reads as disabled with nothing detected, got {is_enabled(name)!r}",
                      is_enabled(name) is False)


@test
def test_is_enabled_auto_true_from_real_credentials_with_no_block_at_all(ctx: Ctx):
    """The addendum's own headline case: "the harness already finds the
    available keys and subscription and uses those" -- no `providers`
    block, no `init`, no `enable()` call, just a real key -- auto-enabled."""
    from halo_harness.providers.enablement import is_enabled
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        ctx.check("OpenRouter auto-enabled from the key alone", is_enabled("openrouter") is True)
        ctx.check("Anthropic (never touched) stays disabled -- no key for IT",
                  is_enabled("anthropic") is False)


@test
def test_explicit_enabled_false_overrides_auto_detection(ctx: Ctx):
    """item 3 of the addendum's own worked example: `enabled: false` hides
    a provider that credentials alone would have auto-enabled."""
    from halo_harness.providers.enablement import disable, is_enabled
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        ctx.check("auto-enabled before any override", is_enabled("openrouter") is True)
        disable("openrouter")
        ctx.check("explicit enabled:false hides it despite the real key",
                  is_enabled("openrouter") is False)


@test
def test_explicit_enabled_true_overrides_missing_credentials(ctx: Ctx):
    """The mirror case: `enabled: true` forces a provider on even with no
    detected credentials at all."""
    from halo_harness.providers.enablement import enable, is_enabled
    with _Env():
        ctx.check("nothing detected -> disabled", is_enabled("openrouter") is False)
        enable("openrouter")
        ctx.check("explicit enabled:true forces it on anyway", is_enabled("openrouter") is True)


@test
def test_enable_disable_round_trip(ctx: Ctx):
    from halo_harness.providers.enablement import disable, enable, is_enabled
    with _Env():
        enable("openrouter")
        ctx.check("openrouter now enabled", is_enabled("openrouter") is True)
        # An override on ONE provider has no effect on any other -- each
        # name's own auto-detect result stands until ITS OWN override is
        # written.
        ctx.check("databricks (never touched, no credentials) still auto-reads as disabled",
                  is_enabled("databricks") is False)
        disable("openrouter")
        ctx.check("openrouter now disabled", is_enabled("openrouter") is False)


@test
def test_cc_requires_authmethod_exactly_claude_ai(ctx: Ctx):
    """The addendum's named edge case: `loggedIn: true` with any OTHER
    authMethod (an API-token/custom-base-url-driven `claude`, as on a work
    VM) must NOT auto-enable the subscription, even though it's "logged
    in" in a loose sense."""
    from halo_harness.providers.enablement import is_enabled
    with _Env():
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "not-claude-ai"})
        ctx.check("loggedIn alone, wrong authMethod -> NOT auto-enabled",
                  is_enabled("claude_subscription") is False)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ctx.check("the real claude.ai authMethod -> auto-enabled",
                  is_enabled("claude_subscription") is True)


@test
def test_canonical_aliases_resolve_to_the_same_row(ctx: Ctx):
    from halo_harness.providers.enablement import canonical, enable, is_enabled
    with _Env():
        enable("cc")  # the ModelRef.provider spelling
        ctx.check(f"'cc' canonicalizes to 'claude_subscription', got {canonical('cc')!r}",
                  canonical("cc") == "claude_subscription")
        ctx.check("is_enabled('claude_subscription') sees the SAME row 'cc' just wrote",
                  is_enabled("claude_subscription") is True)
        ctx.check("is_enabled('claude') (init_providers.py's own picker key) agrees too",
                  is_enabled("claude") is True)


# ---------------------------------------------------------------------------
# Labels (item 21.6).
# ---------------------------------------------------------------------------

@test
def test_label_strings_match_the_brief_exactly(ctx: Ctx):
    from halo_harness.providers.enablement import label_for
    ctx.check(f"cc: label, got {label_for('cc')!r}", label_for("cc") == "Claude Code subscription")
    ctx.check(f"claude_subscription label, got {label_for('claude_subscription')!r}",
              label_for("claude_subscription") == "Claude Code subscription")
    ctx.check(f"ant:/anthropic label, got {label_for('anthropic')!r}",
              label_for("anthropic") == "Anthropic API (key)")
    ctx.check(f"databricks label, got {label_for('databricks')!r}", label_for("databricks") == "Databricks")
    ctx.check(f"openrouter label, got {label_for('openrouter')!r}", label_for("openrouter") == "OpenRouter")
    ctx.check(f"typesafe label, got {label_for('typesafe')!r}", label_for("typesafe") == "TypeSafe")


@test
def test_prefix_table(ctx: Ctx):
    from halo_harness.providers.enablement import PREFIXES
    ctx.check(f"dbx:, got {PREFIXES}", PREFIXES["databricks"] == "dbx:")
    ctx.check(f"or:, got {PREFIXES}", PREFIXES["openrouter"] == "or:")
    ctx.check(f"ant:, got {PREFIXES}", PREFIXES["anthropic"] == "ant:")
    ctx.check(f"cc:, got {PREFIXES}", PREFIXES["claude_subscription"] == "cc:")
    ctx.check(f"typesafe has no prefix yet, got {PREFIXES}", PREFIXES["typesafe"] is None)


# ---------------------------------------------------------------------------
# Migration: H15 part 2 addendum makes this a PERMANENT no-op -- auto-
# detection already computes live what a one-time migration used to write
# once.
# ---------------------------------------------------------------------------

@test
def test_migration_always_writes_nothing_and_returns_none(ctx: Ctx):
    from halo_harness.providers.enablement import ensure_providers_migrated, is_enabled, providers_block_exists
    with _Env():
        os.environ["DATABRICKS_HOST"] = "https://fake-ws.cloud.databricks.com"
        os.environ["DATABRICKS_TOKEN"] = "fake-token"
        note = ensure_providers_migrated()
        ctx.check(f"always returns None, got {note!r}", note is None)
        ctx.check("still no providers block on disk -- migration writes nothing",
                  providers_block_exists() is False)
        ctx.check("databricks is enabled anyway, via live auto-detection, not migration",
                  is_enabled("databricks") is True)


@test
def test_migration_is_a_noop_even_once_a_providers_block_already_exists(ctx: Ctx):
    from halo_harness.providers.enablement import ensure_providers_migrated, enable, is_enabled
    with _Env():
        enable("openrouter")
        note = ensure_providers_migrated()
        ctx.check(f"no-op (None), got {note!r}", note is None)
        ctx.check("openrouter is still enabled (migration never touched it)", is_enabled("openrouter") is True)


# ---------------------------------------------------------------------------
# A hand-typed ref for a disabled provider is refused (item 21.3).
# ---------------------------------------------------------------------------

@test
def test_hand_typed_ref_for_a_disabled_provider_is_refused(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.enablement import disable, enable
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        enable("databricks")  # anything, just to create the block
        disable("openrouter")
        raised = False
        try:
            parse_model_ref("or:deepseek/deepseek-v3.2")
        except InvalidModelError as e:
            raised = True
            msg = str(e)
            ctx.check(f"names the provider, got {msg!r}", "OpenRouter" in msg)
            ctx.check(f"names the fix command, got {msg!r}", "halo providers enable" in msg)
        ctx.check("InvalidModelError was actually raised", raised)


@test
def test_doctor_default_model_check_gives_the_specific_reason_for_an_explicit_disable(ctx: Ctx):
    """1.0.1 part 2 fixpass criticals #2/#3: the parse-time gate is now
    OVERRIDE-ONLY -- it refuses a ref ONLY when the user explicitly
    disabled that provider (`enabled: false`), never merely because
    nothing is configured/detected yet (see the companion "not configured"
    test just below, which covers that case instead -- it used to hit this
    exact "is not enabled" branch too, which no longer reflects what's
    actually wrong). `doctor._check_default_model`'s own `InvalidModelError`
    branch still recovers the SAME specific "init --provider <name>"
    guidance for THIS (explicit-disable) case."""
    from halo_harness import doctor
    from halo_harness.providers.enablement import disable
    from halo_harness.theme import set_config_value
    with _Env():
        os.environ["DATABRICKS_HOST"] = "https://fake-ws.cloud.databricks.com"
        os.environ["DATABRICKS_TOKEN"] = "fake-token"
        disable("databricks")
        set_config_value("model", "dbx:databricks-deepseek-v4-1-flash")
        line = doctor._check_default_model()
        ctx.check(f"WARN, got {line!r}", line.startswith(doctor.WARN))
        ctx.check(f"names the specific databricks-not-enabled reason, not a generic parse failure, got {line!r}",
                  "Databricks is not enabled" in line)
        ctx.check(f"still suggests the real fix command, got {line!r}",
                  "halo init --provider databricks" in line)
        ctx.check(f"never the generic 'does not resolve' wording, got {line!r}", "does not resolve" not in line)


@test
def test_doctor_default_model_check_reports_not_configured_when_nothing_detected_or_overridden(ctx: Ctx):
    """The companion case: no credentials AND no override at all -- the
    gate no longer refuses this at parse time (override-only, never reads
    credentials), so the ref parses fine and `_provider_configured` (the
    SAME real credential check `_check_databricks` itself uses) is what
    correctly reports "not configured" instead of the gate pre-empting it
    with "not enabled" (a different problem with a different fix)."""
    from halo_harness import doctor
    from halo_harness.theme import set_config_value
    with _Env():
        set_config_value("model", "dbx:databricks-deepseek-v4-1-flash")
        line = doctor._check_default_model()
        ctx.check(f"WARN, got {line!r}", line.startswith(doctor.WARN))
        ctx.check(f"names 'not configured', got {line!r}", "not configured" in line)
        ctx.check(f"still suggests the real fix command, got {line!r}",
                  "halo init --provider databricks" in line)


@test
def test_hand_typed_ref_refusal_uses_the_same_message_as_the_model_picker_hint(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.enablement import disable, enable, is_provider_disabled_message
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        enable("databricks")
        disable("anthropic")
        expected = is_provider_disabled_message("anthropic")
        try:
            parse_model_ref("ant:sonnet")
            ctx.check("should have raised", False)
        except InvalidModelError as e:
            ctx.check(f"refusal matches the shared message verbatim, got {e!r} vs {expected!r}", str(e) == expected)


@test
def test_cc_hand_typed_ref_with_no_override_and_no_login_resolves_at_parse_time(ctx: Ctx):
    """1.0.1 part 2 fixpass criticals #2/#3: the parse-time gate is
    OVERRIDE-ONLY now -- it never reads credentials or spawns `claude auth
    status` itself, so a `cc:` ref with no explicit enablement override
    parses fine EVEN WHEN NOTHING IS LOGGED IN (this test's old name
    described the opposite, now-removed behavior: a `_preflight_cc()`-
    sourced specific message surfaced at parse time). The precise "not
    logged in"/"claude not installed" detail is the TURN-time preflight's
    own job now (`agent.cc_runtime._preflight_cc`, exercised end to end in
    test_cc_session.py), never surfaced this early any more -- with no
    loss of detail, just at the right time instead of speculatively."""
    from halo_harness.model import parse_model_ref
    with _Env():
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        ref = parse_model_ref("cc:opus")
        ctx.check(f"resolves fine -- turn-time preflight handles 'not logged in', got {ref}",
                  ref.provider == "cc")


@test
def test_is_provider_disabled_message_never_spawns_or_reads_credentials(ctx: Ctx):
    """The critical fix itself, pinned directly: with NO explicit override
    at all, `is_provider_disabled_message` returns None for EVERY provider
    regardless of credentials/login state -- it must never even LOOK at
    `claude_login_available()`/`credentials_present()` to decide this.
    Poisons `claude_login_available` to prove it is never called."""
    from halo_harness.providers.enablement import is_provider_disabled_message
    import halo_harness.init_providers as init_providers_mod
    real_claude_login = init_providers_mod.claude_login_available

    def _poison():
        raise AssertionError("is_provider_disabled_message must never spawn claude auth status")

    init_providers_mod.claude_login_available = _poison
    try:
        with _Env():
            for name in ("databricks", "openrouter", "anthropic", "claude_subscription", "typesafe"):
                msg = is_provider_disabled_message(name)
                ctx.check(f"{name}: None with no override, got {msg!r}", msg is None)
    finally:
        init_providers_mod.claude_login_available = real_claude_login


@test
def test_cc_explicit_disable_still_uses_the_generic_message_not_the_preflight_one(ctx: Ctx):
    """The mirror case: once the user has EXPLICITLY disabled claude_
    subscription, the message goes back to the generic "run providers
    enable" wording (there's nothing wrong with claude itself to explain)."""
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.enablement import disable
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        disable("claude_subscription")
        try:
            parse_model_ref("cc:opus")
            ctx.check("should have raised", False)
        except InvalidModelError as e:
            ctx.check(f"generic wording, got {e}", "halo providers enable claude_subscription" in str(e))


@test
def test_enabled_provider_resolves_normally(ctx: Ctx):
    """The gate must never block an ENABLED provider -- a plain regression
    guard alongside the refusal tests above."""
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.enablement import enable
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        enable("openrouter")
        ref = parse_model_ref("or:deepseek/deepseek-v3.2")
        ctx.check(f"resolves fine, got {ref}", ref.provider == "openrouter" and ref.model == "deepseek/deepseek-v3.2")


@test
def test_no_providers_block_at_all_never_blocks_resolution(ctx: Ctx):
    """H15 part 2 addendum's own headline case, at the `parse_model_ref`
    level: a box that never ran `init`/`providers enable` at all, just a
    real key -- a hand-typed `or:` ref resolves normally, auto-enabled."""
    from halo_harness.model import parse_model_ref
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        ref = parse_model_ref("or:deepseek/deepseek-v3.2")
        ctx.check(f"resolves fine with no providers block at all, got {ref}", ref.provider == "openrouter")


# ---------------------------------------------------------------------------
# Controller.list_models(): enabled-only + dim hint for detected-but-
# disabled (item 21.2).
# ---------------------------------------------------------------------------

class _FakeModelRef:
    # Deliberately NOT the same ref `_seed_openrouter_catalog` writes --
    # `list_models()` always inserts the session's OWN current model (it
    # must resolve regardless of catalog listing, since the session is
    # already running on it); using a different provider's ref here keeps
    # that fallback from masking what these tests actually check.
    raw = "dbx:databricks-deepseek-v4-1-flash"
    provider = "databricks"


class _FakeModelProfile:
    context_tokens = 128000
    max_output_tokens = 8192


class _FakeSession:
    model_ref = _FakeModelRef()
    model_profile = _FakeModelProfile()


def _seed_openrouter_catalog(state_dir: Path) -> None:
    from halo_harness.providers.databricks import write_models_json
    write_models_json(state_dir, [{"id": "vendor/model-x", "context_length": 128000, "max_output_tokens": 8192,
                                    "pricing": {"prompt": "0.0000008", "completion": "0.0000024"}}])


@test
def test_list_models_hides_openrouter_catalog_when_disabled(ctx: Ctx):
    from halo_harness.controller import Controller
    from halo_harness.providers.enablement import disable, enable
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        _seed_openrouter_catalog(env.state_dir)
        enable("databricks")
        disable("openrouter")
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        refs = [m.get("ref") for m in ctrl.list_models() if "hint" not in m]
        ctx.check(f"no or: rows when disabled, got {refs}", not any(r and r.startswith("or:") for r in refs))


@test
def test_list_models_shows_a_dim_hint_for_detected_but_disabled_openrouter(ctx: Ctx):
    from halo_harness.controller import Controller
    from halo_harness.providers.enablement import disable, enable
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        _seed_openrouter_catalog(env.state_dir)
        enable("databricks")
        disable("openrouter")
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        hints = [m["hint"] for m in ctrl.list_models() if "hint" in m]
        ctx.check(f"exactly one OpenRouter hint, got {hints}", len(hints) == 1)
        ctx.check(f"names OpenRouter and the fix, got {hints}",
                  "OpenRouter" in hints[0] and "halo providers enable openrouter" in hints[0])


@test
def test_list_models_no_hint_when_openrouter_has_no_credentials_at_all(ctx: Ctx):
    """A hint only ever fires for DETECTED-but-disabled -- never for a
    provider that's simply never been set up."""
    from halo_harness.controller import Controller
    with _Env() as env:
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        hints = [m["hint"] for m in ctrl.list_models() if "hint" in m]
        ctx.check(f"no hints at all, got {hints}", hints == [])


@test
def test_cc_group_hidden_with_a_claude_ai_login_present_but_explicitly_disabled(ctx: Ctx):
    """`cc:` models must NOT appear once the user has explicitly disabled
    the subscription, even with a real claude.ai login detected."""
    from halo_harness.controller import Controller
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    from halo_harness.providers.enablement import disable, enable
    with _Env() as env:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        refresh_cached_claude_auth_status()
        enable("databricks")
        disable("claude_subscription")
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        rows = ctrl.list_models()
        cc_refs = [m.get("ref") for m in rows if isinstance(m.get("ref"), str) and m["ref"].startswith("cc:")]
        ctx.check(f"no cc: rows at all, got {cc_refs}", cc_refs == [])
        hints = [m["hint"] for m in rows if "hint" in m]
        ctx.check(f"a hint names the Claude Code subscription instead, got {hints}",
                  any("Claude Code subscription" in h for h in hints))


@test
def test_cc_group_hidden_when_logged_in_via_a_non_claude_ai_authmethod(ctx: Ctx):
    """H15 part 2 addendum's own named case: `loggedIn: true` with an
    authMethod OTHER than `claude.ai` (an API-token/custom-base-url-driven
    `claude`, as on a work VM) must never auto-enable the group, with NO
    explicit disable() call needed -- pure auto-detection narrowness."""
    from halo_harness.controller import Controller
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    with _Env() as env:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "not-claude-ai"})
        refresh_cached_claude_auth_status()
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        cc_refs = [m.get("ref") for m in ctrl.list_models()
                   if isinstance(m.get("ref"), str) and m["ref"].startswith("cc:")]
        ctx.check(f"no cc: rows -- loggedIn alone is not enough, got {cc_refs}", cc_refs == [])


@test
def test_cc_group_shown_once_enabled(ctx: Ctx):
    from halo_harness.controller import Controller
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    from halo_harness.providers.enablement import enable
    with _Env() as env:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        refresh_cached_claude_auth_status()
        enable("claude_subscription")
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        cc_refs = [m.get("ref") for m in ctrl.list_models()
                   if isinstance(m.get("ref"), str) and m["ref"].startswith("cc:")]
        ctx.check(f"cc: rows now present, got {len(cc_refs)}", len(cc_refs) > 0)


@test
def test_ant_group_follows_the_same_rule(ctx: Ctx):
    from halo_harness.controller import Controller
    from halo_harness.providers.enablement import disable, enable
    with _Env() as env:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        enable("databricks")
        disable("anthropic")
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=env.state_dir, routes={})
        rows = ctrl.list_models()
        ant_refs = [m.get("ref") for m in rows if isinstance(m.get("ref"), str) and m["ref"].startswith("ant:")]
        ctx.check(f"no ant: rows while disabled, got {ant_refs}", ant_refs == [])
        enable("anthropic")
        rows2 = ctrl.list_models()
        ant_refs2 = [m.get("ref") for m in rows2 if isinstance(m.get("ref"), str) and m["ref"].startswith("ant:")]
        ctx.check(f"ant: rows appear once enabled, got {len(ant_refs2)}", len(ant_refs2) > 0)


# ---------------------------------------------------------------------------
# ModelPicker: a hint entry renders as a dim, non-selectable line, never a
# selectable model.
# ---------------------------------------------------------------------------

@test
def test_model_picker_splits_hints_out_of_the_selectable_list(ctx: Ctx):
    from halo_harness.tui.dialogs.model_picker import ModelPicker
    models = [{"ref": "or:a", "provider": "openrouter"}, {"hint": "Databricks detected but not enabled"}]
    picker = ModelPicker(models, current="or:a")
    ctx.check(f"hint split out, got {picker.hints}", picker.hints == ["Databricks detected but not enabled"])
    ctx.check(f"only the real model stays selectable, got {picker.models}",
              picker.models == [{"ref": "or:a", "provider": "openrouter"}])


# ---------------------------------------------------------------------------
# configured_providers() respects enablement (feeds init's cross-provider
# default-model pick).
# ---------------------------------------------------------------------------

@test
def test_configured_providers_excludes_a_disabled_one(ctx: Ctx):
    from halo_harness.init_providers import configured_providers
    from halo_harness.providers.enablement import disable, enable
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        enable("openrouter")
        disable("anthropic")
        got = configured_providers()
        ctx.check(f"openrouter included, anthropic excluded, got {got}",
                  "openrouter" in got and "anthropic" not in got)


@test
def test_configured_providers_unaffected_with_no_providers_block(ctx: Ctx):
    from halo_harness.init_providers import configured_providers
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        got = configured_providers()
        ctx.check(f"both auto-enabled from their real keys alone, got {got}",
                  "openrouter" in got and "anthropic" in got)


# ---------------------------------------------------------------------------
# `halo providers` CLI: table + enable/disable.
# ---------------------------------------------------------------------------

@test
def test_cmd_providers_table_lists_all_five(ctx: Ctx):
    import io
    from contextlib import redirect_stdout
    from halo_harness.providers_cli import cmd_providers
    with _Env():
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_providers([])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        out = buf.getvalue()
        for label in ("Databricks", "OpenRouter", "Anthropic API (key)", "Claude Code subscription", "TypeSafe"):
            ctx.check(f"table mentions {label!r}, got {out!r}", label in out)


@test
def test_cmd_providers_shows_openrouter_auto_detected_with_a_key_and_no_init(ctx: Ctx):
    """H15 part 2 addendum's own named test: OpenRouter shown as enabled
    (status "auto (detected from ...)") from a real key alone -- no
    `halo init`, no `providers enable` call, ever."""
    import io
    from contextlib import redirect_stdout
    from halo_harness.providers_cli import cmd_providers, provider_rows
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        rows = {r["name"]: r for r in provider_rows()}
        ctx.check(f"openrouter row is enabled, got {rows['openrouter']}", rows["openrouter"]["enabled"] is True)
        ctx.check(f"status says auto-detected, got {rows['openrouter']['status']!r}",
                  rows["openrouter"]["status"].startswith("auto (detected from"))
        with redirect_stdout(io.StringIO()) as buf:
            cmd_providers([])
        out = buf.getvalue()
        or_line = next(line for line in out.splitlines() if line.startswith("OpenRouter"))
        ctx.check(f"the table's own OpenRouter row says auto-detected, got {or_line!r}",
                  "auto (detected from" in or_line)


@test
def test_cmd_providers_enable_disable(ctx: Ctx):
    import io
    from contextlib import redirect_stdout
    from halo_harness.providers.enablement import is_enabled
    from halo_harness.providers_cli import cmd_providers
    with _Env():
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_providers(["enable", "databricks"])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("databricks now enabled", is_enabled("databricks") is True)
        with redirect_stdout(io.StringIO()):
            cmd_providers(["disable", "databricks"])
        ctx.check("databricks now disabled", is_enabled("databricks") is False)


@test
def test_cmd_providers_enable_accepts_cc_alias(ctx: Ctx):
    import io
    from contextlib import redirect_stdout
    from halo_harness.providers.enablement import is_enabled
    from halo_harness.providers_cli import cmd_providers
    with _Env():
        with redirect_stdout(io.StringIO()):
            rc = cmd_providers(["enable", "cc"])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("claude_subscription enabled via the 'cc' alias", is_enabled("claude_subscription") is True)


@test
def test_cmd_providers_unknown_name_is_exit_2(ctx: Ctx):
    import io
    from contextlib import redirect_stderr
    from halo_harness.providers_cli import cmd_providers
    with _Env():
        with redirect_stderr(io.StringIO()):
            rc = cmd_providers(["enable", "not-a-real-provider"])
        ctx.check(f"exit 2, got {rc}", rc == 2)


# ---------------------------------------------------------------------------
# /providers (headless facade / slash command builtin).
# ---------------------------------------------------------------------------

@test
def test_slash_providers_builtin_registered_and_lists_table(ctx: Ctx):
    from halo_harness.commands.builtins import _BUILTIN_SPECS
    with _Env():
        ctx.check("'providers' is a registered builtin", "providers" in _BUILTIN_SPECS)
        _kind, _desc, _hint, run = _BUILTIN_SPECS["providers"]
        from halo_harness.commands.builtins import HeadlessFacade
        result = run("", HeadlessFacade(cwd=Path.cwd()))
        for label in ("Databricks", "OpenRouter", "TypeSafe"):
            ctx.check(f"/providers table mentions {label!r}, got {result!r}", label in result)


@test
def test_enable_if_was_explicitly_disabled_is_a_noop_with_no_override(ctx: Ctx):
    """1.0.1 part 2 fixpass finding 15: `init`/the init tabs' own "a
    successful setup enables the provider" step must not write a permanent
    override on an ordinary fresh setup (no prior override at all) --
    auto-detection already covers it live, every time, and a written
    override would survive a LATER revocation (a claude.ai logout, a
    deleted key) that auto-detection alone would otherwise have reflected
    immediately."""
    from halo_harness.providers.enablement import enable_if_was_explicitly_disabled, providers_block_exists
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        enable_if_was_explicitly_disabled("openrouter")
        ctx.check("still no providers block at all -- nothing was written",
                  providers_block_exists() is False)


@test
def test_enable_if_was_explicitly_disabled_flips_an_existing_false_override(ctx: Ctx):
    from halo_harness.providers.enablement import disable, enable_if_was_explicitly_disabled, is_enabled
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        disable("openrouter")
        ctx.check("disabled via the explicit override", is_enabled("openrouter") is False)
        enable_if_was_explicitly_disabled("openrouter")
        ctx.check("flipped back to enabled", is_enabled("openrouter") is True)


@test
def test_enable_if_was_explicitly_disabled_leaves_an_already_true_override_alone(ctx: Ctx):
    """Regression guard: a provider the user already explicitly enabled
    stays untouched (no redundant write, no change in behavior either way)."""
    from halo_harness.providers.enablement import enable, enable_if_was_explicitly_disabled, is_enabled
    with _Env():
        enable("databricks")
        enable_if_was_explicitly_disabled("databricks")
        ctx.check("still enabled", is_enabled("databricks") is True)


@test
def test_slash_providers_enable_disable(ctx: Ctx):
    from halo_harness.commands.builtins import _BUILTIN_SPECS, HeadlessFacade
    from halo_harness.providers.enablement import is_enabled
    with _Env():
        _kind, _desc, _hint, run = _BUILTIN_SPECS["providers"]
        facade = HeadlessFacade(cwd=Path.cwd())
        result = run("enable openrouter", facade)
        ctx.check(f"confirms enabling, got {result!r}", "enabled" in result.lower())
        ctx.check("openrouter actually enabled", is_enabled("openrouter") is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
