"""rolo_claude.mcp -- the MCP client (H3 scope A), built on the official
`mcp` SDK (`pip install "mcp>=1.26,<3"`). This package is IMPORTABLE even
when `mcp` isn't installed: every module here that actually touches the SDK
imports it lazily, inside function bodies, never at module import time --
`rolo_claude.mcp.available()` is the one place that checks, so every caller
(manager.py's own resolution path, headless.py, doctor.py) asks it instead
of poking at `sys.modules` itself.

Modules: `client` (McpLoop -- one daemon thread + asyncio loop for all MCP
traffic, sync facade), `stdio` (stdio transport), `http_sse` (http/sse
transport), `manager` (McpServerConfig/McpServerHandle/McpManager -- scope
resolution, lifecycle, tool/resource/prompt listing).
"""

from __future__ import annotations

import importlib.util

_MCP_SPEC = None
_MCP_CHECKED = False


def available() -> bool:
    """True iff the `mcp` package can be imported. Cached after the first
    check (a missing package doesn't appear mid-process); never actually
    imports `mcp` itself -- `importlib.util.find_spec` is enough to answer
    the question without paying import cost for a caller that just wants
    to decide whether to show a notification."""
    global _MCP_SPEC, _MCP_CHECKED
    if not _MCP_CHECKED:
        try:
            _MCP_SPEC = importlib.util.find_spec("mcp")
        except (ImportError, ValueError):
            _MCP_SPEC = None
        _MCP_CHECKED = True
    return _MCP_SPEC is not None


NOT_AVAILABLE_NOTICE = (
    "MCP is disabled: the 'mcp' package isn't installed in this Python environment "
    "(pip install \"mcp>=1.26,<3\"). No MCP servers will be started this session."
)
