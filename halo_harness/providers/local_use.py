"""halo_harness.providers.local_use -- Halo 2.0.3 round 5c (brief items 3
and 4, "use it"): the orchestration behind `halo local serve|stop|import`
and `halo local runtime remove`, kept separate from `local_cli.py`'s own
argument parsing so each step is unit-testable directly (every function
here returns `(ok, lines)` -- plain strings to print -- rather than
calling `print()` itself, and takes a `confirm(question) -> bool`
callable instead of ever touching `stdin` directly, so a test supplies
`lambda q: True/False` with no real terminal involved).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional


def resolve_local_file(model: str, env: Optional[dict] = None):
    """`<model>` -> `providers.local_model_dirs.LocalModelFile` -- an
    exact existing path first, else a name match across every configured
    `huggingface.model_dirs` entry. `None` when neither resolves."""
    from halo_harness.providers.local_model_dirs import describe_path, find_by_name
    direct = describe_path(model)
    return direct if direct is not None else find_by_name(model, env=env)


def model_id_for_path(path, fmt: str) -> str:
    """The `model` name `start_managed_server`/the registry record a
    served file under -- the file's own stem for a `.gguf` file (no
    extension), else the folder's own name for a safetensors/MLX
    directory. ONE helper so `serve_local_model` (which starts a server
    under this id) and `providers.local_models` (which FIX PASS item C
    needs to look this exact id up in the registry to show "serving"
    status for a row that isn't even running yet from THIS process's own
    point of view) can never compute two different ids for the same
    file."""
    path = Path(path)
    return path.stem if fmt == "gguf" else path.name


def install_hint(platform_name: Optional[str] = None) -> str:
    """Brief item 3: "declining prints the one-line install hint per OS
    (brew, winget, the release page)"."""
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return "brew install llama.cpp"
    if platform_name == "win32":
        return "winget install llama.cpp (or download a release from github.com/ggml-org/llama.cpp/releases)"
    return "use your distro's package manager, or download a release from github.com/ggml-org/llama.cpp/releases"


def ensure_llama_server_binary(*, state_dir, confirm: "Callable[[str], bool]", fetcher=None,
                                releases: "Optional[list]" = None, hw_runner=None,
                                backend_override: Optional[str] = None,
                                cuda_runtime_installed: Optional[bool] = None,
                                platform_name: Optional[str] = None) -> "tuple[Optional[list], list]":
    """Finds `llama-server` on PATH/`~/.halo/runtimes/`, or offers to
    fetch+verify+unpack the pinned release (brief item 3) after printing
    the consent sentence and asking `confirm`. `releases`, when given,
    REPLACES the real `fetch_release_list()` call (the test seam: "a fake
    release served by the parameterized mock" -- FIX PASS: `/releases`,
    a LIST, not the single `/releases/latest` object, since that endpoint
    points at a non-binary release live). `backend_override` is `halo
    local serve --backend cuda|vulkan|cpu|metal` -- short-circuits the
    cuda-then-vulkan fallback chain to exactly that one choice.
    `cuda_runtime_installed`, when given, REPLACES the real `has_cuda_
    runtime_installed()` PATH scan (the test seam). `platform_name`, when
    given, REPLACES `sys.platform` for asset selection (FIX PASS: a
    hermetic test must pin which OS's asset it expects regardless of
    which real OS the suite happens to run on -- a test that reads
    `sys.platform` itself produced the WRONG expectation on a different
    host, not a code bug; see `tests/test_local_use.py`'s own fix). Returns
    `(binary_argv_or_None, lines)` -- `lines` is always populated
    (progress/decision text), `None` for the argv on any decline/failure."""
    platform_name = platform_name or sys.platform
    from halo_harness.providers.local_runtime import find_runtime_binary
    found = find_runtime_binary("llama-server", state_dir=state_dir)
    if found is not None:
        return found, []
    from halo_harness.providers.local_runtime_fetch import (backend_candidates, consent_sentence,
                                                              fetch_and_install_llama_server, fetch_release_list,
                                                              has_cuda_runtime_installed, select_release_and_assets)
    from halo_harness.providers.ollama_hw import probe_nvidia_cuda_version
    lines = []
    release_list = releases if releases is not None else fetch_release_list(fetcher=fetcher)
    if not release_list:
        lines.append(f"Could not reach the GitHub releases API to fetch llama-server; install it yourself: "
                      f"{install_hint()}")
        return None, lines
    # Always read the driver's own CUDA version, even with `--backend`
    # forced: `backend_override` only skips the cuda-vs-vulkan FAMILY
    # decision, never the version-level pick WITHIN cuda (12.x vs 13.x).
    cuda_version = probe_nvidia_cuda_version(runner=hw_runner)
    candidates = backend_candidates(platform_name=platform_name, cuda_version=cuda_version, override=backend_override)
    picked = None
    for backend in candidates:
        picked = select_release_and_assets(release_list, platform_name=platform_name, backend=backend,
                                            cuda_version=cuda_version)
        if picked is not None:
            break
    if picked is None:
        lines.append(f"No matching llama.cpp release asset for {platform_name}/{candidates[0]}; install it "
                      f"yourself: {install_hint()}")
        return None, lines
    release, chosen = picked
    # `backend` still holds the loop variable's value at the `break` above
    # -- the backend that actually matched, not necessarily `candidates[0]`.
    already_has_cuda = (has_cuda_runtime_installed(platform_name=platform_name) if cuda_runtime_installed is None
                         else cuda_runtime_installed)
    if backend == "cuda" and len(chosen) > 1 and already_has_cuda:
        chosen = chosen[:1]  # a CUDA toolkit is already here -- skip the redistributable download
    version_tag = release.get("tag_name", "latest")
    total_size = sum(a.get("size") or 0 for a in chosen)
    dest_dir = Path(state_dir) / "runtimes" / version_tag
    vulkan_alt_size = None
    if backend == "cuda":
        vk = select_release_and_assets(release_list, platform_name=platform_name, backend="vulkan")
        if vk is not None:
            vulkan_alt_size = sum(a.get("size") or 0 for a in vk[1])
    lines.append(consent_sentence([a["name"] for a in chosen], total_size, dest_dir,
                                   vulkan_alternative_size=vulkan_alt_size))
    for a in chosen:
        lines.append(f"  {a['name']}: {a.get('browser_download_url')}")
    if not confirm("Download and install now?"):
        lines.append(f"Declined -- install it yourself: {install_hint()}")
        return None, lines
    ok, message = fetch_and_install_llama_server(state_dir=state_dir, version_tag=version_tag, assets=chosen,
                                                   fetcher=fetcher)
    lines.append(message)
    if not ok:
        return None, lines
    return find_runtime_binary("llama-server", state_dir=state_dir), lines


def serve_local_model(model: str, *, runtime: Optional[str] = None, port: Optional[int] = None, keep: bool = False,
                       state_dir=None, confirm: "Optional[Callable[[str], bool]]" = None, env=None,
                       fetcher=None, releases: "Optional[list]" = None, hw_runner=None,
                       backend: Optional[str] = None) -> "tuple[bool, list]":
    """`halo local serve`/the `/local` dialog's `s` key, both routes. The
    `/local`-returned ref on success is `hf:local/<model_id>` (the last
    line, by construction -- see `local_cli.py`'s own echo of it)."""
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    confirm = confirm or (lambda _q: False)
    entry = resolve_local_file(model, env=env)
    if entry is None:
        # Round 5f: "`halo local serve <mlx repo or folder> --runtime
        # mlx_lm` is the explicit form" -- a bare Hugging Face Hub repo id
        # (never resolvable as a FILE -- `resolve_local_file` already
        # tried an exact path and every `huggingface.model_dirs` folder)
        # with `--runtime mlx_lm` EXPLICITLY requested delegates to the
        # SAME `hf:mlx/<repo>` machinery `--model hf:mlx/<repo>` uses,
        # keyed by the repo id itself rather than a file path. Gated on
        # `runtime == "mlx_lm"` specifically -- an unresolvable PATH with
        # no `--runtime` (or `--runtime llama-server`) still gets the
        # plain "no file-backed model found" message below, unchanged.
        if runtime == "mlx_lm" and "/" in model and not any(ch.isspace() for ch in model):
            from halo_harness.providers.huggingface_mlx import ensure_mlx_server
            target, mlx_lines = ensure_mlx_server(model, confirm=confirm, state_dir=state_dir, keep=keep, port=port)
            return target is not None, mlx_lines
        return False, [f"no file-backed model found at or named {model!r} (try an exact path, or /local add "
                        f"a folder first)"]
    from halo_harness.providers.local_fit import fit_result_for_path
    from halo_harness.providers.local_runtime import find_runtime_binary, runtime_for_format, start_managed_server
    fit = fit_result_for_path(entry.path, fmt=entry.format, hw_runner=hw_runner)
    # NOTE: `huggingface.preferred_runtime` (the wizard's "Local models"
    # step) is deliberately NOT consulted here -- today's format->runtime
    # mapping (gguf -> llama-server; safetensors/mlx -> mlx_lm, Apple
    # Silicon only) has exactly one valid runtime per format, so a
    # "preference" can never legitimately override it without risking a
    # format/runtime mismatch (serving a GGUF file through mlx_lm, which
    # expects a safetensors folder). The setting is stored for a future
    # round where genuine choice exists (e.g. round 5f's in-process MLX
    # backend); see docs/MODELS.md.
    chosen_runtime = runtime or runtime_for_format(entry.format)
    if chosen_runtime is None:
        return False, [f"no managed runtime serves a {entry.format} model on this platform ({sys.platform}); "
                        f"try `halo local import` instead for a GGUF file."]
    lines: list = []
    if chosen_runtime == "llama-server":
        binary, fetch_lines = ensure_llama_server_binary(state_dir=state_dir, confirm=confirm, fetcher=fetcher,
                                                           releases=releases, hw_runner=hw_runner,
                                                           backend_override=backend)
        lines.extend(fetch_lines)
    else:
        binary = find_runtime_binary(chosen_runtime, state_dir=state_dir)
        if binary is None:
            lines.append("mlx_lm is not installed -- run `pip install mlx-lm` and try again "
                          "(docs/harness/GPU-RESEARCH.md section 7); Halo does not fetch it this round.")
    if binary is None:
        return False, lines
    context_length = fit.fit_estimate if isinstance(fit.fit_estimate, int) else fit.trained_context
    model_id = model_id_for_path(entry.path, entry.format)
    result, reason = start_managed_server(model=model_id, runtime=chosen_runtime, binary_argv=binary,
                                           model_path=entry.path, context_length=context_length, port=port,
                                           keep=keep, state_dir=state_dir)
    if result is None:
        lines.append(reason)
        return False, lines
    lines.append(f"Serving {entry.path} via {chosen_runtime} on port {result.port} -- "
                 f"use it as hf:local/{model_id}")
    return True, lines


def stop_local_model(model: str, *, state_dir=None) -> "tuple[bool, list]":
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    from halo_harness.providers.local_runtime import stop_managed_server
    ok, message = stop_managed_server(model, state_dir=state_dir)
    return ok, [message]


def import_local_model(model: str, *, name: Optional[str] = None, host_name: Optional[str] = None,
                        confirm: "Optional[Callable[[str], bool]]" = None, env=None,
                        on_status=None) -> "tuple[bool, list]":
    confirm = confirm or (lambda _q: False)
    entry = resolve_local_file(model, env=env)
    if entry is None:
        return False, [f"no file-backed model found at or named {model!r}"]
    from halo_harness.providers.ollama_import import (default_import_name, import_consent_sentence,
                                                        import_gguf_to_ollama, refuse_non_gguf_sentence)
    if entry.format != "gguf":
        return False, [refuse_non_gguf_sentence(entry.format)]
    from halo_harness.providers.ollama import resolve_ollama_host
    host = resolve_ollama_host(host_name, env)
    if host is None:
        return False, ["no Ollama host configured."]
    chosen_name = name or default_import_name(entry.path)
    lines = [import_consent_sentence(entry.path, size_bytes=entry.size_bytes, name=chosen_name)]
    if not confirm("Import now?"):
        lines.append("Declined -- nothing copied.")
        return False, lines
    from halo_harness.providers.ollama_panel import human_bytes
    lines.append(f"Uploading {entry.path.name} ({human_bytes(entry.size_bytes)}) to Ollama's blob store...")
    ok, message = import_gguf_to_ollama(host, chosen_name, entry.path, on_status=on_status)
    lines.append(message if ok else f"Import failed: {message}")
    if ok:
        lines.append(f"Use it as ol:{chosen_name}")
    return ok, lines
