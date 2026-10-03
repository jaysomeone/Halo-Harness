"""halo_harness: Halo Harness, a standalone agent harness that reuses Claude
Code's own config files (CLAUDE.md, settings.json, MCP servers, auto-memory)
while driving OpenRouter/Databricks models instead of Anthropic's API
directly. Continues rolo-claude 1.0.1 under a new name (2.0.0 rename brief):
same behavior, a new product/command name (`halo`, `rolo-claude` kept as a
deprecated alias -- see cli.main_deprecated_alias), `~/.halo` state dir
(migrated once from an existing `~/.rolo-claude`), and `HALO_*` env vars
(old `BRIDGE_*`/`ROLO_CLAUDE_*` names still honoured).

`bridge.py` at the repo root remains the proxy (`halo proxy ...`, or
`python bridge.py ...` directly -- today's launcher+server, unchanged
behavior) and imports this package as a library. See wip/SIGNATURES.md and
docs/ for the cross-section contract.

2.0.1 ("run from any directory" release): no behavior changed from 2.0.0 --
`docs/INSTALL.md` makes the one-line install unmistakable, `doctor`/`init`'s
PATH check now names which of the three copies of `halo` it found, and
every vendored data file (model table, catalogs, the TCSS stylesheet) is
proven, by test, to load the same way regardless of which directory `halo`
is started from.

2.0.2 (W7 round 1): the real OS/terminal-emulator title is re-asserted to
`halo` at TUI start/resume, after every `claude` child exits, and
(print mode) restored at exit -- see `halo_harness.termtitle`, the fix for
a `claude` child leaving the tab/window reading "claude". Roles v2: five
new built-in roles (`planner`, `judge`, `tester`, `compaction`,
`subagent_default`), a role's value may now carry its own reasoning effort
alongside its model, any syntactically-valid custom name a team.json/
template defines is a valid role everywhere, TUI tab completion and shell
completion (`halo completion bash|zsh|powershell`) for role names and
model refs, and named, reusable role templates (`~/.halo/roles/<name>.json`
-- `/roles`/`halo roles template ...`, a TUI editor form or `$EDITOR`).
"""

__version__ = "2.0.2"
