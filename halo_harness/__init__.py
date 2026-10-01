"""rolo_claude: standalone agent harness that reuses Claude Code's own
config files (CLAUDE.md, settings.json, MCP servers, auto-memory) while
driving OpenRouter/Databricks models instead of Anthropic's API directly.

`bridge.py` at the repo root remains the proxy (``claude-bridge proxy ...``,
today's launcher+server, unchanged behavior) and imports this package as a
library. See wip/SIGNATURES.md and docs/ for the cross-section contract.
"""

__version__ = "1.0.1"
