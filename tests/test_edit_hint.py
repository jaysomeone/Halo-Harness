"""tests.test_edit_hint -- H12 brief Part C: model_table.json's optional
per-model/per-family `edit_hint` string, and the Edit tool description it's
appended to for a session whose model family has one. Unit-level (a
synthetic model_table, never the real file, so this suite never depends on
which rows happen to be in it) plus one integration check through the real
`headless.build_session` (the one shared builder both `-p` and the TUI use)
proving the wire tool catalog is otherwise byte-identical.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
ensure_default_provider_credentials()

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_FAMILY_HINT = "Include at least three lines of surrounding context in old_string so the match is unique."


def _table() -> dict:
    return {
        "edit_hints": {"deepseek": _FAMILY_HINT, "kimi": _FAMILY_HINT, "glm": _FAMILY_HINT,
                       "qwen": _FAMILY_HINT, "minimax": _FAMILY_HINT},
        "openrouter": {
            "deepseek/special-row": {"edit_hint": "a per-model override"},
            "deepseek/silenced-row": {"edit_hint": ""},
        },
    }


@test
def test_family_default_applies_to_the_five_named_families(ctx: Ctx):
    from rolo_claude.providers.profiles import edit_hint_for
    table = _table()
    for model_id in ("deepseek/deepseek-v4.1-flash", "moonshotai/kimi-k3", "z-ai/glm-5.3",
                      "qwen/qwen3-coder", "minimax/minimax-m2"):
        hint = edit_hint_for("openrouter", model_id, table)
        ctx.check(f"{model_id!r} gets the family hint, got {hint!r}", hint == _FAMILY_HINT)


@test
def test_claude_and_gpt_and_generic_get_no_hint(ctx: Ctx):
    from rolo_claude.providers.profiles import edit_hint_for
    table = _table()
    for provider, model_id in (("anthropic", "claude-sonnet-5-5"), ("openrouter", "openai/gpt-5"),
                                ("openrouter", "some-vendor/some-model")):
        hint = edit_hint_for(provider, model_id, table)
        ctx.check(f"{model_id!r} gets no hint, got {hint!r}", hint is None)


@test
def test_per_model_row_override_wins_over_the_family_default(ctx: Ctx):
    from rolo_claude.providers.profiles import edit_hint_for
    table = _table()
    hint = edit_hint_for("openrouter", "deepseek/special-row", table)
    ctx.check(f"the row's own edit_hint wins, got {hint!r}", hint == "a per-model override")


@test
def test_explicit_empty_row_override_silences_the_family_default(ctx: Ctx):
    from rolo_claude.providers.profiles import edit_hint_for
    table = _table()
    hint = edit_hint_for("openrouter", "deepseek/silenced-row", table)
    ctx.check(f"an explicit '' row override silences the family default, got {hint!r}", hint is None)


@test
def test_no_model_table_at_all_degrades_to_no_hint(ctx: Ctx):
    from rolo_claude.providers.profiles import edit_hint_for
    ctx.check("an empty table -> no hint, never a crash", edit_hint_for("openrouter", "deepseek/x", {}) is None)


@test
def test_headless_build_session_deepseek_edit_carries_the_line(ctx: Ctx):
    from rolo_claude import headless
    home = Path(tempfile.mkdtemp(prefix="edit-hint-home-"))
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        build = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                        bare=True, print_mode=True)
        edit_def = next(t for t in build.tool_registry.definitions() if t["name"] == "Edit")
        # "rather than guessing" is unique to the appended hint text -- the
        # BASE Edit description already contains the phrase "surrounding
        # context" on its own (its own "provide more context" bullet), so
        # that alone can't distinguish "hinted" from "not hinted".
        ctx.check(f"the real DeepSeek row's hint is present, got tail={edit_def['description'][-200:]!r}",
                  "rather than guessing" in edit_def["description"].lower())
    finally:
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_headless_build_session_cc_and_ant_edit_has_no_line_and_wire_is_otherwise_identical(ctx: Ctx):
    """A `cc:`/`ant:` session's Edit description carries no hint, and every
    OTHER tool definition on the wire (name-sorted, exactly what a real
    request would send) is byte-identical to a DeepSeek session's -- this
    feature touches ONLY the Edit tool's own description string."""
    from rolo_claude import headless
    home = Path(tempfile.mkdtemp(prefix="edit-hint-home-"))
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_auth = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
    try:
        ds = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                     bare=True, print_mode=True)
        ant = headless.build_session(cwd=REPO_DIR, model_ref_raw="ant:sonnet", bare=True, print_mode=True)
        cc = headless.build_session(cwd=REPO_DIR, model_ref_raw="cc:sonnet", bare=True, print_mode=True)

        for label, build in (("ant:sonnet", ant), ("cc:sonnet", cc)):
            edit_def = next(t for t in build.tool_registry.definitions() if t["name"] == "Edit")
            ctx.check(f"{label}'s Edit carries no per-family hint, got tail={edit_def['description'][-120:]!r}",
                      "rather than guessing" not in edit_def["description"].lower())

        ds_defs = {t["name"]: t for t in ds.tool_registry.definitions()}
        for label, build in (("ant:sonnet", ant), ("cc:sonnet", cc)):
            other_defs = {t["name"]: t for t in build.tool_registry.definitions()}
            diffs = [name for name in ds_defs if name != "Edit"
                     and json.dumps(ds_defs[name], sort_keys=True) != json.dumps(other_defs.get(name), sort_keys=True)]
            ctx.check(f"every non-Edit tool definition matches {label} byte-for-byte, diffs={diffs}", not diffs)
    finally:
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        if old_auth is not None:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = old_auth
        else:
            os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
