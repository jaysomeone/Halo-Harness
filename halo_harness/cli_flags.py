"""halo_harness.cli_flags -- W4a: one small, pure function turning the
argparse Namespace `cli.py._build_parser()` produces into the plain dict
`headless.build_session`/`agent.loop.Session` read as `cli_flags` -- shared
by `-p` (`cli.py main()`) and the TUI (`tui/bootstrap.py::build_controller`,
which already has the same Namespace) so the two paths can never drift on
what a flag means. Never raises: a malformed value (`--environment` missing
"=", a non-numeric `--autocompact`) is just dropped from the dict rather
than failing the whole launch over a cosmetic typo.
"""

from __future__ import annotations

from typing import Optional


def _parse_environment_pairs(raw: Optional[list]) -> dict:
    out: dict = {}
    for item in (raw or []):
        if not isinstance(item, str) or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name:
            out[name] = value
    return out


def _parse_fallback_models(raw: Optional[str]) -> list:
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


_TRUTHY_PROMPT_SUGGESTIONS = {"true", "1", "yes", "on"}


def cli_flags_from_args(args) -> dict:
    g = lambda name, default=None: getattr(args, name, default)  # noqa: E731
    return {
        "autocompact": g("autocompact"),
        "ax_screen_reader": bool(g("ax_screen_reader", False)),
        "betas": list(g("betas") or []),
        "brief": bool(g("brief", False)),
        "environment": _parse_environment_pairs(g("environment")),
        "exclude_dynamic_system_prompt_sections": bool(g("exclude_dynamic_system_prompt_sections", False)),
        "fallback_models": _parse_fallback_models(g("fallback_model")),
        "forward_subagent_text": bool(g("forward_subagent_text", False)),
        "include_hook_events": bool(g("include_hook_events", False)),
        "no_session_persistence": bool(g("no_session_persistence", False)),
        "permission_prompt_tool": g("permission_prompt_tool"),
        "permission_prompts": g("permission_prompts"),
        "plugin_dir": list(g("plugin_dir") or []),
        "plugin_url": list(g("plugin_url") or []),
        "prompt_suggestions": (g("prompt_suggestions") or "").lower() in _TRUTHY_PROMPT_SUGGESTIONS,
        "restricted": bool(g("restricted", False)),
        "system_prompt_snapshot": g("system_prompt_snapshot") or "off",
        "worktree": g("worktree"),
    }
