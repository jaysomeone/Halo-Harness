"""rolo_claude.mcp.manager -- McpServerConfig/parse_server/scope resolution
+ McpServerHandle/McpManager (H3 scope A). The only module that decides
WHICH servers exist and in what order; `client.py`/`stdio.py`/`http_sse.py`
do the actual async transport work this module drives through `McpLoop`'s
sync facade. Imports `mcp` lazily (only inside `McpServerHandle`'s async
methods, via `stdio`/`http_sse`) so this module -- and everything that
merely imports it to call `resolve_server_configs` for config PARSING --
stays usable without the SDK installed; only `start()`/`start_all()` need it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import lookup_project

log = logging.getLogger("bridge")

# Fire-and-forget `stack.aclose()` cleanup tasks (see `_connect_once`) MUST
# be held by a strong reference somewhere for their whole lifetime: asyncio
# docs, `create_task()`: "the event loop only keeps weak references to
# tasks... a task can disappear at any time before it's done ... For
# reliable 'fire-and-forget' background tasks, gather them in a
# collection." Without this, the task -- and the live subprocess/pipes its
# `stack.aclose()` was supposed to tear down -- can vanish mid-cleanup,
# which is exactly the leaked-transport shape that (verified on Windows)
# crashes at interpreter shutdown rather than merely leaking. Each task
# removes itself via `add_done_callback` once it finishes.
_background_cleanup_tasks: set = set()

# ---- timeouts (binary-facts sec.9) -----------------------------------------

_INT32_MAX = 2**31 - 1


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return min(v, _INT32_MAX)
        except ValueError:
            pass
    return default


def mcp_timeout_ms() -> int:
    return _env_positive_int("MCP_TIMEOUT", 30_000)


def mcp_timeout_s() -> float:
    return mcp_timeout_ms() / 1000.0


def mcp_connect_timeout_s() -> float:
    return _env_positive_int("MCP_CONNECT_TIMEOUT_MS", 5_000) / 1000.0


def tool_timeout_s(server_timeout_ms: Optional[float]) -> float:
    """server `timeout` (>=1000) ?? MCP_TOOL_TIMEOUT ?? 1e8 ms (~27.8h),
    clamped to [1000, 2^31-1] -- binary-facts sec.9, verbatim."""
    if isinstance(server_timeout_ms, (int, float)) and server_timeout_ms >= 1000:
        return min(int(server_timeout_ms), _INT32_MAX) / 1000.0
    raw = os.environ.get("MCP_TOOL_TIMEOUT")
    if raw:
        try:
            v = int(raw)
            if v >= 1000:
                return min(v, _INT32_MAX) / 1000.0
        except ValueError:
            pass
    return min(int(1e8), _INT32_MAX) / 1000.0


# ---- name sanitising (binary-facts sec.9) ----------------------------------

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_name(name: str) -> str:
    """`wn(e) = e.replace(/[^a-zA-Z0-9_-]/g, "_")` -- verbatim, no `_+`
    collapse (that extra step is claude.ai-connector-specific, out of scope
    here -- see binary-facts sec.9)."""
    return _SANITIZE_RE.sub("_", name or "")


def mcp_tool_name(server: str, tool: str) -> str:
    return f"mcp__{sanitize_name(server)}__{sanitize_name(tool)}"


def split_mcp_tool_name(wire_name: str) -> "Optional[tuple[str, str]]":
    """Inverse of `mcp_tool_name`, best-effort: splits on the FIRST `__`
    after the `mcp__` prefix (matches permissions.py's own parsing) --
    `None` if `wire_name` doesn't start with `mcp__`."""
    if not wire_name.startswith("mcp__"):
        return None
    rest = wire_name[len("mcp__"):]
    if "__" not in rest:
        return rest, ""
    server, tool = rest.split("__", 1)
    return server, tool


# ---- ${VAR} / ${VAR:-default} expansion ------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")
_CREDENTIAL_NAME_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASS|AUTH|CREDENTIAL)", re.IGNORECASE)


def _looks_like_credential(var_name: str) -> bool:
    return bool(_CREDENTIAL_NAME_RE.search(var_name))


def expand_string(text: str, env: dict, *, credential_blank: bool = False, warnings: Optional[list] = None) -> str:
    """`${VAR}` -> env[VAR], unset -> left VERBATIM + a warning appended to
    `warnings`; `${VAR:-default}` -> env[VAR] or `default` when unset (no
    warning -- a default means "unset is expected"). `credential_blank`
    (D-CFG: "credential vars read as empty for remote url/headers") makes a
    credential-shaped var name resolve to "" instead of its real value,
    regardless of whether it's actually set -- url/headers are logged/
    displayed places a real secret must never land."""
    if not isinstance(text, str) or "${" not in text:
        return text

    def _sub(m: "re.Match") -> str:
        name, has_default, default = m.group(1), m.group(2), m.group(3)
        if credential_blank and _looks_like_credential(name):
            return ""
        if name in env:
            return env[name]
        if has_default is not None:
            return default or ""
        if warnings is not None:
            warnings.append(f"${{{name}}} is unset and was left unexpanded")
        return m.group(0)

    return _VAR_RE.sub(_sub, text)


# ---- McpServerConfig --------------------------------------------------------

@dataclass
class McpServerConfig:
    name: str
    type: str  # stdio | http | sse | ws | websocket | sdk | invalid
    command: Optional[str] = None
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: dict = field(default_factory=dict)
    headers_helper: Optional[str] = None
    timeout_ms: Optional[int] = None
    always_load: bool = False
    oauth: Optional[dict] = None
    scope: str = "user"  # local | project | user | flag | managed | dynamic
    source_path: Optional[str] = None
    plugin: Optional[str] = None
    disabled_reason: Optional[str] = None
    pending_approval: bool = False


def parse_server(name: str, raw, *, scope: str, source_path: Optional[str] = None) -> Optional[McpServerConfig]:
    """One `~/.claude.json`/`.mcp.json`/`--mcp-config` entry -> McpServerConfig,
    or a disabled placeholder (never None for a dict entry -- a caller that
    wants to skip unsupported transports still sees WHY in `.disabled_reason`
    rather than the entry silently vanishing); `raw` not being a dict at all
    (malformed config) is the only case that returns None."""
    if not isinstance(raw, dict):
        return None
    stype = raw.get("type")
    command = raw.get("command")
    url = raw.get("url")
    if not stype:
        if command:
            stype = "stdio"
        elif url:
            return McpServerConfig(name=name, type="invalid", scope=scope, source_path=source_path,
                                    disabled_reason="a `url` without `type` is invalid [D-CFG]")
        else:
            return McpServerConfig(name=name, type="invalid", scope=scope, source_path=source_path,
                                    disabled_reason="entry has neither `command` nor `url`+`type`")
    if stype == "streamable-http":
        stype = "http"
    if stype in ("ws", "websocket", "sdk"):
        return McpServerConfig(name=name, type=stype, scope=scope, source_path=source_path,
                                disabled_reason=f"{stype!r} servers are not supported yet (skipped)")
    if stype not in ("stdio", "http", "sse"):
        return McpServerConfig(name=name, type="invalid", scope=scope, source_path=source_path,
                                disabled_reason=f"unknown transport {stype!r}")
    return McpServerConfig(
        name=name, type=stype, command=command, args=list(raw.get("args") or []),
        env=dict(raw.get("env") or {}), cwd=raw.get("cwd"), url=url,
        headers=dict(raw.get("headers") or {}), headers_helper=raw.get("headersHelper"),
        timeout_ms=raw.get("timeout"), always_load=bool(raw.get("alwaysLoad", False)),
        oauth=raw.get("oauth"), scope=scope, source_path=source_path,
    )


def expand_config(config: McpServerConfig, env: dict) -> "tuple[McpServerConfig, list[str]]":
    warnings: list = []
    command = expand_string(config.command, env, warnings=warnings) if config.command else config.command
    args = [expand_string(a, env, warnings=warnings) for a in config.args]
    env_block = {k: expand_string(v, env, warnings=warnings) for k, v in config.env.items()}
    url = expand_string(config.url, env, credential_blank=True, warnings=warnings) if config.url else config.url
    headers = {k: expand_string(v, env, credential_blank=True, warnings=warnings) for k, v in config.headers.items()}
    new_cfg = dataclass_replace_config(config, command=command, args=args, env=env_block, url=url, headers=headers)
    return new_cfg, warnings


def dataclass_replace_config(config: McpServerConfig, **kwargs) -> McpServerConfig:
    import dataclasses
    return dataclasses.replace(config, **kwargs)


# ---- config-source loaders ---------------------------------------------------

def _load_json_file(path: Path) -> "tuple[Optional[dict], Optional[str]]":
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        return None, f"could not read {path}: {e}"
    except ValueError as e:
        return None, f"{path} is not valid JSON: {e}"
    if not isinstance(data, dict):
        return None, f"{path} must contain a JSON object"
    return data, None


def _load_managed_mcp_json(path: Optional[Path]) -> Optional[dict]:
    """`managed-mcp.json` present -> EXCLUSIVE [bin]; None means "not
    present, fall through to the normal resolution chain" -- distinct from
    an empty `{}` (present but defines zero servers, which is STILL
    exclusive: every other source is ignored)."""
    if path is None or not path.exists():
        return None
    data, err = _load_json_file(path)
    if err:
        log.warning("managed-mcp.json: %s (treating as empty -- exclusive either way)", err)
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _load_mcp_config_arg(spec: str, cwd: Path) -> "tuple[dict, Optional[str]]":
    """One `--mcp-config` value: a JSON object string (inline), or a path
    to a JSON file -- either shaped `{"mcpServers": {...}}` or a bare
    `{server: entry, ...}` map (Claude Code accepts both)."""
    text = spec
    source = spec
    path = Path(spec)
    if not path.is_absolute():
        path = cwd / spec
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as e:
            return {}, f"--mcp-config {spec!r}: could not read: {e}"
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, f"--mcp-config {source!r}: not valid JSON: {e}"
    if not isinstance(data, dict):
        return {}, f"--mcp-config {source!r}: must be a JSON object"
    servers = data.get("mcpServers", data)
    return (servers if isinstance(servers, dict) else {}), None


def _load_dot_mcp_json(path: Path) -> "tuple[dict, Optional[str]]":
    data, err = _load_json_file(path)
    if err:
        return {}, err
    servers = data.get("mcpServers")
    return (servers if isinstance(servers, dict) else {}), None


# ---- scope resolution [D-CFG] -----------------------------------------------

def resolve_server_configs(
    *, cwd: Path, claude_json: dict, mcp_config_flag: Optional[list] = None,
    strict_mcp_config: bool = False, print_mode: bool = False,
    approvals: Optional[dict] = None, managed_mcp_path: Optional[Path] = None,
    env_for_expansion: Optional[dict] = None, extra_dynamic: Optional[dict] = None,
) -> "tuple[dict[str, McpServerConfig], list[str]]":
    """(name -> McpServerConfig, notices). First-name-wins across tiers, in
    THIS order [D-CFG]: `managed-mcp.json` present -> EXCLUSIVE -> else
    `--mcp-config` (repeatable) -> (stop here if `--strict-mcp-config`) ->
    local `projects[cwd].mcpServers` (both key forms) -> `<cwd>/.mcp.json`
    (approval-gated; `disabledMcpjsonServers` always wins even over a name
    already claimed by a HIGHER tier -- Claude Code removes it outright) ->
    user `mcpServers` -> `extra_dynamic` (this run's `--chrome`/
    `--playwright` servers, lowest precedence). `projects[cwd].
    disabledMcpServers` removes a name after every tier has been consulted.
    `${VAR}`/`${VAR:-default}` expansion is applied to every surviving
    entry as the last step."""
    notices: list = []
    resolved: "dict[str, McpServerConfig]" = {}

    managed = _load_managed_mcp_json(managed_mcp_path)
    if managed is not None:
        for name, raw in managed.items():
            cfg = parse_server(name, raw, scope="managed", source_path=str(managed_mcp_path))
            if cfg is not None:
                resolved[name] = cfg
        return _expand_all(resolved, env_for_expansion, notices)

    def _add_missing(entries: dict, scope: str, source_path=None):
        for name, raw in entries.items():
            if name not in resolved:
                cfg = parse_server(name, raw, scope=scope, source_path=source_path)
                if cfg is not None:
                    resolved[name] = cfg

    for spec in (mcp_config_flag or []):
        entries, err = _load_mcp_config_arg(spec, Path(cwd))
        if err:
            notices.append(err)
            continue
        _add_missing(entries, "flag", source_path=spec)

    if strict_mcp_config:
        return _expand_all(resolved, env_for_expansion, notices)

    proj = lookup_project(claude_json, cwd)
    local_servers = proj.get("mcpServers")
    if isinstance(local_servers, dict):
        _add_missing(local_servers, "local")

    mcp_json_path = Path(cwd) / ".mcp.json"
    disabled_project = set(proj.get("disabledMcpjsonServers") or [])
    if mcp_json_path.exists():
        project_entries, perr = _load_dot_mcp_json(mcp_json_path)
        if perr:
            notices.append(perr)
        else:
            enabled = set(proj.get("enabledMcpjsonServers") or [])
            enable_all = bool(proj.get("enableAllProjectMcpServers"))
            our_approvals = approvals or {}
            for name, raw in project_entries.items():
                if name in resolved or name in disabled_project:
                    continue
                cfg = parse_server(name, raw, scope="project", source_path=str(mcp_json_path))
                if cfg is None:
                    continue
                approved = (name in enabled) or enable_all or print_mode or bool(our_approvals.get(name))
                if not approved:
                    cfg.pending_approval = True
                resolved[name] = cfg

    user_servers = claude_json.get("mcpServers")
    if isinstance(user_servers, dict):
        _add_missing(user_servers, "user")

    if extra_dynamic:
        for name, cfg in extra_dynamic.items():
            resolved.setdefault(name, cfg)

    for name in disabled_project:
        resolved.pop(name, None)
    for name in set(proj.get("disabledMcpServers") or []):
        resolved.pop(name, None)

    return _expand_all(resolved, env_for_expansion, notices)


def _expand_all(resolved: dict, env_for_expansion, notices: list) -> "tuple[dict, list]":
    if not env_for_expansion:
        return resolved, notices
    final = {}
    for name, cfg in resolved.items():
        expanded, warns = expand_config(cfg, env_for_expansion)
        for w in warns:
            notices.append(f"mcp server {name!r}: {w}")
        final[name] = expanded
    return final, notices


# ---- McpServerHandle ---------------------------------------------------------

# state vocabulary: pending | pending_approval | connecting | connected |
# failed | disabled | needs_auth | closed [D-CFG].
class McpServerHandle:
    """One server's whole connection lifecycle, transport-agnostic past
    `_connect_once` (stdio vs http vs sse only matters for how the
    AsyncExitStack gets opened -- every method after that talks to the same
    `mcp.ClientSession`). Every public method here is a SYNC call; open and
    close run inside the ONE `_lifecycle_task` spawned by `start()` (or by
    `McpManager.start_all()`'s own `_start_one`), never as separate
    `McpLoop.run()` calls -- see `_lifecycle_task`'s own docstring for why.
    Only `_connect_once`/`_lifecycle_task`/`_open_transport` are coroutines,
    and only they ever import `mcp`/`stdio`/`http_sse`."""

    def __init__(self, config: McpServerConfig, loop, *, tool_env: dict, cwd) -> None:
        self.config = config
        self._loop = loop
        self._tool_env = tool_env
        self._cwd = cwd
        if config.type in ("invalid", "ws", "websocket", "sdk"):
            self.state = "disabled"
        elif config.pending_approval:
            self.state = "pending_approval"
        else:
            self.state = "pending"
        self.error: Optional[str] = None
        self.instructions: Optional[str] = None
        self.tools: list = []       # raw SDK Tool objects
        self.tools_fetch_failed = False
        self._stack = None
        self._session = None
        self._errlog = None
        # anyio's cancel scopes (inside stdio_client's own TaskGroup) must
        # be entered AND exited by the SAME asyncio Task -- verified on
        # Linux/WSL: `RuntimeError("Attempted to exit cancel scope in a
        # different task than it was entered in")` when open() and
        # close() each ran as their own `McpLoop.run()` call (a fresh
        # Task every time). `_lifecycle_task` is ONE coroutine, spawned
        # once and left running for the connection's whole life, that
        # does the open, then awaits `_close_event`, then does the close
        # -- ordinary calls (call_tool/list_resources/...) stay as
        # independent per-call `run()` tasks below (safe: they only send/
        # receive on already-open streams, never touch the scope itself).
        self._close_event: Optional[asyncio.Event] = None
        self._lifecycle_future: "Optional[concurrent.futures.Future]" = None

    def _resolved_headers(self) -> dict:
        headers = dict(self.config.headers or {})
        if self.config.headers_helper:
            try:
                from rolo_claude.mcp.http_sse import merged_headers, run_headers_helper
                helper_headers = run_headers_helper(
                    self.config.headers_helper, env=self._tool_env,
                    server_name=self.config.name, url=self.config.url or "",
                )
                headers = merged_headers(headers, helper_headers)
            except Exception as e:  # headersHelper failure -> static headers, never fatal
                log.debug("mcp %s: headersHelper failed (%s) -- using static headers only", self.config.name, e)
        return headers

    async def _open_transport(self, connect_timeout: float):
        from rolo_claude.mcp import http_sse as http_sse_mod
        from rolo_claude.mcp import stdio as stdio_mod

        if self.config.type == "stdio":
            self._errlog = stdio_mod.open_errlog(self.config.name)
            env = stdio_mod.build_env(self.config.env, self._tool_env, self._cwd)
            return await stdio_mod.connect(
                command=self.config.command, args=self.config.args, env=env,
                cwd=self.config.cwd, errlog=self._errlog, connect_timeout=connect_timeout,
            )
        if self.config.type == "http":
            return await http_sse_mod.connect_http(
                url=self.config.url, headers=self._resolved_headers(), connect_timeout=connect_timeout)
        if self.config.type == "sse":
            log.debug("mcp %s: 'sse' transport is deprecated (use 'http')", self.config.name)
            return await http_sse_mod.connect_sse(
                url=self.config.url, headers=self._resolved_headers(), connect_timeout=connect_timeout)
        raise ValueError(f"unsupported transport: {self.config.type!r}")

    async def _connect_once(self) -> None:
        """Open the transport, initialize, list_tools -- raises on any
        connect/initialize failure (list_tools failing on its own is
        caught and recorded as `tools_fetch_failed` instead, same as
        before). Called from inside `_lifecycle_task`, never directly.

        `_do()` has its OWN try/except around `initialize()`: the outer
        `asyncio.wait_for` below cancels `_do()` on a slow/hung server
        (MCP_TIMEOUT elapsed) -- without this, a `stack` that
        `_open_transport` already opened (a real, live subprocess + its
        pipes) would be silently dropped with nothing left to ever close
        it. That's not a Python-level memory leak (refcounting collects
        the AsyncExitStack object itself just fine) -- it's an OS-level
        one: the child process keeps running and its pipe/overlapped-I/O
        registrations stay live on the loop's proactor, which is exactly
        the state that (verified on Windows) CRASHES rather than merely
        leaks once the owning loop is later closed or GC'd during
        interpreter shutdown [see tests/test_mcp_manager.py::
        test_manager_start_all_is_bounded_by_mcp_timeout, which
        deliberately reproduces this exact timing]. The close is fired via
        `asyncio.create_task` rather than awaited here: the installed mcp
        SDK's own `stdio_client` shutdown is already shielded + bounded
        (close stdin, up to a multi-second grace period for the server to
        exit on its own, then a hard kill -- mcp/client/stdio.py), but
        awaiting that inline would turn THIS failed connect attempt's
        latency into that same multi-second wait; a detached task lets the
        caller see the timeout fail fast while the subprocess still gets
        torn down a moment later, on the very loop McpManager.close_all()/
        McpLoop.stop() already wait out before the process ever exits."""
        connect_timeout = mcp_connect_timeout_s()
        overall_timeout = mcp_timeout_s()

        async def _do():
            stack, session = await self._open_transport(connect_timeout)
            try:
                init_result = await session.initialize()
            except BaseException:
                async def _cleanup() -> None:
                    try:
                        await stack.aclose()
                    except Exception:
                        pass
                cleanup_task = asyncio.create_task(_cleanup())
                _background_cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(_background_cleanup_tasks.discard)
                raise
            return stack, session, init_result

        stack, session, init_result = await asyncio.wait_for(_do(), timeout=overall_timeout)
        self._stack, self._session = stack, session
        self.instructions = getattr(init_result, "instructions", None)
        try:
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=overall_timeout)
            self.tools = list(getattr(tools_result, "tools", None) or [])
        except Exception as e:
            # connection + handshake succeeded but the tool listing itself
            # failed -- "! Connected . tools fetch failed" in mcp_cli.py's
            # status vocabulary, not a hard "failed" state.
            self.tools_fetch_failed = True
            log.debug("mcp %s: tools/list failed after a clean initialize: %s", self.config.name, e)

    async def _lifecycle_task(self, connect_future: "concurrent.futures.Future") -> None:
        """The ONE task that owns this connection's cancel-scope-sensitive
        boundary: connect, resolve `connect_future` either way, then (only
        on success) block until `close()` sets `self._close_event`, then
        `aclose()` the stack -- all in this same task/coroutine.
        `connect_future` is a plain `concurrent.futures.Future`, thread-
        safe to resolve from here and awaitable from EITHER a sync caller
        (`.result(timeout=)`, `start()`) or an async one already running
        on this same loop (`await asyncio.wrap_future(...)`,
        `McpManager.start_all()`'s `_start_one`) -- one implementation
        serves both call sites."""
        self._close_event = asyncio.Event()
        try:
            await self._connect_once()
        except Exception as e:
            if not connect_future.done():
                connect_future.set_exception(e)
            return
        if not connect_future.done():
            connect_future.set_result(None)
        try:
            await self._close_event.wait()
        finally:
            if self._stack is not None:
                await self._stack.aclose()

    def start(self) -> None:
        if self.state in ("disabled", "pending_approval"):
            return
        from rolo_claude.mcp import http_sse as http_sse_mod
        self.state = "connecting"
        connect_future: "concurrent.futures.Future" = concurrent.futures.Future()
        self._lifecycle_future = self._loop.spawn(self._lifecycle_task(connect_future))
        try:
            connect_future.result(timeout=mcp_timeout_s() + 4)
        except Exception as e:
            self.state = "needs_auth" if http_sse_mod.looks_like_auth_required(e) else "failed"
            self.error = f"{type(e).__name__}: {e}"
            return
        self.state = "connected"

    def call_tool(self, name: str, arguments: dict, *, timeout: Optional[float] = None):
        if self._session is None:
            raise RuntimeError(f"mcp server {self.config.name!r} is not connected (state={self.state})")
        return self._loop.run(
            self._session.call_tool(name, arguments or {}, read_timeout_seconds=timeout),
            timeout=(timeout + 3) if timeout else mcp_timeout_s() + 3,
        )

    def list_resources(self) -> list:
        if self._session is None:
            return []
        try:
            result = self._loop.run(self._session.list_resources(), timeout=mcp_timeout_s())
        except Exception:
            return []
        return list(getattr(result, "resources", None) or [])

    def read_resource(self, uri: str):
        if self._session is None:
            raise RuntimeError(f"mcp server {self.config.name!r} is not connected (state={self.state})")
        return self._loop.run(self._session.read_resource(uri), timeout=mcp_timeout_s())

    def list_prompts(self) -> list:
        if self._session is None:
            return []
        try:
            result = self._loop.run(self._session.list_prompts(), timeout=mcp_timeout_s())
        except Exception:
            return []
        return list(getattr(result, "prompts", None) or [])

    def get_prompt(self, name: str, arguments: Optional[dict] = None):
        if self._session is None:
            raise RuntimeError(f"mcp server {self.config.name!r} is not connected (state={self.state})")
        return self._loop.run(self._session.get_prompt(name, arguments or {}), timeout=mcp_timeout_s())

    def close(self, timeout: float = 5.0) -> None:
        if self._close_event is not None and self._lifecycle_future is not None:
            # Signal the SAME task that opened the connection to run its
            # own `aclose()` -- never a separate `run()` call (that would
            # recreate the cross-task cancel-scope bug this design fixes).
            try:
                self._loop.call_soon(self._close_event.set)
                self._lifecycle_future.result(timeout=timeout)
            except Exception:
                pass
        if self._errlog is not None:
            self._errlog.close()
        self.state = "closed"
        self._stack = None
        self._session = None


# ---- McpManager ---------------------------------------------------------------

class McpManager:
    """Owns one `McpServerHandle` per configured server name and the ONE
    `McpLoop` they all share. `start_all()` is the only method that blocks
    for real wall-clock time (bounded by MCP_TIMEOUT); every other method
    is a thin, fast, synchronous wrapper a tool call or CLI command can use
    directly."""

    def __init__(self, configs: "dict[str, McpServerConfig]", *, loop=None,
                 tool_env: Optional[dict] = None, cwd=None, lazy_names: Optional[set] = None) -> None:
        from rolo_claude.mcp.client import McpLoop
        self.loop = loop or McpLoop()
        self.configs = configs
        self._lazy_names = set(lazy_names or ())
        self._closed = False
        self.handles: "dict[str, McpServerHandle]" = {
            name: McpServerHandle(cfg, self.loop, tool_env=tool_env or {}, cwd=cwd)
            for name, cfg in configs.items()
        }

    def start_all(self) -> None:
        """Start every eligible server (not lazy, not disabled, not
        pending_approval) IN PARALLEL, the whole phase bounded by
        MCP_TIMEOUT [D-CFG]; a per-server failure is recorded on its own
        handle and never aborts the others, and this method itself never
        raises -- `status()` afterward is how a caller learns what
        happened. Safe to call with zero eligible servers (no-op)."""
        from rolo_claude.mcp import http_sse as http_sse_mod

        # idempotent: a handle already connected/connecting/failed/
        # needs_auth/closed from an EARLIER start_all()/start() is never
        # re-targeted -- only genuinely still-"pending" handles are. A
        # caller (e.g. one that lazily adds servers between two
        # start_all() calls) can call this safely more than once; without
        # this guard a second call would spawn a SECOND lifecycle task for
        # an already-running connection, racing it for `self._stack`/
        # `self._session`/`self._close_event` (verified: a real, if slow,
        # test flake -- `all_tools()` transiently seeing zero tools while
        # the spurious restart was "connecting" again).
        targets = [h for name, h in self.handles.items()
                   if name not in self._lazy_names and h.state == "pending"]
        if not targets:
            return

        async def _start_one(h: McpServerHandle) -> None:
            # Same lifecycle-task mechanism `McpServerHandle.start()` uses
            # (never the old direct `await h._connect_once()`, which would
            # reintroduce the cross-task cancel-scope bug for THIS, the
            # parallel-startup path -- `close_all()` later signals this
            # SAME spawned task via `h._close_event`/`h._lifecycle_future`).
            h.state = "connecting"
            connect_future: "concurrent.futures.Future" = concurrent.futures.Future()
            h._lifecycle_future = self.loop.spawn(h._lifecycle_task(connect_future))
            try:
                await asyncio.wrap_future(connect_future)
                h.state = "connected"
            except Exception as e:
                h.state = "needs_auth" if http_sse_mod.looks_like_auth_required(e) else "failed"
                h.error = f"{type(e).__name__}: {e}"

        async def _start_all_coro() -> None:
            # `asyncio.gather(...)` must be CALLED from inside a coroutine
            # already running ON the target loop -- calling it eagerly as
            # a plain argument expression (the obvious-looking
            # `self.loop.run(asyncio.gather(...), ...)`) evaluates it on
            # THIS (calling) thread, with no loop of its own running,
            # binding the resulting future to nothing McpLoop's daemon
            # thread ever drives; every `_start_one` coroutine object then
            # sits uncreated/unscheduled and every handle stays "pending"
            # forever. Wrapping it in `_start_all_coro` defers the
            # `gather()` call until `McpLoop.run` has actually scheduled
            # THIS coroutine onto the daemon loop.
            await asyncio.gather(*(_start_one(h) for h in targets), return_exceptions=True)

        overall = mcp_timeout_s()
        try:
            self.loop.run(_start_all_coro(), timeout=overall + 3)
        except Exception as e:
            # the OUTER bound itself lapsed (belt-and-suspenders past each
            # target's own inner wait_for) -- anything still mid-connect
            # never got to record its own terminal state; do it here so a
            # caller never observes a permanently-stuck "connecting".
            for h in targets:
                if h.state == "connecting":
                    h.state = "failed"
                    h.error = f"startup exceeded MCP_TIMEOUT ({overall:.1f}s): {type(e).__name__}: {e}"

    def ensure_started(self, name: str) -> None:
        """`mcpLazy` servers (and a not-yet-approved `.mcp.json` server
        that just became approved) connect on FIRST USE instead of at
        `start_all()` time."""
        h = self.handles.get(name)
        if h is not None and h.state == "pending":
            h.start()

    def all_tools(self) -> "list[tuple[str, str, object]]":
        """`[(server_name, wire_tool_name, sdk_tool), ...]` across every
        CONNECTED server, name-sorted by WIRE name -- the candidate pool
        `agent/catalog.py` selects preload/deferred from, and what
        ToolSearch searches over for a not-yet-loaded name."""
        out = []
        for server_name, h in self.handles.items():
            if h.state != "connected":
                continue
            for t in h.tools:
                out.append((server_name, mcp_tool_name(server_name, t.name), t))
        out.sort(key=lambda triple: triple[1])
        return out

    def call(self, server: str, tool: str, arguments: dict, *, timeout: Optional[float] = None):
        h = self.handles.get(server)
        if h is None:
            raise RuntimeError(f"unknown mcp server: {server!r}")
        self.ensure_started(server)
        effective_timeout = timeout if timeout is not None else tool_timeout_s(h.config.timeout_ms)
        return h.call_tool(tool, arguments, timeout=effective_timeout)

    def status(self) -> "list[dict]":
        """One dict per configured server (connected or not) -- `mcp
        list`/`/mcp`/`doctor` all build their own display from this,
        never from `.handles` directly."""
        out = []
        for name, h in self.handles.items():
            out.append({
                "name": name, "type": h.config.type, "command": h.config.command, "args": h.config.args,
                "url": h.config.url, "state": h.state, "error": h.error,
                "tools_fetch_failed": h.tools_fetch_failed,
                "tool_count": len(h.tools) if h.state == "connected" else 0,
                "instructions": h.instructions, "scope": h.config.scope,
            })
        return out

    def reconnect(self, name: str) -> bool:
        h = self.handles.get(name)
        if h is None:
            return False
        h.close()
        h.state = "pending_approval" if h.config.pending_approval else "pending"
        h.start()
        return h.state == "connected"

    def close_all(self, timeout: float = 5.0) -> None:
        """Close every handle, then hard-stop the shared loop -- the WHOLE
        operation is bounded by `timeout` (D-CFG: "close_all() with a 5s
        deadline"), each handle getting a fair share of whatever's left.
        Idempotent: a second call -- including one made by the mcp.client
        process-exit safety net for a manager the caller already closed
        itself -- is a no-op rather than re-touching handles or spinning
        the shared loop back up."""
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + timeout
        for h in self.handles.values():
            remaining = max(0.1, deadline - time.monotonic())
            h.close(timeout=remaining)
        self.loop.stop(timeout=max(0.1, deadline - time.monotonic()))

    def resources(self) -> "list[tuple[str, object]]":
        out = []
        for name, h in self.handles.items():
            if h.state == "connected":
                out.extend((name, r) for r in h.list_resources())
        return out

    def read_resource(self, server: str, uri: str):
        h = self.handles.get(server)
        if h is None:
            raise RuntimeError(f"unknown mcp server: {server!r}")
        return h.read_resource(uri)

    def prompts(self) -> "list[tuple[str, object]]":
        out = []
        for name, h in self.handles.items():
            if h.state == "connected":
                out.extend((name, p) for p in h.list_prompts())
        return out

    def get_prompt(self, server: str, name: str, arguments: Optional[dict] = None):
        h = self.handles.get(server)
        if h is None:
            raise RuntimeError(f"unknown mcp server: {server!r}")
        return h.get_prompt(name, arguments)
