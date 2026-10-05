"""halo_harness.providers.lmstudio_cache -- Halo 2.0.3 round 5b part 2
(brief item 6, "the machine Halo runs on is a host too"): LM Studio's own
model folder joins the Hugging Face Hub cache scan (`providers.
huggingface_hub_cache.scan_hub_cache`) as a second on-disk source for
`/local` -- models LM Studio has already downloaded through its own
in-app browser (research doc section 8: "Models download through LM
Studio's own in-app model browser (Hub-backed) rather than the `hf` CLI"),
so they never show up in the `~/.cache/huggingface/hub` scan at all.

Default path: `~/.lmstudio/models` -- LM Studio's own documented default
on every OS this round could check for (the brief itself names this path;
the exact in-app "Model Storage" setting that can move it was NOT
independently confirmed live this round -- `huggingface.lmstudio_models_dir`
below is the escape hatch for a box where it's been moved). Layout: LM
Studio mirrors the Hub's own `<publisher>/<model-name>/` nesting as plain
folders (never the `models--org--name` blob/snapshot structure the REAL
Hub cache uses) -- a GGUF file sits directly in the model folder, or a
transformers-style folder holds `config.json` beside `*.safetensors`; this
module reads that structure directly, the same "never shell out, read
files" rule `huggingface_hub_cache.py` already follows.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from halo_harness.providers.huggingface_hub_cache import HubCacheModel

log = logging.getLogger("bridge")

_GGUF_SUFFIX = ".gguf"
_MAX_WALK_DEPTH = 6  # a user's own model folder; bounded only against a pathological symlink loop


def resolve_lmstudio_models_dir(env: Optional[dict] = None) -> Path:
    """`huggingface.lmstudio_models_dir` (config.json, an escape hatch for
    a box where LM Studio's own "Model Storage" setting moved the folder
    -- UNCONFIRMED exactly where that setting itself persists, so Halo
    never reads it directly) else `~/.lmstudio/models`, the documented
    default. Never checked for existence here -- `scan_lmstudio_models`
    treats a missing folder as "nothing found", not an error."""
    del env  # accepted for call-site symmetry with scan_hub_cache's own env-seam; no env var of its own today
    from halo_harness.theme import get_config_value
    configured = get_config_value("huggingface.lmstudio_models_dir", default=None)
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    return Path.home() / ".lmstudio" / "models"


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(path, followlinks=False):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
            except OSError:
                continue
    return total


def _relative_name(path: Path, root: Path) -> str:
    try:
        return "/".join(path.relative_to(root).parts)
    except ValueError:
        return path.name


def scan_lmstudio_models(root: Optional[Path] = None, env: Optional[dict] = None) -> "list[HubCacheModel]":
    """One `HubCacheModel` row per bare `.gguf` file OR per transformers-
    style folder (`config.json` beside `*.safetensors`) found under `root`
    (or `resolve_lmstudio_models_dir(env)` when omitted -- tests ALWAYS
    pass an explicit fixture `root`, never the real folder). `[]` when the
    root doesn't exist or isn't a directory -- never an error. `repo_id`
    is the slash-joined path relative to `root` (LM Studio's own
    `<publisher>/<model>/...` nesting, not the Hub cache's `org/name`
    reconstruction -- this module's rows are never addressable as an
    `hf:<org>/<model>` ref by themselves, only discoverable, matching the
    round 5c brief's own "discovery is not yet use" framing)."""
    root = Path(root) if root is not None else resolve_lmstudio_models_dir(env)
    if not root.is_dir():
        return []
    out: "list[HubCacheModel]" = []
    seen_dirs: set = set()
    root_depth = len(root.parts)
    for current_root, dirs, names in os.walk(root, followlinks=False):
        current = Path(current_root)
        if len(current.parts) - root_depth > _MAX_WALK_DEPTH:
            dirs[:] = []
            continue
        lower_names = {n.lower(): n for n in names}
        has_config = "config.json" in lower_names
        safetensors = [n for n in names if n.lower().endswith(".safetensors")]
        if has_config and safetensors:
            size = _dir_size_bytes(current)
            out.append(HubCacheModel(repo_id=_relative_name(current, root), dirname=current.name,
                                      size_bytes=size, formats=("safetensors",), path=current))
            seen_dirs.add(current)
            dirs[:] = []  # a transformers folder's own subdirs (checkpoints, etc.) are never scanned further
            continue
        for n in names:
            if n.lower().endswith(_GGUF_SUFFIX):
                p = current / n
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                out.append(HubCacheModel(repo_id=_relative_name(p, root), dirname=p.name,
                                          size_bytes=size, formats=("gguf",), path=p))
    return out
