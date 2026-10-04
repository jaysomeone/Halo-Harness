"""halo_harness.providers.huggingface_hub_cache -- Halo 2.0.3 round 5:
walks the Hugging Face Hub's local model cache for models present on disk
but not necessarily being served by anything right now -- the second
input (alongside a running server's own `/v1/models`) the shared `/local`
discovery view needs. Design: `plans/2.0.3-ollama-round2-brief.md` "Round
5" and `docs/harness/LOCAL-MODELS-RESEARCH.md` section 8's own "How models
arrive on disk" paragraph.

Layout (research doc section 8): `hf download` populates
`<hub_root>/models--<org>--<name>/{blobs/, snapshots/<revision>/, refs/}`
-- `blobs/` holds the actual content-addressed files (real on-disk usage);
`snapshots/<revision>/` holds symlinks back to those blobs, named like the
repo's real filenames (this is where a `.safetensors`/`.gguf` extension is
actually visible -- blob names are bare hashes). This module reads
STRUCTURE directly (file names + sizes) rather than shelling out to `hf
cache ls`, since the `/local` view needs structured data, not CLI text
(the research doc's own framing for why).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge")

_FORMAT_SUFFIXES = (".safetensors", ".gguf")


def resolve_hub_cache_root(env: Optional[dict] = None) -> Path:
    """`$HF_HUB_CACHE` (used AS-IS, the hub dir itself) else `$HF_HOME/hub`
    else `~/.cache/huggingface/hub` (research doc section 8). Never
    checked for existence here -- `scan_hub_cache` treats a missing root
    as "nothing cached", not an error."""
    env = env if env is not None else os.environ
    hub_cache = env.get("HF_HUB_CACHE")
    if hub_cache:
        return Path(hub_cache)
    hf_home = env.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


@dataclass(frozen=True)
class HubCacheModel:
    """One `models--<org>--<name>` directory. `repo_id` is the
    reconstructed `org/name` (best-effort -- see `_repo_id_from_dirname`);
    `size_bytes` sums `blobs/`'s real files only (never double-counting the
    `snapshots/` symlinks that point back to them); `formats` is a sorted
    tuple of extensions found among `snapshots/**` file NAMES (a blob's own
    name is a bare content hash, never a usable extension)."""
    repo_id: str
    dirname: str
    size_bytes: int
    formats: "tuple[str, ...]" = field(default_factory=tuple)


def _repo_id_from_dirname(dirname: str) -> str:
    """`models--Qwen--Qwen3-32B` -> `Qwen/Qwen3-32B`. `split("--", 1)` --
    the ordinary case has exactly one org/name separator; an org or model
    name that itself contains "--" is a documented edge case this doesn't
    try to disambiguate further (the raw `dirname` is always ALSO kept on
    the row, so nothing is lost, just possibly mis-split)."""
    rest = dirname[len("models--"):] if dirname.startswith("models--") else dirname
    org, sep, name = rest.partition("--")
    return f"{org}/{name}" if sep else org


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
            except OSError:
                continue
    return total


def _formats_in_snapshots(snapshots_dir: Path) -> "tuple[str, ...]":
    found = set()
    for root, _dirs, names in os.walk(snapshots_dir, followlinks=False):
        for n in names:
            low = n.lower()
            for suffix in _FORMAT_SUFFIXES:
                if low.endswith(suffix):
                    found.add(suffix.lstrip("."))
    return tuple(sorted(found))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def scan_hub_cache(root: Optional[Path] = None, env: Optional[dict] = None) -> "list[HubCacheModel]":
    """One row per `models--<org>--<name>` directory directly under
    `root` (or `resolve_hub_cache_root(env)` when `root` is omitted --
    tests ALWAYS pass an explicit fixture `root` instead, never the real
    cache). `[]` when the root doesn't exist, isn't a directory, or is
    empty -- never an error. A top-level entry whose NAME starts with
    `models--` but is itself a symlink resolving OUTSIDE `root` is
    skipped, logged at DEBUG, never followed (round 5 brief item 3: "never
    following symlinks outside the cache")."""
    root = Path(root) if root is not None else resolve_hub_cache_root(env)
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith("models--") or not entry.is_dir():
            continue
        if entry.is_symlink() and not _is_within(entry, root):
            log.debug("huggingface_hub_cache: skipped %r -- a symlink resolving outside the cache root", entry.name)
            continue
        blobs_dir = entry / "blobs"
        size_bytes = _dir_size_bytes(blobs_dir) if blobs_dir.is_dir() else _dir_size_bytes(entry)
        snapshots_dir = entry / "snapshots"
        formats = _formats_in_snapshots(snapshots_dir) if snapshots_dir.is_dir() else ()
        out.append(HubCacheModel(repo_id=_repo_id_from_dirname(entry.name), dirname=entry.name,
                                  size_bytes=size_bytes, formats=formats))
    return out
