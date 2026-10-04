"""tests.test_providers_huggingface_hub_cache -- Halo 2.0.3 round 5: the
Hugging Face Hub cache scan, against a FIXTURE tree only -- never the
real `~/.cache/huggingface/hub` (every test here builds its own tree
under a fresh tempdir and passes it as `root=` explicitly).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _make_model(root: Path, dirname: str, *, blob_name: str, blob_bytes: bytes, snapshot_filename: str) -> None:
    model_dir = root / dirname
    blobs = model_dir / "blobs"
    snapshots = model_dir / "snapshots" / "deadbeef"
    blobs.mkdir(parents=True)
    snapshots.mkdir(parents=True)
    blob_path = blobs / blob_name
    blob_path.write_bytes(blob_bytes)
    link_path = snapshots / snapshot_filename
    try:
        os.symlink(blob_path, link_path)
    except (OSError, NotImplementedError):
        # No symlink permission (some Windows configurations) -- a plain
        # copy still exercises the same filename-based format detection
        # and a slightly inflated size count, acceptable for this fixture.
        link_path.write_bytes(blob_bytes)


@test
def test_scan_empty_or_missing_root_returns_nothing(ctx: Ctx):
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    d = Path(tempfile.mkdtemp(prefix="hubcache-empty-"))
    ctx.check("a missing root returns []", scan_hub_cache(root=d / "does-not-exist") == [])
    ctx.check("an empty existing root returns []", scan_hub_cache(root=d) == [])


@test
def test_scan_reports_repo_id_size_and_gguf_format(ctx: Ctx):
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    d = Path(tempfile.mkdtemp(prefix="hubcache-gguf-"))
    _make_model(d, "models--Qwen--Qwen3-32B", blob_name="a" * 40, blob_bytes=b"\0" * 2048,
                snapshot_filename="Qwen3-32B-Q4_K_M.gguf")
    rows = scan_hub_cache(root=d)
    ctx.check(f"exactly one row, got {rows}", len(rows) == 1)
    row = rows[0]
    ctx.check(f"repo_id reconstructed, got {row.repo_id!r}", row.repo_id == "Qwen/Qwen3-32B")
    ctx.check(f"dirname kept verbatim, got {row.dirname!r}", row.dirname == "models--Qwen--Qwen3-32B")
    ctx.check(f"size counts the blob's real bytes, got {row.size_bytes}", row.size_bytes == 2048)
    ctx.check(f"gguf format detected, got {row.formats}", row.formats == ("gguf",))


@test
def test_scan_reports_safetensors_format_and_multiple_models(ctx: Ctx):
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    d = Path(tempfile.mkdtemp(prefix="hubcache-multi-"))
    _make_model(d, "models--meta-llama--Llama-3", blob_name="b" * 40, blob_bytes=b"\0" * 4096,
                snapshot_filename="model.safetensors")
    _make_model(d, "models--Qwen--Qwen3-32B", blob_name="c" * 40, blob_bytes=b"\0" * 1024,
                snapshot_filename="Qwen3-32B.gguf")
    rows = {r.repo_id: r for r in scan_hub_cache(root=d)}
    ctx.check(f"both models present, got {sorted(rows)}", set(rows) == {"meta-llama/Llama-3", "Qwen/Qwen3-32B"})
    ctx.check(f"safetensors detected, got {rows['meta-llama/Llama-3'].formats}",
              rows["meta-llama/Llama-3"].formats == ("safetensors",))


@test
def test_scan_never_double_counts_blobs_as_snapshots(ctx: Ctx):
    """Size comes from `blobs/` ONLY -- a snapshot symlink pointing back at
    the same blob must never be counted a second time."""
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    d = Path(tempfile.mkdtemp(prefix="hubcache-nodouble-"))
    _make_model(d, "models--org--one-file", blob_name="d" * 40, blob_bytes=b"\0" * 777,
                snapshot_filename="model.gguf")
    rows = scan_hub_cache(root=d)
    ctx.check(f"size is the blob's size, not 2x it, got {rows[0].size_bytes}", rows[0].size_bytes == 777)


@test
def test_scan_skips_a_top_level_symlink_escaping_the_cache_root(ctx: Ctx):
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    d = Path(tempfile.mkdtemp(prefix="hubcache-escape-"))
    outside = Path(tempfile.mkdtemp(prefix="hubcache-outside-"))
    (outside / "evil").mkdir()
    link = d / "models--escaped--repo"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        ctx.check("symlinks unsupported on this host -- nothing to test, skipped cleanly", True)
        return
    _make_model(d, "models--real--repo", blob_name="e" * 40, blob_bytes=b"\0" * 10, snapshot_filename="model.gguf")
    rows = scan_hub_cache(root=d)
    repo_ids = {r.repo_id for r in rows}
    ctx.check(f"the escaping entry is skipped, got {repo_ids}", "escaped/repo" not in repo_ids)
    ctx.check(f"the real entry is still reported, got {repo_ids}", "real/repo" in repo_ids)


@test
def test_scan_root_resolution_env_precedence(ctx: Ctx):
    from halo_harness.providers.huggingface_hub_cache import resolve_hub_cache_root
    d = Path(tempfile.mkdtemp(prefix="hubcache-root-"))
    hub_cache_dir, home_dir = d / "direct-hub-cache", d / "hf-home"
    ctx.check("HF_HUB_CACHE wins outright", resolve_hub_cache_root(
        {"HF_HUB_CACHE": str(hub_cache_dir), "HF_HOME": str(home_dir)}) == hub_cache_dir)
    ctx.check("HF_HOME/hub when HF_HUB_CACHE unset",
              resolve_hub_cache_root({"HF_HOME": str(home_dir)}) == home_dir / "hub")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
