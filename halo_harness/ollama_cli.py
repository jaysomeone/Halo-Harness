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
    # Round 5b (brief item 2): `halo ollama calibrate <model> [--host NAME]
    # [--start N]` is its own subcommand, dispatched the same way `halo`'s
    # own top-level `cli.py` dispatches "roles"/"local" -- never confused
    # with the `--host`/`--refresh` flags the bare `halo ollama` (no
    # subcommand) form already takes.
    if argv and argv[0] == "calibrate":
        return cmd_ollama_calibrate(argv[1:])
    if argv and argv[0] == "doctor":
        return cmd_ollama_doctor(argv[1:])
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


def cmd_ollama_calibrate(argv: list) -> int:
    """`halo ollama calibrate <model> [--host NAME] [--start N]` (brief
    item 2): the explicit, user-invoked twin of the auto-calibrate trigger
    `agent/loop.py` runs on first use -- same `providers.ollama_calibrate.
    run_calibration` stepping loop, same `~/.halo/ollama-fit.json` record,
    just with its own plain multi-line printout instead of one notice."""
    from halo_harness.config.paths import bridge_home
    from halo_harness.providers.ollama import get_catalog, probe_version, resolve_ollama_host, trained_context_for
    from halo_harness.providers.ollama_calibrate import MIN_CALIBRATE_CTX, record_calibration, run_calibration
    from halo_harness.providers.ollama_hw import catalog_row, estimate_fit_for_host, is_local_host

    parser = argparse.ArgumentParser(
        prog="halo ollama calibrate", add_help=True,
        description="Load <model> at decreasing candidate num_ctx values until it is fully resident in GPU "
                    "memory (or does not fit at all), then step UP from there to find the true ceiling, and "
                    "remember the result in ~/.halo/ollama-fit.json.",
    )
    parser.add_argument("model", help="the Ollama model tag to calibrate, e.g. qwen3-coder:30b")
    parser.add_argument("--host", default=None, help="a configured ollama.hosts[] name (default host otherwise)")
    parser.add_argument("--start", type=int, default=None,
                         help="the first num_ctx to try (default: the GPU-based estimate when a local or ssh "
                             "read exists, else 32768)")
    # Round 5b part 2 (brief item 7): "halo ollama calibrate also steps UP
    # from a fitting first guess by powers of two ... --no-up skips it".
    parser.add_argument("--no-up", action="store_true",
                         help="stop at the first fitting candidate found while stepping DOWN -- never probe "
                             "larger contexts afterward")
    args = parser.parse_args(argv)

    host = resolve_ollama_host(args.host)
    if host is None:
        print(f"halo ollama calibrate: no configured host named {args.host!r} (see `ollama.hosts` in "
              f"~/.halo/config.json)", file=sys.stderr)
        return 1
    print(f"Calibrating {args.model} on '{host.name}' ({host.url}) "
          f"{'locally' if is_local_host(host) else 'over the network'}...")
    catalog = get_catalog(host, force=True)
    row = catalog_row(catalog, args.model)
    if row is None:
        print(f"halo ollama calibrate: {args.model!r} is not in this host's catalog -- "
              f"`halo ollama --host {host.name}` lists what is", file=sys.stderr)
        return 1
    digest = row.get("digest")
    version_info = probe_version(host)
    ollama_version = (version_info or {}).get("version") if isinstance(version_info, dict) else None
    start = args.start
    if start is None:
        estimate = estimate_fit_for_host(host, args.model, catalog)
        start = estimate if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0 else 32768
    print(f"  starting candidate: num_ctx={start}" + ("" if not args.no_up else " (--no-up: stepping up skipped)"))
    result = run_calibration(host, args.model, start_ctx=start, keep_alive=host.keep_alive,
                              step_up=not args.no_up, trained_context=trained_context_for(catalog, args.model))
    record = record_calibration(bridge_home(), host_url=host.url, model=args.model, digest=digest,
                                 max_full_gpu_ctx=result.max_full_gpu_ctx, ollama_version=ollama_version)
    if result.outcome == "fits":
        print(f"  fits fully in GPU memory at num_ctx={result.max_full_gpu_ctx} ({result.steps} step(s))")
    else:
        print(f"  does not fit fully in GPU memory even at num_ctx={MIN_CALIBRATE_CTX} ({result.steps} step(s))")
        print("  the live fit estimate/remote default will be used instead")
    print(f"  recorded in ~/.halo/ollama-fit.json (digest {record['digest']!r}, ollama {ollama_version or '?'})")
    return 0


def cmd_ollama_doctor(argv: list) -> int:
    """`halo ollama doctor [--host NAME]` (Halo 2.0.3 round 5b part 2,
    brief item 4): the SAME `providers.ollama_panel.analyze_host`/
    `format_host_analysis` print `halo ollama` already does, PLUS
    `providers.ollama_panel.host_setup_checklist`'s own plain-sentence,
    per-OS recommendations -- see that function's own docstring for the
    local/remote OS rule."""
    from halo_harness.providers.ollama import resolve_ollama_hosts
    from halo_harness.providers.ollama_panel import analyze_host, format_host_analysis, host_setup_checklist

    parser = argparse.ArgumentParser(
        prog="halo ollama doctor", add_help=True,
        description="What each configured Ollama host exposes, plus the documented host-tuning "
                    "recommendations Halo cannot read back (flash attention, KV cache type, keep-alive, "
                    "num_parallel, context length) and where each one lives per OS.",
    )
    parser.add_argument("--host", default=None, help="only this configured host (by name), not every one")
    args = parser.parse_args(argv)

    hosts = resolve_ollama_hosts()
    if args.host:
        hosts = [h for h in hosts if h.name.lower() == args.host.lower()]
        if not hosts:
            print(f"halo ollama doctor: no configured host named {args.host!r} (see `ollama.hosts` in "
                  f"~/.halo/config.json)", file=sys.stderr)
            return 1
    if not hosts:
        print("No Ollama hosts configured.", file=sys.stderr)
        return 0
    for i, host in enumerate(hosts):
        if i:
            print()
        print(format_host_analysis(analyze_host(host)))
        for line in host_setup_checklist(host):
            print(f"  {line}")
    return 0
