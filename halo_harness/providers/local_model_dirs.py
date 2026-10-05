"""halo_harness.providers.local_model_dirs -- Halo 2.0.3 round 5c (brief
item 1): "discovery, with the user in control of where" -- `huggingface.
model_dirs`, a user-maintained list of folders scanned recursively (depth-
limited, symlink-safe) for `.gguf` files, safetensors model folders (a
`config.json` beside `*.safetensors`), and MLX folders. `/local add
<path>`/`/local forget <path>` (`commands/builtins.py`) and the wizard's
"Local models" step both call `add_model_dir`/`forget_model_dir` below;
`halo local` and `providers.local_models.build_local_view` both call
`scan_model_dirs`.

Scan discipline matches every existing scanner in this codebase (`providers.
huggingface_hub_cache.scan_hub_cache`, `providers.lmstudio_cache.
scan_lmstudio_models`): `os.walk(..., followlinks=False)` throughout, which
never DESCENDS into a symlinked subdirectory at all (whether it points
back inside the scanned root or escapes it) -- the simplest, already-
established, deliberately conservative guard; a symlink "escaping the
root" fixture is covered by this for free, with no extra path-resolution
logic needed.

MLX folders are NOT a different on-disk shape from a safetensors model
folder (`config.json` beside `*.safetensors` either way) -- mlx-lm's own
quantize step writes safetensors too (GPU-RESEARCH.md section 7). This
module labels a folder "mlx" on a best-effort, UNCONFIRMED heuristic (never
independently verified against a real mlx-lm output this round): any path
component from the configured root down to the folder case-insensitively
contains "mlx" (covers an `mlx-community/...` folder name, the Hub org
`providers.local_models` already keys off for the HUB CACHE path), OR
`config.json` carries a top-level `"quantization"` key (mlx-lm's own
documented quantize-output convention, general knowledge, not fetched by
either research doc this round) -- every other safetensors folder is
labelled plain "safetensors". `providers.local_fit` reads both identically
either way; the label only ever affects which managed runtime `halo local
serve` prefers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge")

_MAX_WALK_DEPTH = 8  # a user's own folder tree; bounded only against a pathological structure


@dataclass(frozen=True)
class LocalModelFile:
    """One discovered file (`format == "gguf"`) or folder (`"safetensors"`/
    `"mlx"`). `path` is the GGUF file itself, or the folder holding
    `config.json`+`*.safetensors` -- exactly what `providers.local_fit.
    read_fit_inputs_for_path`'s own `fmt`-dispatch expects. `root` is the
    configured `huggingface.model_dirs` entry this came from (for display:
    "found under <root>")."""
    path: Path
    format: str  # "gguf" | "safetensors" | "mlx"
    size_bytes: int
    root: Path


def resolve_model_dirs(env: Optional[dict] = None) -> "list[Path]":
    """`huggingface.model_dirs` from `~/.halo/config.json` -- a plain list
    of path strings, expanded (`~`) but NOT required to exist here (a
    stale/typo'd entry is simply skipped, logged at DEBUG, when it's
    actually scanned). `env` is accepted for call-site symmetry with every
    other `resolve_*`-style function in `providers/`; this one reads no
    environment variable of its own today."""
    del env
    from halo_harness.theme import get_config_value
    raw = get_config_value("huggingface.model_dirs", default=None)
    if not isinstance(raw, list):
        return []
    return [Path(p).expanduser() for p in raw if isinstance(p, str) and p.strip()]


def add_model_dir(path: str) -> "tuple[bool, str]":
    """`/local add <path>`/the wizard's "add a folder" field. `(False,
    reason)` when `path` isn't an existing directory or is already in the
    list (compared by its RESOLVED path, so "." and the absolute path it
    resolves to are recognized as the same entry); otherwise appends and
    persists, returning `(True, message)`."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return False, f"not a directory: {p}"
    resolved = p.resolve()
    dirs = resolve_model_dirs()
    if any(d.resolve() == resolved for d in dirs):
        return False, f"already added: {resolved}"
    dirs.append(p)
    from halo_harness.theme import set_config_value
    set_config_value("huggingface.model_dirs", [str(d) for d in dirs])
    return True, f"added {resolved}"


def forget_model_dir(path: str) -> "tuple[bool, str]":
    """`/local forget <path>` -- matches by RESOLVED path (works even once
    the folder no longer exists on disk; `Path.resolve()` never requires
    existence). `(False, reason)` when nothing in the list matches."""
    p = Path(path).expanduser()
    resolved = p.resolve()
    dirs = resolve_model_dirs()
    kept = [d for d in dirs if d.resolve() != resolved]
    if len(kept) == len(dirs):
        return False, f"not in huggingface.model_dirs: {resolved}"
    from halo_harness.theme import set_config_value
    set_config_value("huggingface.model_dirs", [str(d) for d in kept])
    return True, f"removed {resolved}"


def _looks_like_mlx(folder: Path, root: Path, config: dict) -> bool:
    try:
        rel_parts = folder.relative_to(root).parts
    except ValueError:
        rel_parts = folder.parts
    if any("mlx" in part.lower() for part in rel_parts):
        return True
    return isinstance(config.get("quantization"), dict)


def _scan_one_root(root: Path) -> "list[LocalModelFile]":
    if not root.is_dir():
        return []
    from halo_harness.providers.safetensors_config import read_safetensors_config
    out: "list[LocalModelFile]" = []
    for current_root, dirs, names in os.walk(root, followlinks=False):
        current = Path(current_root)
        depth = len(current.parts) - len(root.parts)
        if depth > _MAX_WALK_DEPTH:
            dirs[:] = []
            continue
        lower_names = {n.lower() for n in names}
        safetensors_files = [n for n in names if n.lower().endswith(".safetensors")]
        if "config.json" in lower_names and safetensors_files:
            config = read_safetensors_config(current) or {}
            fmt = "mlx" if _looks_like_mlx(current, root, config) else "safetensors"
            size = sum((current / n).stat().st_size for n in names
                       if (current / n).is_file() and not (current / n).is_symlink())
            out.append(LocalModelFile(path=current, format=fmt, size_bytes=size, root=root))
            dirs[:] = []  # a transformers folder's own subdirs (checkpoints, ...) are never scanned further
            continue
        for n in names:
            if n.lower().endswith(".gguf"):
                p = current / n
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                out.append(LocalModelFile(path=p, format="gguf", size_bytes=size, root=root))
    return out


def describe_path(path) -> Optional[LocalModelFile]:
    """Classifies ONE path directly (no directory walk) -- `halo local
    serve`/`import <model>`'s own "the user gave an exact path, possibly
    outside any configured `huggingface.model_dirs` entry" case. `None`
    when `path` is neither a `.gguf` file nor a safetensors-shaped folder
    (`config.json` beside `*.safetensors`)."""
    p = Path(path).expanduser()
    if p.is_file() and p.suffix.lower() == ".gguf":
        return LocalModelFile(path=p, format="gguf", size_bytes=p.stat().st_size, root=p.parent)
    if p.is_dir():
        from halo_harness.providers.safetensors_config import read_safetensors_config
        config = read_safetensors_config(p)
        safetensors_files = [n for n in p.iterdir() if n.is_file() and n.suffix.lower() == ".safetensors"]
        if config is not None and safetensors_files:
            fmt = "mlx" if _looks_like_mlx(p, p.parent, config) else "safetensors"
            size = sum(n.stat().st_size for n in p.iterdir() if n.is_file())
            return LocalModelFile(path=p, format=fmt, size_bytes=size, root=p.parent)
    return None


def scan_model_dirs(dirs: "Optional[list]" = None, env: Optional[dict] = None) -> "list[LocalModelFile]":
    """One row per discovered `.gguf` file or safetensors/MLX folder,
    across every `huggingface.model_dirs` entry (or the explicit `dirs`
    list a test passes instead of reading config -- tests ALWAYS pass this
    explicitly, never the real configured list). A missing/non-directory
    root is skipped, never an error."""
    roots = [Path(d) for d in dirs] if dirs is not None else resolve_model_dirs(env)
    out: "list[LocalModelFile]" = []
    for root in roots:
        try:
            out.extend(_scan_one_root(root))
        except OSError:
            log.debug("local_model_dirs: scan of %r failed", root, exc_info=True)
    return out


def find_by_name(query: str, env: Optional[dict] = None) -> Optional[LocalModelFile]:
    """`halo local serve <model>`'s convenience lookup when `<model>`
    isn't itself an existing path: the first `scan_model_dirs()` entry
    whose file/folder NAME (case-insensitive, with or without the
    `.gguf`/`.safetensors` suffix) equals `query` -- `None` when nothing
    matches (the caller then reports plainly, never guesses a different
    model)."""
    target = query.strip().lower()
    for entry in scan_model_dirs(env=env):
        stem = entry.path.name.lower()
        if stem == target or Path(stem).stem == target:
            return entry
    return None
