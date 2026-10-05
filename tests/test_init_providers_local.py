"""tests.test_init_providers_local -- Halo 2.0.3 round 5 (brief item 5):
`halo init`'s new Ollama and Hugging Face tabs -- `init_providers.py`'s
TAB_PROVIDERS/TAB_LABEL registration, `tab_credential_state`/`save_tab_
credentials`/`refresh_tab_catalog`/`model_entries_for_provider` for both,
and a generalized "Save must work for every TAB_PROVIDERS entry" guard
(the round 4 worker's own fix-pass note: "an unwired tab would fail on
Save").
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

_ENV_NAMES = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "HF_TOKEN", "OLLAMA_HOST",
              "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "ANTHROPIC_API_KEY",
              "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS")


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_NAMES}
        d = Path(tempfile.mkdtemp(prefix="init-local-tabs-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "env-file")
        for k in ("HF_TOKEN", "OLLAMA_HOST", "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home, self.state_dir = d, d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- registration -----------------------------------------------------------

@test
def test_ollama_and_huggingface_are_registered_tabs(ctx: Ctx):
    from halo_harness.init_providers import PROVIDERS, TAB_LABEL, TAB_PROVIDERS
    ctx.check(f"both in TAB_PROVIDERS, got {TAB_PROVIDERS}",
              "ollama" in TAB_PROVIDERS and "huggingface" in TAB_PROVIDERS)
    ctx.check(f"ollama labeled, got {TAB_LABEL.get('ollama')!r}", TAB_LABEL.get("ollama") == "Ollama (local or LAN)")
    ctx.check(f"huggingface labeled, got {TAB_LABEL.get('huggingface')!r}", TAB_LABEL.get("huggingface") == "Hugging Face")
    ctx.check("neither joins the OLD sequential --provider/--preset picker's own PROVIDERS tuple "
              "(no sensible hardcoded default model for either -- see init_providers.py's own comment)",
              "ollama" not in PROVIDERS and "huggingface" not in PROVIDERS)


# ---- Ollama tab: one path (add a host) --------------------------------------

@test
def test_ollama_tab_state_blank_then_configured_after_save(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials, tab_credential_state
    with _Env():
        state = tab_credential_state("ollama")
        ctx.check(f"not configured yet, got {state}", state["configured"] is False)
        ctx.check("fields offered: name/url/api_key",
                  {f["name"] for f in state["fields"]} == {"name", "url", "api_key"})
        ok, msg = save_tab_credentials("ollama", {"name": "lan-box", "url": "my-box:11434", "api_key": "cloud-key"})
        ctx.check(f"Save succeeds, got {(ok, msg)}", ok is True)
        from halo_harness.providers.ollama import resolve_ollama_hosts
        hosts = resolve_ollama_hosts({})
        ctx.check(f"exactly the one host now configured, got {hosts}",
                  len(hosts) == 1 and hosts[0].name == "lan-box")
        ctx.check(f"URL normalized with a scheme, got {hosts[0].url!r}", hosts[0].url == "http://my-box:11434")
        ctx.check(f"api_key stored, got {hosts[0].api_key!r}", hosts[0].api_key == "cloud-key")
        state2 = tab_credential_state("ollama")
        ctx.check(f"now configured, got {state2['configured']}", state2["configured"] is True)
        ctx.check("fields STAY VISIBLE (additive tab, unlike every other provider above)",
                  len(state2["fields"]) == 3)


@test
def test_ollama_tab_blank_save_registers_the_default_local_daemon(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    from halo_harness.providers.ollama import DEFAULT_OLLAMA_URL, resolve_ollama_hosts
    with _Env():
        ok, _msg = save_tab_credentials("ollama", {"name": "", "url": "", "api_key": ""})
        ctx.check("blank Save still succeeds (registers the default address)", ok is True)
        hosts = resolve_ollama_hosts({})
        ctx.check(f"the default local daemon address, got {hosts[0].url!r}", hosts[0].url == DEFAULT_OLLAMA_URL)
        ctx.check(f"name defaults to 'default', got {hosts[0].name!r}", hosts[0].name == "default")


@test
def test_ollama_tab_save_twice_same_name_overwrites_not_duplicates(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    from halo_harness.providers.ollama import resolve_ollama_hosts
    with _Env():
        save_tab_credentials("ollama", {"name": "lan", "url": "http://box-one:11434", "api_key": ""})
        save_tab_credentials("ollama", {"name": "lan", "url": "http://box-two:11434", "api_key": ""})
        hosts = resolve_ollama_hosts({})
        ctx.check(f"exactly one 'lan' entry, got {hosts}", len(hosts) == 1)
        ctx.check(f"the SECOND url wins, got {hosts[0].url!r}", hosts[0].url == "http://box-two:11434")


# ---- Hugging Face tab: three independent paths ------------------------------

@test
def test_hf_tab_token_only_path(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("huggingface", {"token": "hf-router-tok"})
        ctx.check(f"Save succeeds, got {(ok, msg)}", ok is True and "HF_TOKEN" in msg)
        ctx.check("the real process env now carries it (same-process callers see it immediately)",
                  os.environ.get("HF_TOKEN") == "hf-router-tok")


@test
def test_hf_tab_endpoint_only_path_needs_both_name_and_url(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    from halo_harness.providers.huggingface import resolve_huggingface_endpoints
    with _Env():
        ok0, _msg0 = save_tab_credentials("huggingface", {"endpoint_name": "prod"})  # URL missing
        ctx.check("name alone is not enough", ok0 is False)
        ok, msg = save_tab_credentials("huggingface", {"endpoint_name": "prod", "endpoint_url": "https://ep.example/v1",
                                                         "endpoint_token": "ep-tok"})
        ctx.check(f"Save succeeds, got {(ok, msg)}", ok is True)
        entries = resolve_huggingface_endpoints()
        ctx.check(f"one endpoint stored, got {entries}",
                  len(entries) == 1 and entries[0].name == "prod" and entries[0].token == "ep-tok")


@test
def test_hf_tab_local_server_only_path(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    from halo_harness.providers.huggingface import resolve_huggingface_local_servers
    with _Env():
        ok, msg = save_tab_credentials("huggingface", {"local_url": "http://127.0.0.1:8080/v1", "local_key": "k"})
        ctx.check(f"Save succeeds, got {(ok, msg)}", ok is True)
        servers = resolve_huggingface_local_servers()
        ctx.check(f"one local server stored, got {servers}",
                  len(servers) == 1 and servers[0].url == "http://127.0.0.1:8080/v1" and servers[0].api_key == "k")


@test
def test_hf_tab_all_blank_refuses_plainly(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("huggingface", {"token": "", "endpoint_name": "", "endpoint_url": "",
                                                         "endpoint_token": "", "local_url": "", "local_key": ""})
        ctx.check(f"refused, got {(ok, msg)}", ok is False and "auto-detection" in msg)


@test
def test_hf_tab_all_three_paths_at_once(ctx: Ctx):
    """Nothing stops the user from filling in all three in one Save."""
    from halo_harness.init_providers import save_tab_credentials
    from halo_harness.providers.huggingface import resolve_huggingface_endpoints, resolve_huggingface_local_servers
    with _Env():
        ok, msg = save_tab_credentials("huggingface", {
            "token": "tok", "endpoint_name": "prod", "endpoint_url": "https://ep.example/v1",
            "local_url": "http://127.0.0.1:8080/v1",
        })
        ctx.check(f"Save succeeds, mentions all three, got {(ok, msg)}",
                  ok is True and "HF_TOKEN" in msg and "endpoints" in msg and "local_servers" in msg)
        ctx.check("endpoint stored", len(resolve_huggingface_endpoints()) == 1)
        ctx.check("local server stored", len(resolve_huggingface_local_servers()) == 1)
        ctx.check("router token stored", os.environ.get("HF_TOKEN") == "tok")


@test
def test_hf_tab_state_reflects_whichever_sources_are_configured(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials, tab_credential_state
    with _Env():
        state0 = tab_credential_state("huggingface")
        ctx.check("not configured with nothing set", state0["configured"] is False)
        save_tab_credentials("huggingface", {"local_url": "http://127.0.0.1:8080/v1"})
        state1 = tab_credential_state("huggingface")
        ctx.check(f"configured via local server alone, got {state1}",
                  state1["configured"] is True and "local server" in state1["masked"])
        ctx.check("fields STAY VISIBLE (additive tab)", len(state1["fields"]) == 6)


# ---- refresh_tab_catalog / model_entries_for_provider -----------------------

@test
def test_refresh_tab_catalog_huggingface_noop_without_router_token(ctx: Ctx):
    from halo_harness.init_providers import refresh_tab_catalog
    with _Env():
        ctx.check("endpoint/local-only setup has no router catalog to refresh",
                  refresh_tab_catalog("huggingface") == (True, ""))


@test
def test_refresh_tab_catalog_ollama_is_a_noop(ctx: Ctx):
    from halo_harness.init_providers import refresh_tab_catalog
    with _Env():
        ctx.check("ollama has no persisted catalog of its own to cache here",
                  refresh_tab_catalog("ollama") == (True, ""))


@test
def test_model_entries_for_provider_huggingface_reads_the_cached_router_catalog(ctx: Ctx):
    from halo_harness.init_providers import model_entries_for_provider
    from halo_harness.providers.huggingface_catalog import write_hf_models_json
    with _Env() as e:
        write_hf_models_json(e.state_dir, [{"id": "org/model-a", "context_length": 8192}])
        entries = model_entries_for_provider("huggingface", e.state_dir)
        ctx.check(f"the cached router model is listed, got {entries}",
                  entries == [{"ref": "hf:org/model-a", "context_tokens": 8192,
                               "price_in_per_m": None, "price_out_per_m": None}])


# ---- generalized "Save must work for every path" guard --------------------

@test
def test_save_never_raises_for_any_registered_tab_with_every_field_blank(ctx: Ctx):
    """The round 4 worker's own fix-pass note: "an unwired tab would fail
    on Save." Generalized so a FUTURE tab added to TAB_PROVIDERS without a
    matching `save_tab_credentials` branch fails this test immediately
    instead of surfacing at review."""
    from halo_harness.init_providers import TAB_PROVIDERS, save_tab_credentials, tab_credential_state
    with _Env():
        for provider in TAB_PROVIDERS:
            if provider == "claude":
                continue  # its own save path spawns `claude auth status`; covered elsewhere
            state = tab_credential_state(provider)
            values = {f["name"]: "" for f in state["fields"]}
            try:
                ok, msg = save_tab_credentials(provider, values)
            except Exception as e:
                ctx.check(f"{provider}: Save must never raise, got {type(e).__name__}: {e}", False)
                continue
            ctx.check(f"{provider}: Save returns a (bool, str) pair, got {(ok, msg)!r}",
                      isinstance(ok, bool) and isinstance(msg, str))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
