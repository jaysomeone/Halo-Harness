"""halo_harness.local_cli -- `halo local [--refresh]` and its round 5c
subcommands (`add`/`forget`/`serve`/`stop`/`import`/`runtime remove`), the
CLI twin of `/local`'s TUI dialog and `commands/builtins.py`'s `/local`
print-mode fallback. The default (no subcommand) view renders `providers.
local_models.build_local_view`/`format_local_view`, so none of the three
surfaces can quietly disagree about what's running/cached locally; every
subcommand below is a thin argument-parsing wrapper over `providers.
local_model_dirs`/`local_use`, which hold the actual logic (and its own
tests) independent of argparse/stdin.
"""

from __future__ import annotations

import argparse
import sys


def _confirm(question: str, *, yes: bool) -> bool:
    """`--yes`, else a plain stdin yes/no -- the SAME idiom `halo work-
    matrix apply` already uses (`input(... "[y/N] ")`, EOFError -> no)."""
    if yes:
        return True
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def _cmd_default(argv: list) -> int:
    from halo_harness.providers.local_models import build_local_view, format_local_view
    parser = argparse.ArgumentParser(
        prog="halo local", add_help=True,
        description="Merged local-model view: Ollama hosts, running Hugging Face local servers "
                     "(auto-detected and manual), the Hugging Face Hub cache, and huggingface.model_dirs.")
    parser.add_argument("--refresh", action="store_true",
                         help="bypass the Ollama catalog's short-TTL cache and probe every configured "
                              "huggingface.local_servers entry (skipped otherwise -- see docs/MODELS.md)")
    args = parser.parse_args(argv)
    print(format_local_view(build_local_view(refresh=args.refresh)))
    return 0


def _cmd_add(argv: list) -> int:
    from halo_harness.providers.local_model_dirs import add_model_dir
    parser = argparse.ArgumentParser(prog="halo local add", description="Add a folder to huggingface.model_dirs.")
    parser.add_argument("path")
    args = parser.parse_args(argv)
    ok, message = add_model_dir(args.path)
    print(message)
    return 0 if ok else 1


def _cmd_forget(argv: list) -> int:
    from halo_harness.providers.local_model_dirs import forget_model_dir
    parser = argparse.ArgumentParser(prog="halo local forget",
                                      description="Remove a folder from huggingface.model_dirs.")
    parser.add_argument("path")
    args = parser.parse_args(argv)
    ok, message = forget_model_dir(args.path)
    print(message)
    return 0 if ok else 1


def _cmd_serve(argv: list) -> int:
    from halo_harness.providers.local_use import serve_local_model
    parser = argparse.ArgumentParser(prog="halo local serve", description="Serve a file-backed model on a "
                                      "free loopback port with a managed llama-server/mlx_lm runtime.")
    parser.add_argument("model", help="an exact .gguf/model-folder path, a name shown by `halo local`, or "
                         "(round 5f, with --runtime mlx_lm) a bare Hugging Face Hub repo id, e.g. "
                         "mlx-community/Qwen2.5-7B-Instruct-4bit -- the explicit form of hf:mlx/<org>/<repo>")
    parser.add_argument("--runtime", choices=("llama-server", "mlx_lm"), default=None,
                         help="mlx_lm is Apple Silicon only; round 5f: also accepts a bare repo id (above)")
    parser.add_argument("--backend", choices=("cuda", "vulkan", "cpu", "metal"), default=None,
                         help="override which llama-server build to fetch (default: CUDA if your driver "
                              "reports a version, else Vulkan; Metal on macOS)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--keep", action="store_true", help="keep running after this halo process exits")
    parser.add_argument("--yes", action="store_true", help="consent to fetching the llama-server runtime "
                         "if none is found, without asking")
    args = parser.parse_args(argv)
    ok, lines = serve_local_model(args.model, runtime=args.runtime, port=args.port, keep=args.keep,
                                   backend=args.backend, confirm=lambda q: _confirm(q, yes=args.yes))
    for line in lines:
        print(line)
    return 0 if ok else 1


def _cmd_stop(argv: list) -> int:
    from halo_harness.providers.local_use import stop_local_model
    parser = argparse.ArgumentParser(prog="halo local stop", description="Stop a model started by `halo local serve`.")
    parser.add_argument("model")
    args = parser.parse_args(argv)
    ok, lines = stop_local_model(args.model)
    for line in lines:
        print(line)
    return 0 if ok else 1


def _cmd_import(argv: list) -> int:
    from halo_harness.providers.local_use import import_local_model
    parser = argparse.ArgumentParser(prog="halo local import", description="Copy a .gguf file into Ollama's "
                                      "own model store (ollama create via /api/create).")
    parser.add_argument("model", help="an exact .gguf path, or a name shown by `halo local`")
    parser.add_argument("--name", default=None, help="the new ol:<name> (default: the file's own stem)")
    parser.add_argument("--host", default=None, help="an ollama.hosts entry name (default: the default host)")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    ok, lines = import_local_model(args.model, name=args.name, host_name=args.host,
                                    confirm=lambda q: _confirm(q, yes=args.yes),
                                    on_status=lambda s: print(f"  {s.get('status', s)}"))
    for line in lines:
        print(line)
    return 0 if ok else 1


def _cmd_runtime(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="halo local runtime", description="Manage fetched llama.cpp runtimes.")
    sub = parser.add_subparsers(dest="action", required=True)
    remove_p = sub.add_parser("remove", help="delete a fetched llama-server runtime from ~/.halo/runtimes/")
    remove_p.add_argument("version", nargs="?", default=None, help="a release tag (default: every version)")
    args = parser.parse_args(argv)
    if args.action == "remove":
        from halo_harness.config.paths import bridge_home
        from halo_harness.providers.local_runtime_fetch import remove_runtime
        ok, message = remove_runtime(bridge_home(), args.version)
        print(message)
        return 0 if ok else 1
    return 2


_SUBCOMMANDS = {"add": _cmd_add, "forget": _cmd_forget, "serve": _cmd_serve, "stop": _cmd_stop,
                "import": _cmd_import, "runtime": _cmd_runtime}


def cmd_local(argv: list) -> int:
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])
    return _cmd_default(argv)
