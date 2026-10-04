"""halo_harness.local_cli -- `halo local [--refresh]` (Halo 2.0.3 round 5,
brief item 4): the CLI twin of `/local`'s TUI dialog and `commands/
builtins.py`'s `/local` print-mode fallback -- all three render
`providers.local_models.build_local_view`/`format_local_view`, so none of
the three can quietly disagree about what's running/cached locally.
"""

from __future__ import annotations

import argparse


def cmd_local(argv: list) -> int:
    from halo_harness.providers.local_models import build_local_view, format_local_view

    parser = argparse.ArgumentParser(
        prog="halo local", add_help=True,
        description="Merged local-model view: Ollama hosts, running Hugging Face local servers "
                     "(auto-detected and manual), and the Hugging Face Hub cache.")
    parser.add_argument("--refresh", action="store_true",
                         help="bypass the Ollama catalog's short-TTL cache and probe every configured "
                              "huggingface.local_servers entry (skipped otherwise -- see docs/MODELS.md)")
    args = parser.parse_args(argv)

    print(format_local_view(build_local_view(refresh=args.refresh)))
    return 0
