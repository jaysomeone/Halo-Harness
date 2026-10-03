"""tests/test_sub_usage.py -- the `cc:`/`cx:` subscription usage segment
("5h 58% · wk 18%"): Claude Code's `rate_limit_event` recorded by
`agent/cc_runtime.py`, both routes read back by `providers/sub_usage.py`,
carried on the `status` event, and rendered right after the context bar.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

# Verified live against claude 2.1.288 (`claude -p --output-format stream-json --verbose`).
_CC_EVENT = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": 1791004200, "rateLimitType": "five_hour", "isUsingOverage": False,
    "unifiedWindows": {"five_hour": {"utilization": 0.58, "resetsAt": 1791004200},
                       "seven_day": {"utilization": 0.18, "resetsAt": 1791180000}}},
    "uuid": "07e1fd4b", "session_id": "312dfd6b"}
_BEFORE_RESET = 1791000000


def _state() -> Path:
    return Path(tempfile.mkdtemp(prefix="sub-usage-"))


@test
def test_cc_rate_limit_event_recorded_and_read_back(ctx: Ctx):
    from halo_harness.providers.sub_usage import record_cc_rate_limits, subscription_usage
    state = _state()
    record_cc_rate_limits(_CC_EVENT["rate_limit_info"], state)
    usage = subscription_usage("cc", state, now=_BEFORE_RESET)
    ctx.check(f"58% / 18%, got {usage!r}", usage == {"session_pct": 58, "weekly_pct": 18})


@test
def test_cc_single_window_event_keeps_the_other_window(ctx: Ctx):
    from halo_harness.providers.sub_usage import record_cc_rate_limits, subscription_usage
    state = _state()
    record_cc_rate_limits(_CC_EVENT["rate_limit_info"], state)
    record_cc_rate_limits({"rateLimitType": "five_hour", "utilization": 0.61, "resetsAt": 1791004200}, state)
    usage = subscription_usage("cc", state, now=_BEFORE_RESET)
    ctx.check(f"5 h updated, weekly kept, got {usage!r}", usage == {"session_pct": 61, "weekly_pct": 18})


@test
def test_rolled_over_window_reads_zero(ctx: Ctx):
    from halo_harness.providers.sub_usage import record_cc_rate_limits, subscription_usage
    state = _state()
    record_cc_rate_limits(_CC_EVENT["rate_limit_info"], state)
    usage = subscription_usage("cc", state, now=1791004200 + 1)
    ctx.check(f"5 h reset -> 0, weekly unchanged, got {usage!r}", usage == {"session_pct": 0, "weekly_pct": 18})


@test
def test_cx_reads_the_codex_cache(ctx: Ctx):
    from halo_harness.providers.cx_models import record_rate_limits
    from halo_harness.providers.sub_usage import subscription_usage
    state = _state()
    later = int(time.time()) + 3600
    record_rate_limits({"planType": "team",
                        "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": later},
                        "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": later}}, state)
    usage = subscription_usage("cx", state)
    ctx.check(f"5% / 3%, got {usage!r}", usage == {"session_pct": 5, "weekly_pct": 3})


@test
def test_other_routes_and_no_reading_are_none(ctx: Ctx):
    from halo_harness.providers.sub_usage import format_usage_segment, subscription_usage
    state = _state()
    ctx.check("openrouter -> None", subscription_usage("openrouter", state) is None)
    ctx.check("cc before any reading -> None", subscription_usage("cc", state) is None)
    ctx.check("blank segment", format_usage_segment(None) == "")
    ctx.check("segment text", format_usage_segment({"session_pct": 58, "weekly_pct": 18}) == "5h 58% · wk 18%")


@test
def test_cc_runtime_records_the_event(ctx: Ctx):
    import os
    from halo_harness.agent import cc_runtime
    from halo_harness.providers.sub_usage import load_cc_usage
    state = _state()
    saved = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state)
    try:
        out = cc_runtime._events_for_stdout_obj(None, 1, _CC_EVENT, None)
        data = load_cc_usage()
    finally:
        if saved is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = saved
    ctx.check(f"no UI events, got {out!r}", out == [])
    ctx.check(f"recorded under the state dir, got {data!r}",
              data.get("session", {}).get("used_percent") == 58 and data.get("weekly", {}).get("used_percent") == 18)


@test
def test_context_prime_records_the_event(ctx: Ctx):
    # claude emits rate_limit_event only on a process's FIRST turn -- a
    # mid-session switch into cc: makes that turn the silent context prime.
    import os
    from types import SimpleNamespace
    from halo_harness.agent import cc_runtime
    from halo_harness.providers.sub_usage import load_cc_usage

    class _Proc:
        def __init__(self, events):
            self.events = list(events)

        def send_user_line(self, text):
            pass

        def read_event(self):
            return self.events.pop(0) if self.events else None

    proc = _Proc([_CC_EVENT, {"type": "result", "subtype": "success"}])
    session = SimpleNamespace(interactive=True)
    state = _state()
    saved = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state)
    saved_confirm = cc_runtime._confirm_session_id
    cc_runtime._confirm_session_id = lambda *a: None
    try:
        cc_runtime._prime_with_context(SimpleNamespace(process=proc), "context", session)
        data = load_cc_usage()
    finally:
        cc_runtime._confirm_session_id = saved_confirm
        if saved is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = saved
    ctx.check(f"recorded during the prime, got {data!r}",
              data.get("session", {}).get("used_percent") == 58 and data.get("weekly", {}).get("used_percent") == 18)
    ctx.check("interactive restored", session.interactive is True)


# `claude -p /usage`'s report, verified live against claude 2.1.288.
_USAGE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 7% used · resets Oct 3, 2:30pm (America/New_York)\n"
    "Current week (all models): 20% used · resets Oct 5, 2am (America/New_York)\n\n"
    "What's contributing to your limits usage?\n")
_OCT_3_NOON_ET = 1791043200  # 2026-10-03 12:00 America/New_York


@test
def test_usage_text_parsed(ctx: Ctx):
    from halo_harness.providers.sub_usage import parse_cc_usage_text
    r = parse_cc_usage_text(_USAGE_TEXT, now=_OCT_3_NOON_ET)
    ctx.check(f"7% / 20%, got {r!r}",
              r["session"]["used_percent"] == 7 and r["weekly"]["used_percent"] == 20)
    ctx.check(f"2:30pm ET resets, got {r['session']['resets_at']}", r["session"]["resets_at"] == _OCT_3_NOON_ET + 9000)
    ctx.check(f"Oct 5 2am ET resets, got {r['weekly']['resets_at']}",
              r["weekly"]["resets_at"] == _OCT_3_NOON_ET + 86400 + 14 * 3600)
    ctx.check("no usage lines -> {}", parse_cc_usage_text("You are using API usage billing") == {})


@test
def test_usage_resets_year_and_day_rollover(ctx: Ctx):
    from halo_harness.providers.sub_usage import _parse_resets
    dec_30 = 1798650000  # 2026-12-30 12:00 America/New_York
    jan = _parse_resets("Jan 2, 1am (America/New_York)", dec_30)
    ctx.check(f"Jan 2 read on Dec 30 is next year, got {jan}", jan is not None and 0 < jan - dec_30 < 4 * 86400)
    tomorrow = _parse_resets("2am (America/New_York)", _OCT_3_NOON_ET)
    ctx.check(f"time-only already passed -> tomorrow, got {tomorrow}", tomorrow == _OCT_3_NOON_ET + 14 * 3600)
    ctx.check("garbage -> None", _parse_resets("soon", _OCT_3_NOON_ET) is None)


@test
def test_probe_records_the_usage_report(ctx: Ctx):
    import json
    import os
    from halo_harness.providers.sub_usage import load_cc_usage, probe_cc_usage
    state = _state()
    fake = state / "fake_claude.py"
    fake.write_text("import json, sys\nassert '/usage' in sys.argv\n"
                    f"print(json.dumps({{'num_turns': 0, 'result': {_USAGE_TEXT!r}}}))\n", encoding="utf-8")
    saved = os.environ.get("BRIDGE_CLAUDE_EXE")
    os.environ["BRIDGE_CLAUDE_EXE"] = f'"{sys.executable}" "{fake}"'
    try:
        ok = probe_cc_usage(state)
    finally:
        if saved is None:
            os.environ.pop("BRIDGE_CLAUDE_EXE", None)
        else:
            os.environ["BRIDGE_CLAUDE_EXE"] = saved
    data = load_cc_usage(state)
    ctx.check(f"recorded, got {ok!r} {json.dumps(data)}",
              ok and data["session"]["used_percent"] == 7 and data["weekly"]["used_percent"] == 20)


@test
def test_refresh_only_for_cc_on_a_subscription(ctx: Ctx):
    import os
    from halo_harness.providers import sub_usage
    keys = ("BRIDGE_TEST_CC_AUTH_STATUS", "BRIDGE_TEST_NO_BACKGROUND_NET")
    saved_env = {k: os.environ.get(k) for k in keys}
    saved_probe, calls = sub_usage.probe_cc_usage, []
    sub_usage.probe_cc_usage = lambda *a, **kw: calls.append(1)
    sub_usage._last_probe_at = float("-inf")
    try:
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = '{"loggedIn": true, "authMethod": "api_key"}'
        ctx.check("api-key login -> no probe", sub_usage.maybe_refresh_cc_usage("cc") is False)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = '{"loggedIn": true, "authMethod": "claude.ai"}'
        ctx.check("non-cc route -> no probe", sub_usage.maybe_refresh_cc_usage("cx") is False)
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        ctx.check("background net disabled -> no probe", sub_usage.maybe_refresh_cc_usage("cc") is False)
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        ctx.check("cc on claude.ai -> probe", sub_usage.maybe_refresh_cc_usage("cc") is True)
        ctx.check("throttled right after", sub_usage.maybe_refresh_cc_usage("cc") is False)
        for _ in range(100):
            if calls and not sub_usage._probe_running:
                break
            time.sleep(0.01)
        ctx.check(f"probe ran once, got {len(calls)}", len(calls) == 1)
    finally:
        sub_usage.probe_cc_usage = saved_probe
        sub_usage._last_probe_at = float("-inf")
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_status_bar_renders_usage_after_the_context_bar(ctx: Ctx):
    import asyncio
    from halo_harness.testing.fake_controller import FakeController
    from halo_harness.tui.app import BridgeApp

    async def body():
        fake = FakeController()
        fake.state_dir = _state()
        app = BridgeApp(fake, cwd=str(Path(__file__).resolve().parent.parent))
        async with app.run_test(size=(240, 40)):
            bar = app.status_bar
            bar.apply_status({"model": "cc:opus-5.5", "context_tokens": 1000, "context_limit": 200000,
                              "subscription_usage": {"session_pct": 58, "weekly_pct": 18}})
            bar._refresh_display()
            text = bar.render().plain
            ctx_pos, usage_pos = text.find("]"), text.find("5h 58% · wk 18%")
            ctx.check(f"usage right after the context bar, got {text!r}", -1 < ctx_pos < usage_pos)
            bar.apply_status({"model": "or:some-model", "subscription_usage": None})
            bar._refresh_display()
            ctx.check(f"cleared on a non-subscription route, got {bar.render().plain!r}",
                      "5h " not in bar.render().plain)
    asyncio.run(body())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
