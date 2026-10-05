"""halo_harness.providers.huggingface_mlx -- Halo 2.0.3 round 5f: the
`hf:mlx/<org>/<repo>` route. Apple Silicon only, off by default, lazy
import (nothing here is imported unless an `hf:mlx/*` ref is actually
parsed/resolved -- a box with no `mlx-lm` installed, or not even a Mac, is
completely untouched). Builds on round 5c's managed-server primitives
(`providers.local_runtime.start_managed_server`/the `~/.halo/run/local-
servers.json` registry) UNCHANGED: `mlx_lm_server_argv` already takes
`model_path` as a bare string and does `str(model_path)`, so passing a Hub
repo id (never a real path) works today with no change to that module --
`mlx_lm.server --model <repo_id>` is exactly how mlx-lm's own README says
to point it at a Hub repo (it downloads/reuses the Hub cache on first use,
GPU-RESEARCH.md section 7).

Design: `plans/2.0.3-ollama-round2-brief.md` "Round 5f",
`docs/harness/GPU-RESEARCH.md` section 7 and the Apple memory-share rule.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

from halo_harness.providers.huggingface_local_resolve import ResolvedLocalServer

_UNSUPPORTED_SENTENCE = "MLX runs on Apple Silicon only."


def is_apple_silicon(*, platform_name: Optional[str] = None, machine: Optional[str] = None) -> bool:
    """`True` only for macOS on an arm64/aarch64 CPU -- deliberately NOT
    just `sys.platform == "darwin"` (an Intel Mac has no Metal unified-
    memory story mlx-lm's own README describes, and PyPI's own classifiers
    for `mlx-lm` now list Linux/CUDA/CPU extras too -- GPU-RESEARCH.md's
    "Corrections" section 4 -- so "darwin" alone is no longer a safe
    Apple-Silicon proxy). `platform_name`/`machine`, when given, REPLACE
    `sys.platform`/`platform.machine()` -- the test seam, so a hermetic
    suite can pin both the "yes" and "no" cases on ANY real host."""
    platform_name = platform_name if platform_name is not None else sys.platform
    if platform_name != "darwin":
        return False
    if machine is None:
        import platform as _platform
        machine = _platform.machine()
    return (machine or "").lower() in ("arm64", "aarch64")


def mlx_unsupported_sentence() -> str:
    """Brief item 1: "on other platforms `hf:mlx/...` gives one plain
    sentence and nothing else changes" -- exactly this string, nothing
    appended (no fix hint, no "see docs" -- there is nothing to fix on a
    non-Apple-Silicon machine)."""
    return _UNSUPPORTED_SENTENCE


def _approx_cached_size_bytes(repo_id: str, *, env: Optional[dict] = None) -> Optional[int]:
    """The repo's own on-disk size IF it's already (partially or fully) in
    the Hub cache -- `None` when it isn't cached at all yet, in which case
    the consent sentence names the repo without a size rather than
    guessing one (mlx_lm's own download size isn't knowable without
    calling the Hub API, which round 5f's brief does not ask this
    sentence to do)."""
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    for m in scan_hub_cache(env=env):
        if m.repo_id == repo_id:
            return m.size_bytes
    return None


def consent_sentence(repo_id: str, *, env: Optional[dict] = None) -> str:
    """Brief item 2: "names the repo, the approximate size when the hub
    cache or the API tells it, and the cache destination." This round
    reads the size from the hub cache ONLY (never a live Hub API call --
    this function/`ensure_mlx_server` never touch the network themselves;
    `mlx_lm.server` itself is what downloads, the next time it runs) --
    unknown when nothing is cached for this repo yet, named plainly rather
    than guessed."""
    from halo_harness.providers.huggingface_hub_cache import resolve_hub_cache_root
    from halo_harness.providers.ollama_panel import human_bytes
    cache_dir = resolve_hub_cache_root(env)
    size_bytes = _approx_cached_size_bytes(repo_id, env=env)
    size_text = f"already {human_bytes(size_bytes)} in the Hub cache" if size_bytes else \
        "size unknown until mlx_lm.server starts the download"
    return (f"Halo will start a managed mlx_lm.server for {repo_id} on a free loopback port -- "
            f"{size_text}; weights are fetched into (or reused from) {cache_dir} by mlx_lm itself, "
            f"never added to PATH, stopped when this session exits unless kept, removable with "
            f"`halo local stop {repo_id}`.")


def ensure_mlx_server(repo_id: str, *, confirm: "Optional[Callable[[str], bool]]" = None, state_dir=None,
                       keep: bool = False, port: "Optional[int]" = None, platform_name: Optional[str] = None,
                       machine: Optional[str] = None, binary_argv: "Optional[list]" = None,
                       env: Optional[dict] = None) -> "tuple[Optional[ResolvedLocalServer], list[str]]":
    """`(target, lines)` -- the ONE function both `headless.build_session`
    and `doctor_local.py`/`halo local serve --runtime mlx_lm` call to turn
    an `hf:mlx/<repo_id>` ref into a running, reachable server:

    1. Not Apple Silicon -> `(None, [mlx_unsupported_sentence()])`, nothing
       else attempted (brief item 1).
    2. Already running (an EXACT `model == repo_id` match in the round 5c
       registry, `huggingface_local_resolve.resolve_managed_server_by_
       model`) -> reused as-is, no consent asked again, no second process
       started (brief item 2: "reused when already running").
    3. Otherwise: prints `consent_sentence`, asks `confirm` (default: a
       bare `lambda _q: True` -- every real call site auto-proceeds and
       just prints the notice, the same "explicit ref typed by the user IS
       the consent" rule every other `hf:`/`or:` cloud ref already follows
       with no separate gate; `confirm` stays injectable so a test can
       still pin the decline path). Declining returns `(None, lines)`
       with nothing started. `mlx_lm` not importable -> `(None, lines)`
       naming the extra to install.
    4. Starts it (`providers.local_runtime.start_managed_server`, runtime
       "mlx_lm", `model_path=repo_id` -- a bare string, exactly what
       `mlx_lm_server_argv` already handles), registered under `model=
       repo_id` so a LATER `hf:mlx/<same repo>` call (even from a fresh
       `halo` process) finds it via step 2 instead of starting a second
       one, and `halo local stop <repo_id>` works unchanged.

    `binary_argv`, when given, REPLACES the real `find_runtime_binary
    ("mlx_lm", ...)` call -- the test seam (a hermetic suite has no real
    `mlx-lm` pip install; it injects a fake stub's argv instead, same
    pattern `local_use.ensure_llama_server_binary`'s `fetcher`/`releases`
    seams already use)."""
    if not is_apple_silicon(platform_name=platform_name, machine=machine):
        return None, [mlx_unsupported_sentence()]
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    from halo_harness.providers.huggingface_local_resolve import resolve_managed_server_by_model
    existing = resolve_managed_server_by_model(repo_id, state_dir=state_dir)
    if existing is not None:
        return existing, [f"Reusing the running mlx_lm server for {repo_id} on {existing.base_url}."]
    confirm = confirm or (lambda _q: True)
    lines = [consent_sentence(repo_id, env=env)]
    if not confirm(lines[0]):
        lines.append("Declined -- nothing started.")
        return None, lines
    from halo_harness.providers.local_runtime import find_runtime_binary, start_managed_server
    binary = binary_argv if binary_argv is not None else find_runtime_binary("mlx_lm", state_dir=state_dir)
    if binary is None:
        lines.append('mlx_lm is not installed -- run `uv tool install "halo-harness[mlx]"` and try again '
                     "(docs/harness/GPU-RESEARCH.md section 7).")
        return None, lines
    entry, reason = start_managed_server(model=repo_id, runtime="mlx_lm", binary_argv=binary, model_path=repo_id,
                                          port=port, keep=keep, state_dir=state_dir)
    if entry is None:
        lines.append(reason)
        return None, lines
    lines.append(f"Serving {repo_id} via mlx_lm on port {entry.port} -- use it as hf:mlx/{repo_id}")
    return ResolvedLocalServer(name=repo_id, base_url=entry.base_url, api_key=None), lines
