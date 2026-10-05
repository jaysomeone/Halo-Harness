"""halo_harness.mcp.connectors -- the claude.ai connectors bridge (Halo
2.0.1, "W4 MCP: claude.ai connectors bridge" in HALO-2.0.1-gaplist-brief.md).

On a box where `claude` is installed and logged into claude.ai, `claude mcp
list` reports account-side connectors (verified live on the Kali VM:
`claude.ai Claude Docs: https://api.anthropic.com/v1/pages/mcp - ✔ Connected`)
that exist nowhere on disk -- they live in the claude.ai account and are
reachable only through the `claude` binary's own login. Halo can never see
or authorize them directly (and never reads `~/.claude/.credentials.json`),
so this module discovers them THROUGH `claude` (a plain `claude mcp list`
subprocess, plus one headless `claude -p --output-format stream-json` start
to learn each connector's own tool names from its `system/init` line) and
caches the result under `~/.halo/mcp/connectors.json`.

Generic by design: no connector name is special-cased anywhere here -- the
vendor connectors dropped from this release (2026-10-01, per the owner's
own call: "you can drop that completely") are just more rows of the same
shape, and nothing below would need to change if they came back.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from halo_harness.config.paths import background_net_disabled, bridge_home

DISCOVERY_TIMEOUT_S = 20.0

# binary-facts sec.9: claude.ai connector wire names collapse runs of "_"
# after sanitising -- the generic `mcp.manager.sanitize_name` deliberately
# does NOT (that collapse is "claude.ai-connector-specific").
_WIRE_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")
_UNDERSCORE_COLLAPSE_RE = re.compile(r"_+")


def _wn_connector(text: str) -> str:
    return _UNDERSCORE_COLLAPSE_RE.sub("_", _WIRE_SANITIZE_RE.sub("_", text or ""))


def account_token(name: str) -> str:
    """e.g. "Claude Docs" -> "Claude_Docs" -- the `<name>` half of a real
    `mcp__claude_ai_<name>__<tool>` wire name."""
    return _wn_connector(name)


def wire_prefix(name: str) -> str:
    """The FULL sanitised server token Claude Code embeds in
    `mcp__<token>__<tool>` for an account-side connector -- `wn("claude.ai "
    + name)`, e.g. "Claude Docs" -> "claude_ai_Claude_Docs"."""
    return _wn_connector(f"claude.ai {name}")


def slugify(name: str) -> str:
    """The halo tool slug: `connector__<slug>` (brief's own examples:
    `connector__claude_docs`, `connector__other_service`)."""
    return account_token(name).lower().strip("_") or "connector"


@dataclass
class ConnectorInfo:
    name: str
    slug: str
    account_token: str
    url: str = ""
    host: str = ""
    status: str = "unknown"       # connected | needs_auth | failed | unknown
    status_text: str = ""         # claude's own trailing status text, verbatim
    tools: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "slug": self.slug, "account_token": self.account_token,
                "url": self.url, "host": self.host, "status": self.status,
                "status_text": self.status_text, "tools": list(self.tools)}

    @staticmethod
    def from_dict(d: dict) -> "ConnectorInfo":
        return ConnectorInfo(name=d.get("name", ""), slug=d.get("slug", ""),
                              account_token=d.get("account_token", ""), url=d.get("url", ""),
                              host=d.get("host", ""), status=d.get("status", "unknown"),
                              status_text=d.get("status_text", ""), tools=list(d.get("tools") or []))


# ---- `claude mcp list` parsing ---------------------------------------------

_LIST_LINE_RE = re.compile(r"^claude\.ai\s+(.+?):\s+(\S+)\s+-\s+(.+)$")
_STATUS_NEEDLES = (("needs authentication", "needs_auth"), ("connected", "connected"), ("failed", "failed"))


def _classify_status(text: str) -> str:
    low = text.strip().lower()
    for needle, state in _STATUS_NEEDLES:
        if needle in low:
            return state
    return "unknown"


def parse_claude_mcp_list(output: str) -> "list[ConnectorInfo]":
    """Parses only the `claude.ai <Name>: <url> - <status>` lines a real
    `claude mcp list` prints for an account-side connector; every other
    line (halo's own stdio/http servers, blank lines) is a different shape
    and simply ignored -- halo already knows about those through
    `mcp.manager.resolve_server_configs`."""
    out = []
    for raw_line in (output or "").splitlines():
        m = _LIST_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        name, url, status_text = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        host = urlparse(url).hostname or url
        out.append(ConnectorInfo(name=name, slug=slugify(name), account_token=account_token(name),
                                   url=url, host=host, status=_classify_status(status_text),
                                   status_text=status_text))
    return out


# ---- subprocess plumbing ----------------------------------------------------

def _claude_subprocess_env() -> dict:
    from halo_harness.providers.config import cc_child_env
    return cc_child_env(dict(os.environ))


def run_claude_mcp_list(*, timeout: float = DISCOVERY_TIMEOUT_S) -> "tuple[str, str]":
    """`(stdout, stderr)` -- never raises; a missing binary/timeout/OSError
    all degrade to `("", "<reason>")` so a caller can treat them uniformly
    as "found nothing, here's why"."""
    from halo_harness.providers.cc_models import ClaudeCodeNotFoundError, resolve_claude_launch_argv
    try:
        argv = resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError as e:
        return "", str(e)
    try:
        # encoding="utf-8" (never the Windows console's own codepage default,
        # which can't represent claude's own "✔"/"✗" status glyphs
        # -- verified live: cp1252 raises UnicodeEncodeError INSIDE the
        # child's own `print`, same fix `agent/cc_process.ClaudeCodeProcess`
        # already applies to every other claude subprocess this harness spawns).
        proc = subprocess.run(argv + ["mcp", "list"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=_claude_subprocess_env())
    except subprocess.TimeoutExpired:
        # Halo 2.0.2 W7 round 1 (brief F): a real `claude` child DID
        # start (subprocess.run's timeout path kills it after launch) --
        # re-assert halo's own title in case it scribbled the console/
        # terminal title before being killed.
        from halo_harness.termtitle import reassert_after_claude_child
        reassert_after_claude_child()
        return "", f"claude mcp list timed out after {timeout:.0f}s"
    except OSError as e:
        return "", f"{type(e).__name__}: {e}"
    from halo_harness.termtitle import reassert_after_claude_child
    reassert_after_claude_child()
    return proc.stdout, proc.stderr


_CLAUDE_AI_TOOL_RE = re.compile(r"^mcp__claude_ai_(.+?)__([A-Za-z0-9_]+)$")


def group_claude_ai_tool_names(tool_names) -> "dict[str, list[str]]":
    buckets: dict = {}
    for wire_name in tool_names or []:
        if not isinstance(wire_name, str):
            continue
        m = _CLAUDE_AI_TOOL_RE.match(wire_name)
        if not m:
            continue
        buckets.setdefault(m.group(1), []).append(m.group(2))
    return buckets


def _read_one_event_with_timeout(proc, timeout: float) -> Optional[dict]:
    """Bounds `ClaudeCodeProcess.read_event()` (a plain blocking
    `readline()` + `json.loads`, no timeout of its own) the same way
    `agent/cc_runtime.py`'s own reader thread bounds every blocking read
    against this exact process class."""
    box: "list[Optional[dict]]" = [None]

    def _reader() -> None:
        try:
            box[0] = proc.read_event()
        except (OSError, ValueError):
            box[0] = None

    t = threading.Thread(target=_reader, daemon=True, name="halo-connectors-initline")
    t.start()
    t.join(timeout)
    return box[0]


def discover_connector_tool_names(*, timeout: float = DISCOVERY_TIMEOUT_S) -> "dict[str, list[str]]":
    """One headless `claude -p --output-format stream-json` start, reading
    only its `system/init` line for the `mcp__claude_ai_<name>__<tool>`
    names it lists (the SAME init line `agent/cc_runtime.py` already
    parses for the `cc:` route's own bridge) -- the process is killed right
    after that line arrives, so no model turn is ever spent. Spawned via
    `agent.cc_process.ClaudeCodeProcess` (the SAME class the `cc:` route's
    own bridge uses) rather than a bare `subprocess.Popen`, so an early
    kill reaches claude's whole process GROUP (POSIX) / process tree
    (Windows Job Object) -- never a lone orphaned child."""
    from halo_harness.agent.cc_process import ClaudeCodeProcess
    from halo_harness.providers.cc_models import ClaudeCodeNotFoundError, resolve_claude_launch_argv
    try:
        argv = resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError:
        return {}
    argv = argv + ["-p", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose"]
    try:
        proc = ClaudeCodeProcess(argv, cwd=Path.cwd(), env=_claude_subprocess_env())
    except OSError:
        return {}
    try:
        obj = _read_one_event_with_timeout(proc, timeout)
    finally:
        proc.kill()
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        proc.close_stdin()
    if not obj or obj.get("type") != "system" or obj.get("subtype") != "init":
        return {}
    return group_claude_ai_tool_names(obj.get("tools") or [])


def discover_connectors_now(*, timeout: float = DISCOVERY_TIMEOUT_S) -> "list[ConnectorInfo]":
    """The real (blocking, subprocess-spawning) discovery -- callers off
    the UI thread only (`ensure_discovered_in_background` below); a CLI
    one-shot like `halo mcp list`/`halo doctor` has no UI thread to
    protect and may call this inline, same contract as their own existing
    live MCP health checks. Never raises."""
    stdout, _stderr = run_claude_mcp_list(timeout=timeout)
    connectors = parse_claude_mcp_list(stdout)
    if not connectors:
        return []
    tool_buckets = discover_connector_tool_names(timeout=timeout)
    for c in connectors:
        c.tools = tool_buckets.get(c.account_token, [])
    return connectors
