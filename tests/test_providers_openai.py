"""tests.test_providers_openai -- Halo 2.0.3 round 5i part 1: `oai:` ref
parsing (incl. dialect selection + `openai.dialect_overrides`), credential
resolution, enablement, reachability, and `resolve_model_profile`'s
models.dev cross-check. Catalog probe/cache/TTL lives in tests/
test_providers_openai_catalog.py; the body builder/SSE decoder live in
tests/test_providers_openai_responses.py; the print-mode end-to-end runs
against the parameterized tests/helpers/mock_openai.py fake live in
tests/test_providers_openai_session.py.
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

_PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY", "HF_TOKEN", "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
    "BRIDGE_OPENAI_BASE_URL", "HALO_OPENAI_BASE_URL",
)


class _Env:
    """Same pattern as tests/test_providers_huggingface.py's own `_Env` --
    scopes BRIDGE_TEST_HOME/BRIDGE_STATE_DIR/BRIDGE_ENV_FILE to a fresh
    tempdir and snapshots/restores every env var this module touches, so
    the real `~/.halo` and a developer's own real `OPENAI_API_KEY` (if
    any) are never read or clobbered."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="oai-providers-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- ref parsing + dialect selection ----------------------------------------

@test
def test_oai_ref_parses_provider_and_bare_model(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _Env():
        ref = parse_model_ref("oai:gpt-5")
        ctx.check(f"provider openai, got {ref.provider!r}", ref.provider == "openai")
        ctx.check(f"model is the bare id, got {ref.model!r}", ref.model == "gpt-5")
        ctx.check(f"dialect openai-chat for an untabled id, got {ref.dialect!r}", ref.dialect == "openai-chat")


@test
def test_oai_ref_selects_responses_dialect_for_the_two_confirmed_ids(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _Env():
        for model_id in ("gpt-6-astra", "gpt-6.1-sol"):
            ref = parse_model_ref(f"oai:{model_id}")
            ctx.check(f"{model_id} selects openai-responses, got {ref.dialect!r}", ref.dialect == "openai-responses")


@test
def test_oai_similarly_named_ids_do_not_match_the_table(ctx: Ctx):
    """docs/harness/OPENAI-RESEARCH.md's own corrected-assumption note:
    gpt-6-sol/gpt-6-luna/gpt-5.6-sol are REAL, DIFFERENT ids from the two
    the Responses guide actually names -- a substring match would have
    wrongly swept them in."""
    from halo_harness.model import parse_model_ref
    with _Env():
        for model_id in ("gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"):
            ref = parse_model_ref(f"oai:{model_id}")
            ctx.check(f"{model_id} stays openai-chat, got {ref.dialect!r}", ref.dialect == "openai-chat")


@test
def test_oai_bare_prefix_refused(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        try:
            parse_model_ref("oai:")
            ctx.check("oai: alone must raise InvalidModelError", False)
        except InvalidModelError as e:
            ctx.check("message names the oai: form", "oai:" in str(e))


@test
def test_oai_dialect_override_either_direction(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.theme import set_config_value
    with _Env():
        set_config_value("openai.dialect_overrides", {"gpt-6-astra": "chat", "gpt-5": "responses"})
        ref_astra = parse_model_ref("oai:gpt-6-astra")
        ctx.check(f"override to chat wins over the table, got {ref_astra.dialect!r}",
                  ref_astra.dialect == "openai-chat")
        ref_5 = parse_model_ref("oai:gpt-5")
        ctx.check(f"override to responses wins with no table entry, got {ref_5.dialect!r}",
                  ref_5.dialect == "openai-responses")
        ref_sol = parse_model_ref("oai:gpt-6.1-sol")
        ctx.check(f"an unoverridden tabled id keeps the table's answer, got {ref_sol.dialect!r}",
                  ref_sol.dialect == "openai-responses")


@test
def test_oai_dialect_override_bad_value_falls_back_to_table(ctx: Ctx):
    from halo_harness.providers.responses_request import resolve_openai_dialect
    ctx.check("a typo'd override value is ignored, falling back to the table",
              resolve_openai_dialect("gpt-6-astra", overrides={"gpt-6-astra": "nonsense"}) == "openai-responses")
    ctx.check("same for an untabled id -- falls back to openai-chat",
              resolve_openai_dialect("gpt-5", overrides={"gpt-5": "nonsense"}) == "openai-chat")


@test
def test_oai_disabled_provider_refused_at_parse_time(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    from halo_harness.theme import set_config_value
    with _Env():
        set_config_value("providers", {"openai": {"enabled": False}})
        try:
            parse_model_ref("oai:gpt-5")
            ctx.check("a disabled provider must raise InvalidModelError", False)
        except InvalidModelError as e:
            ctx.check("message names OpenAI", "OpenAI" in str(e) or "openai" in str(e))


# ---- credentials -------------------------------------------------------------

@test
def test_resolve_openai_none_without_key(ctx: Ctx):
    from halo_harness.providers.config import resolve_openai
    with _Env():
        ctx.check("no OPENAI_API_KEY -> None", resolve_openai({}) is None)


@test
def test_resolve_openai_default_and_overridden_base_url(ctx: Ctx):
    from halo_harness.providers.config import resolve_openai
    with _Env():
        cfg = resolve_openai({"OPENAI_API_KEY": "sk-abc"})
        ctx.check(f"default base_url, got {cfg.base_url!r}", cfg.base_url == "https://api.openai.com/v1")
        ctx.check(f"api_key carried through, got {cfg.api_key!r}", cfg.api_key == "sk-abc")
        cfg2 = resolve_openai({"OPENAI_API_KEY": "sk-abc", "BRIDGE_OPENAI_BASE_URL": "http://127.0.0.1:9/v1"})
        ctx.check(f"BRIDGE_OPENAI_BASE_URL overrides for tests, got {cfg2.base_url!r}",
                  cfg2.base_url == "http://127.0.0.1:9/v1")


@test
def test_headless_resolve_creds_openai(ctx: Ctx):
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    with _Env():
        ref = parse_model_ref("oai:gpt-5")
        ctx.check("no key -> None creds", _resolve_creds(ref, settings=None) is None)
        os.environ["OPENAI_API_KEY"] = "sk-headless"
        creds = _resolve_creds(ref, settings=None)
        ctx.check(f"creds resolved, got {creds!r}", creds is not None and creds.api_key == "sk-headless"
                  and creds.base_url == "https://api.openai.com/v1")


@test
def test_provider_not_configured_message_names_the_env_var(ctx: Ctx):
    """Pins `providers.stream._run_phase1`/`_run_phase1_responses`'s own
    named message (brief item 1's "a missing entry gives a plain message
    naming the config key", same contract huggingface/ollama already
    have) -- without a live upstream, by calling phase 1 directly with
    `creds=None`."""
    from pathlib import Path as _Path
    from halo_harness.providers.routing import Route
    from halo_harness.providers.stream import CompletionRequest, ProviderNotConfigured, _run_phase1, _run_phase1_responses
    with _Env():
        route_chat = Route(provider="openai", upstream_model="gpt-5", dialect="openai-chat")
        req_chat = CompletionRequest(body={}, route=route_chat, profile={}, creds=None,
                                      state_dir=_Path(os.environ["BRIDGE_STATE_DIR"]), extra_headers={},
                                      model_label="oai:gpt-5", prebuilt_oai_body={"model": "gpt-5"})
        try:
            _run_phase1(req_chat)
            ctx.check("must raise ProviderNotConfigured", False)
        except ProviderNotConfigured as e:
            ctx.check(f"names OPENAI_API_KEY, got {e!r}", "OPENAI_API_KEY" in str(e))

        route_resp = Route(provider="openai", upstream_model="gpt-6-astra", dialect="openai-responses")
        req_resp = CompletionRequest(body={}, route=route_resp, profile={}, creds=None,
                                      state_dir=_Path(os.environ["BRIDGE_STATE_DIR"]), extra_headers={},
                                      model_label="oai:gpt-6-astra", prebuilt_responses_body={"model": "gpt-6-astra"})
        try:
            _run_phase1_responses(req_resp)
            ctx.check("must raise ProviderNotConfigured", False)
        except ProviderNotConfigured as e:
            ctx.check(f"names OPENAI_API_KEY, got {e!r}", "OPENAI_API_KEY" in str(e))


# ---- enablement / reachability -----------------------------------------------

@test
def test_enablement_table_entries(ctx: Ctx):
    from halo_harness.providers.enablement import LABELS, PREFIXES, canonical, credentials_present, is_enabled
    with _Env():
        ctx.check("oai alias canonicalizes to openai", canonical("oai") == "openai")
        ctx.check(f"label, got {LABELS['openai']!r}", LABELS["openai"] == "OpenAI API (key)")
        ctx.check(f"prefix, got {PREFIXES['openai']!r}", PREFIXES["openai"] == "oai:")
        ctx.check("not detected with no key", credentials_present("openai") is False)
        ctx.check("auto-enablement follows detection (false here)", is_enabled("openai") is False)
        os.environ["OPENAI_API_KEY"] = "sk-enable"
        ctx.check("detected once the key is set", credentials_present("openai") is True)
        ctx.check("auto-enabled once detected, no override needed", is_enabled("openai") is True)


@test
def test_reachability_base_url(ctx: Ctx):
    from halo_harness.providers.reachability import provider_base_url
    with _Env():
        ctx.check("default base url with no config", provider_base_url("openai") == "https://api.openai.com/v1")
        os.environ["OPENAI_API_KEY"] = "sk-x"
        os.environ["BRIDGE_OPENAI_BASE_URL"] = "http://127.0.0.1:12345/v1"
        ctx.check("overridden base url once configured", provider_base_url("openai") == "http://127.0.0.1:12345/v1")


# ---- resolve_model_profile (models.dev cross-check) --------------------------

@test
def test_resolve_model_profile_vendored_fallback(ctx: Ctx):
    from halo_harness.model import parse_model_ref, resolve_model_profile
    with _Env():
        ref = parse_model_ref("oai:gpt-6.1-sol")
        profile = resolve_model_profile(ref, Path(os.environ["BRIDGE_STATE_DIR"]))
        ctx.check(f"real context from the vendored fallback, got {profile.context_tokens}",
                  profile.context_tokens == 1_050_000)
        ctx.check(f"real pricing from the vendored fallback, got {profile.price_in}",
                  profile.price_in is not None and profile.price_in > 0)
        ctx.check(f"vision true (modalities.input carries image), got {profile.vision}", profile.vision is True)
        ctx.check(f"reasoning marked, got {profile.reasoning!r}", profile.reasoning == "openai")


@test
def test_resolve_model_profile_unknown_id_is_the_bare_default(ctx: Ctx):
    from halo_harness.model import ModelProfile, parse_model_ref, resolve_model_profile
    with _Env():
        ref = parse_model_ref("oai:totally-unknown-model-id-xyz")
        profile = resolve_model_profile(ref, Path(os.environ["BRIDGE_STATE_DIR"]))
        ctx.check("falls back to the plain dataclass default", profile == ModelProfile())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
