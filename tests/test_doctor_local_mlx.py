"""tests.test_doctor_local_mlx -- Halo 2.0.3 round 5f: `halo doctor
--local --model hf:mlx/...`/`hf:local/...` -- `doctor_local.py`'s new
huggingface branch (`_resolved_huggingface`/`_send_turn_for`/`_HFHost`),
hermetic via `tests/helpers/mock_openai.MockUpstream`'s new `doctor-good`/
`doctor-bad` scenarios (never a real model, never real mlx_lm). This
suite never runs on a real Mac, so the `hf:mlx/*` case is pinned on its
deterministic, portable outcome: the one-sentence Apple-Silicon refusal,
surfaced as all 4 steps' shared FAIL reason -- `doctor --local` "accepts"
the ref (parses it, dispatches to the right code path, never crashes)
without needing real Apple Silicon to prove that much. The full start/
consent/reuse path (platform injected) is covered directly against
`providers.huggingface_mlx.ensure_mlx_server` in
tests/test_providers_huggingface_mlx.py instead -- `doctor_local.py` has
no seam of its own to inject a fake platform/runtime through, by design
(production never needs one).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_openai import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = ("HF_TOKEN", "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                      "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS", "OLLAMA_HOST")


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="doctor-local-mlx-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_doctor_local_hf_mlx_on_non_apple_box_fails_all_steps_with_sentence(ctx: Ctx):
    from halo_harness.doctor_local import run_local_acceptance_check
    with _Env() as e:
        steps, ok = run_local_acceptance_check("hf:mlx/mlx-community/does-not-matter", state_dir=e.state_dir)
        ctx.check("4 steps always returned", len(steps) == 4)
        ctx.check("overall FAIL (this test box is never Apple Silicon)", ok is False)
        ctx.check(f"every step shares the Apple-Silicon sentence as its reason, got {steps!r}",
                  all(s["reason"] == "MLX runs on Apple Silicon only." for s in steps))
        ctx.check("every step is marked FAIL, never a crash", all(s["ok"] is False for s in steps))


@test
def test_doctor_local_hf_local_generalized_dispatch_all_steps_pass(ctx: Ctx):
    """Proves the NEW generalized dispatch (`_resolved`/`_send_turn_for`/
    `_HFHost`) actually decodes a real openai-chat turn end to end --
    an `hf:local/*` ref (never gated on Apple Silicon) against a
    `MockUpstream` scripted with the new `doctor-good` scenario."""
    from halo_harness.doctor_local import format_acceptance_lines, run_local_acceptance_check
    from halo_harness.theme import set_config_value
    with _Env() as e:
        mock = MockUpstream(path_prefix="/v1").start()
        try:
            set_config_value("huggingface.local_servers",
                              [{"name": "bench", "url": mock.base_url, "default": True}])
            steps, ok = run_local_acceptance_check("hf:local/mock/doctor-good@bench", state_dir=e.state_dir)
            ctx.check(f"all 4 steps PASS, got {format_acceptance_lines(steps)}", ok is True)
            ctx.check("load step names the server", "bench" in steps[0]["reason"])
            ctx.check("tool call step dispatched Read", "Read" in steps[1]["reason"])
            ctx.check("structured output step validated the schema", "halo" in steps[2]["reason"]
                      or "schema" in steps[2]["reason"])
            ctx.check("compaction summary produced non-empty text", "summary" in steps[3]["reason"])
        finally:
            mock.stop()


@test
def test_doctor_local_hf_local_bad_scenario_fails_steps_cleanly(ctx: Ctx):
    from halo_harness.doctor_local import run_local_acceptance_check
    from halo_harness.theme import set_config_value
    with _Env() as e:
        mock = MockUpstream(path_prefix="/v1").start()
        try:
            set_config_value("huggingface.local_servers",
                              [{"name": "bench", "url": mock.base_url, "default": True}])
            steps, ok = run_local_acceptance_check("hf:local/mock/doctor-bad@bench", state_dir=e.state_dir)
            ctx.check("overall FAIL", ok is False)
            ctx.check("load step still PASSES (the 'ready' reply is identical either way)", steps[0]["ok"] is True)
            ctx.check("tool call step FAILS on the bad args", steps[1]["ok"] is False)
            ctx.check("structured output step FAILS on non-JSON prose", steps[2]["ok"] is False)
            ctx.check("compaction summary step FAILS on an empty reply", steps[3]["ok"] is False)
        finally:
            mock.stop()


@test
def test_check_mlx_extra_omitted_on_this_non_apple_box(ctx: Ctx):
    """Brief item 1: "on other platforms nothing else changes" -- this
    dev/CI box is never Apple Silicon, so `halo doctor`'s own check list
    must not mention MLX at all."""
    from halo_harness.doctor import _check_entries
    with _Env():
        # `_check_entries` includes `_check_ollama_hosts()`, which (unlike
        # every OTHER check this suite scopes) reads `OLLAMA_HOST` off the
        # bare environment with no `env=` override of its own -- pinned
        # to an unreachable address (never the synthesized 127.0.0.1:11434
        # default, which could be a REAL local daemon on this build host)
        # so this stays a fast, hermetic, no-network call.
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"
        ids = [cid for cid, _line in _check_entries()]
        ctx.check(f"no mlx_extra entry on a non-Apple-Silicon box, got ids containing "
                  f"{[i for i in ids if 'mlx' in i]!r}", "mlx_extra" not in ids)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
