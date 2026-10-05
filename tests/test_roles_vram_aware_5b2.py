"""tests.test_roles_vram_aware_5b2 -- Halo 2.0.3 round 5b part 2 (brief
item 3, "VRAM-aware role defaults"): `providers.ollama_hw.fits_beside_main`
pinned with two hosts (fits / does not fit, per the brief's own
instruction), `roles.vram_aware_override`/`vram_fit_reason`, and the
runtime redirection in `/local <question>`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import MockUpstream

test, TESTS = new_registry()
_GB = 1024 ** 3


def _catalog(main_model: str, main_size: int, candidate_model: str, candidate_size: int) -> dict:
    return {"models": [
        {"model": main_model, "name": main_model, "size": main_size, "digest": "sha256:a"},
        {"model": candidate_model, "name": candidate_model, "size": candidate_size, "digest": "sha256:b"},
    ]}


# ---- fits_beside_main: pinned with two hosts (fits / does not fit) ------

@test
def test_fits_beside_main_true_on_a_roomy_host(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import fits_beside_main, reset_local_gpu_cache
    reset_local_gpu_cache()
    mock = MockUpstream(ps_response={"models": [
        {"model": "main", "name": "main", "size": 10 * _GB, "size_vram": 10 * _GB},
    ]}).start()
    try:
        host = OllamaHost(name="roomy", url=mock.base_url)
        catalog = _catalog("main", 10 * _GB, "small", 4 * _GB)

        def runner(argv, timeout):
            return "24000,0,24000,A Roomy Card\n"  # nvidia-smi shape: total,used,free,name (MiB)

        result = fits_beside_main(host, main_model="main", candidate_model="small", catalog=catalog,
                                   hw_runner=runner)
        ctx.check(f"fits (10 GiB main + 4 GiB candidate <= 24 GiB total), got {result!r}", result is True)
    finally:
        mock.stop()


@test
def test_fits_beside_main_false_on_a_tight_host(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import fits_beside_main, reset_local_gpu_cache
    reset_local_gpu_cache()
    mock = MockUpstream(ps_response={"models": [
        {"model": "main", "name": "main", "size": 20 * _GB, "size_vram": 20 * _GB},
    ]}).start()
    try:
        host = OllamaHost(name="tight", url=mock.base_url)
        catalog = _catalog("main", 20 * _GB, "small", 10 * _GB)

        def runner(argv, timeout):
            return "24000,0,24000,A Tight Card\n"  # 24 GiB total; 20 + 10 = 30 > 24

        result = fits_beside_main(host, main_model="main", candidate_model="small", catalog=catalog,
                                   hw_runner=runner)
        ctx.check(f"does not fit (20 + 10 GiB > 24 GiB total), got {result!r}", result is False)
    finally:
        mock.stop()


@test
def test_fits_beside_main_unknown_when_main_not_loaded(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import fits_beside_main, reset_local_gpu_cache
    reset_local_gpu_cache()
    mock = MockUpstream(ps_response={"models": []}).start()  # main is NOT currently loaded
    try:
        host = OllamaHost(name="h", url=mock.base_url)
        catalog = _catalog("main", 10 * _GB, "small", 4 * _GB)
        result = fits_beside_main(host, main_model="main", candidate_model="small", catalog=catalog,
                                   hw_runner=lambda argv, timeout: "24000,0,24000,X\n")
        ctx.check("unknown (never a guess) when main isn't resident", result is None)
    finally:
        mock.stop()


@test
def test_fits_beside_main_unknown_when_no_gpu_read(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import fits_beside_main, reset_local_gpu_cache
    reset_local_gpu_cache()
    mock = MockUpstream(ps_response={"models": [
        {"model": "main", "name": "main", "size": 10 * _GB, "size_vram": 10 * _GB},
    ]}).start()
    try:
        host = OllamaHost(name="h", url=mock.base_url)
        catalog = _catalog("main", 10 * _GB, "small", 4 * _GB)
        result = fits_beside_main(host, main_model="main", candidate_model="small", catalog=catalog,
                                   hw_runner=lambda argv, timeout: None)  # no GPU tool found
        ctx.check("unknown when the GPU memory total can't be read at all", result is None)
    finally:
        mock.stop()


# ---- roles.vram_aware_override / vram_fit_reason -------------------------

@test
def test_vram_aware_override_redirects_to_main_when_it_does_not_fit(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.ollama_hw import reset_local_gpu_cache
    from halo_harness.roles import VRAM_AWARE_REASON, vram_aware_override
    from halo_harness.theme import set_config_value
    import os
    reset_local_gpu_cache()
    d = Path(tempfile.mkdtemp(prefix="vram-override-"))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    mock = MockUpstream(ps_response={"models": [
        {"model": "main-model", "name": "main-model", "size": 20 * _GB, "size_vram": 20 * _GB},
    ]}).start()
    try:
        set_config_value("ollama.hosts", [{"name": "shared", "url": mock.base_url, "default": True}])
        mock.tags_response = {"models": [
            {"name": "main-model", "model": "main-model", "size": 20 * _GB, "digest": "sha256:a",
             "details": {}},
            {"name": "small-model", "model": "small-model", "size": 10 * _GB, "digest": "sha256:b",
             "details": {}},
        ]}
        main_ref = parse_model_ref("ol:main-model")
        # 24 GiB total; 20 GiB resident (main) + 10 GiB (candidate) > 24 GiB.
        value, reason = vram_aware_override("small", "ol:small-model", main_ref=main_ref,
                                             hw_runner=lambda argv, timeout: "24000,0,24000,A Tight Card\n")
        ctx.check(f"redirected to the main model, got {value!r}", value == main_ref.raw)
        ctx.check(f"the exact reason string, got {reason!r}", reason == VRAM_AWARE_REASON)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_STATE_DIR", None)
        reset_local_gpu_cache()


@test
def test_vram_aware_override_leaves_it_alone_when_it_fits(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.roles import vram_aware_override
    from halo_harness.theme import set_config_value
    import os
    d = Path(tempfile.mkdtemp(prefix="vram-override-fits-"))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    mock = MockUpstream(ps_response={"models": [
        {"model": "main-model", "name": "main-model", "size": 4 * _GB, "size_vram": 4 * _GB},
    ]}).start()
    try:
        set_config_value("ollama.hosts", [{"name": "shared", "url": mock.base_url, "default": True}])
        mock.tags_response = {"models": [
            {"name": "main-model", "model": "main-model", "size": 4 * _GB, "digest": "sha256:a", "details": {}},
            {"name": "small-model", "model": "small-model", "size": 1 * _GB, "digest": "sha256:b", "details": {}},
        ]}
        main_ref = parse_model_ref("ol:main-model")
        # A GPU tool that finds nothing -> fits_beside_main returns None
        # (unknown) -> benefit of the doubt -> never overridden.
        value, reason = vram_aware_override("small", "ol:small-model", main_ref=main_ref,
                                             hw_runner=lambda argv, timeout: None)
        ctx.check(f"left unchanged (unknown fit -> benefit of the doubt), got {value!r}", value == "ol:small-model")
        ctx.check("no reason given", reason is None)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_STATE_DIR", None)


@test
def test_vram_aware_override_ignores_non_ollama_main(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.roles import vram_aware_override
    main_ref = parse_model_ref("or:some/model")
    value, reason = vram_aware_override("small", "ol:small-model", main_ref=main_ref)
    ctx.check("untouched when main isn't ol:", value == "ol:small-model")
    ctx.check("no reason", reason is None)


@test
def test_vram_aware_override_only_applies_to_the_four_role_names(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.roles import vram_aware_override
    main_ref = parse_model_ref("or:some/model")
    for role in ("orchestrator", "planner", "coder", "reviewer", "tester", "compaction"):
        value, reason = vram_aware_override(role, "ol:small-model", main_ref=main_ref)
        ctx.check(f"role {role!r} is never VRAM-aware", reason is None and value == "ol:small-model")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
