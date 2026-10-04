"""tests.test_providers_ollama_catalog -- Halo 2.0.3 round 2: `ollama.hosts`
config resolution (incl. `OLLAMA_HOST` seeding the default), the background
`/api/version` enablement probe (honouring `BRIDGE_TEST_NO_BACKGROUND_NET`),
the per-host `/api/tags` + `/api/show` catalog cache (short TTL), and the
capability probe's durable per-digest cache. Request-builder/decoder
pinning tests are in tests/test_providers_ollama.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- host config resolution -------------------------------------------------

@test
def test_resolve_hosts_default_when_nothing_configured(ctx: Ctx):
    from halo_harness.providers.ollama import DEFAULT_OLLAMA_URL, resolve_ollama_hosts
    _fresh_state_dir("ol-hosts-default-")
    try:
        hosts = resolve_ollama_hosts(env={})
        ctx.check("exactly one synthesized default host", len(hosts) == 1)
        ctx.check(f"127.0.0.1:11434, got {hosts[0].url}", hosts[0].url == DEFAULT_OLLAMA_URL)
        ctx.check("marked default", hosts[0].default is True)
    finally:
        _clear_state_dir_env()


@test
def test_ollama_host_env_seeds_the_default_host(ctx: Ctx):
    from halo_harness.providers.ollama import resolve_ollama_hosts
    _fresh_state_dir("ol-hosts-env-")
    try:
        hosts = resolve_ollama_hosts(env={"OLLAMA_HOST": "0.0.0.0:11434"})
        ctx.check(f"normalized to a full URL, got {hosts[0].url}", hosts[0].url == "http://0.0.0.0:11434")
        hosts2 = resolve_ollama_hosts(env={"OLLAMA_HOST": "https://ollama.com", "OLLAMA_API_KEY": "k-123"})
        ctx.check("a scheme already present is kept as-is", hosts2[0].url == "https://ollama.com")
        ctx.check("OLLAMA_API_KEY seeds the one synthesized host's api_key", hosts2[0].api_key == "k-123")
    finally:
        _clear_state_dir_env()


@test
def test_configured_hosts_list_and_named_selection(ctx: Ctx):
    """`ol:<model>@<hostname>` (model.py) resolves through exactly this --
    a LAN host and an Ollama Cloud entry addressed by name, one default."""
    from halo_harness.providers.ollama import resolve_ollama_host, resolve_ollama_hosts
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-hosts-configured-")
    try:
        set_config_value("ollama.hosts", [
            {"name": "lan", "url": "127.0.0.1:11434", "max_ctx": 32768, "keep_alive": "10m"},
            {"name": "cloud", "url": "https://ollama.com", "api_key": "fake-cloud-key", "default": True},
        ])
        hosts = resolve_ollama_hosts()
        ctx.check("both entries present", {h.name for h in hosts} == {"lan", "cloud"})
        ctx.check("lan host normalized (bare host:port -> http://)",
                  [h for h in hosts if h.name == "lan"][0].url == "http://127.0.0.1:11434")
        ctx.check("named lookup: lan", resolve_ollama_host("lan").max_ctx == 32768)
        ctx.check("named lookup is case-insensitive", resolve_ollama_host("LAN").name == "lan")
        ctx.check("no name -> the entry marked default", resolve_ollama_host().name == "cloud")
        ctx.check("unknown name -> None (never silently falls back)", resolve_ollama_host("nope") is None)
        ctx.check("cloud host's api_key never leaks onto the lan entry",
                  [h for h in hosts if h.name == "lan"][0].api_key is None)
    finally:
        _clear_state_dir_env()


@test
def test_configured_host_parses_round_5b_kv_cache_type_and_ssh(ctx: Ctx):
    """`ollama.hosts[].kv_cache_type`/`ollama.hosts[].ssh` (round 5b,
    docs/CONFIG.md) round-trip through the SAME config parsing every
    other per-host field already does -- never a second mechanism."""
    from halo_harness.providers.ollama import resolve_ollama_host
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-hosts-5b-fields-")
    try:
        set_config_value("ollama.hosts", [
            {"name": "tuned", "url": "127.0.0.1:11434", "kv_cache_type": "q4_0", "ssh": "user@gpu-box"},
            {"name": "plain", "url": "127.0.0.1:11435", "default": True},
        ])
        tuned = resolve_ollama_host("tuned")
        ctx.check(f"kv_cache_type parsed, got {tuned.kv_cache_type!r}", tuned.kv_cache_type == "q4_0")
        ctx.check(f"ssh parsed, got {tuned.ssh!r}", tuned.ssh == "user@gpu-box")
        plain = resolve_ollama_host("plain")
        ctx.check("neither field set on an entry that doesn't configure them",
                  plain.kv_cache_type is None and plain.ssh is None)
    finally:
        _clear_state_dir_env()


@test
def test_malformed_host_entry_skipped_not_crashed(ctx: Ctx):
    from halo_harness.providers.ollama import resolve_ollama_hosts
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-hosts-malformed-")
    try:
        set_config_value("ollama.hosts", [{"name": "no-url-here"}, "not even a dict"])
        hosts = resolve_ollama_hosts(env={})
        ctx.check("falls back to the synthesized default, never raises", len(hosts) == 1 and hosts[0].default)
    finally:
        _clear_state_dir_env()


# ---- enablement probe: background GET /api/version -------------------------

@test
def test_probe_version_reachable_and_unreachable(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost, probe_version
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        result = probe_version(host)
        ctx.check(f"reachable host answers, got {result}", result == {"version": "0.1.0-mock"})
    dead_host = OllamaHost(name="dead", url="http://127.0.0.1:1")
    ctx.check("unreachable host -> None, never raises", probe_version(dead_host, timeout=0.5) is None)


@test
def test_probe_hosts_background_honours_no_background_net(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost, probe_hosts_background
    saved = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
    try:
        with MockUpstream() as mock:
            host = OllamaHost(name="default", url=mock.base_url)
            results = []
            probe_hosts_background([host], on_result=lambda h, r: results.append(r))
            time.sleep(0.2)
            ctx.check("never touches the network under the flag -- on_result never fires",
                      results == [])
    finally:
        if saved is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = saved


@test
def test_probe_hosts_background_runs_for_real_when_allowed(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost, probe_hosts_background
    saved = os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
    try:
        with MockUpstream() as mock:
            host = OllamaHost(name="default", url=mock.base_url)
            done = threading.Event()
            results = []

            def on_result(h, r):
                results.append(r)
                done.set()

            probe_hosts_background([host], on_result=on_result)
            ctx.check("the background probe actually completed", done.wait(timeout=5))
            ctx.check(f"handed the real /api/version body back, got {results}",
                      results and results[0] == {"version": "0.1.0-mock"})
    finally:
        if saved is not None:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = saved


# ---- catalog: /api/tags + /api/show, cached per host with a short TTL -----

@test
def test_get_catalog_merges_tags_and_show(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost, get_catalog, reset_catalog_cache, trained_context_for
    reset_catalog_cache()
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        catalog = get_catalog(host)
        names = sorted(m["model"] for m in catalog["models"])
        ctx.check(f"both tagged models present, got {names}", names == ["gpt-oss:20b", "qwen3:30b"])
        ctx.check("trained context from /api/show's model_info, via /api/tags' details.family",
                  trained_context_for(catalog, "qwen3:30b") == 40960)
        ctx.check("capabilities merged in from /api/show",
                  "tools" in next(m["capabilities"] for m in catalog["models"] if m["model"] == "qwen3:30b"))


@test
def test_get_catalog_cached_within_ttl_force_refreshes(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost, get_catalog, reset_catalog_cache
    reset_catalog_cache()
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        get_catalog(host, ttl_s=30.0)
        mock.clear()
        get_catalog(host, ttl_s=30.0)
        ctx.check("a second read within the TTL window never re-hits the host",
                  len(mock.requests) == 0)
        get_catalog(host, ttl_s=30.0, force=True)
        ctx.check("force=True always re-fetches", len(mock.requests) > 0)
    reset_catalog_cache()


@test
def test_get_catalog_unreachable_host_keeps_last_good_snapshot(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost, get_catalog, reset_catalog_cache
    reset_catalog_cache()
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        first = get_catalog(host, ttl_s=0.0)  # ttl 0 -> every call re-fetches
    # host is gone now (the `with` block exited) -- a forced re-fetch must
    # keep serving the last good snapshot instead of flashing an empty one.
    second = get_catalog(host, ttl_s=0.0)
    ctx.check("unreachable host still returns the last good model list",
              second["models"] == first["models"] and len(second["models"]) == 2)
    reset_catalog_cache()


# ---- capability probe: cached per digest, honours BRIDGE_TEST_NO_BACKGROUND_NET

@test
def test_capability_probe_cached_per_digest(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_capability import load_capability_cache, probe_tool_capability
    state_dir = _fresh_state_dir("ol-capability-")
    # This test exercises the REAL (non-cached) probe path on a cache miss,
    # which is exactly what BRIDGE_TEST_NO_BACKGROUND_NET=1 (the suite-wide
    # default, tests.helpers.provider_env_defaults) suppresses -- lifted for
    # just this test, restored in `finally` either way.
    saved_no_net = os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
    try:
        with MockUpstream() as mock:
            host = OllamaHost(name="default", url=mock.base_url)
            calls = []

            def prober(h, model, timeout):
                calls.append(model)
                return model == "qwen3:30b"

            r1 = probe_tool_capability(host, "qwen3:30b", "sha256:abc", state_dir, prober=prober)
            ctx.check("probed once for a new digest", r1 is True and calls == ["qwen3:30b"])

            # Same digest again -- round 2 pinning requirement: "not re-sent
            # on a second catalog read within the TTL" -- a cache HIT never
            # calls the prober at all, regardless of how soon it's asked again.
            r2 = probe_tool_capability(host, "qwen3:30b", "sha256:abc", state_dir, prober=prober)
            ctx.check(f"cache hit -- prober NOT called a second time, calls={calls}",
                      r2 is True and calls == ["qwen3:30b"])

            # A DIFFERENT digest (the model was re-pulled) probes again.
            r3 = probe_tool_capability(host, "qwen3:30b", "sha256:def-new-pull", state_dir, prober=prober)
            ctx.check("a new digest under the same model name probes again",
                      r3 is True and calls == ["qwen3:30b", "qwen3:30b"])

            cache = load_capability_cache(state_dir)
            ctx.check("both digests persisted", set(cache.keys()) == {"sha256:abc", "sha256:def-new-pull"})
    finally:
        _clear_state_dir_env()
        if saved_no_net is not None:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = saved_no_net


@test
def test_capability_probe_unknown_under_no_background_net(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_capability import probe_tool_capability
    state_dir = _fresh_state_dir("ol-capability-nonet-")
    saved = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
    try:
        host = OllamaHost(name="default", url="http://127.0.0.1:1")

        def boom(*a, **k):
            raise AssertionError("must never be called under BRIDGE_TEST_NO_BACKGROUND_NET")

        result = probe_tool_capability(host, "qwen3:30b", "sha256:never-probed", state_dir, prober=boom)
        ctx.check("unknown (None), never False, on a cache miss under the flag", result is None)
    finally:
        _clear_state_dir_env()
        if saved is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = saved


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
