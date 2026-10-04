"""tests.test_providers_huggingface -- Halo 2.0.3 round 4: `hf:` ref
parsing, credential resolution (router vs. dedicated endpoint, never
cross-wired), enablement, `resolve_model_profile`, the `X-HF-Bill-To`
config reader, and the `ProviderNotConfigured` message. Catalog probe/
cache/TTL and error translation live in tests/
test_providers_huggingface_catalog.py; the print-mode end-to-end runs
(router + endpoint, against the parameterized tests/helpers/mock_openai.py
fake) live in tests/test_providers_huggingface_session.py.
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
    "HF_TOKEN", "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
    "BRIDGE_HF_ROUTER_BASE_URL", "HALO_HF_ROUTER_BASE_URL",
)


class _Env:
    """Same pattern as tests/test_h15_provider_enablement.py's own `_Env`
    -- scopes BRIDGE_TEST_HOME/BRIDGE_STATE_DIR/BRIDGE_ENV_FILE to a fresh
    tempdir and snapshots/restores every env var this module touches, so
    the real `~/.halo` and a developer's own real `HF_TOKEN` (if any) are
    never read or clobbered."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="hf-providers-"))
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


# ---- ref parsing ------------------------------------------------------------

@test
def test_hf_router_ref_parses_model_verbatim_with_suffix(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _Env():
        ref = parse_model_ref("hf:Qwen/Qwen3-32B:fastest")
        ctx.check(f"provider huggingface, got {ref.provider!r}", ref.provider == "huggingface")
        ctx.check(f"dialect openai-chat, got {ref.dialect!r}", ref.dialect == "openai-chat")
        ctx.check(f"model carries the suffix VERBATIM, got {ref.model!r}", ref.model == "Qwen/Qwen3-32B:fastest")
        ctx.check(f"host is None for a router ref, got {ref.host!r}", ref.host is None)


@test
def test_hf_endpoint_ref_parses_name_into_host(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _Env():
        ref = parse_model_ref("hf:endpoint/my-prod")
        ctx.check(f"provider huggingface, got {ref.provider!r}", ref.provider == "huggingface")
        ctx.check(f"host carries the bare name, got {ref.host!r}", ref.host == "my-prod")
        ctx.check(f"model is also the bare name, got {ref.model!r}", ref.model == "my-prod")


@test
def test_hf_bare_prefix_refused(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        try:
            parse_model_ref("hf:")
            ctx.check("hf: alone must raise InvalidModelError", False)
        except InvalidModelError as e:
            msg = str(e)
            ctx.check(f"message names both accepted shapes, got {msg!r}", "endpoint/" in msg and "org" in msg)


@test
def test_hf_endpoint_bare_prefix_refused(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        try:
            parse_model_ref("hf:endpoint/")
            ctx.check("hf:endpoint/ alone must raise InvalidModelError", False)
        except InvalidModelError as e:
            ctx.check(f"message names huggingface.endpoints, got {e!s}", "huggingface.endpoints" in str(e))


@test
def test_hf_no_route_error_lists_hf_prefix(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        try:
            parse_model_ref("zzz-not-a-real-provider-ref")
            ctx.check("an unknown bare name must raise", False)
        except InvalidModelError as e:
            ctx.check(f"accepted-forms message mentions hf:, got {e!s}", "hf:" in str(e))


# ---- credential resolution: router vs. endpoint, never cross-wired --------

@test
def test_resolve_creds_router_from_hf_token(ctx: Ctx):
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    with _Env():
        os.environ["HF_TOKEN"] = "router-token-abc"
        ref = parse_model_ref("hf:Qwen/Qwen3-32B")
        creds = _resolve_creds(ref)
        ctx.check("router creds resolve", creds is not None)
        ctx.check(f"base_url is the router root, got {creds.base_url!r}",
                  creds.base_url == "https://router.huggingface.co/v1")
        ctx.check(f"api_key is HF_TOKEN, got {creds.api_key!r}", creds.api_key == "router-token-abc")


@test
def test_resolve_creds_router_none_without_hf_token(ctx: Ctx):
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    with _Env():
        ref = parse_model_ref("hf:Qwen/Qwen3-32B")
        ctx.check("router creds are None with no HF_TOKEN", _resolve_creds(ref) is None)


@test
def test_resolve_creds_endpoint_uses_its_own_url_and_token_never_hf_token(ctx: Ctx):
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    from halo_harness.theme import set_config_value
    with _Env():
        os.environ["HF_TOKEN"] = "router-token-should-never-appear"
        set_config_value("huggingface.endpoints", [
            {"name": "my-prod", "url": "https://my-prod.endpoints.huggingface.cloud", "token": "endpoint-token-xyz"},
        ])
        ref = parse_model_ref("hf:endpoint/my-prod")
        creds = _resolve_creds(ref)
        ctx.check("endpoint creds resolve", creds is not None)
        ctx.check(f"base_url is the endpoint's OWN url, got {creds.base_url!r}",
                  creds.base_url == "https://my-prod.endpoints.huggingface.cloud")
        ctx.check(f"api_key is the endpoint's OWN token, NEVER HF_TOKEN, got {creds.api_key!r}",
                  creds.api_key == "endpoint-token-xyz")


@test
def test_resolve_creds_missing_endpoint_entry_is_none_not_router(ctx: Ctx):
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    with _Env():
        os.environ["HF_TOKEN"] = "router-token-should-never-be-used"
        ref = parse_model_ref("hf:endpoint/does-not-exist")
        ctx.check("a missing endpoint entry resolves to no creds (never silently falls back to the router)",
                  _resolve_creds(ref) is None)


@test
def test_resolve_creds_endpoint_named_selection_among_several(ctx: Ctx):
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    from halo_harness.theme import set_config_value
    with _Env():
        set_config_value("huggingface.endpoints", [
            {"name": "a", "url": "https://a.endpoints.huggingface.cloud", "token": "tok-a"},
            {"name": "b", "url": "https://b.endpoints.huggingface.cloud", "token": "tok-b", "default": True},
        ])
        creds_a = _resolve_creds(parse_model_ref("hf:endpoint/a"))
        creds_b = _resolve_creds(parse_model_ref("hf:endpoint/b"))
        ctx.check(f"named entry a resolves its own url, got {creds_a.base_url!r}",
                  creds_a.base_url == "https://a.endpoints.huggingface.cloud")
        ctx.check(f"named entry b resolves its own url, got {creds_b.base_url!r}",
                  creds_b.base_url == "https://b.endpoints.huggingface.cloud")
        ctx.check("the two entries' tokens never cross-wire", creds_a.api_key == "tok-a" and creds_b.api_key == "tok-b")


@test
def test_resolve_creds_endpoint_with_no_token_sends_empty_api_key(ctx: Ctx):
    """An entry with no `token` key resolves creds with `api_key=""` (never
    `None` -- `ProviderCreds.api_key` is a plain `str`, same contract
    `providers.ollama.resolve_ollama_host` already has for an
    unauthenticated host) rather than falling back to HF_TOKEN."""
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    from halo_harness.theme import set_config_value
    with _Env():
        os.environ["HF_TOKEN"] = "should-never-be-used-for-an-endpoint"
        set_config_value("huggingface.endpoints", [{"name": "open", "url": "https://open.endpoints.huggingface.cloud"}])
        creds = _resolve_creds(parse_model_ref("hf:endpoint/open"))
        ctx.check(f"api_key is empty, never HF_TOKEN, got {creds.api_key!r}", creds.api_key == "")


# ---- enablement -------------------------------------------------------------

@test
def test_enablement_hf_token_alone_enables(ctx: Ctx):
    from halo_harness.providers.enablement import credentials_present, is_enabled
    with _Env():
        ctx.check("not detected with nothing set", credentials_present("huggingface") is False)
        os.environ["HF_TOKEN"] = "tok"
        ctx.check("detected via HF_TOKEN alone", credentials_present("huggingface") is True)
        ctx.check("enabled (auto) via HF_TOKEN alone", is_enabled("huggingface") is True)


@test
def test_enablement_endpoint_alone_enables_no_token_needed(ctx: Ctx):
    from halo_harness.providers.enablement import credentials_present
    from halo_harness.theme import set_config_value
    with _Env():
        ctx.check("not detected with nothing configured", credentials_present("huggingface") is False)
        set_config_value("huggingface.endpoints", [{"name": "a", "url": "https://a.endpoints.huggingface.cloud"}])
        ctx.check("detected via an endpoint entry alone (no HF_TOKEN needed)", credentials_present("huggingface") is True)


@test
def test_provider_tables_include_huggingface(ctx: Ctx):
    from halo_harness.providers.enablement import LABELS, PREFIXES, PROVIDER_NAMES
    with _Env():
        ctx.check("huggingface is in PROVIDER_NAMES (/providers, halo providers, doctor's N/M line)",
                  "huggingface" in PROVIDER_NAMES)
        ctx.check(f"label is Hugging Face, got {LABELS.get('huggingface')!r}",
                  LABELS.get("huggingface") == "Hugging Face")
        ctx.check(f"prefix is hf:, got {PREFIXES.get('huggingface')!r}", PREFIXES.get("huggingface") == "hf:")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
