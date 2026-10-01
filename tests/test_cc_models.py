"""tests.test_cc_models -- H11 Part A/C: cc:/ant: alias resolution (with
routes.json alias hops, [1m] pass-through, bare-word routing to cc:/ant:/
an error naming both), `claude auth status` JSON parsing (never the
credentials file), and model_table profile fields for the six subscription
models plus Claude Code's own sonnet/haiku aliases.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

# H11b finding 27: this module's own scratch home -- `profile_fields_for_
# cc_model`/`refresh_cc_catalog` read/write `<bridge_home()>/cc-models.
# json`, which defaulted to the REAL ~/.rolo-claude/cc-models.json here
# (verified: this module read the real file). See test_cc_session.py's
# own comment on this same line for why it's set once, at import time.
os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="cc-models-scratchhome-")

test, TESTS = new_registry()


def _clear_cc_env():
    for k in ("BRIDGE_TEST_CC_AUTH_STATUS", "ANTHROPIC_API_KEY", "BRIDGE_CLAUDE_EXE"):
        os.environ.pop(k, None)


# ---- cc:/ant: alias tables -------------------------------------------------

@test
def test_cc_prefix_resolves_all_named_aliases(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        # H15 part 2 addendum: `cc:` auto-enables only with a REAL
        # claude.ai login detected -- without this, `_refuse_if_disabled`
        # would refuse every ref below on a box (or CI image) with no
        # real `claude` login at all.
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        expected = {
            "cc:fable": "claude-fable-5-1", "cc:opus": "claude-opus-5-5",
            "cc:opus-5": "claude-opus-5", "cc:opus-5.0": "claude-opus-5",
            "cc:opus-4.8": "claude-opus-4-8", "cc:opus-4.6": "claude-opus-4-6",
            "cc:sonnet": "sonnet", "cc:sonnet-5": "claude-sonnet-5", "cc:haiku": "haiku",
        }
        for raw, model in expected.items():
            ref = parse_model_ref(raw)
            ctx.check(f"{raw} -> provider cc", ref.provider == "cc")
            ctx.check(f"{raw} -> model {model!r}, got {ref.model!r}", ref.model == model)
            ctx.check(f"{raw} -> dialect cc-subprocess", ref.dialect == "cc-subprocess")
    finally:
        _clear_cc_env()


@test
def test_cc_prefix_full_id_passes_through_unchanged(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ref = parse_model_ref("cc:claude-opus-4")
        ctx.check("unknown name passes through", ref.model == "claude-opus-4")
    finally:
        _clear_cc_env()


@test
def test_cc_prefix_1m_suffix_preserved(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ref = parse_model_ref("cc:sonnet-5[1m]")
        ctx.check(f"alias resolved with [1m] kept, got {ref.model!r}", ref.model == "claude-sonnet-5[1m]")
        ref2 = parse_model_ref("cc:claude-opus-4[1m]")
        ctx.check("full id + [1m] passes through", ref2.model == "claude-opus-4[1m]")
    finally:
        _clear_cc_env()


@test
def test_ant_prefix_resolves_the_same_alias_names(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        # H15 part 2 addendum: `ant:` auto-enables only with a real
        # ANTHROPIC_API_KEY detected.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        expected = {
            "ant:fable": "claude-fable-5-1", "ant:opus": "claude-opus-5-5",
            "ant:opus-5": "claude-opus-5", "ant:opus-4.8": "claude-opus-4-8",
            "ant:opus-4.6": "claude-opus-4-6", "ant:sonnet-5": "claude-sonnet-5",
        }
        for raw, model in expected.items():
            ref = parse_model_ref(raw)
            ctx.check(f"{raw} -> provider anthropic", ref.provider == "anthropic")
            ctx.check(f"{raw} -> model {model!r}, got {ref.model!r}", ref.model == model)
            ctx.check(f"{raw} -> dialect anthropic-passthrough", ref.dialect == "anthropic-passthrough")
    finally:
        _clear_cc_env()


@test
def test_ant_prefix_sonnet_and_haiku_resolve_to_concrete_ids_not_bare_aliases(ctx: Ctx):
    """Unlike cc: (where Claude Code resolves its OWN sonnet/haiku
    aliases), ant: hits the real API directly and needs a concrete id."""
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        ref_sonnet = parse_model_ref("ant:sonnet")
        ref_haiku = parse_model_ref("ant:haiku")
        ctx.check(f"ant:sonnet is a real id, got {ref_sonnet.model!r}", ref_sonnet.model not in ("sonnet", ""))
        ctx.check(f"ant:haiku is a real id, got {ref_haiku.model!r}", ref_haiku.model not in ("haiku", ""))
    finally:
        _clear_cc_env()


@test
def test_ant_prefix_existing_behavior_unaffected_for_unknown_names(ctx: Ctx):
    """Regression guard: test_model.py's own test_parse_ant_prefix case."""
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        ref = parse_model_ref("ant:claude-opus-4")
        ctx.check("provider anthropic", ref.provider == "anthropic")
        ctx.check("model bare (ant: stripped, no alias applied)", ref.model == "claude-opus-4")
    finally:
        _clear_cc_env()


@test
def test_routes_json_alias_hop_into_cc_prefix(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        routes = {"aliases": {"my-opus": "cc:opus"}}
        ref = parse_model_ref("my-opus", routes)
        ctx.check("provider cc via routes.json alias hop", ref.provider == "cc")
        ctx.check(f"model resolved, got {ref.model!r}", ref.model == "claude-opus-5-5")
    finally:
        _clear_cc_env()


# ---- bare alias routing (cc: vs ant: vs error) -----------------------------

@test
def test_bare_alias_routes_to_cc_when_logged_in_and_no_key(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ref = parse_model_ref("opus")
        ctx.check(f"bare opus -> cc, got provider={ref.provider!r}", ref.provider == "cc")
        ctx.check("bare opus -> claude-opus-5-5", ref.model == "claude-opus-5-5")
    finally:
        _clear_cc_env()


@test
def test_bare_alias_routes_to_ant_when_key_set(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True})
        ref = parse_model_ref("opus")
        ctx.check(f"bare opus -> anthropic when key set, got provider={ref.provider!r}", ref.provider == "anthropic")
    finally:
        _clear_cc_env()


@test
def test_bare_alias_key_wins_over_login(ctx: Ctx):
    """Brief: 'ant: when the key is set' -- a deliberate key is never
    silently overridden by an incidental subscription login."""
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True})
        ref = parse_model_ref("sonnet-5")
        ctx.check("key wins over login", ref.provider == "anthropic")
    finally:
        _clear_cc_env()


@test
def test_bare_alias_errors_naming_both_options_when_neither_available(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    from rolo_claude.providers.routing import InvalidModelError
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        try:
            parse_model_ref("opus")
            ctx.check("must raise when neither cc: nor ant: is available", False)
        except InvalidModelError as e:
            msg = str(e)
            ctx.check(f"error names cc:, got {msg!r}", "cc:opus" in msg)
            ctx.check(f"error names ant:, got {msg!r}", "ant:opus" in msg)
    finally:
        _clear_cc_env()


@test
def test_bare_non_alias_word_still_raises_invalid_model_error(ctx: Ctx):
    """Regression guard: test_model.py's own test_parse_invalid_raises case."""
    from rolo_claude.model import parse_model_ref
    from rolo_claude.providers.routing import InvalidModelError
    _clear_cc_env()
    try:
        parse_model_ref("totally-unrecognized-form")
        ctx.check("must raise", False)
    except InvalidModelError:
        ctx.check("InvalidModelError raised", True)


# ---- claude auth status / claude binary resolution -------------------------

@test
def test_claude_auth_status_parses_real_shaped_json(ctx: Ctx):
    from rolo_claude.providers.cc_models import claude_auth_status
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "email": "owner@example.com",
            "subscriptionType": "max",
        })
        status = claude_auth_status()
        ctx.check("logged_in True", status.logged_in is True)
        ctx.check("auth_method claude.ai", status.auth_method == "claude.ai")
        ctx.check("subscription_type max", status.subscription_type == "max")
    finally:
        _clear_cc_env()


@test
def test_claude_auth_status_not_logged_in(ctx: Ctx):
    from rolo_claude.providers.cc_models import claude_auth_status
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        status = claude_auth_status()
        ctx.check("logged_in False", status is not None and status.logged_in is False)
    finally:
        _clear_cc_env()


@test
def test_claude_auth_status_unparseable_output_is_not_logged_in(ctx: Ctx):
    from rolo_claude.providers.cc_models import claude_auth_status
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = "not json at all"
        status = claude_auth_status()
        ctx.check("unparseable -> logged_in False, not None", status is not None and status.logged_in is False)
    finally:
        _clear_cc_env()


@test
def test_claude_not_found_returns_none(ctx: Ctx):
    """A BRIDGE_CLAUDE_EXE whose own executable does not exist at all (not
    "an interpreter that exists but can't find ITS script", which is
    indistinguishable from "installed but broken" and correctly reported
    as logged_in=False, not None -- see the not-logged-in-shaped tests
    above) -- subprocess.run itself raises FileNotFoundError, which
    claude_auth_status treats as "claude not found"."""
    from rolo_claude.providers.cc_models import claude_auth_status
    _clear_cc_env()
    missing = Path(tempfile.gettempdir()) / "definitely-not-a-real-claude-binary-xyz.exe"
    try:
        os.environ["BRIDGE_CLAUDE_EXE"] = '"' + str(missing) + '"'
        status = claude_auth_status()
        ctx.check(f"claude not found -> None, got {status!r}", status is None)
    finally:
        _clear_cc_env()


@test
def test_resolve_claude_launch_argv_honors_bridge_claude_exe_two_tokens(ctx: Ctx):
    """Windows can't exec a bare .py path -- BRIDGE_CLAUDE_EXE must accept
    a quoted "<interpreter> <script>" two-token form (Part C's fake claude
    is exactly this shape)."""
    from rolo_claude.providers.cc_models import resolve_claude_launch_argv
    _clear_cc_env()
    try:
        fake = str(Path(__file__).resolve().parent / "helpers" / "fake_claude_cc.py")
        os.environ["BRIDGE_CLAUDE_EXE"] = '"' + sys.executable + '" "' + fake + '"'
        argv = resolve_claude_launch_argv()
        ctx.check(f"two tokens, got {argv!r}", len(argv) == 2)
        ctx.check("first token is the interpreter", argv[0] == sys.executable)
        ctx.check("second token is the fake script (no stray quotes)", argv[1] == fake)
    finally:
        _clear_cc_env()


@test
def test_credentials_file_is_never_opened(ctx: Ctx):
    """Binding constraint: rolo-claude never reads
    ~/.claude/.credentials.json -- a sentinel file under BRIDGE_TEST_HOME
    is never touched by claude_auth_status()/bare-alias resolution, even
    though both run real subprocess/env-var logic that COULD have reached
    for it by mistake."""
    import builtins
    from rolo_claude.providers.cc_models import claude_auth_status, default_bare_alias_route

    home_dir = Path(tempfile.mkdtemp(prefix="cc-cred-home-"))
    creds_path = home_dir / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps({"sentinel": "must-never-be-read"}), encoding="utf-8")

    _clear_cc_env()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home_dir)
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True})

    real_open = builtins.open
    opened_creds = []

    def _guarded_open(file, *a, **kw):
        try:
            if str(file) == str(creds_path):
                opened_creds.append(str(file))
        except Exception:
            pass
        return real_open(file, *a, **kw)

    builtins.open = _guarded_open
    try:
        claude_auth_status()
        default_bare_alias_route()
    finally:
        builtins.open = real_open
        _clear_cc_env()
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)

    ctx.check(f"credentials file never opened, got opens={opened_creds}", opened_creds == [])
    ctx.check("sentinel content unchanged", json.loads(creds_path.read_text())["sentinel"] == "must-never-be-read")


# ---- profile fields ---------------------------------------------------------

@test
def test_profile_fields_known_for_all_six_models_plus_cc_aliases(ctx: Ctx):
    from rolo_claude.providers.cc_models import profile_fields_for_cc_model
    for model_id in ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                       "claude-opus-4-6", "claude-sonnet-5", "sonnet", "haiku"):
        fields = profile_fields_for_cc_model(model_id)
        ctx.check(f"{model_id} has fields", fields is not None)
        ctx.check(f"{model_id} has context_tokens", isinstance(fields.get("context_tokens"), int))
        ctx.check(f"{model_id} has max_output_tokens", isinstance(fields.get("max_output_tokens"), int))


@test
def test_profile_fields_unknown_model_returns_none(ctx: Ctx):
    from rolo_claude.providers.cc_models import profile_fields_for_cc_model
    ctx.check("unknown id -> None", profile_fields_for_cc_model("claude-totally-made-up") is None)


@test
def test_resolve_model_profile_cc_uses_the_table(ctx: Ctx):
    from rolo_claude.model import parse_model_ref, resolve_model_profile
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ref = parse_model_ref("cc:opus")
        profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="cc-profile-")), {})
        ctx.check(f"context_tokens 1M, got {profile.context_tokens}", profile.context_tokens == 1_000_000)
        ctx.check("reasoning native", profile.reasoning == "native")
        ctx.check("has real pricing, not None", profile.price_in is not None and profile.price_out is not None)
    finally:
        _clear_cc_env()


#  ---- H15 addendum 2: explicit opus-5.5/sonnet-5.5 aliases ------------------

@test
def test_opus_5_5_and_sonnet_5_5_aliases_resolve_the_same_as_the_bare_latest_pointer(ctx: Ctx):
    """rolo (personal Mac): doesn't see Opus 5.5 in the cc: list because
    nothing is labelled "5.5" -- `opus` resolves to it, but a reader can't
    tell that from the alias name alone. These two new explicit names
    resolve to the identical id the bare "latest" pointer already does."""
    from rolo_claude.providers.cc_models import ANT_ALIASES, CC_ALIASES
    ctx.check(f"CC_ALIASES['opus-5.5'], got {CC_ALIASES.get('opus-5.5')!r}",
              CC_ALIASES.get("opus-5.5") == "claude-opus-5-5")
    ctx.check(f"CC_ALIASES['opus-5.5'] == CC_ALIASES['opus'], got {CC_ALIASES.get('opus')!r}",
              CC_ALIASES.get("opus-5.5") == CC_ALIASES.get("opus"))
    ctx.check(f"ANT_ALIASES['opus-5.5'], got {ANT_ALIASES.get('opus-5.5')!r}",
              ANT_ALIASES.get("opus-5.5") == "claude-opus-5-5")
    ctx.check(f"CC_ALIASES['sonnet-5.5'], got {CC_ALIASES.get('sonnet-5.5')!r}",
              CC_ALIASES.get("sonnet-5.5") == "claude-sonnet-5-5")
    ctx.check(f"ANT_ALIASES['sonnet-5.5'], got {ANT_ALIASES.get('sonnet-5.5')!r}",
              ANT_ALIASES.get("sonnet-5.5") == "claude-sonnet-5-5")
    ctx.check(f"ANT_ALIASES['sonnet-5.5'] == ANT_ALIASES['sonnet'], got {ANT_ALIASES.get('sonnet')!r}",
              ANT_ALIASES.get("sonnet-5.5") == ANT_ALIASES.get("sonnet"))


@test
def test_cc_and_ant_prefix_resolve_the_new_dotted_five_five_aliases(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    _clear_cc_env()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        ref = parse_model_ref("cc:opus-5.5")
        ctx.check(f"cc:opus-5.5 -> claude-opus-5-5, got {ref.model!r}", ref.model == "claude-opus-5-5")
        ref2 = parse_model_ref("cc:sonnet-5.5")
        ctx.check(f"cc:sonnet-5.5 -> claude-sonnet-5-5, got {ref2.model!r}", ref2.model == "claude-sonnet-5-5")
        ref3 = parse_model_ref("ant:opus-5.5")
        ctx.check(f"ant:opus-5.5 -> claude-opus-5-5, got {ref3.model!r}", ref3.model == "claude-opus-5-5")
        ref4 = parse_model_ref("ant:sonnet-5.5")
        ctx.check(f"ant:sonnet-5.5 -> claude-sonnet-5-5, got {ref4.model!r}", ref4.model == "claude-sonnet-5-5")
    finally:
        _clear_cc_env()


@test
def test_alias_display_detail_shows_resolved_id_with_a_latest_note_for_opus_and_sonnet(ctx: Ctx):
    from rolo_claude.providers.cc_models import alias_display_detail
    ctx.check(f"opus -> resolved id + latest note, got {alias_display_detail('opus')!r}",
              alias_display_detail("opus") == "-> claude-opus-5-5 (latest Opus)")
    ctx.check(f"sonnet -> resolved id + latest note, got {alias_display_detail('sonnet')!r}",
              alias_display_detail("sonnet") == "-> claude-sonnet-5-5 (latest Sonnet)")


@test
def test_alias_display_detail_has_no_note_for_an_already_versioned_name(ctx: Ctx):
    from rolo_claude.providers.cc_models import alias_display_detail
    ctx.check(f"opus-5.5 -> no parenthetical, got {alias_display_detail('opus-5.5')!r}",
              alias_display_detail("opus-5.5") == "-> claude-opus-5-5")
    ctx.check(f"haiku -> no parenthetical, got {alias_display_detail('haiku')!r}",
              alias_display_detail("haiku") == "-> claude-haiku-4-5-20251001")
    ctx.check(f"opus-4.6 -> no parenthetical, got {alias_display_detail('opus-4.6')!r}",
              alias_display_detail("opus-4.6") == "-> claude-opus-4-6")


@test
def test_cc_ant_entries_carry_the_display_detail_field(ctx: Ctx):
    """The shared helper `/model`'s ant: group AND the init picker both go
    through (`init_providers._cc_ant_entries`) actually wires `detail` onto
    every row it builds."""
    from rolo_claude.init_providers import _cc_ant_entries
    from rolo_claude.providers.cc_models import ANT_ALIASES
    entries = {e["ref"]: e for e in _cc_ant_entries("ant", ANT_ALIASES)}
    ctx.check(f"ant:opus carries a detail, got {entries['ant:opus']}",
              entries["ant:opus"]["detail"] == "-> claude-opus-5-5 (latest Opus)")
    ctx.check(f"ant:opus-5.5 carries a detail, got {entries['ant:opus-5.5']}",
              entries["ant:opus-5.5"]["detail"] == "-> claude-opus-5-5")


@test
def test_list_models_cc_rows_carry_the_display_detail_and_the_part_c_group_label(ctx: Ctx):
    """`Controller.list_models()`'s OWN (separate, non-`_cc_ant_entries`)
    cc: loop also carries `detail`, and the group header is Part C's own
    "Claude Code subscription" label (never the stale "claude.ai
    subscription" string)."""
    from rolo_claude.controller import Controller
    from rolo_claude.providers.enablement import enable
    _clear_cc_env()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        d = Path(tempfile.mkdtemp(prefix="cc-list-models-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        enable("claude_subscription")

        class _FakeModelRef:
            raw = "cc:opus"
            provider = "cc"

        class _FakeModelProfile:
            context_tokens = 1_000_000
            max_output_tokens = 128_000

        class _FakeSession:
            model_ref = _FakeModelRef()
            model_profile = _FakeModelProfile()

        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=d / ".rolo-claude", routes={})
        cc_rows = {m["ref"]: m for m in ctrl.list_models() if m.get("provider") == "cc"}
        ctx.check(f"cc:opus present, got {list(cc_rows)}", "cc:opus" in cc_rows)
        ctx.check(f"carries the resolved-id detail, got {cc_rows['cc:opus']}",
                  cc_rows["cc:opus"].get("detail") == "-> claude-opus-5-5 (latest Opus)")
        ctx.check(f"group is the Part C label, got {cc_rows['cc:opus'].get('group')!r}",
                  cc_rows["cc:opus"].get("group") == "Claude Code subscription")
    finally:
        _clear_cc_env()
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_resolve_model_profile_cc_unknown_id_still_gets_a_sane_default(ctx: Ctx):
    from rolo_claude.model import ModelRef, resolve_model_profile
    ref = ModelRef(raw="cc:claude-made-up-9000", provider="cc", model="claude-made-up-9000", dialect="cc-subprocess")
    profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="cc-profile2-")), {})
    ctx.check("never crashes, has a context window", profile.context_tokens > 0)
    ctx.check("reasoning native (a real Claude model always supports thinking)", profile.reasoning == "native")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
