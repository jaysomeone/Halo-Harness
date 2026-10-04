"""tests.test_mcp_status_editor -- Halo 2.0.2 round D leftover 1:
`tui/dialogs/mcp_status.py`'s `e` editor launch used to pass vim's own
`+<line>` argument to EVERY `$EDITOR` unconditionally -- `code`/`subl`
don't understand `+42` and would try to open a file literally named
that. `_editor_command` is the pure (no subprocess/Textual) piece that
picks the right syntax; this pins it directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_vim_family_uses_plus_line(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import _editor_command
    for editor in ("vim", "vi", "nvim", "nano", "emacs"):
        got = _editor_command(editor, "/tmp/x.json", 42)
        ctx.check(f"{editor} +42, got {got!r}", got == f'{editor} +42 "/tmp/x.json"')


@test
def test_code_uses_goto_not_plus(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import _editor_command
    got = _editor_command("code", "/tmp/x.json", 42)
    ctx.check(f"code --goto path:line, got {got!r}", got == 'code --goto "/tmp/x.json:42"')
    ctx.check("never the vim-only +42 form", "+42" not in got)


@test
def test_code_with_its_own_flags_preserved(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import _editor_command
    got = _editor_command("code --wait", "/tmp/x.json", 7)
    ctx.check(f"own flags kept, still --goto, got {got!r}", got == 'code --wait --goto "/tmp/x.json:7"')


@test
def test_sublime_uses_colon_line(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import _editor_command
    got = _editor_command("subl", "/tmp/x.json", 3)
    ctx.check(f"subl path:line, got {got!r}", got == 'subl "/tmp/x.json:3"')


@test
def test_unknown_editor_opens_plain_no_line_arg(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import _editor_command
    got = _editor_command("notepad", "/tmp/x.json", 5)
    ctx.check(f"no line arg at all, got {got!r}", got == 'notepad "/tmp/x.json"')
    ctx.check("never guesses +5 for an unknown editor", "+5" not in got)


@test
def test_windows_exe_and_cmd_suffixes_still_match(ctx: Ctx):
    from halo_harness.tui.dialogs.mcp_status import _editor_command
    ctx.check("vim.exe matches vim", "+9" in _editor_command("vim.exe", "x", 9))
    ctx.check("code.cmd matches code", "--goto" in _editor_command("code.cmd", "x", 9))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
