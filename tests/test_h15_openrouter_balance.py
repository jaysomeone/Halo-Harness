"""tests.test_h15_openrouter_balance -- H15 part 2 addendum 4 (corrected
against OpenRouter's real OpenAPI spec): the OpenRouter account-balance
status-bar segment ("OR $12.40 left" / "OR $3.21 used"), `/cost` and
`/providers`'s matching line. Covers `providers.openrouter_account`
(fetch_key_info against GET /key with the ordinary key, fetch_credits
against GET /credits with a SEPARATE management key, resolve_balance's own
3-way preference order, the in-memory cache), the status bar's own
render/dim logic, the background worker end to end via a real BridgeApp
mount, a failing endpoint never raising, and the management key never
being sent to any endpoint but /credits.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_get_endpoints import MockGetEndpoints

# 1.0.1 part 2 fixpass finding 9: every test in this file points OpenRouter
# at a loopback MockGetEndpoints server (never the real openrouter.ai) to
# simulate /key and /credits -- the production fix that refuses a
# non-official host before sending either key would otherwise refuse every
# one of these mocks too. Set ONCE at import time (most tests here call
# `refresh_cached_openrouter_balance`/`fetch_key_info`/`fetch_credits`
# directly, with no per-test env scoping of their own) -- this test-only
# seam is never set outside this suite and never weakens the real check;
# see `is_openrouter_official_host`'s own docstring. `test_h15b_fixpass.py`'s
# own pinning test for the REAL (strict) behavior deliberately does not set it.
os.environ["BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE"] = "1"

test, TESTS = new_registry()

_KEY_INFO_NO_LIMIT_BODY = {"data": {"label": "my-key", "limit": None, "usage": 3.21,
                                     "limit_remaining": None, "is_free_tier": False}}
_KEY_INFO_WITH_LIMIT_BODY = {"data": {"label": "capped-key", "limit": 20.0, "usage": 7.0,
                                       "limit_remaining": 13.0, "is_free_tier": False,
                                       "rate_limit": {"requests": 200, "interval": "10s"}}}
_CREDITS_BODY = {"data": {"total_credits": 50.0, "total_usage": 37.60}}


def _reset():
    from rolo_claude.providers.openrouter_account import reset_cached_openrouter_balance
    reset_cached_openrouter_balance()


class _EnvKeys:
    """Snapshots/restores OPENROUTER_API_KEY/OPENROUTER_MANAGEMENT_KEY/
    BRIDGE_OPENROUTER_BASE_URL (plus the other providers' own credential
    vars -- a mounted BridgeApp's launch-time `catalog_auto_refresh_worker`
    independently checks is_enabled("anthropic")/is_enabled("databricks")
    too, which must never see a REAL ambient credential and fire a real
    network call as a side effect of one of these tests) and scopes
    BRIDGE_TEST_HOME/BRIDGE_STATE_DIR (is_enabled()'s own `providers` block
    check reads `~/.rolo-claude/config.json` via plain bridge_home(),
    unscoped by any state_dir= passed elsewhere) -- also resets the
    module-level balance cache on both enter and exit, since it is a plain
    Python global, unaffected by env scoping (would otherwise leak between
    tests/files in a full `tests/run_all.py` run)."""

    _CRED_VARS = ("OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "BRIDGE_OPENROUTER_BASE_URL",
                  "ANTHROPIC_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "TYPESAFE_API_KEY")
    _VARS = _CRED_VARS + ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        for k in self._CRED_VARS:
            os.environ.pop(k, None)
        d = Path(tempfile.mkdtemp(prefix="h15-or-balance-env-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".rolo-claude")
        # 1.0.1 part 2 fixpass finding 9: every test in this file points
        # OpenRouter at a loopback mock server (never the real openrouter.ai)
        # to simulate /key and /credits -- the production fix that refuses
        # a non-official host before sending either key would otherwise
        # refuse every one of these mocks too; this test-only seam (never
        # set outside this suite) opts back in without weakening the real
        # check. `test_h15b_fixpass.py`'s own pinning test for the REAL
        # (strict) behavior deliberately does NOT set this.
        os.environ["BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE"] = "1"
        _reset()
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reset()


# ---------------------------------------------------------------------------
# fetch_key_info (GET /key, ordinary key) / fetch_credits (GET /credits,
# management key) / resolve_balance's 3-way preference.
# ---------------------------------------------------------------------------

@test
def test_fetch_key_info_hits_get_key_not_auth_key(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import fetch_key_info
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        info = fetch_key_info(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check(f"key info fetched, got {info}", info is not None)
        ctx.check(f"label my-key, got {info.label!r}", info.label == "my-key")
        ctx.check(f"usage 3.21, got {info.usage}", info.usage == 3.21)
        ctx.check(f"no limit set, got {info.limit!r}", info.limit is None)
        ctx.check("the real /key path was hit", any(r["path"] == "/api/v1/key" for r in mock.requests))
        ctx.check("the (nonexistent) /auth/key path was never hit",
                  not any(r["path"] == "/api/v1/auth/key" for r in mock.requests))
        ctx.check("sends the harness User-Agent",
                  any("rolo-claude" in r.get("headers", {}).get("User-Agent", "") for r in mock.requests))
    finally:
        mock.stop()


@test
def test_fetch_key_info_with_a_limit_parses_limit_remaining_and_rate_limit(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import fetch_key_info
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_WITH_LIMIT_BODY)}).start()
    try:
        info = fetch_key_info(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check(f"limit 20.0, got {info.limit}", info.limit == 20.0)
        ctx.check(f"limit_remaining 13.0, got {info.limit_remaining}", info.limit_remaining == 13.0)
        ctx.check(f"rate_limit carried through, got {info.rate_limit}",
                  info.rate_limit == {"requests": 200, "interval": "10s"})
    finally:
        mock.stop()


@test
def test_fetch_credits_requires_the_management_key(ctx: Ctx):
    """The mock records exactly which bearer token each request carried --
    this pins that `fetch_credits` sends whatever key it's GIVEN (the
    caller's job, `refresh_cached_openrouter_balance`, is what must never
    pass the ordinary key here -- covered separately below)."""
    from rolo_claude.providers.openrouter_account import fetch_credits
    mock = MockGetEndpoints({"/api/v1/credits": (200, _CREDITS_BODY)}).start()
    try:
        credits = fetch_credits(mock.base_url + "/api/v1", "sk-or-management")
        ctx.check(f"credits fetched, got {credits}", credits is not None)
        ctx.check(f"remaining 12.40, got {credits.remaining}", abs(credits.remaining - 12.40) < 1e-9)
        sent = [r for r in mock.requests if r["path"] == "/api/v1/credits"]
        ctx.check(f"the management key was the bearer token, got {sent}",
                  sent and sent[0]["headers"].get("Authorization") == "Bearer sk-or-management")
    finally:
        mock.stop()


@test
def test_fetch_key_info_connect_failure_returns_none_never_raises(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import fetch_key_info
    result = fetch_key_info("http://127.0.0.1:1/api/v1", "sk-or-fake")
    ctx.check(f"connect failure -> None, never raises, got {result!r}", result is None)


@test
def test_resolve_balance_prefers_limit_remaining_when_the_key_has_a_limit(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import OrCredits, OrKeyInfo, resolve_balance
    key_info = OrKeyInfo(label="capped", limit=20.0, usage=7.0, limit_remaining=13.0, is_free_tier=False)
    credits = OrCredits(total_credits=50.0, total_usage=37.60)
    got = resolve_balance(key_info, credits)
    ctx.check(f"kind=limit_remaining, amount=13.0, got {got}", got == {"amount": 13.0, "kind": "limit_remaining"})


@test
def test_resolve_balance_falls_back_to_management_key_credits_when_no_limit(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import OrCredits, OrKeyInfo, resolve_balance
    key_info = OrKeyInfo(label="uncapped", limit=None, usage=3.21, limit_remaining=None, is_free_tier=False)
    credits = OrCredits(total_credits=50.0, total_usage=37.60)
    got = resolve_balance(key_info, credits)
    ctx.check(f"kind=credits, amount~12.40, got {got}",
              got["kind"] == "credits" and abs(got["amount"] - 12.40) < 1e-9)


@test
def test_resolve_balance_falls_back_to_this_keys_own_usage_when_neither_available(ctx: Ctx):
    """The honest third fallback: an unlimited key, no management key
    configured at all (credits=None) -- shows THIS key's own spend."""
    from rolo_claude.providers.openrouter_account import OrKeyInfo, resolve_balance
    key_info = OrKeyInfo(label="uncapped", limit=None, usage=3.21, limit_remaining=None, is_free_tier=False)
    got = resolve_balance(key_info, None)
    ctx.check(f"kind=usage, amount=3.21, got {got}", got == {"amount": 3.21, "kind": "usage"})


@test
def test_resolve_balance_none_when_nothing_at_all_available(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import resolve_balance
    ctx.check("both None -> None", resolve_balance(None, None) is None)


# ---------------------------------------------------------------------------
# refresh_cached_openrouter_balance: the three segment variants end to end,
# and the management key is NEVER sent to /key.
# ---------------------------------------------------------------------------

@test
def test_refresh_variant_limit_remaining(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import (
        cached_openrouter_balance, format_status_bar_segment, refresh_cached_openrouter_balance,
    )
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_WITH_LIMIT_BODY)}).start()
    try:
        _reset()
        entry = refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check(f"entry present, got {entry}", entry is not None and entry["kind"] == "limit_remaining")
        ctx.check(f"status bar text, got {format_status_bar_segment()!r}",
                  format_status_bar_segment() == "OR $13.00 left")
    finally:
        mock.stop()
        _reset()


@test
def test_refresh_variant_management_key_credits(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import format_status_bar_segment, refresh_cached_openrouter_balance
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY),
                              "/api/v1/credits": (200, _CREDITS_BODY)}).start()
    try:
        _reset()
        entry = refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary",
                                                   management_key="sk-or-management")
        ctx.check(f"kind=credits, got {entry}", entry is not None and entry["kind"] == "credits")
        ctx.check(f"status bar text, got {format_status_bar_segment()!r}",
                  format_status_bar_segment() == "OR $12.40 left")
    finally:
        mock.stop()
        _reset()


@test
def test_refresh_variant_this_keys_usage_no_management_key(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import format_status_bar_segment, refresh_cached_openrouter_balance
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        _reset()
        entry = refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check(f"kind=usage (no management key given at all), got {entry}",
                  entry is not None and entry["kind"] == "usage")
        ctx.check(f"status bar text says 'used', got {format_status_bar_segment()!r}",
                  format_status_bar_segment() == "OR $3.21 used")
    finally:
        mock.stop()
        _reset()


@test
def test_management_key_never_sent_to_key_endpoint(ctx: Ctx):
    """The addendum's own explicit safety ask: the management key must
    never leak onto the ordinary /key call."""
    from rolo_claude.providers.openrouter_account import refresh_cached_openrouter_balance
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY),
                              "/api/v1/credits": (200, _CREDITS_BODY)}).start()
    try:
        _reset()
        refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary",
                                           management_key="sk-or-management")
        key_calls = [r for r in mock.requests if r["path"] == "/api/v1/key"]
        credits_calls = [r for r in mock.requests if r["path"] == "/api/v1/credits"]
        ctx.check(f"/key saw ONLY the ordinary key, got {key_calls}",
                  key_calls and all(c["headers"].get("Authorization") == "Bearer sk-or-ordinary" for c in key_calls))
        ctx.check(f"/credits saw ONLY the management key, got {credits_calls}",
                  credits_calls and all(c["headers"].get("Authorization") == "Bearer sk-or-management"
                                         for c in credits_calls))
    finally:
        mock.stop()
        _reset()


@test
def test_a_failed_refresh_never_clobbers_a_previously_good_reading(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import cached_openrouter_balance, refresh_cached_openrouter_balance
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        _reset()
        refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check("a real reading is cached", cached_openrouter_balance() is not None)
        mock.stop()  # now every subsequent call fails to connect
        result = refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check(f"the failed attempt itself reports None, got {result!r}", result is None)
        ctx.check("the OLD good reading is still there", cached_openrouter_balance() is not None)
    finally:
        _reset()


@test
def test_post_turn_debounce_blocks_a_second_immediate_refresh(ctx: Ctx):
    from rolo_claude.providers.openrouter_account import (
        openrouter_balance_refresh_due_after_turn, refresh_cached_openrouter_balance,
    )
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        _reset()
        ctx.check("due before any attempt at all", openrouter_balance_refresh_due_after_turn() is True)
        refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        ctx.check("NOT due immediately after an attempt (60s debounce)",
                  openrouter_balance_refresh_due_after_turn() is False)
    finally:
        mock.stop()
        _reset()


# ---------------------------------------------------------------------------
# tui/slash.py::or_balance_refresh_worker -- enablement gating, management
# key resolution, force vs debounced.
# ---------------------------------------------------------------------------

class _FakeStatusBar:
    def __init__(self):
        self.or_balance_text = None
        self.or_balance_fetched_at = None

    def set_or_balance(self, segment_text, *, fetched_at=None):
        self.or_balance_text = segment_text
        self.or_balance_fetched_at = fetched_at


class _FakeAppForWorker:
    def __init__(self):
        self.status_bar = _FakeStatusBar()

    def call_from_thread(self, fn, *a, **kw):
        fn(*a, **kw)


@test
def test_or_balance_refresh_worker_noop_when_openrouter_not_enabled(ctx: Ctx):
    from rolo_claude.tui.slash import or_balance_refresh_worker
    with _EnvKeys():
        app = _FakeAppForWorker()
        or_balance_refresh_worker(app, force=True)
        ctx.check("status bar untouched -- nothing to fetch", app.status_bar.or_balance_text is None)


@test
def test_or_balance_refresh_worker_uses_usage_variant_with_only_the_ordinary_key(ctx: Ctx):
    from rolo_claude.tui.slash import or_balance_refresh_worker
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        with _EnvKeys():
            os.environ["OPENROUTER_API_KEY"] = "sk-or-ordinary"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            app = _FakeAppForWorker()
            or_balance_refresh_worker(app, force=True)
            ctx.check(f"status bar shows the 'used' variant, got {app.status_bar.or_balance_text!r}",
                      app.status_bar.or_balance_text == "OR $3.21 used")
    finally:
        mock.stop()


@test
def test_or_balance_refresh_worker_uses_management_key_when_configured(ctx: Ctx):
    from rolo_claude.tui.slash import or_balance_refresh_worker
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY),
                              "/api/v1/credits": (200, _CREDITS_BODY)}).start()
    try:
        with _EnvKeys():
            os.environ["OPENROUTER_API_KEY"] = "sk-or-ordinary"
            os.environ["OPENROUTER_MANAGEMENT_KEY"] = "sk-or-management"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            app = _FakeAppForWorker()
            or_balance_refresh_worker(app, force=True)
            ctx.check(f"status bar shows the credits variant, got {app.status_bar.or_balance_text!r}",
                      app.status_bar.or_balance_text == "OR $12.40 left")
    finally:
        mock.stop()


@test
def test_or_balance_refresh_worker_reads_the_controllers_effective_env_not_bare_os_environ(ctx: Ctx):
    """N2c (1.0.1 final pass): the balance worker must resolve the
    OpenRouter key/management key from app.controller.settings.
    effective_env when a real Controller/Settings is attached -- never bare
    os.environ -- so a credential scoped only to the session's own trust-
    filtered settings chain is honored exactly like a real turn would."""
    from rolo_claude.providers.enablement import enable
    from rolo_claude.tui.slash import or_balance_refresh_worker

    class _FakeSettingsForWorker:
        def __init__(self, env):
            self.effective_env = env

    class _FakeControllerForWorker:
        def __init__(self, env):
            self.settings = _FakeSettingsForWorker(env)

    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        with _EnvKeys():
            # Deliberately nothing in the REAL process env -- the key only
            # ever lives in the fake controller's own effective_env; an
            # explicit override bypasses is_enabled()'s own bare-env
            # auto-detection, which this fix never touches.
            enable("openrouter")
            app = _FakeAppForWorker()
            app.controller = _FakeControllerForWorker({
                "OPENROUTER_API_KEY": "sk-or-from-effective-env",
                "BRIDGE_OPENROUTER_BASE_URL": mock.base_url + "/api/v1",
            })
            or_balance_refresh_worker(app, force=True)
            ctx.check(f"resolved and fetched using the controller's own effective_env, got "
                      f"{app.status_bar.or_balance_text!r}", app.status_bar.or_balance_text == "OR $3.21 used")
            sent = [r for r in mock.requests if r["path"] == "/api/v1/key"]
            ctx.check(f"the key from effective_env was actually sent, got {sent}",
                      sent and sent[0]["headers"].get("Authorization") == "Bearer sk-or-from-effective-env")
    finally:
        mock.stop()


@test
def test_or_balance_refresh_worker_failing_endpoint_never_raises_and_leaves_bar_untouched(ctx: Ctx):
    from rolo_claude.tui.slash import or_balance_refresh_worker
    with _EnvKeys():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-ordinary"
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = "http://127.0.0.1:1/api/v1"  # nothing listening
        app = _FakeAppForWorker()
        or_balance_refresh_worker(app, force=True)  # must not raise
        ctx.check("segment stays omitted (never populated)", app.status_bar.or_balance_text is None)


# ---------------------------------------------------------------------------
# StatusBar: segment text, omission, and the 10-minute dim threshold. A real
# `StatusBar` is a mounted Textual widget (its own `_refresh_display` calls
# `self.update(...)`, which needs a live app/console) -- exercised through a
# minimal real `BridgeApp` mount, same convention test_tui.py's own pilot
# tests use throughout, rather than constructing it standalone.
# ---------------------------------------------------------------------------

def _run_in_mounted_app(fn) -> None:
    """Mounts a real (FakeController-backed) BridgeApp and calls
    `fn(app.status_bar)` from inside its `run_test()` context -- `fn` does
    its own ctx.check calls via closure."""
    import asyncio
    from rolo_claude.testing.fake_controller import FakeController
    from rolo_claude.tui.app import BridgeApp

    async def body():
        fake = FakeController()
        fake.state_dir = Path(tempfile.mkdtemp(prefix="h15-or-balance-bar-"))
        app = BridgeApp(fake, cwd=str(Path(__file__).resolve().parent.parent))
        async with app.run_test(size=(120, 40)):
            fn(app.status_bar)
    asyncio.run(body())


def _plain(bar) -> str:
    bar._refresh_display()
    rendered = bar.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


@test
def test_status_bar_segment_omitted_until_a_balance_is_set(ctx: Ctx):
    def _check(bar):
        ctx.check("omitted entirely before any reading", bar.or_balance_text is None)
        ctx.check(f"no 'OR $' segment rendered, got {_plain(bar)!r}", "OR $" not in _plain(bar))
    _run_in_mounted_app(_check)


@test
def test_status_bar_shows_left_variant(ctx: Ctx):
    def _check(bar):
        bar.set_or_balance("OR $12.40 left")
        text = _plain(bar)
        ctx.check(f"renders 'OR $12.40 left', got {text!r}", "OR $12.40 left" in text)
    _run_in_mounted_app(_check)


@test
def test_status_bar_shows_used_variant(ctx: Ctx):
    def _check(bar):
        bar.set_or_balance("OR $3.21 used")
        text = _plain(bar)
        ctx.check(f"renders 'OR $3.21 used', got {text!r}", "OR $3.21 used" in text)
    _run_in_mounted_app(_check)


@test
def test_status_bar_dims_the_balance_once_older_than_ten_minutes(ctx: Ctx):
    def _check(bar):
        bar.set_or_balance("OR $12.40 left", fetched_at=time.monotonic())
        text = _plain(bar)
        ctx.check(f"fresh reading present, got {text!r}", "OR $12.40 left" in text)
        # Backdated 11 minutes -- still shown, just (per the addendum)
        # dimmed; the text itself is unchanged either way, only the style
        # differs, so this pins "never hidden" rather than inspecting
        # Rich styling directly.
        bar.set_or_balance("OR $12.40 left", fetched_at=time.monotonic() - 660.0)
        text2 = _plain(bar)
        ctx.check(f"stale reading is STILL shown (dimmed, not hidden), got {text2!r}", "OR $12.40 left" in text2)
    _run_in_mounted_app(_check)


@test
def test_status_bar_segment_sits_right_after_cost(ctx: Ctx):
    def _check(bar):
        bar.apply_cost(1.5)
        bar.set_or_balance("OR $12.40 left")
        text = _plain(bar)
        cost_pos = text.find("$1.50")
        or_pos = text.find("OR $12.40 left")
        mode_pos = text.find(" default ")
        ctx.check(f"cost appears, got {text!r}", cost_pos != -1)
        ctx.check(f"OR balance appears right after cost (before mode), got {text!r}",
                  or_pos != -1 and cost_pos < or_pos < (mode_pos if mode_pos != -1 else len(text) + 1))
    _run_in_mounted_app(_check)


# ---------------------------------------------------------------------------
# End to end: a real BridgeApp mount with the /key mock.
# ---------------------------------------------------------------------------

@test
def test_launch_populates_the_status_bar_segment_end_to_end(ctx: Ctx):
    import asyncio
    from rolo_claude.testing.fake_controller import FakeController
    from rolo_claude.tui.app import BridgeApp

    async def body():
        mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_WITH_LIMIT_BODY)}).start()
        with _EnvKeys():
            os.environ["OPENROUTER_API_KEY"] = "sk-or-ordinary"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            try:
                fake = FakeController()
                fake.state_dir = Path(tempfile.mkdtemp(prefix="h15-or-balance-launch-"))
                app = BridgeApp(fake, cwd=str(Path(__file__).resolve().parent.parent))
                async with app.run_test(size=(120, 40)) as pilot:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and app.status_bar.or_balance_text is None:
                        await pilot.pause(0.1)
                    ctx.check(f"status bar populated by launch alone, got {app.status_bar.or_balance_text!r}",
                              app.status_bar.or_balance_text == "OR $13.00 left")
            finally:
                mock.stop()
    asyncio.run(body())


# ---------------------------------------------------------------------------
# /cost shows the same figure, naming which of the three kinds it is.
# ---------------------------------------------------------------------------

@test
def test_cmd_cost_shows_the_limit_remaining_line(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_cost
    from rolo_claude.providers.openrouter_account import refresh_cached_openrouter_balance
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_WITH_LIMIT_BODY)}).start()
    try:
        _reset()
        refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        out = _cmd_cost("", HeadlessFacade(cwd=Path.cwd()))
        ctx.check(f"names the remaining figure, got {out!r}", "$13.00 remaining" in out)
        ctx.check(f"names it as this key's own limit, got {out!r}", "this key's own limit" in out)
        ctx.check(f"names the key label, got {out!r}", "capped-key" in out)
    finally:
        mock.stop()
        _reset()


@test
def test_cmd_cost_shows_the_usage_line_when_no_limit_or_management_key(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_cost
    from rolo_claude.providers.openrouter_account import refresh_cached_openrouter_balance
    mock = MockGetEndpoints({"/api/v1/key": (200, _KEY_INFO_NO_LIMIT_BODY)}).start()
    try:
        _reset()
        refresh_cached_openrouter_balance(mock.base_url + "/api/v1", "sk-or-ordinary")
        out = _cmd_cost("", HeadlessFacade(cwd=Path.cwd()))
        ctx.check(f"names the spend figure, got {out!r}", "$3.21 used so far" in out)
        ctx.check(f"explains why (no limit/management key), got {out!r}", "no management key" in out)
    finally:
        mock.stop()
        _reset()


@test
def test_cmd_cost_omits_the_balance_line_when_never_fetched(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_cost
    _reset()
    out = _cmd_cost("", HeadlessFacade(cwd=Path.cwd()))
    ctx.check(f"no OpenRouter line at all, got {out!r}", "OpenRouter" not in out)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
