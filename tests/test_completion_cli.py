"""tests.test_completion_cli -- Halo 2.0.2 (W7 round 1, brief A.4): `halo
completion bash|zsh|powershell`. Verification section: "shell completion:
... a script completing subcommands, role names and the cached model
refs" -- these tests check exactly that (substring presence), never a
real shell's own parsing (no bash/zsh/pwsh binary required to run this
suite).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


def _run(argv: list) -> "tuple[int, str]":
    from halo_harness.completion_cli import cmd_completion
    old_out = sys.stdout
    sys.stdout = StringIO()
    try:
        rc = cmd_completion(argv)
        return rc, sys.stdout.getvalue()
    finally:
        sys.stdout = old_out


@test
def test_cached_model_refs_reads_both_caches_with_no_network(ctx: Ctx):
    from halo_harness.completion_cli import cached_model_refs
    from halo_harness.providers.databricks import dbx_endpoints_path, models_json_path
    d = _fresh_state_dir("completion-cli-cache-")
    try:
        models_json_path(d).write_text(json.dumps({"vendor/model-a": {}, "vendor/model-b": {}}), encoding="utf-8")
        dbx_endpoints_path(d).write_text(json.dumps({"databricks-foo": {}}), encoding="utf-8")
        refs = cached_model_refs(d)
        ctx.check(f"OpenRouter refs prefixed or:, got {refs}", "or:vendor/model-a" in refs and "or:vendor/model-b" in refs)
        ctx.check(f"Databricks refs prefixed dbx:, got {refs}", "dbx:databricks-foo" in refs)
    finally:
        _clear_state_dir_env()


@test
def test_cached_model_refs_empty_cache_is_empty_list(ctx: Ctx):
    from halo_harness.completion_cli import cached_model_refs
    d = _fresh_state_dir("completion-cli-empty-")
    try:
        ctx.check("no cache yet -> []", cached_model_refs(d) == [])
    finally:
        _clear_state_dir_env()


@test
def test_bash_script_contains_subcommands_roles_and_models(ctx: Ctx):
    from halo_harness.providers.databricks import models_json_path
    d = _fresh_state_dir("completion-cli-bash-")
    try:
        models_json_path(d).write_text(json.dumps({"vendor/x": {}}), encoding="utf-8")
        rc, out = _run(["bash", "--state-dir", str(d)])
        ctx.check(f"rc 0, got {rc}", rc == 0)
        ctx.check("names the completion function", "_halo_completion" in out)
        ctx.check("contains a built-in subcommand", "stats" in out)
        ctx.check("contains a built-in role name", "coder" in out)
        ctx.check("contains a cached model ref", "or:vendor/x" in out)
    finally:
        _clear_state_dir_env()


@test
def test_zsh_script_contains_subcommands_roles_and_models(ctx: Ctx):
    from halo_harness.providers.databricks import dbx_endpoints_path
    d = _fresh_state_dir("completion-cli-zsh-")
    try:
        dbx_endpoints_path(d).write_text(json.dumps({"databricks-foo": {}}), encoding="utf-8")
        rc, out = _run(["zsh", "--state-dir", str(d)])
        ctx.check(f"rc 0, got {rc}", rc == 0)
        ctx.check("compdef header present", out.startswith("#compdef halo"))
        ctx.check("contains a built-in subcommand", "roles" in out)
        ctx.check("contains a built-in role name", "researcher" in out)
        # finding 37: zsh's own `_describe` reads `:` as the word/
        # description separator, so a literal unescaped "dbx:..." would
        # never actually complete as one word -- the colon must be
        # escaped in the generated script.
        ctx.check(f"the cached model ref's colon is escaped for zsh, got a snippet: "
                  f"{out[out.index('databricks-foo') - 10:out.index('databricks-foo') + 5]!r}",
                  "dbx\\:databricks-foo" in out and "dbx:databricks-foo" not in out)
    finally:
        _clear_state_dir_env()


@test
def test_subcommands_includes_update_org_and_setup(ctx: Ctx):
    """2.0.2 review finding 37 pin: `SUBCOMMANDS` was missing `update`,
    `org` and `setup` outright (verified against cli.py::main's own
    dispatch table) -- `halo <Tab>` never offered any of the three."""
    from halo_harness.completion_cli import SUBCOMMANDS
    for name in ("update", "org", "setup"):
        ctx.check(f"{name!r} is in SUBCOMMANDS, got {SUBCOMMANDS}", name in SUBCOMMANDS)


@test
def test_bash_script_uses_ltrim_colon_completions(ctx: Ctx):
    """finding 37: `:` is in bash's own default COMP_WORDBREAKS -- without
    `__ltrim_colon_completions`, a model ref like "or:vendor/x" never
    actually completed (compgen matched the full word, bash only ever
    inserts what comes after the last colon)."""
    d = _fresh_state_dir("completion-cli-bash-ltrim-")
    try:
        rc, out = _run(["bash", "--state-dir", str(d)])
        ctx.check(f"rc 0, got {rc}", rc == 0)
        ctx.check("calls __ltrim_colon_completions", "__ltrim_colon_completions" in out)
    finally:
        _clear_state_dir_env()


@test
def test_powershell_script_contains_subcommands_and_roles(ctx: Ctx):
    d = _fresh_state_dir("completion-cli-ps-")
    try:
        rc, out = _run(["powershell", "--state-dir", str(d)])
        ctx.check(f"rc 0, got {rc}", rc == 0)
        ctx.check("registers a native argument completer", "Register-ArgumentCompleter" in out)
        ctx.check("contains a built-in subcommand", "'init'" in out)
        ctx.check("contains a built-in role name", "'judge'" in out)
    finally:
        _clear_state_dir_env()


@test
def test_unknown_shell_is_a_clean_argparse_error(ctx: Ctx):
    from halo_harness.completion_cli import cmd_completion
    try:
        cmd_completion(["fish"])
        ctx.check("should have exited", False)
    except SystemExit as e:
        ctx.check(f"argparse's own exit-2 usage error, got {e.code}", e.code == 2)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
