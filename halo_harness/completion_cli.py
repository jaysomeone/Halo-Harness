"""halo_harness.completion_cli -- Halo 2.0.2 (W7 round 1, brief A.4):
`halo completion bash|zsh|powershell` prints a shell-completion script for
the `halo` command itself -- subcommands, role names (`roles.
known_role_names()`), and cached model refs, read straight from the state
dir's own catalog cache files (`models.json`/`dbx-endpoints.json`) with NO
network call of its own (brief: "no network" -- the cache is whatever a
prior `halo init`/`models --refresh`/session already wrote; an empty/
missing cache just means fewer model-ref completions offered, never an
error).

The exact top-level subcommand list is the same one `cli.py::main` itself
dispatches on (kept here as a plain tuple rather than introspecting `cli.py`
-- that module imports a LOT for argument parsing alone, and a shell-
completion script has no need to trigger any of it).
"""

from __future__ import annotations

import argparse
import sys

# Mirrors cli.py::main's own `if argv and argv[0] == "<name>":` dispatch
# table, plus the Halo 2.0.2 additions this round introduces. 2.0.2
# review finding 37: `update`, `org` and `setup` were missing outright --
# `halo <Tab>` never offered them at all. Checked against cli.py::main's
# own dispatch list by tests/test_completion_cli.py, so this can't drift
# again unnoticed.
SUBCOMMANDS = (
    "proxy", "models", "mcp", "config", "doctor", "update", "work-matrix", "init",
    "providers", "stats", "improve", "export", "bugreport", "timeline",
    "worktree", "bg", "roles", "org", "setup", "completion",
    # Halo 2.0.3 round 5d: this round's own new top-level command.
    # "ollama"/"local" (rounds 2/5) were already missing here before this
    # round touched this file -- a separate, pre-existing gap, left as
    # found; see the round 5d worker report.
    "gym",
)


def cached_model_refs(state_dir=None) -> "list[str]":
    """`or:<id>` for every OpenRouter `models.json` key, `dbx:<name>` for
    every Databricks `dbx-endpoints.json` key -- whatever is ALREADY
    cached under `state_dir` (default `~/.halo`), read with plain file
    I/O only. Never raises; `[]` on a fresh box with no cache yet."""
    from halo_harness.config.paths import bridge_home
    from halo_harness.providers.databricks import load_dbx_endpoints_json, load_models_json

    sd = state_dir if state_dir is not None else bridge_home()
    refs: list = []
    try:
        refs += [f"or:{k}" for k in load_models_json(sd).keys()]
    except Exception:
        pass
    try:
        refs += [f"dbx:{k}" for k in load_dbx_endpoints_json(sd).keys()]
    except Exception:
        pass
    return sorted(set(refs))


def _completion_data(state_dir=None) -> "tuple[list[str], list[str], list[str]]":
    from halo_harness.roles import known_role_names
    return list(SUBCOMMANDS), list(known_role_names()), cached_model_refs(state_dir)


def _bash_script(subcommands: list, role_names: list, model_refs: list) -> str:
    words = " ".join(subcommands + role_names + model_refs)
    subs = " ".join(subcommands)
    # 2.0.2 review finding 37: `:` is in bash's own default COMP_WORDBREAKS
    # -- bash hands `$cur` everything AFTER the last colon (so typing
    # "or:deep" gives `cur="deep"`), but `compgen -W "...or:deepseek/x..."`
    # matches against the FULL candidate word including its "or:" prefix,
    # so a model ref never completed at all. `__ltrim_colon_completions`
    # (the standard bash-completion idiom for exactly this) trims each
    # COMPREPLY entry back down to what bash will actually insert after
    # the last colon in `$cur`.
    return f"""# halo bash completion -- `eval "$(halo completion bash)"`, or save to a
# file your bash's completion setup sources.
_halo_completion() {{
    local cur prev words_arr cword
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    cword=$COMP_CWORD
    if [ "$cword" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "{subs}" -- "$cur") )
        return 0
    fi
    COMPREPLY=( $(compgen -W "{words}" -- "$cur") )
    type __ltrim_colon_completions >/dev/null 2>&1 && __ltrim_colon_completions "$cur"
}}
complete -F _halo_completion halo
"""


def _zsh_script(subcommands: list, role_names: list, model_refs: list) -> str:
    # finding 37: zsh's own `_describe` reads `:` as the word/description
    # separator in each array entry -- a bare model ref like
    # "or:deepseek/deepseek-chat" was read as word="or", description=
    # "deepseek/deepseek-chat", so only "or" (never the real ref) was
    # ever offered/inserted. Escaped as `\:` here, the same way zsh's own
    # own completion functions escape a literal colon in a candidate.
    escaped_words = [w.replace(":", "\\:") for w in (subcommands + role_names + model_refs)]
    words = " ".join(escaped_words)
    subs = " ".join(subcommands)
    return f"""#compdef halo
# halo zsh completion -- `eval "$(halo completion zsh)"`, or save to a file
# on your $fpath as `_halo` (no leading dash) and run `compinit`.
_halo() {{
    local -a subcommands words_all
    subcommands=({subs})
    words_all=({words})
    if (( CURRENT == 2 )); then
        _describe 'halo subcommand' subcommands
        return
    fi
    _describe 'halo argument' words_all
}}
_halo "$@"
"""


def _powershell_script(subcommands: list, role_names: list, model_refs: list) -> str:
    def _ps_array(items: list) -> str:
        return ", ".join(f"'{i}'" for i in items)

    subs_arr = _ps_array(subcommands)
    words_arr = _ps_array(subcommands + role_names + model_refs)
    return f"""# halo PowerShell completion -- dot-source this (`. halo-completion.ps1`)
# or add it to your $PROFILE.
Register-ArgumentCompleter -Native -CommandName halo -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $subcommands = @({subs_arr})
    $allWords = @({words_arr})
    $tokens = $commandAst.CommandElements.Count
    $candidates = if ($tokens -le 2) {{ $subcommands }} else {{ $allWords }}
    $candidates | Where-Object {{ $_ -like "$wordToComplete*" }} |
        ForEach-Object {{ [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }}
}}
"""


_SHELLS = {"bash": _bash_script, "zsh": _zsh_script, "powershell": _powershell_script}


def cmd_completion(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="halo completion", add_help=True,
        description="Print a shell-completion script for the halo command (subcommands, role names, cached model refs).",
    )
    parser.add_argument("shell", choices=sorted(_SHELLS), help="which shell to print a script for")
    parser.add_argument("--state-dir", default=None, help="read the catalog cache from here instead of ~/.halo")
    args = parser.parse_args(argv)

    subcommands, role_names, model_refs = _completion_data(args.state_dir)
    print(_SHELLS[args.shell](subcommands, role_names, model_refs), end="")
    return 0
