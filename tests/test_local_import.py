"""tests.test_local_import -- Halo 2.0.3 round 5c, FIX PASS after the live
run: the Ollama import path (option B) against tests/helpers/mock_ollama.
MockUpstream's new `/api/blobs/*` HEAD/POST handling -- sha256, the blob
HEAD/upload round trip, the `files`-shaped `/api/create` body, the
consent sentence, the upload-progress callback, the success/error
streamed-status paths, and the non-GGUF refusal. The OLDER `modelfile`
form is gone (live run: `HTTP 400 {"error":"neither 'from' or 'files' was
specified"}` on build 0.34.2) -- nothing here builds or sends one.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_ollama import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _write_gguf(size: int = 2048) -> Path:
    d = Path(tempfile.mkdtemp(prefix="local-import-"))
    p = d / "qwen3-test.gguf"
    p.write_bytes(b"GGUF" + b"\0" * size)
    return p


@test
def test_sha256_file_matches_hashlib(ctx: Ctx):
    import hashlib
    from halo_harness.providers.ollama_import import sha256_file
    p = _write_gguf(size=5000)
    ctx.check("streamed sha256 matches a plain hashlib read",
              sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest())


@test
def test_consent_sentence_names_size_and_destination_store(ctx: Ctx):
    from halo_harness.providers.ollama_import import import_consent_sentence
    p = _write_gguf(size=2048)
    text = import_consent_sentence(p, size_bytes=p.stat().st_size, name="qwen3-test")
    ctx.check(f"names the file, got {text!r}", p.name in text)
    ctx.check(f"names the new model name, got {text!r}", "qwen3-test" in text)
    ctx.check(f"names Ollama's store, got {text!r}", "Ollama" in text and "store" in text)


@test
def test_default_import_name_from_file_stem(ctx: Ctx):
    from halo_harness.providers.ollama_import import default_import_name
    ctx.check("lowercased, spaces/underscores to dashes",
              default_import_name("/x/My Model_Name.gguf") == "my-model-name")


@test
def test_import_uploads_a_new_blob_then_creates_with_files(ctx: Ctx):
    """The blob is NOT yet on the mock daemon -- HEAD 404s, so the raw
    bytes are POSTed before /api/create, which must carry a `files` map
    keyed by the file's own basename to `sha256:<hex>`, never a bare
    `modelfile` field."""
    import hashlib
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_import import import_gguf_to_ollama
    p = _write_gguf()
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    statuses = []
    progress = []
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        ok, msg = import_gguf_to_ollama(host, "qwen3-test", p, on_status=statuses.append,
                                         on_upload_progress=lambda sent, total: progress.append((sent, total)))
        ctx.check(f"import succeeds, got ({ok}, {msg!r})", ok and msg == "success")
        ctx.check(f"progress lines were delivered, got {statuses}", len(statuses) >= 2)
        ctx.check(f"upload progress was reported, got {progress}",
                  progress and progress[-1] == (p.stat().st_size, p.stat().st_size))
        head_calls = [r for r in mock.requests if r["method"] == "HEAD" and digest in r["path"]]
        ctx.check(f"the blob was HEAD-checked first, got {head_calls}", len(head_calls) == 1)
        blob_posts = [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/").endswith(digest)]
        ctx.check(f"exactly one blob upload, got {blob_posts}", len(blob_posts) == 1)
        ctx.check(f"the uploaded bytes hash to the same digest, got {blob_posts[0]['body']}",
                  blob_posts[0]["body"]["_digest_matches_bytes"] is True)
        create_calls = [r for r in mock.requests if r["path"].rstrip("/") == "/api/create"]
        ctx.check(f"exactly one /api/create call, got {create_calls}", len(create_calls) == 1)
        sent = create_calls[0]["body"]
        ctx.check(f"model name on the wire, got {sent}", sent.get("model") == "qwen3-test")
        ctx.check(f"files map on the wire (never modelfile), got {sent}",
                  sent.get("files") == {p.name: f"sha256:{digest}"} and "modelfile" not in sent)
        ctx.check(f"stream requested, got {sent}", sent.get("stream") is True)


@test
def test_import_skips_the_upload_when_the_blob_already_exists(ctx: Ctx):
    import hashlib
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_import import import_gguf_to_ollama
    p = _write_gguf()
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    with MockUpstream(known_blobs={digest}) as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        ok, _msg = import_gguf_to_ollama(host, "qwen3-test", p)
        ctx.check("import still succeeds", ok)
        blob_posts = [r for r in mock.requests if r["method"] == "POST" and digest in r["path"]]
        ctx.check(f"no upload happened, got {blob_posts}", blob_posts == [])
        create_calls = [r for r in mock.requests if r["path"].rstrip("/") == "/api/create"]
        ctx.check(f"create still ran with the files map, got {create_calls}",
                  len(create_calls) == 1 and create_calls[0]["body"]["files"] == {p.name: f"sha256:{digest}"})


@test
def test_import_error_status_line_surfaces_the_message(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_import import import_gguf_to_ollama
    p = _write_gguf()
    with MockUpstream(create_responses={"bad-model": [{"status": "verifying blob"},
                                                        {"error": "invalid file magic"}]}) as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        ok, msg = import_gguf_to_ollama(host, "bad-model", p)
        ctx.check(f"import fails plainly, got ({ok}, {msg!r})", ok is False and msg == "invalid file magic")


@test
def test_import_refuses_a_missing_file(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_import import import_gguf_to_ollama
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        ok, msg = import_gguf_to_ollama(host, "x", Path(tempfile.mkdtemp()) / "nope.gguf")
        ctx.check(f"missing file refused, got ({ok}, {msg!r})", ok is False and "not a file" in msg)


@test
def test_import_refuses_a_non_gguf_suffix(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_import import import_gguf_to_ollama
    d = Path(tempfile.mkdtemp(prefix="not-gguf-"))
    p = d / "model.safetensors"
    p.write_bytes(b"\0" * 10)
    with MockUpstream() as mock:
        host = OllamaHost(name="default", url=mock.base_url)
        ok, msg = import_gguf_to_ollama(host, "x", p)
        ctx.check(f"non-gguf refused with the plain sentence, got ({ok}, {msg!r})",
                  ok is False and "halo local serve" in msg)


@test
def test_local_use_import_local_model_prints_an_upload_progress_line(ctx: Ctx):
    from halo_harness.providers.local_use import import_local_model
    from halo_harness.providers.ollama import OllamaHost
    import halo_harness.providers.ollama as ollama_mod
    p = _write_gguf()
    with MockUpstream() as mock:
        real_resolve = ollama_mod.resolve_ollama_host
        ollama_mod.resolve_ollama_host = lambda name=None, env=None: OllamaHost(name="default", url=mock.base_url)
        try:
            ok, lines = import_local_model(str(p), name="my-import", confirm=lambda _q: True)
        finally:
            ollama_mod.resolve_ollama_host = real_resolve
        ctx.check(f"import succeeds, got {lines}", ok)
        ctx.check(f"an uploading progress line was printed, got {lines}",
                  any("Uploading" in ln for ln in lines))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
