"""Managed subscription profile isolation and guided Codex login."""
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo_harness.accounts import (AccountSetupError, active_codex_profile, add_codex_account,
                                   codex_profile_env, format_accounts, list_account_profiles,
                                   next_codex_profile, prepare_codex_profile, set_active_account)
from halo_harness.providers.cx_models import CodexLoginStatus
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_prepare_codex_profile_is_private_and_isolated(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    first = prepare_codex_profile("personal", state_dir=state)
    second = prepare_codex_profile("work", state_dir=state)
    ctx.check("separate CODEX_HOME directories", first.config_home != second.config_home)
    ctx.check("file credential storage forced",
              first.config_home.joinpath("config.toml").read_text() == 'cli_auth_credentials_store = "file"\n')
    if os.name != "nt":
        ctx.check("profile directory mode 700", stat.S_IMODE(first.profile_dir.stat().st_mode) == 0o700)
        ctx.check("config file mode 600", stat.S_IMODE(first.config_home.joinpath("config.toml").stat().st_mode) == 0o600)


@test
def test_account_name_cannot_escape_profile_root(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    for bad in ("../other", "/tmp/account", "two words", ""):
        try:
            prepare_codex_profile(bad, state_dir=state)
        except AccountSetupError:
            pass
        else:
            ctx.check(f"unsafe name rejected: {bad!r}", False)


@test
def test_existing_codex_config_is_not_overwritten(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    profile = prepare_codex_profile("personal", state_dir=state)
    config = profile.config_home / "config.toml"
    config.write_text('cli_auth_credentials_store = "file"\nmodel = "custom"\n', encoding="utf-8")
    prepare_codex_profile("personal", state_dir=state)
    ctx.check("custom config preserved", 'model = "custom"' in config.read_text(encoding="utf-8"))


@test
def test_add_codex_account_uses_profile_home_and_official_login(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        Path(kwargs["env"]["CODEX_HOME"], "auth.json").write_text("fake-test-token", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def fake_status(*, env):
        calls.append(("status", env))
        return CodexLoginStatus(True, method="chatgpt", text="Logged in using ChatGPT")

    old = os.environ.get("HALO_CODEX_EXE")
    os.environ["HALO_CODEX_EXE"] = sys.executable
    try:
        profile, text = add_codex_account("personal", state_dir=state, run=fake_run,
                                          login_status=fake_status, base_env={})
    finally:
        if old is None:
            os.environ.pop("HALO_CODEX_EXE", None)
        else:
            os.environ["HALO_CODEX_EXE"] = old
    ctx.check("official login subcommand launched", calls[0][0][-1] == "login")
    ctx.check("login receives isolated CODEX_HOME", calls[0][1]["env"]["CODEX_HOME"] == str(profile.config_home))
    ctx.check("status checks the same profile", calls[1][1]["CODEX_HOME"] == str(profile.config_home))
    ctx.check("successful status returned", "ChatGPT" in text)


@test
def test_add_refuses_to_replace_an_existing_login(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    profile = prepare_codex_profile("personal", state_dir=state)
    (profile.config_home / "auth.json").write_text("existing-secret", encoding="utf-8")
    try:
        add_codex_account("personal", state_dir=state, run=lambda *a, **k: None)
    except AccountSetupError as exc:
        ctx.check("friendly duplicate message", "already has a saved login" in str(exc))
    else:
        ctx.check("existing login was not replaced", False)


@test
def test_list_and_format_never_read_or_print_token(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    profile = prepare_codex_profile("personal", state_dir=state)
    secret = "secret-that-must-not-print"
    (profile.config_home / "auth.json").write_text(secret, encoding="utf-8")
    profiles = list_account_profiles(state_dir=state)
    shown = format_accounts(state_dir=state)
    ctx.check("one profile listed", [p.name for p in profiles] == ["personal"])
    ctx.check("friendly status shown", "login saved" in shown)
    ctx.check("credential contents absent", secret not in shown)


@test
def test_profile_env_keeps_accounts_separate(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    a = prepare_codex_profile("a", state_dir=state)
    b = prepare_codex_profile("b", state_dir=state)
    ctx.check("A selected", codex_profile_env(a, {})["CODEX_HOME"] == str(a.config_home))
    ctx.check("B selected", codex_profile_env(b, {})["CODEX_HOME"] == str(b.config_home))


@test
def test_fallback_prefers_available_but_rechecks_cached_exhausted_profiles(ctx: Ctx):
    from halo_harness.providers.cx_models import record_rate_limits

    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    prepare_codex_profile("a", state_dir=state)
    exhausted = prepare_codex_profile("b", state_dir=state)
    available = prepare_codex_profile("c", state_dir=state)
    record_rate_limits({"rateLimitReachedType": "primary",
                        "primary": {"usedPercent": 100, "resetsAt": 2000}}, exhausted.profile_dir)

    def fake_status(*, env):
        name = Path(env["CODEX_HOME"]).parent.name
        return CodexLoginStatus(name != "missing", method="chatgpt" if name != "missing" else None)

    chosen = next_codex_profile("a", state_dir=state, login_status=fake_status, now=1000, base_env={})
    ctx.check("next available same-provider account selected", chosen == available)
    last_chance = next_codex_profile("a", state_dir=state, login_status=fake_status, now=1000, base_env={},
                                     excluded_names={"c"})
    ctx.check("cached limit cannot permanently exclude a logged-in account", last_chance == exhausted)
    none_left = next_codex_profile("a", state_dir=state, login_status=fake_status, now=1000, base_env={},
                                   excluded_names={"b", "c"})
    ctx.check("already attempted accounts are never selected again", none_left is None)


@test
def test_expired_primary_limit_does_not_borrow_secondary_reset(ctx: Ctx):
    from halo_harness.accounts import _profile_limit_is_active
    from halo_harness.providers.cx_models import record_rate_limits
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    profile = prepare_codex_profile("a", state_dir=state)
    for reached in ("primary", "workspace_owner_credits_depleted"):
        record_rate_limits({"rateLimitReachedType": reached,
                            "primary": {"usedPercent": 100, "resetsAt": 900},
                            "secondary": {"usedPercent": 32, "resetsAt": 2000}}, profile.profile_dir)
        ctx.check(f"expired primary is not blocked by secondary for {reached}",
                  not _profile_limit_is_active(profile, now=1000))
    record_rate_limits({"primary": {"usedPercent": 100, "resetsAt": 1100}}, profile.profile_dir)
    ctx.check("a genuinely active window remains a deprioritization signal",
              _profile_limit_is_active(profile, now=1000))


@test
def test_fallback_never_uses_logged_out_or_api_key_profiles(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    for name in ("a", "api", "logged-out", "subscription"):
        prepare_codex_profile(name, state_dir=state)
    def status(*, env):
        name = Path(env["CODEX_HOME"]).parent.name
        return CodexLoginStatus(name != "logged-out", method="api_key" if name == "api" else "chatgpt")
    selected = next_codex_profile("a", state_dir=state, login_status=status, base_env={})
    ctx.check("only the other verified subscription is selected", selected.name == "subscription")


@test
def test_runtime_uses_the_active_profile_environment(ctx: Ctx):
    from halo_harness.agent.codex_runtime import _cx_child_env, _active_profile

    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    prepare_codex_profile("personal", state_dir=state)
    work = prepare_codex_profile("work", state_dir=state)
    set_active_account("codex", "work", state_dir=state)
    session = SimpleNamespace(state_dir=state, tool_env={"SAFE_VALUE": "kept"})
    selected = _active_profile(session)
    env = _cx_child_env(session)
    ctx.check("runtime selected active account", selected == work)
    ctx.check("runtime pins isolated CODEX_HOME", env["CODEX_HOME"] == str(work.config_home))
    ctx.check("ordinary safe env survives", env["SAFE_VALUE"] == "kept")


@test
def test_active_account_selection_is_persisted_without_secrets(ctx: Ctx):
    state = Path(tempfile.mkdtemp(prefix="accounts-state-"))
    prepare_codex_profile("personal", state_dir=state)
    work = prepare_codex_profile("work", state_dir=state)
    ctx.check("first profile is the default", active_codex_profile(state_dir=state).name == "personal")
    set_active_account("codex", "work", state_dir=state)
    ctx.check("explicit selection wins", active_codex_profile(state_dir=state) == work)
    selection = state / "accounts" / "active.json"
    ctx.check("selection contains only a friendly name", "work" in selection.read_text() and "token" not in selection.read_text())
    if os.name != "nt":
        ctx.check("selection file mode 600", stat.S_IMODE(selection.stat().st_mode) == 0o600)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
