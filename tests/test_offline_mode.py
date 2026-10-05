"""tests.test_offline_mode -- Halo 2.0.3 round 5e: enforced offline mode.
`providers.http`'s `_check_offline_allowed`/`open_upstream`/`urlopen_tls`
gate, the allow-list (`ollama.hosts`/`huggingface.local_servers`/the
managed-server registry), loopback still reachable under offline, the
status bar's "offline" chip, and an invariant test (test_privacy_scan.py's
own grep style) that every raw network-opening call in the tracked tree
goes through the one choke point or is a named, reasoned exception.

Hermetic throughout: every test scopes `BRIDGE_TEST_HOME`/`BRIDGE_STATE_DIR`
itself (`_fresh_state_dir`/`build_fake_home`, the same helpers every other
suite in this tree uses) and restores `HALO_OFFLINE` when it touches it --
never the real `~/.halo`.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _env(fn):
    """Snapshot/restore HALO_OFFLINE and BRIDGE_TEST_HOME/BRIDGE_STATE_DIR
    around one test -- the same per-test env-scoping convention every other
    suite here uses, applied to exactly the names this file touches."""
    import functools

    @functools.wraps(fn)
    def wrapper(ctx):
        keys = ("HALO_OFFLINE", "BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            return fn(ctx)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return wrapper


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

@test
@_env
def test_offline_off_by_default_never_blocks(ctx: Ctx):
    os.environ.pop("HALO_OFFLINE", None)
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-off-")))
    from halo_harness.providers.http import _check_offline_allowed
    _check_offline_allowed("example.com")  # must not raise


@test
@_env
def test_offline_on_blocks_a_remote_host_with_the_plain_sentence(ctx: Ctx):
    os.environ["HALO_OFFLINE"] = "1"
    from halo_harness.providers.http import OfflineBlocked, _check_offline_allowed
    try:
        _check_offline_allowed("example.com")
        ctx.check("a remote host must raise OfflineBlocked", False)
    except OfflineBlocked as e:
        ctx.check(f"message names the host, got {e!r}", str(e) == "offline mode: not connecting to example.com")


@test
@_env
def test_offline_on_allows_loopback(ctx: Ctx):
    os.environ["HALO_OFFLINE"] = "1"
    from halo_harness.providers.http import _check_offline_allowed
    for host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        _check_offline_allowed(host)  # must not raise


@test
@_env
def test_offline_on_allows_a_configured_ollama_host(ctx: Ctx):
    """A LAN Ollama host the user actually configured in ollama.hosts is
    allow-listed by hostname, even though it is neither loopback nor the
    default host -- privacy-scan-safe: a hostname, never a numeric
    private-range IP literal."""
    os.environ["HALO_OFFLINE"] = "1"
    fh_home = Path(tempfile.mkdtemp(prefix="offline-allow-"))
    os.environ["BRIDGE_TEST_HOME"] = str(fh_home)
    from halo_harness.theme import set_config_value
    set_config_value("ollama.hosts", [{"name": "gpubox", "url": "http://gpubox.example:11434"}])
    from halo_harness.providers.http import _check_offline_allowed
    _check_offline_allowed("gpubox.example")  # must not raise
    from halo_harness.providers.http import OfflineBlocked
    try:
        _check_offline_allowed("some-other-host.example")
        ctx.check("an UNCONFIGURED LAN-shaped host must still be refused", False)
    except OfflineBlocked:
        pass


@test
@_env
def test_offline_on_allows_a_configured_hf_local_server(ctx: Ctx):
    os.environ["HALO_OFFLINE"] = "1"
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-hf-")))
    from halo_harness.theme import set_config_value
    set_config_value("huggingface.local_servers", [{"name": "box", "url": "http://llm-box.example:8080"}])
    from halo_harness.providers.http import _check_offline_allowed
    _check_offline_allowed("llm-box.example")  # must not raise


@test
@_env
def test_offline_on_allows_the_managed_server_registry(ctx: Ctx):
    os.environ["HALO_OFFLINE"] = "1"
    state_dir = Path(tempfile.mkdtemp(prefix="offline-managed-"))
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    from halo_harness.providers.local_runtime import save_registry
    save_registry(state_dir, [{"model": "m", "runtime": "llama-server", "pid": 1, "port": 1234,
                                "base_url": "http://127.0.0.1:1234", "started": "now", "keep": False}])
    from halo_harness.providers.http import allowlisted_local_hosts
    ctx.check(f"127.0.0.1 is in the allow-list from the registry, got {allowlisted_local_hosts()}",
              "127.0.0.1" in allowlisted_local_hosts())


# ---------------------------------------------------------------------------
# open_upstream / urlopen_tls themselves
# ---------------------------------------------------------------------------

@test
@_env
def test_open_upstream_refuses_a_remote_host(ctx: Ctx):
    os.environ["HALO_OFFLINE"] = "1"
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-ou-")))
    from halo_harness.providers.http import OfflineBlocked, open_upstream
    try:
        open_upstream("example.com", 443, True)
        ctx.check("open_upstream must refuse a remote host while offline", False)
    except OfflineBlocked as e:
        ctx.check(f"is-a-UpstreamConnectError and names the host, got {e}",
                  "offline mode: not connecting to example.com" == str(e))


@test
@_env
def test_open_upstream_still_reaches_a_real_loopback_server(ctx: Ctx):
    """The allow-list point of the brief, proven against a REAL socket: a
    loopback listener is reachable even with offline mode on."""
    os.environ["HALO_OFFLINE"] = "1"
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-loop-")))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    accepted = []

    def _accept_once():
        try:
            conn, _ = srv.accept()
            accepted.append(conn)
        except Exception:
            pass

    t = threading.Thread(target=_accept_once, daemon=True)
    t.start()
    from halo_harness.providers.http import open_upstream
    try:
        conn = open_upstream("127.0.0.1", port, False, connect_timeout=5)
        t.join(timeout=5)
        ctx.check("a real TCP connection to loopback was accepted", bool(accepted))
        conn.close()
    finally:
        for c in accepted:
            try:
                c.close()
            except Exception:
                pass
        srv.close()


@test
@_env
def test_urlopen_tls_refuses_a_remote_host_given_a_bare_string(ctx: Ctx):
    """team_config.py's own caller passes a bare URL STRING, never a
    Request object -- the host-extraction fallback (`req if not hasattr
    (req, "full_url")`) must still work, not silently skip the gate."""
    os.environ["HALO_OFFLINE"] = "1"
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-urlopen-")))
    from halo_harness.providers.http import OfflineBlocked, urlopen_tls
    try:
        urlopen_tls("https://example.com/team.json", timeout=1)
        ctx.check("urlopen_tls must refuse a remote host given a bare string", False)
    except OfflineBlocked as e:
        ctx.check(f"names the real host, got {e}", "example.com" in str(e))


@test
@_env
def test_urlopen_tls_refuses_a_remote_host_given_a_request_object(ctx: Ctx):
    os.environ["HALO_OFFLINE"] = "1"
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-urlopen2-")))
    import urllib.request
    from halo_harness.providers.http import OfflineBlocked, urlopen_tls
    req = urllib.request.Request("https://api.github.com/x", headers={"User-Agent": "halo"})
    try:
        urlopen_tls(req, timeout=1)
        ctx.check("urlopen_tls must refuse a remote Request object while offline", False)
    except OfflineBlocked as e:
        ctx.check(f"names the real host, got {e}", "api.github.com" in str(e))


@test
def test_is_offline_refusal_message_never_matches_a_connect_failure_message(ctx: Ctx):
    from halo_harness.providers.http import (
        format_connect_error, format_offline_refusal, is_connect_failure_message, is_offline_refusal_message,
    )
    connect_msg = format_connect_error("example.com", "boom")
    offline_msg = format_offline_refusal("example.com")
    ctx.check("a connect failure is never mistaken for an offline refusal",
              not is_offline_refusal_message(connect_msg))
    ctx.check("an offline refusal is never mistaken for a connect failure",
              not is_connect_failure_message(offline_msg))
    ctx.check("each marker matches its own message", is_connect_failure_message(connect_msg)
              and is_offline_refusal_message(offline_msg))


# ---------------------------------------------------------------------------
# The status bar's "offline" chip
# ---------------------------------------------------------------------------

@test
def test_status_bar_offline_chip(ctx: Ctx):
    """`StatusBar` is a real Textual `Static` widget -- `_refresh_display`'s
    own `self.update(text)` needs a mounted App (`textual._context.
    NoActiveAppError` otherwise, the same reason every OTHER test in this
    tree that touches the status bar uses a `_FakeStatusBar` double instead
    of the real widget). Rather than skip the real rendering logic
    entirely, `Static.update` itself is patched to just CAPTURE its `text`
    argument -- `_refresh_display`'s own Text-building (offline_str/
    saved-suffix included) still runs for real; only the actual widget
    paint is skipped."""
    from textual.widgets import Static
    from halo_harness.tui.widgets.statusbar import StatusBar
    captured = []
    original = Static.update
    Static.update = lambda self, content="": captured.append(content)
    try:
        bar = StatusBar(cwd="")
        ctx.check("offline defaults to False", bar.offline is False)
        ctx.check("no 'offline' chip rendered while off", "offline" not in captured[-1].plain)
        bar.set_offline(True)
        ctx.check("set_offline(True) takes effect", bar.offline is True)
        ctx.check(f"the 'offline' chip is rendered, got {captured[-1].plain!r}", "offline" in captured[-1].plain)
        bar.set_offline(False)
        ctx.check("chip disappears again", "offline" not in captured[-1].plain)
        bar.apply_status({"saved_usd": 0.0042})
        ctx.check(f"apply_status picks up saved_usd, got {bar.saved_usd}", bar.saved_usd == 0.0042)
        ctx.check(f"the chip shows the saved figure, got {captured[-1].plain!r}",
                  "saved $0.0042" in captured[-1].plain)
        bar.apply_status({})
        ctx.check("a status dict with no saved_usd key never blanks out a real reading", bar.saved_usd == 0.0042)
    finally:
        Static.update = original


# ---------------------------------------------------------------------------
# Invariant: every raw network-opening call in the tracked tree goes through
# the choke point, or is one of these named, reasoned exceptions.
# ---------------------------------------------------------------------------

# Each pattern is a raw primitive capable of opening a socket OUTSIDE
# providers.http's own open_upstream/urlopen_tls. (file, reason):
_ALLOWED_RAW_NETWORK_CALLERS = {
    "halo_harness/providers/http.py":
        "the choke point itself -- open_upstream/urlopen_tls/default_tls_context are DEFINED here using "
        "these primitives; _check_offline_allowed runs inside both before either is ever used",
    "halo_harness/tools/webfetch.py":
        "needs a same-host-redirect handler urlopen_tls doesn't offer; calls providers.http."
        "_check_offline_allowed directly, right before opening, so it is still gated",
    "halo_harness/mcp/oauth.py":
        "MCP SERVER OAuth discovery/token exchange -- a user-configured MCP server's own endpoint "
        "(which may itself be local), a separate concern from the model-provider/update/catalog network "
        "surface this round's brief enumerates; not wired to the offline gate this round",
}

_RAW_NETWORK_RE = re.compile(
    r"urllib\.request\.urlopen\(|http\.client\.HTTPSConnection\(|http\.client\.HTTPConnection\("
    r"|socket\.create_connection\(|urllib\.request\.build_opener\("
    r"|(?:^|[^.\w])urlopen\("
)


def _tracked_py_files() -> "list[Path]":
    try:
        out = subprocess.run(["git", "ls-files", "halo_harness"], capture_output=True, text=True,
                              cwd=str(REPO_DIR), check=True)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "halo_harness"],
                                    capture_output=True, text=True, cwd=str(REPO_DIR), check=True)
        lines = list(dict.fromkeys(out.stdout.splitlines() + untracked.stdout.splitlines()))
        return [REPO_DIR / p for p in lines if p.endswith(".py")]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in (REPO_DIR / "halo_harness").rglob("*.py") if "__pycache__" not in p.parts]


@test
def test_every_raw_network_call_is_the_choke_point_or_a_named_exception(ctx: Ctx):
    problems = []
    for path in _tracked_py_files():
        try:
            rel = path.resolve().relative_to(REPO_DIR).as_posix()
        except ValueError:
            continue
        if rel in _ALLOWED_RAW_NETWORK_CALLERS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _RAW_NETWORK_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{rel}:{line_no}: raw network call {m.group(0)!r} outside the choke point "
                             f"and not in _ALLOWED_RAW_NETWORK_CALLERS")
    ctx.check("no un-allowlisted raw network call found:\n  " + "\n  ".join(problems), not problems)


@test
def test_allowed_raw_network_callers_list_is_still_accurate(ctx: Ctx):
    """The companion direction: every file in the allow-list must still
    actually contain a raw network call -- an allow-list entry nobody
    needs any more is exactly the kind of stale carve-out that quietly
    hides a REAL gap reappearing somewhere else later."""
    problems = []
    for rel in _ALLOWED_RAW_NETWORK_CALLERS:
        path = REPO_DIR / rel
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if not _RAW_NETWORK_RE.search(text):
            problems.append(f"{rel}: allow-listed but no raw network call found any more -- remove the entry")
    ctx.check("every allow-list entry is still needed:\n  " + "\n  ".join(problems), not problems)


# ---------------------------------------------------------------------------
# /offline (commands.builtins._cmd_offline)
# ---------------------------------------------------------------------------

@test
@_env
def test_cmd_offline_bare_reports_state_and_on_off_persists(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_offline
    os.environ.pop("HALO_OFFLINE", None)
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="offline-cmd-")))
    facade = HeadlessFacade(cwd=REPO_DIR)
    bare = _cmd_offline("", facade)
    ctx.check(f"bare reports off by default, got {bare!r}", "Offline mode: off" in bare)
    on = _cmd_offline("on", facade)
    ctx.check(f"on acknowledges, got {on!r}", "Offline mode: on" in on)
    from halo_harness.providers.http import offline_mode_enabled
    ctx.check("offline_mode_enabled() reflects it immediately, same process", offline_mode_enabled() is True)
    off = _cmd_offline("off", facade)
    ctx.check(f"off acknowledges, got {off!r}", "Offline mode: off" in off)
    ctx.check("offline_mode_enabled() reflects the toggle back off", offline_mode_enabled() is False)
    bad = _cmd_offline("sideways", facade)
    ctx.check(f"a bad argument gets a usage line, got {bad!r}", bad == "Usage: /offline [on|off]")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
