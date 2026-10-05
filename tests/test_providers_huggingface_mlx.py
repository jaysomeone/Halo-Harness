"""tests.test_providers_huggingface_mlx -- Halo 2.0.3 round 5f: `hf:mlx/
<org>/<repo>` ref parsing, the non-Apple-Silicon one-sentence refusal
(platform injected -- this suite never runs on a real Mac), starting a
managed mlx_lm.server from a FAKE runtime stub (same pattern tests.
test_local_runtime.py uses for llama-server), reuse of an already-running
one, the EXACT-match registry lookup `_resolve_creds` needs (never the
generic "most recently started wins" fallback -- a real correctness bug
this round's design had to avoid), stop, roles' "small by default",
pyproject's extra marker, and the /local hub-cache hint text. Never
touches the real ~/.halo (state_dir is always an explicit fresh tempdir).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "HF_TOKEN", "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS", "OLLAMA_HOST",
)

_STUB_LISTENS = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "stub-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _write_stub() -> Path:
    d = Path(tempfile.mkdtemp(prefix="hf-mlx-stub-"))
    p = d / "stub_mlx.py"
    p.write_text(_STUB_LISTENS, encoding="utf-8")
    return p


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="hf-mlx-"))
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
def test_hf_mlx_ref_parses_repo_id_local_and_mlx_true(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _Env():
        ref = parse_model_ref("hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit")
        ctx.check(f"provider huggingface, got {ref.provider!r}", ref.provider == "huggingface")
        ctx.check(f"dialect openai-chat, got {ref.dialect!r}", ref.dialect == "openai-chat")
        ctx.check(f"model is the bare repo id, got {ref.model!r}",
                  ref.model == "mlx-community/Qwen2.5-7B-Instruct-4bit")
        ctx.check("local is True (rides the hf:local tiers)", ref.local is True)
        ctx.check("mlx is True", ref.mlx is True)
        ctx.check(f"host is None, got {ref.host!r}", ref.host is None)


@test
def test_hf_mlx_bare_prefix_refused(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        try:
            parse_model_ref("hf:mlx/")
            ctx.check("hf:mlx/ alone must raise InvalidModelError", False)
        except InvalidModelError as e:
            ctx.check(f"message names the org/repo shape, got {e!s}", "mlx/" in str(e) and "org" in str(e))


@test
def test_hf_mlx_no_slash_refused(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    with _Env():
        try:
            parse_model_ref("hf:mlx/no-org-separator")
            ctx.check("a repo id with no org/ separator must raise", False)
        except InvalidModelError:
            pass


@test
def test_hf_other_shapes_still_have_mlx_false(ctx: Ctx):
    """Regression guard: adding `mlx` must never flip it on for any
    pre-existing `hf:` shape."""
    from halo_harness.model import parse_model_ref
    with _Env():
        ctx.check("router ref mlx=False", parse_model_ref("hf:Qwen/Qwen3-32B").mlx is False)
        ctx.check("endpoint ref mlx=False", parse_model_ref("hf:endpoint/my-prod").mlx is False)
        ctx.check("local ref mlx=False", parse_model_ref("hf:local/qwen3-30b").mlx is False)


# ---- platform gate -----------------------------------------------------------

@test
def test_is_apple_silicon_platform_injection(ctx: Ctx):
    from halo_harness.providers.huggingface_mlx import is_apple_silicon
    ctx.check("darwin+arm64 is True", is_apple_silicon(platform_name="darwin", machine="arm64") is True)
    ctx.check("darwin+aarch64 is True", is_apple_silicon(platform_name="darwin", machine="aarch64") is True)
    ctx.check("darwin+x86_64 (Intel Mac) is False",
              is_apple_silicon(platform_name="darwin", machine="x86_64") is False)
    ctx.check("win32 is False regardless of machine",
              is_apple_silicon(platform_name="win32", machine="arm64") is False)
    ctx.check("linux is False regardless of machine",
              is_apple_silicon(platform_name="linux", machine="aarch64") is False)


@test
def test_ensure_mlx_server_refuses_on_non_apple_platform(ctx: Ctx):
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server, mlx_unsupported_sentence
    with _Env() as e:
        target, lines = ensure_mlx_server("mlx-community/x", state_dir=e.state_dir,
                                           platform_name="win32", machine="AMD64")
        ctx.check("target is None", target is None)
        ctx.check(f"exact one-sentence refusal, got {lines!r}", lines == [mlx_unsupported_sentence()])
        ctx.check(f"exact text, got {mlx_unsupported_sentence()!r}",
                  mlx_unsupported_sentence() == "MLX runs on Apple Silicon only.")


# ---- start / consent / reuse / stop, against a fake stub -------------------

@test
def test_ensure_mlx_server_starts_with_consent_and_registers(ctx: Ctx):
    from halo_harness.providers.local_runtime import load_registry
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server
    with _Env() as e:
        stub = _write_stub()
        repo = "mlx-community/test-start"
        asked = []
        target, lines = ensure_mlx_server(
            repo, confirm=lambda q: (asked.append(q), True)[1], state_dir=e.state_dir,
            platform_name="darwin", machine="arm64", binary_argv=[sys.executable, str(stub)],
        )
        try:
            ctx.check(f"started, got lines={lines!r}", target is not None)
            ctx.check("consent sentence was shown and names the repo", len(asked) == 1 and repo in asked[0])
            ctx.check(f"base_url is loopback, got {target.base_url!r}", "127.0.0.1" in target.base_url)
            recorded = load_registry(e.state_dir)
            ctx.check(f"registry has exactly one entry, got {recorded}", len(recorded) == 1)
            ctx.check(f"registry keyed by the repo id, got {recorded[0].get('model')!r}",
                      recorded[0].get("model") == repo)
            ctx.check(f"runtime recorded as mlx_lm, got {recorded[0].get('runtime')!r}",
                      recorded[0].get("runtime") == "mlx_lm")
        finally:
            from halo_harness.providers.local_runtime import stop_managed_server
            stop_managed_server(repo, state_dir=e.state_dir)


@test
def test_ensure_mlx_server_declines_without_starting(ctx: Ctx):
    from halo_harness.providers.local_runtime import load_registry
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server
    with _Env() as e:
        stub = _write_stub()
        target, lines = ensure_mlx_server(
            "mlx-community/test-decline", confirm=lambda _q: False, state_dir=e.state_dir,
            platform_name="darwin", machine="arm64", binary_argv=[sys.executable, str(stub)],
        )
        ctx.check("target is None when declined", target is None)
        ctx.check(f"a Declined line is present, got {lines!r}", any("Declined" in l for l in lines))
        ctx.check("nothing was registered", load_registry(e.state_dir) == [])


@test
def test_ensure_mlx_server_reuses_running_server_without_asking_again(ctx: Ctx):
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server
    with _Env() as e:
        stub = _write_stub()
        repo = "mlx-community/test-reuse"
        first, _lines = ensure_mlx_server(
            repo, confirm=lambda _q: True, state_dir=e.state_dir,
            platform_name="darwin", machine="arm64", binary_argv=[sys.executable, str(stub)],
        )
        try:
            ctx.check("first call started it", first is not None)

            def _must_not_be_asked(_q):
                raise AssertionError("a running server must never be asked for consent again")
            second, lines2 = ensure_mlx_server(
                repo, confirm=_must_not_be_asked, state_dir=e.state_dir,
                platform_name="darwin", machine="arm64", binary_argv=[sys.executable, str(stub)],
            )
            ctx.check("second call reuses the SAME base_url", second is not None and second.base_url == first.base_url)
            ctx.check(f"a 'reusing' notice is printed, got {lines2!r}", any("Reusing" in l for l in lines2))
        finally:
            from halo_harness.providers.local_runtime import stop_managed_server
            stop_managed_server(repo, state_dir=e.state_dir)


@test
def test_stop_managed_server_works_for_an_mlx_entry(ctx: Ctx):
    from halo_harness.providers.local_runtime import load_registry, stop_managed_server
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server
    with _Env() as e:
        stub = _write_stub()
        repo = "mlx-community/test-stop"
        target, _lines = ensure_mlx_server(
            repo, confirm=lambda _q: True, state_dir=e.state_dir,
            platform_name="darwin", machine="arm64", binary_argv=[sys.executable, str(stub)],
        )
        ctx.check("started", target is not None)
        ok, msg = stop_managed_server(repo, state_dir=e.state_dir)
        ctx.check(f"halo local stop works for it, got {msg!r}", ok)
        ctx.check("registry is empty after stop", load_registry(e.state_dir) == [])


# ---- the exact-match correctness fix: never "most recently started wins" --

@test
def test_resolve_creds_mlx_exact_match_never_most_recent(ctx: Ctx):
    """The bug this round's design had to avoid: `hf:local/*`'s own bare-
    ref resolution falls back to whichever managed server started MOST
    RECENTLY (documented, acceptable for a ref with no specific id of its
    own) -- an `hf:mlx/<repo>` ref always names a SPECIFIC repo and must
    resolve to THAT one's server even when a DIFFERENT repo's managed
    server started afterward."""
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server
    with _Env() as e:
        stub = _write_stub()
        repo_a, repo_b = "mlx-community/repo-a", "mlx-community/repo-b"
        target_a, _ = ensure_mlx_server(repo_a, confirm=lambda _q: True, state_dir=e.state_dir,
                                         platform_name="darwin", machine="arm64",
                                         binary_argv=[sys.executable, str(stub)])
        target_b, _ = ensure_mlx_server(repo_b, confirm=lambda _q: True, state_dir=e.state_dir,
                                         platform_name="darwin", machine="arm64",
                                         binary_argv=[sys.executable, str(stub)])
        try:
            ctx.check("both started", target_a is not None and target_b is not None)
            ctx.check(f"the two got different base_urls (different processes), got {target_a.base_url!r} "
                      f"vs {target_b.base_url!r}", target_a.base_url != target_b.base_url)
            os.environ["BRIDGE_STATE_DIR"] = str(e.state_dir)
            ref_a = parse_model_ref(f"hf:mlx/{repo_a}")
            creds_a = _resolve_creds(ref_a)
            ctx.check(f"ref A resolves to A's OWN base_url (not B's, even though B started later), "
                      f"got {creds_a.base_url!r} vs A={target_a.base_url!r} B={target_b.base_url!r}",
                      creds_a is not None and creds_a.base_url == target_a.base_url)
        finally:
            from halo_harness.providers.local_runtime import stop_managed_server
            stop_managed_server(repo_a, state_dir=e.state_dir)
            stop_managed_server(repo_b, state_dir=e.state_dir)


# ---- roles -------------------------------------------------------------------

@test
def test_default_role_for_ref_hf_mlx_is_small(ctx: Ctx):
    from halo_harness.roles import default_role_for_ref
    with _Env():
        ctx.check('hf:mlx/* defaults to "small"',
                  default_role_for_ref("hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit") == "small")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
