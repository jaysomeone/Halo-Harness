"""tests.test_hotfix_101_tls -- 1.0.1 hotfix 11: clear VERIFY_X509_STRICT on
every TLS context the harness builds (Python 3.13+ turns it on by default;
a corporate TLS-inspection proxy's re-signing CA -- verified live -- can
carry a non-critical basicConstraints extension that only X509_STRICT
rejects, which no other major HTTP client on the same network enforces).
"""
from __future__ import annotations

import os
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, run_all, print_results, SkipTest

test, TESTS = new_registry()


@test
def test_default_tls_context_clears_verify_x509_strict_when_present(ctx: Ctx):
    from rolo_claude.providers.http import default_tls_context
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if not strict_flag:
        raise SkipTest("this Python's ssl module has no VERIFY_X509_STRICT at all (< 3.13)")
    ctx_obj = default_tls_context()
    ctx.check(f"VERIFY_X509_STRICT is cleared, got verify_flags={ctx_obj.verify_flags!r}",
              not (ctx_obj.verify_flags & strict_flag))


@test
def test_default_tls_context_never_weakens_hostname_or_cert_verification(ctx: Ctx):
    from rolo_claude.providers.http import default_tls_context
    ctx_obj = default_tls_context()
    ctx.check(f"check_hostname stays on, got {ctx_obj.check_hostname}", ctx_obj.check_hostname is True)
    ctx.check(f"verify_mode stays CERT_REQUIRED, got {ctx_obj.verify_mode}",
              ctx_obj.verify_mode == ssl.CERT_REQUIRED)


@test
def test_default_tls_context_loads_custom_ca_bundle_env(ctx: Ctx):
    """The three-env-var CA-bundle loading `open_upstream` already had is
    preserved verbatim -- monkeypatches `load_verify_locations` so this
    never needs a real CA file on disk."""
    from rolo_claude.providers.http import default_tls_context
    calls = []
    real_load = ssl.SSLContext.load_verify_locations

    def _fake_load(self, cafile=None, **kw):
        calls.append(cafile)

    old = os.environ.get("BRIDGE_CA_BUNDLE")
    os.environ["BRIDGE_CA_BUNDLE"] = "/fake/ca-bundle.pem"
    os.environ.pop("NODE_EXTRA_CA_CERTS", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    ssl.SSLContext.load_verify_locations = _fake_load
    try:
        default_tls_context()
        # `ssl.create_default_context()` itself may ALSO call
        # `load_verify_locations`/`load_default_certs` internally while
        # setting up the system trust store (observed: one or two `cafile=
        # None` calls before ours) -- assert OUR explicit call happened,
        # not that it was the only one.
        ctx.check(f"BRIDGE_CA_BUNDLE was loaded, got {calls}", "/fake/ca-bundle.pem" in calls)
    finally:
        ssl.SSLContext.load_verify_locations = real_load
        if old is None:
            os.environ.pop("BRIDGE_CA_BUNDLE", None)
        else:
            os.environ["BRIDGE_CA_BUNDLE"] = old


@test
def test_default_tls_context_ca_bundle_load_failure_falls_through_not_raises(ctx: Ctx):
    from rolo_claude.providers.http import default_tls_context
    old = os.environ.get("BRIDGE_CA_BUNDLE")
    os.environ["BRIDGE_CA_BUNDLE"] = "/definitely/does/not/exist.pem"
    try:
        ctx_obj = default_tls_context()  # must not raise even though the path is bogus
        ctx.check("returns a real SSLContext despite a bad CA bundle path", isinstance(ctx_obj, ssl.SSLContext))
    finally:
        if old is None:
            os.environ.pop("BRIDGE_CA_BUNDLE", None)
        else:
            os.environ["BRIDGE_CA_BUNDLE"] = old


@test
def test_open_upstream_builds_its_tls_context_through_the_helper(ctx: Ctx):
    import rolo_claude.providers.http as http_mod
    calls = []
    real = http_mod.default_tls_context

    def _tripwire():
        calls.append(True)
        return real()

    http_mod.default_tls_context = _tripwire
    try:
        try:
            http_mod.open_upstream("totally-unresolvable-host.invalid", 443, True, connect_timeout=2)
        except http_mod.UpstreamConnectError:
            pass  # expected -- the host doesn't resolve; we only care that the context was built
        ctx.check("open_upstream obtained its TLS context via default_tls_context()", calls == [True])
    finally:
        http_mod.default_tls_context = real


@test
def test_open_upstream_skips_tls_context_for_plain_http(ctx: Ctx):
    import rolo_claude.providers.http as http_mod
    calls = []
    real = http_mod.default_tls_context

    def _tripwire():
        calls.append(True)
        return real()

    http_mod.default_tls_context = _tripwire
    try:
        try:
            http_mod.open_upstream("totally-unresolvable-host.invalid", 80, False, connect_timeout=2)
        except http_mod.UpstreamConnectError:
            pass
        ctx.check("no TLS context built for a plain (non-TLS) connection", calls == [])
    finally:
        http_mod.default_tls_context = real


def _assert_module_uses_urlopen_tls(ctx: Ctx, module_name: str, call, *, arg_check=None) -> None:
    """Monkeypatches `<module_name>`'s own `urlopen_tls` (imported locally,
    at call time, inside each real call site -- so patching the SOURCE
    module's attribute is exactly what a real call sees) and asserts `call`
    reaches it, never a direct `urllib.request.urlopen`."""
    import importlib
    http_mod = importlib.import_module("rolo_claude.providers.http")
    seen = []
    real = http_mod.urlopen_tls

    class _FakeResp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake(req, timeout=None):
        seen.append((req, timeout))
        return _FakeResp()

    http_mod.urlopen_tls = _fake
    try:
        call()
        ctx.check(f"{module_name} routed its HTTPS call through urlopen_tls, got {len(seen)} call(s)",
                  len(seen) == 1)
        if arg_check is not None and seen:
            arg_check(seen[0])
    finally:
        http_mod.urlopen_tls = real


@test
def test_linux_fixes_fetch_json_uses_urlopen_tls(ctx: Ctx):
    from rolo_claude import linux_fixes
    _assert_module_uses_urlopen_tls(ctx, "linux_fixes._default_fetch_json",
                                    lambda: linux_fixes._default_fetch_json("https://example.invalid/release.json"))


@test
def test_linux_fixes_fetch_bytes_uses_urlopen_tls(ctx: Ctx):
    import importlib
    http_mod = importlib.import_module("rolo_claude.providers.http")
    from rolo_claude import linux_fixes
    seen = []
    real = http_mod.urlopen_tls

    class _FakeResp:
        def read(self):
            return b"binary-data"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake(req, timeout=None):
        seen.append((req, timeout))
        return _FakeResp()

    http_mod.urlopen_tls = _fake
    try:
        out = linux_fixes._default_fetch_bytes("https://example.invalid/rg.tar.gz")
        ctx.check(f"routed through urlopen_tls, got {len(seen)} call(s)", len(seen) == 1)
        ctx.check(f"returns the fetched bytes, got {out!r}", out == b"binary-data")
    finally:
        http_mod.urlopen_tls = real


@test
def test_team_config_read_source_uses_urlopen_tls_for_a_url(ctx: Ctx):
    from rolo_claude import team_config
    _assert_module_uses_urlopen_tls(ctx, "team_config._read_source",
                                    lambda: team_config._read_source("https://example.invalid/team.json"))


@test
def test_team_config_read_source_local_file_never_touches_urlopen_tls(ctx: Ctx):
    import importlib
    http_mod = importlib.import_module("rolo_claude.providers.http")
    from rolo_claude import team_config
    calls = []
    real = http_mod.urlopen_tls
    http_mod.urlopen_tls = lambda *a, **kw: calls.append(1)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "team.json"
            p.write_text('{"host": "https://x"}', encoding="utf-8")
            text = team_config._read_source(str(p))
            ctx.check(f"local file read directly, got {text!r}", "host" in text)
            ctx.check("never called urlopen_tls for a local path", calls == [])
    finally:
        http_mod.urlopen_tls = real


@test
def test_websearch_uses_urlopen_tls(ctx: Ctx):
    from rolo_claude.tools.websearch import WebSearchTool

    def _call():
        tool = WebSearchTool(base_url="https://example.invalid", api_key="k", model="m")
        tool.run({"query": "test query"}, None)  # fake response isn't a real answer shape -- transport is what matters

    _assert_module_uses_urlopen_tls(ctx, "tools.websearch.WebSearchTool.run", _call)


@test
def test_hooks_http_hook_uses_urlopen_tls(ctx: Ctx):
    from rolo_claude.hooks import HookDef, run_http_hook

    def _call():
        hook = HookDef(type="http", url="https://example.invalid/hook", headers={}, allowed_env_vars=[])
        run_http_hook(hook, {"event": "test"}, timeout_s=2.0, env={})

    _assert_module_uses_urlopen_tls(ctx, "hooks.run_http_hook", _call)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
