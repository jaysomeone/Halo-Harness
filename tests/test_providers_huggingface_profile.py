"""tests.test_providers_huggingface_profile -- Halo 2.0.3 round 4:
`resolve_model_profile` for `hf:` refs (catalog hit / miss, an endpoint ref
never consults the catalog), the `huggingface.bill_to` config reader, and
the `ProviderNotConfigured` message `providers.stream._run_phase1` raises
for a huggingface route with no creds (naming BOTH config keys, never
mislabeled "OpenRouter not configured"). Ref parsing/credential resolution/
enablement live in tests/test_providers_huggingface.py.
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
)


class _Env:
    """Same pattern as tests/test_providers_huggingface.py's own `_Env`."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="hf-profile-"))
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


# ---- resolve_model_profile --------------------------------------------------

@test
def test_profile_router_ref_reads_catalog_strips_suffix(ctx: Ctx):
    from halo_harness.model import parse_model_ref, resolve_model_profile
    from halo_harness.providers.huggingface_catalog import write_hf_models_json
    with _Env() as env:
        write_hf_models_json(env.state_dir, [{"id": "Qwen/Qwen3-32B", "context_length": 40960,
                                               "pricing": {"prompt": "0.0000002", "completion": "0.0000006"}}])
        ref = parse_model_ref("hf:Qwen/Qwen3-32B:cheapest")
        profile = resolve_model_profile(ref, env.state_dir)
        ctx.check(f"context_tokens from the catalog (suffix stripped before lookup), got {profile.context_tokens}",
                  profile.context_tokens == 40960)
        ctx.check(f"price_in parsed from the catalog's pricing.prompt, got {profile.price_in}",
                  profile.price_in == 2e-07)
        ctx.check(f"price_out parsed from the catalog's pricing.completion, got {profile.price_out}",
                  profile.price_out == 6e-07)


@test
def test_profile_router_ref_defaults_without_a_catalog_entry(ctx: Ctx):
    from halo_harness.model import ModelProfile, parse_model_ref, resolve_model_profile
    with _Env() as env:
        ref = parse_model_ref("hf:never-cached/model")
        profile = resolve_model_profile(ref, env.state_dir)
        ctx.check(f"falls to the bare dataclass default, got {profile!r}", profile == ModelProfile())


@test
def test_profile_endpoint_ref_never_consults_the_catalog(ctx: Ctx):
    """Seeds a catalog entry whose id happens to equal the endpoint name --
    proves the endpoint branch never reads it (there is no catalog concept
    for a dedicated endpoint at all: it was never listed by the router's
    own GET /v1/models)."""
    from halo_harness.model import ModelProfile, parse_model_ref, resolve_model_profile
    from halo_harness.providers.huggingface_catalog import write_hf_models_json
    with _Env() as env:
        write_hf_models_json(env.state_dir, [{"id": "my-prod", "context_length": 999999}])
        ref = parse_model_ref("hf:endpoint/my-prod")
        profile = resolve_model_profile(ref, env.state_dir)
        ctx.check(f"an endpoint ref always gets the bare dataclass default, got {profile!r}",
                  profile == ModelProfile())


# ---- huggingface.bill_to ----------------------------------------------------

@test
def test_resolve_huggingface_bill_to(ctx: Ctx):
    from halo_harness.providers.huggingface import resolve_huggingface_bill_to
    from halo_harness.theme import set_config_value
    with _Env():
        ctx.check("None when unset", resolve_huggingface_bill_to() is None)
        set_config_value("huggingface.bill_to", "my-org")
        ctx.check(f"reads huggingface.bill_to, got {resolve_huggingface_bill_to()!r}",
                  resolve_huggingface_bill_to() == "my-org")


# ---- ProviderNotConfigured message -----------------------------------------

@test
def test_provider_not_configured_message_names_both_config_keys(ctx: Ctx):
    """Before this round's stream.py fix, ANY non-databricks provider with
    no creds got the hardcoded "OpenRouter not configured" label -- wrong
    and unhelpful for huggingface. Pins the real message instead."""
    from halo_harness.providers.routing import Route
    from halo_harness.providers.stream import CompletionRequest, ProviderNotConfigured, stream_completion
    with _Env() as env:
        route = Route(provider="huggingface", upstream_model="org/model", dialect="openai-chat")
        req = CompletionRequest(
            body={"messages": []}, route=route, profile={}, creds=None, state_dir=env.state_dir,
            extra_headers={}, model_label="hf:org/model", prebuilt_oai_body={"model": "org/model", "messages": []},
        )
        try:
            next(stream_completion(req))
            ctx.check("must raise ProviderNotConfigured", False)
        except ProviderNotConfigured as e:
            msg = str(e)
            ctx.check(f"names HF_TOKEN, got {msg!r}", "HF_TOKEN" in msg)
            ctx.check(f"names huggingface.endpoints, got {msg!r}", "huggingface.endpoints" in msg)
            ctx.check(f"never the old OpenRouter mislabel, got {msg!r}", "OpenRouter" not in msg)


# ---- "hf" alias (mirrors cc/dbx/or/ant) -------------------------------------

@test
def test_hf_alias_resolves_to_canonical_name(ctx: Ctx):
    from halo_harness.providers.enablement import canonical, is_enabled
    with _Env():
        ctx.check(f"canonical('hf') == huggingface, got {canonical('hf')!r}", canonical("hf") == "huggingface")
        os.environ["HF_TOKEN"] = "tok"
        ctx.check("is_enabled('hf') agrees with is_enabled('huggingface')",
                  is_enabled("hf") == is_enabled("huggingface") == True)  # noqa: E712


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
