"""tests.test_providers_huggingface_local_probe -- Halo 2.0.3 round 5:
`hf:local/*` auto-detection across several `tests.helpers.mock_openai.
MockUpstream` instances bound to test-chosen ports (never the real
default ports 8080/8000/1234 -- the `HF_LOCAL_PROBE_PORTS` override from
`providers.huggingface_local_probe.local_probe_ports`), the manual-entry
on-demand probe (bearer pinning), the context-length field-name
heuristic, and the `BRIDGE_TEST_NO_BACKGROUND_NET` gate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_openai import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


class _NoBackgroundNetGate:
    """Saves/clears/restores `BRIDGE_TEST_NO_BACKGROUND_NET` in the REAL
    process environment -- `providers.config.paths.background_net_
    disabled()` reads bare `os.environ` directly (never the `env` dict a
    caller passes for PORT overrides), so a module-level default this
    process already set (e.g. by an earlier-imported test module calling
    `tests.helpers.provider_env_defaults.ensure_default_provider_
    credentials`) must be explicitly cleared for the DURATION of a test
    that wants the real (mocked, loopback-only) probe to actually run."""

    def __enter__(self):
        self._saved = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        return self

    def __exit__(self, *exc):
        if self._saved is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = self._saved


# ---- local_probe_ports override --------------------------------------------

@test
def test_local_probe_ports_default_matches_research_doc(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import DEFAULT_LOCAL_PORTS, local_probe_ports
    ctx.check(f"8080/8000/1234, got {local_probe_ports({})}", local_probe_ports({}) == DEFAULT_LOCAL_PORTS)


@test
def test_local_probe_ports_env_override(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import local_probe_ports
    ports = local_probe_ports({"HF_LOCAL_PROBE_PORTS": "19001, 19002 ,19003"})
    ctx.check(f"parsed and trimmed, got {ports}", ports == (19001, 19002, 19003))


@test
def test_local_probe_ports_malformed_env_falls_back_to_defaults(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import DEFAULT_LOCAL_PORTS, local_probe_ports
    ctx.check("malformed override never raises, falls back",
              local_probe_ports({"HF_LOCAL_PROBE_PORTS": "not-a-port"}) == DEFAULT_LOCAL_PORTS)


@test
def test_local_probe_ports_config_override(ctx: Ctx):
    import tempfile
    from halo_harness.providers.huggingface_local_probe import local_probe_ports
    d = Path(tempfile.mkdtemp(prefix="hf-ports-cfg-"))
    saved_home, saved_state = os.environ.get("BRIDGE_TEST_HOME"), os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
    try:
        from halo_harness.theme import set_config_value
        set_config_value("huggingface.local_probe_ports", [19011, 19012])
        ctx.check(f"config list used when no env override, got {local_probe_ports({})}",
                  local_probe_ports({}) == (19011, 19012))
    finally:
        for k, v in (("BRIDGE_TEST_HOME", saved_home), ("BRIDGE_STATE_DIR", saved_state)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- context-length field-name heuristic (pure, no network) ---------------

@test
def test_extract_context_length_recognizes_each_documented_field_name(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import _extract_context_length
    for key in ("max_model_len", "context_length", "context_window", "n_ctx", "max_position_embeddings"):
        row = {"id": "m", key: 4096}
        ctx.check(f"{key} recognized, got {_extract_context_length(row)}", _extract_context_length(row) == 4096)
    ctx.check("no recognized field -> None", _extract_context_length({"id": "m"}) is None)
    ctx.check("a non-positive value is never trusted",
              _extract_context_length({"id": "m", "n_ctx": 0}) is None)


# ---- auto-detect across several mock ports at once -------------------------

@test
def test_auto_detect_finds_every_mock_bound_to_the_overridden_ports(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import auto_detect_local_servers
    mocks = [MockUpstream(path_prefix="/v1").start() for _ in range(3)]
    try:
        mocks[0].models_response = {"data": [{"id": "llama-server-model", "n_ctx": 8192}]}
        mocks[1].models_response = {"data": [{"id": "vllm-model", "max_model_len": 32768}]}
        mocks[2].models_response = {"data": [{"id": "lmstudio-model"}]}  # no context field at all
        ports = [m.port for m in mocks]
        env = {"HF_LOCAL_PROBE_PORTS": ",".join(str(p) for p in ports)}
        with _NoBackgroundNetGate():
            detected = auto_detect_local_servers(env=env)
        ctx.check(f"all three test-chosen ports answered, got {len(detected)}", len(detected) == 3)
        names = {d.name for d in detected}
        ctx.check(f"named auto:<port>, got {names}", names == {f"auto:{p}" for p in ports})
        by_model = {mid: d for d in detected for mid in d.model_ids}
        ctx.check("llama-server-model's n_ctx read back", by_model["llama-server-model"].context_by_model.get(
            "llama-server-model") == 8192)
        ctx.check("vllm-model's max_model_len read back",
                  by_model["vllm-model"].context_by_model.get("vllm-model") == 32768)
        ctx.check("lmstudio-model with no field reported stays unknown (never guessed)",
                  "lmstudio-model" not in by_model["lmstudio-model"].context_by_model)
        for d in detected:
            ctx.check(f"auto-detected entries are never 'manual', got {d.manual!r}", d.manual is False)
    finally:
        for m in mocks:
            m.stop()


@test
def test_auto_detect_skips_a_port_with_nothing_listening(ctx: Ctx):
    from tests.helpers.mock_openai import free_port
    from halo_harness.providers.huggingface_local_probe import auto_detect_local_servers
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        mock.models_response = {"data": [{"id": "m"}]}
        dead_port = free_port()  # picked free, then deliberately never bound
        env = {"HF_LOCAL_PROBE_PORTS": f"{dead_port},{mock.port}"}
        with _NoBackgroundNetGate():
            detected = auto_detect_local_servers(env=env)
        ctx.check(f"only the real mock answered, got {[d.name for d in detected]}", len(detected) == 1)
        ctx.check(f"it's the mock's own port, got {detected[0].name!r}", detected[0].name == f"auto:{mock.port}")
    finally:
        mock.stop()


@test
def test_auto_detect_is_a_no_op_under_the_background_net_gate(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import auto_detect_local_servers
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        mock.models_response = {"data": [{"id": "m"}]}
        saved = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        try:
            detected = auto_detect_local_servers(env={"HF_LOCAL_PROBE_PORTS": str(mock.port)})
        finally:
            if saved is None:
                os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
            else:
                os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = saved
        ctx.check(f"gated to an empty list, got {detected}", detected == [])
        ctx.check(f"the mock never even received a request, got {len(mock.requests)}", len(mock.requests) == 0)
    finally:
        mock.stop()


# ---- manual entry on-demand probe: bearer pinning, never gated ------------

@test
def test_probe_manual_server_sends_its_own_bearer(ctx: Ctx):
    from halo_harness.providers.huggingface import HFLocalServer
    from halo_harness.providers.huggingface_local_probe import probe_manual_server
    mock = MockUpstream(path_prefix="/v1", expected_bearer="manual-entry-token").start()
    try:
        mock.models_response = {"data": [{"id": "m", "context_length": 2048}]}
        server = HFLocalServer(name="bench", url=mock.base_url, api_key="manual-entry-token")
        info = probe_manual_server(server)
        ctx.check("manual probe succeeds with the right bearer", info is not None)
        ctx.check(f"marked manual=True, got {info.manual!r}", info.manual is True)
        ctx.check(f"keeps the configured name, got {info.name!r}", info.name == "bench")
    finally:
        mock.stop()


@test
def test_probe_manual_server_wrong_key_fails_cleanly(ctx: Ctx):
    from halo_harness.providers.huggingface import HFLocalServer
    from halo_harness.providers.huggingface_local_probe import probe_manual_server
    mock = MockUpstream(path_prefix="/v1", expected_bearer="the-real-token").start()
    try:
        mock.models_response = {"data": [{"id": "m"}]}
        server = HFLocalServer(name="bench", url=mock.base_url, api_key="the-wrong-token")
        ctx.check("a 401 from the wrong key resolves to None, never a traceback",
                  probe_manual_server(server) is None)
    finally:
        mock.stop()


@test
def test_probe_manual_server_runs_even_under_the_background_net_gate(ctx: Ctx):
    """Brief item 2: "a manual entry is never probed in the background
    unless the user asks" -- this IS the user asking (`/local refresh`),
    so it must run regardless of `BRIDGE_TEST_NO_BACKGROUND_NET`."""
    from halo_harness.providers.huggingface import HFLocalServer
    from halo_harness.providers.huggingface_local_probe import probe_manual_server
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        mock.models_response = {"data": [{"id": "m"}]}
        saved = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        try:
            info = probe_manual_server(HFLocalServer(name="bench", url=mock.base_url))
        finally:
            if saved is None:
                os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
            else:
                os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = saved
        ctx.check("runs for real despite the gate", info is not None and info.model_ids == ("m",))
    finally:
        mock.stop()


@test
def test_probe_llama_server_props_degrades_to_none_on_404(ctx: Ctx):
    """`/props` isn't served by the plain MockUpstream fixture (no such
    route) -- the real, important contract to pin is that this NEVER
    raises and degrades to `None` rather than blocking `probe_models_
    endpoint` on an unconfirmed, best-effort endpoint."""
    from halo_harness.providers.huggingface_local_probe import probe_llama_server_props
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        ctx.check("a 404 /props degrades to None, never raises",
                  probe_llama_server_props(mock.base_url) is None)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
