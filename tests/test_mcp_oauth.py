"""tests.test_mcp_oauth -- halo_harness/mcp/oauth.py (Halo 2.0.1 gap-list
brief, W4b item 2): the generic OAuth authorization-code (+ PKCE) flow,
tested only against tests/helpers/fake_oauth_server.py (never any real
vendor). Pins: endpoint discovery (explicit config and RFC 8414 metadata),
the full round trip (including REAL PKCE verification on the fake
server's own side), denial/CSRF/timeout failure paths, and token storage
under ~/.halo/mcp/oauth/ -- never Claude's own files.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.fake_oauth_server import FakeOAuthServer
from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import oauth

test, TESTS = new_registry()


def _with_scoped_home(fn):
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            return fn(Path(td))
        finally:
            if old is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old


def _run_flow_against(server: FakeOAuthServer, *, oauth_cfg: dict, deny: bool = False, timeout: float = 10.0):
    """Drives `run_authorization_flow` on a background thread (it blocks
    on the local callback) while the main thread plays "the browser": a
    plain `urlopen` of the printed authorize URL follows the fake
    server's 302 straight to halo's own local callback -- no real browser
    or mocking needed. Returns `(tokens, err, printed_lines)`."""
    printed: list = []
    result: list = []

    def _runner() -> None:
        tokens, err = oauth.run_authorization_flow(
            server_name="fake-server", server_url=server.base_url, oauth_cfg=oauth_cfg,
            open_browser=False, callback_timeout=timeout, print_fn=printed.append)
        result.append((tokens, err))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(printed) < 2:
        time.sleep(0.02)
    assert len(printed) >= 2, f"authorize URL never printed, got {printed!r}"
    authorize_url = printed[1].strip()
    if deny:
        authorize_url += "&deny=1"
    urlopen(authorize_url, timeout=5.0).read()
    t.join(timeout=timeout + 5.0)
    assert result, "run_authorization_flow never returned"
    tokens, err = result[0]
    return tokens, err, printed


@test
def test_explicit_endpoints_skip_discovery(ctx: Ctx):
    cfg = {"authorization_endpoint": "https://x/authorize", "token_endpoint": "https://x/token"}
    auth_ep, token_ep, err = oauth.discover_endpoints("https://x/mcp", cfg)
    ctx.check("explicit endpoints returned verbatim", (auth_ep, token_ep) == (cfg["authorization_endpoint"], cfg["token_endpoint"]))
    ctx.check("no error", err is None)


@test
def test_metadata_discovery_from_well_known(ctx: Ctx):
    server = FakeOAuthServer()
    server.start()
    try:
        auth_ep, token_ep, err = oauth.discover_endpoints(server.base_url + "/mcp", {})
        ctx.check(f"discovered authorize endpoint, got {auth_ep!r}", auth_ep == server.base_url + "/authorize")
        ctx.check(f"discovered token endpoint, got {token_ep!r}", token_ep == server.base_url + "/token")
        ctx.check("no error", err is None)
    finally:
        server.stop()


@test
def test_full_round_trip_with_real_pkce_verification(ctx: Ctx):
    server = FakeOAuthServer()
    server.start()
    try:
        tokens, err, _printed = _run_flow_against(
            server, oauth_cfg={"authorization_endpoint": server.base_url + "/authorize",
                                "token_endpoint": server.base_url + "/token", "client_id": "halo-test-client"})
        ctx.check(f"no error, got {err!r}", err is None)
        ctx.check(f"got the fake access token, got {tokens!r}", tokens.get("access_token") == "fake-access-token")
        ctx.check("refresh token present", tokens.get("refresh_token") == "fake-refresh-token")
        ctx.check("the server's own PKCE check actually ran (one token call recorded)", len(server.token_calls) == 1)
    finally:
        server.stop()


@test
def test_client_secret_is_sent_and_verified(ctx: Ctx):
    server = FakeOAuthServer(require_client_secret="s3cr3t")
    server.start()
    try:
        tokens, err, _ = _run_flow_against(
            server, oauth_cfg={"authorization_endpoint": server.base_url + "/authorize",
                                "token_endpoint": server.base_url + "/token", "client_id": "cid",
                                "client_secret": "s3cr3t"})
        ctx.check(f"no error with the right secret, got {err!r}", err is None)
        ctx.check("access token returned", tokens.get("access_token") == "fake-access-token")
    finally:
        server.stop()


@test
def test_wrong_client_secret_fails_cleanly(ctx: Ctx):
    server = FakeOAuthServer(require_client_secret="s3cr3t")
    server.start()
    try:
        tokens, err, _ = _run_flow_against(
            server, oauth_cfg={"authorization_endpoint": server.base_url + "/authorize",
                                "token_endpoint": server.base_url + "/token", "client_id": "cid",
                                "client_secret": "wrong"})
        ctx.check(f"no tokens, got {tokens!r}", tokens is None)
        ctx.check(f"a clean error naming the failure, got {err!r}", err is not None and "token exchange failed" in err)
    finally:
        server.stop()


@test
def test_user_denies_authorization(ctx: Ctx):
    server = FakeOAuthServer()
    server.start()
    try:
        tokens, err, _ = _run_flow_against(
            server, deny=True,
            oauth_cfg={"authorization_endpoint": server.base_url + "/authorize",
                       "token_endpoint": server.base_url + "/token", "client_id": "cid"})
        ctx.check(f"no tokens on denial, got {tokens!r}", tokens is None)
        ctx.check(f"error mentions the denial, got {err!r}", err is not None and "authorization failed" in err)
    finally:
        server.stop()


@test
def test_missing_endpoints_is_a_clean_error(ctx: Ctx):
    tokens, err = oauth.run_authorization_flow(server_name="x", server_url="", oauth_cfg={},
                                                 open_browser=False, callback_timeout=1.0, print_fn=lambda *_: None)
    ctx.check(f"no tokens, got {tokens!r}", tokens is None)
    ctx.check(f"a clean 'no endpoint' error, got {err!r}", err is not None and "endpoint" in err)


@test
def test_timeout_waiting_for_redirect_is_a_clean_error(ctx: Ctx):
    server = FakeOAuthServer()
    server.start()
    try:
        tokens, err = oauth.run_authorization_flow(
            server_name="x", server_url=server.base_url,
            oauth_cfg={"authorization_endpoint": server.base_url + "/authorize",
                       "token_endpoint": server.base_url + "/token", "client_id": "cid"},
            open_browser=False, callback_timeout=0.3, print_fn=lambda *_: None)
        ctx.check(f"no tokens, got {tokens!r}", tokens is None)
        ctx.check(f"a clean timeout error, got {err!r}", err is not None and "timed out" in err)
    finally:
        server.stop()


@test
def test_tokens_persist_under_halo_mcp_oauth_never_claude_files(ctx: Ctx):
    def _run(home: Path):
        path = oauth.save_tokens("my-remote", {"access_token": "abc", "refresh_token": "def"})
        ctx.check(f"stored under ~/.halo/mcp/oauth/, got {path}",
                  str(path).replace("\\", "/").endswith(".halo/mcp/oauth/my-remote.json"))
        ctx.check("never under a .claude path", ".claude" not in str(path))
        loaded = oauth.load_tokens("my-remote")
        ctx.check(f"round-trips, got {loaded!r}", loaded is not None and loaded.get("access_token") == "abc")
        ctx.check("clear_tokens reports success", oauth.clear_tokens("my-remote") is True)
        ctx.check("now gone", oauth.load_tokens("my-remote") is None)
        ctx.check("a second clear reports nothing to clear", oauth.clear_tokens("my-remote") is False)
    _with_scoped_home(_run)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
