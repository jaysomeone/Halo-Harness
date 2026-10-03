"""tests.test_tui_completion -- Halo 2.0.2 (W7 round 1, brief A.4):
`tui/completion.py`'s `filter_items` ranking and `role_command_arg_index`/
`current_token`'s new "arg" kind for `/role`/`/roles set` argument
completion. All pure functions (no textual import -- see that module's own
docstring), so this is a plain hermetic unit test file, not part of
test_tui.py's Textual pilot suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- filter_items -----------------------------------------------------------

@test
def test_filter_items_empty_prefix_returns_everything_unchanged(ctx: Ctx):
    from halo_harness.tui.completion import filter_items
    items = ["b", "a", "c"]
    ctx.check("empty prefix -> unchanged order", filter_items(items, "") == items)


@test
def test_filter_items_prefix_matches_rank_before_substring_matches(ctx: Ctx):
    from halo_harness.tui.completion import filter_items
    items = ["xcoder", "coder", "reviewer", "coder2"]
    ctx.check(f"prefix matches first (own relative order), then substring, got {filter_items(items, 'co')}",
              filter_items(items, "co") == ["coder", "coder2", "xcoder"])


@test
def test_filter_items_is_case_insensitive(ctx: Ctx):
    from halo_harness.tui.completion import filter_items
    ctx.check("case-insensitive prefix match", filter_items(["Coder", "Reviewer"], "co") == ["Coder"])


@test
def test_filter_items_no_match_is_empty(ctx: Ctx):
    from halo_harness.tui.completion import filter_items
    ctx.check("nothing matches -> []", filter_items(["coder", "reviewer"], "zzz") == [])


# ---- role_command_arg_index / current_token's "arg" kind -------------------

@test
def test_role_command_arg_index_detects_all_three_slots(ctx: Ctx):
    from halo_harness.tui.completion import role_command_arg_index
    cases = [
        ("/role cod", 9, 0),          # typing the role name
        ("/role coder ", 12, 1),      # just finished the name, starting the model
        ("/role coder or:x", 17, 1),  # mid-model
        ("/role coder or:x ", 17, 2),  # just finished the model, starting effort
        ("/role coder or:x hi", 19, 2),
        ("/roles set cod", 14, 0),
        ("/roles set coder or:x hi", 24, 2),
    ]
    for text, pos, expected in cases:
        got = role_command_arg_index(text, pos)
        ctx.check(f"{text!r} @ {pos} -> {expected}, got {got}", got == expected)


@test
def test_role_command_arg_index_none_for_unrelated_lines(ctx: Ctx):
    from halo_harness.tui.completion import role_command_arg_index
    for text, pos in (("/rol", 4), ("/roles", 6), ("hello /role x", 13), ("/roleplay x", 11)):
        ctx.check(f"{text!r} is not a /role(s set) line, got {role_command_arg_index(text, pos)!r}",
                  role_command_arg_index(text, pos) is None)


@test
def test_current_token_reports_arg_kind_for_role_command_arguments(ctx: Ctx):
    from halo_harness.tui.completion import current_token
    kind, start, token = current_token("/role cod", 9)
    ctx.check(f"kind == 'arg', got {kind!r}", kind == "arg")
    ctx.check(f"token text is the partial argument, got {token!r}", token == "cod")
    ctx.check(f"start is where that argument begins, got {start}", start == 6)


@test
def test_current_token_still_reports_slash_for_the_command_name_itself(ctx: Ctx):
    """Typing `/role` itself (no trailing space yet) is still command-NAME
    completion, not argument completion -- unaffected by this round."""
    from halo_harness.tui.completion import current_token
    ctx.check("still 'slash' kind", current_token("/role", 5)[0] == "slash")


@test
def test_current_token_unaffected_for_plain_slash_and_at_completion(ctx: Ctx):
    """No regression: ordinary `/`/`@` completion (every OTHER command)
    is byte-for-byte unchanged by the new "arg" branch."""
    from halo_harness.tui.completion import current_token
    ctx.check("'/mod' -> slash", current_token("/mod", 4)[0] == "slash")
    ctx.check("'@src/fo' -> at", current_token("@src/fo", 7)[0] == "at")
    ctx.check("plain text with no / or @ -> ''", current_token("hello world", 5)[0] == "")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
