"""halo_harness.ollama_cli -- `halo ollama [--host NAME] [--refresh]`
(Halo 2.0.3 round 3, brief item 4): the CLI twin of `/ollama`'s TUI panel
and `commands/builtins.py`'s `/ollama` print-mode fallback -- all three
render `providers.ollama_panel.analyze_host`/`format_host_analysis`, so
none of the three can quietly disagree about what a host looks like.
"""

from __future__ import annotations

import argparse
import sys


def cmd_ollama(argv: list) -> int:
    from halo_harness.providers.ollama import resolve_ollama_hosts
    from halo_harness.providers.ollama_panel import analyze_host, format_host_analysis

    parser = argparse.ArgumentParser(prog="halo ollama", add_help=True,
                                      description="Per-host Ollama analysis: reachability, version, loaded "
                                                   "models (offload, context, KV cost), and tool-catalog sizing.")
    parser.add_argument("--host", default=None, help="only this configured host (by name), not every one")
    parser.add_argument("--refresh", action="store_true",
                         help="bypass the short-TTL catalog cache and re-read /api/tags+/api/show now")
    args = parser.parse_args(argv)

    hosts = resolve_ollama_hosts()
    if args.host:
        hosts = [h for h in hosts if h.name.lower() == args.host.lower()]
        if not hosts:
            print(f"halo ollama: no configured host named {args.host!r} (see `ollama.hosts` in "
                  f"~/.halo/config.json, or docs/MODELS.md's Ollama section)", file=sys.stderr)
            return 1
    if not hosts:
        print("No Ollama hosts configured.", file=sys.stderr)
        return 0
    for i, host in enumerate(hosts):
        if i:
            print()
        print(format_host_analysis(analyze_host(host, force=args.refresh)))
    return 0
