"""halo_harness.mcp -- the MCP client (H3 scope A), built on the official
`mcp` SDK (`pip install "mcp>=2.2,<3"`). This package is IMPORTABLE even
when `mcp` isn't installed: every module here that actually touches the SDK
imports it lazily, inside function bodies, never at module import time --
`halo_harness.mcp.available()` is the one place that checks, so every caller
(manager.py's own resolution path, headless.py, doctor.py) asks it instead
of poking at `sys.modules` itself.

Modules: `client` (McpLoop -- one daemon thread + asyncio loop for all MCP
traffic, sync facade), `stdio` (stdio transport), `http_sse` (http/sse
transport), `manager` (McpServerConfig/McpServerHandle/McpManager -- scope
resolution, lifecycle, tool/resource/prompt listing).
"""

from __future__ import annotations

import importlib.util
from typing import Optional

_MCP_SPEC = None
_MCP_CHECKED = False


def _installed_major_version() -> Optional[int]:
    """Best-effort installed `mcp` major version, without importing the
    package itself (finding 12: the code reads 2.x-only field names/APIs,
    so a 1.x install must read as unsupported rather than silently sending
    empty schemas and crashing on the first real tool call)."""
    try:
        from importlib.metadata import version as _pkg_version
        raw = _pkg_version("mcp")
    except Exception:
        try:
            import mcp as _mcp_mod  # last resort -- pays the import cost
            raw = getattr(_mcp_mod, "__version__", None)
        except Exception:
            return None
    if not raw:
        return None
    try:
        return int(str(raw).split(".")[0])
    except (ValueError, IndexError):
        return None


def available() -> bool:
    """True iff a SUPPORTED `mcp` package (>=2.x -- finding 12) can be
    imported. Cached after the first check (an install doesn't change
    mid-process); the spec lookup never actually imports `mcp` itself, but
    the version check (only reached once a spec IS found) does pay a small
    `importlib.metadata` cost -- still no `import mcp`."""
    global _MCP_SPEC, _MCP_CHECKED
    if not _MCP_CHECKED:
        try:
            _MCP_SPEC = importlib.util.find_spec("mcp")
        except (ImportError, ValueError):
            _MCP_SPEC = None
        _MCP_CHECKED = True
    if _MCP_SPEC is None:
        return False
    major = _installed_major_version()
    return major is None or major >= 2  # unknown version: don't block on a metadata quirk


NOT_AVAILABLE_NOTICE = (
    "MCP is disabled: no supported 'mcp' package (pip install \"mcp>=2.2,<3\") is importable "
    "in this Python environment -- either it isn't installed, or an old 1.x install is present "
    "and its API doesn't match (empty tool schemas, timedelta-vs-float call args). No MCP "
    "servers will be started this session."
)
