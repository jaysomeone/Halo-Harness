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
"""

__version__ = "2.0.0"
