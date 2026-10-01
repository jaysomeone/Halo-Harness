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

# H4 fix (was: a fire-and-forget `asyncio.create_task(_cleanup())` set here,
# for a `session.initialize()` failure's `stack.aclose()` in `_connect_once`
# below) -- a DETACHED cleanup task meant `_connect_once` returned/raised
# before the SDK's own shielded stdio shutdown (the thing that actually
# kills the spawned child) had finished running, so `_lifecycle_task`'s
# `except Exception: connect_future.set_exception(e); return` -- and
# everything chained off THAT, including `McpManager.close_all()`'s own
# `_lifecycle_future.result(timeout=...)` wait -- observed the connect
# attempt as "done" while the child process was still alive in the
# background. Verified on Windows: `test_startup_timeout_then_close_all_
# leaves_no_child_and_no_future_exception` failed ("no child process
# survives") because `close_all()` returned, and the test's own 5s poll
# loop expired, before the detached task ever got a chance to run to
# completion. `_do()` now AWAITS that cleanup directly instead (see
# `_connect_once`), so this bookkeeping set is no longer needed.

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
    clamped to [1000, 2^31-1] -- binary-facts sec.9, verbatim. finding 4:
    the clamp applies to WHICHEVER source wins, so a `MCP_TOOL_TIMEOUT`
    below 1000 is raised to the 1000ms floor rather than being treated as
    invalid and silently falling through to the ~27.8h default."""
    if isinstance(server_timeout_ms, (int, float)) and server_timeout_ms >= 1000:
        return min(int(server_timeout_ms), _INT32_MAX) / 1000.0
    raw = os.environ.get("MCP_TOOL_TIMEOUT")
    if raw:
        try:
            v = int(raw)
            return max(1000, min(v, _INT32_MAX)) / 1000.0
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


def mcp_approval_key(raw_entry: dict) -> str:
    """sha256 hex digest of a `.mcp.json` server entry's own JSON shape
    (D-CFG must-do: "approvals keyed by entry sha256", not by name) --
    approving `{"command": "safe.exe"}` under a name must NOT still read
    as approved once that name's entry is edited to `{"command": "evil.
    exe"}`; keying by content instead of name makes a tampered/edited
    entry require re-approval automatically. `raw_entry` is the UNEXPANDED
    dict exactly as read from `.mcp.json` (stable across `${VAR}`
    expansion, which can vary per environment)."""
    import hashlib
    canonical = json.dumps(raw_entry, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    # H13 Part A: tri-state -- `None` means "mcpLazy wasn't set on this
    # entry", resolved to a concrete bool by `_apply_lazy_defaults` (below)
    # before a caller ever sees it: an explicit per-server True/False always
    # wins; otherwise the GLOBAL `settings.json` "mcpLazy" default applies
    # (True -- lazy-by-default -- when that's absent too). `always_load`
    # servers are forced eager regardless (see `_apply_lazy_defaults`).
    lazy: Optional[bool] = None
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
        # H13 Part A: preserve tri-state here -- an entry with no "mcpLazy"
        # key at all must resolve against the GLOBAL default later
        # (`_apply_lazy_defaults`), not silently collapse to False the way
        # `bool(raw.get("mcpLazy", False))` used to.
        lazy=(bool(raw["mcpLazy"]) if raw.get("mcpLazy") is not None else None),
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
    `{server: entry, ...}` map (Claude Code accepts both).

    finding 10: an inline JSON value is tried FIRST, before ever touching
    the filesystem -- the old order (`Path(spec).exists()` first) raises
    `OSError: [Errno 36] File name too long` on Linux for a long inline
    JSON string with no `/` in it (verified in WSL: a 418-char inline
    value), crashing `build_manager` with no session at all; Windows just
    silently returns False for the same input, masking the same bug
    there. A value that visibly starts with `{` is JSON first, a path
    second; `Path.exists()` itself is also wrapped, so a WEIRDER OS-level
    path error on the non-JSON-looking branch can't crash this either."""
    text = spec
    source = spec
    stripped = spec.lstrip()
    if not stripped.startswith("{"):
        path = Path(spec)
        if not path.is_absolute():
            path = cwd / spec
        try:
            path_exists = path.exists()
        except OSError:
            path_exists = False
        if path_exists:
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
    settings: Optional[object] = None, plugin_servers: Optional[dict] = None,
) -> "tuple[dict[str, McpServerConfig], list[str]]":
    """(name -> McpServerConfig, notices). First-name-wins across tiers, in
    THIS order [D-CFG, finding 14]: `managed-mcp.json` present -> EXCLUSIVE
    -> else `--mcp-config` (repeatable) THEN `extra_dynamic` (this run's
    `--chrome`/`--playwright` servers -- SAME precedence as `--mcp-config`,
    both being "a flag the user passed this run" [finding 14], so a
    same-named lower-scope entry can never silently override what the flag
    asked for) -> (stop here if `--strict-mcp-config`, dropping
    `extra_dynamic` same as everything else, but now with a NOTICE) ->
    local `projects[cwd].mcpServers` (both key forms) -> `<cwd>/.mcp.json`
    (approval-gated; `disabledMcpjsonServers` always wins even over a name
    already claimed by a HIGHER tier, but (finding 11) only ever removes an
    entry that actually CAME FROM `.mcp.json` -- never a same-named user/
    local/flag/dynamic server) -> user `mcpServers` -> `plugin_servers`
    (installed-plugin-provided servers, lowest config-file precedence).
    `projects[cwd].disabledMcpServers` (no "json") removes a name after
    every tier regardless of its scope -- unlike `disabledMcpjsonServers`,
    this one really is a blanket removal by design. Managed
    `allowedMcpServers`/`deniedMcpServers` [finding 11] are applied last,
    on every return path. `enabledMcpjsonServers`/`disabledMcpjsonServers`/
    `enableAllProjectMcpServers` are read from BOTH `~/.claude.json`
    projects AND the resolved `settings` [finding 11]. `${VAR}`/
    `${VAR:-default}` expansion is applied to every surviving entry last."""
    notices: list = []
    resolved: "dict[str, McpServerConfig]" = {}
    settings_raw = getattr(settings, "raw", None)
    if not isinstance(settings_raw, dict):
        settings_raw = {}

    managed = _load_managed_mcp_json(managed_mcp_path)
    if managed is not None:
        for name, raw in managed.items():
            cfg = parse_server(name, raw, scope="managed", source_path=str(managed_mcp_path))
            if cfg is not None:
                resolved[name] = cfg
        _notice_dropped_dynamic(extra_dynamic, resolved, notices, "managed-mcp.json is exclusive")
        resolved, policy_notices = _apply_mcp_server_policy(resolved, settings_raw)
        notices.extend(policy_notices)
        resolved = _apply_lazy_defaults(resolved, settings_raw)
        return _expand_all(resolved, env_for_expansion, notices)

    def _add_missing(entries: dict, scope: str, source_path=None):
        """`entries`: name -> RAW dict (a `.mcp.json`/`~/.claude.json`-
        shaped entry, not yet parsed)."""
        for name, raw in entries.items():
            if name not in resolved:
                cfg = parse_server(name, raw, scope=scope, source_path=source_path)
                if cfg is not None:
                    resolved[name] = cfg

    def _add_missing_configs(entries: Optional[dict]):
        """`entries`: name -> ALREADY-BUILT `McpServerConfig` (extra_dynamic's
        `--chrome`/`--playwright` entries, `plugin_servers`'s discovered
        ones) -- never re-parsed (parse_server only understands raw
        dicts; handing it a McpServerConfig would just return None)."""
        for name, cfg in (entries or {}).items():
            resolved.setdefault(name, cfg)

    for spec in (mcp_config_flag or []):
        entries, err = _load_mcp_config_arg(spec, Path(cwd))
        if err:
            notices.append(err)
            continue
        _add_missing(entries, "flag", source_path=spec)

    if strict_mcp_config:
        _notice_dropped_dynamic(extra_dynamic, resolved, notices,
                                 "--strict-mcp-config restricts MCP servers to --mcp-config entries only")
        resolved, policy_notices = _apply_mcp_server_policy(resolved, settings_raw)
        notices.extend(policy_notices)
        resolved = _apply_lazy_defaults(resolved, settings_raw)
        return _expand_all(resolved, env_for_expansion, notices)

    _add_missing_configs(extra_dynamic)

    proj = lookup_project(claude_json, cwd)
    local_servers = proj.get("mcpServers")
    if isinstance(local_servers, dict):
        _add_missing(local_servers, "local")

    mcp_json_path = Path(cwd) / ".mcp.json"
    disabled_project = (set(proj.get("disabledMcpjsonServers") or [])
                         | set(settings_raw.get("disabledMcpjsonServers") or []))
    if mcp_json_path.exists():
        project_entries, perr = _load_dot_mcp_json(mcp_json_path)
        if perr:
            notices.append(perr)
        else:
            enabled = (set(proj.get("enabledMcpjsonServers") or [])
                       | set(settings_raw.get("enabledMcpjsonServers") or []))
            enable_all = (bool(proj.get("enableAllProjectMcpServers"))
                          or bool(settings_raw.get("enableAllProjectMcpServers")))
            our_approvals = approvals or {}
            for name, raw in project_entries.items():
                if name in resolved or name in disabled_project:
                    continue
                cfg = parse_server(name, raw, scope="project", source_path=str(mcp_json_path))
                if cfg is None:
                    continue
                approved = (name in enabled) or enable_all or print_mode \
                    or bool(our_approvals.get(mcp_approval_key(raw)))
                if not approved:
                    cfg.pending_approval = True
                resolved[name] = cfg

    user_servers = claude_json.get("mcpServers")
    if isinstance(user_servers, dict):
        _add_missing(user_servers, "user")

    _add_missing_configs(plugin_servers)

    for name in disabled_project:
        existing = resolved.get(name)
        if existing is not None and existing.scope == "project":
            resolved.pop(name, None)
    for name in set(proj.get("disabledMcpServers") or []):
        resolved.pop(name, None)

    resolved, policy_notices = _apply_mcp_server_policy(resolved, settings_raw)
    notices.extend(policy_notices)
    resolved = _apply_lazy_defaults(resolved, settings_raw)
    return _expand_all(resolved, env_for_expansion, notices)


def _notice_dropped_dynamic(extra_dynamic: Optional[dict], resolved: dict, notices: list, why: str) -> None:
    """finding 14: `--chrome`/`--playwright` disappearing under
    `--strict-mcp-config`/`managed-mcp.json` is CORRECT (both really are
    meant to be total overrides -- see the "No findings in" list) but must
    never be silent."""
    for name in (extra_dynamic or {}):
        if name not in resolved:
            notices.append(f"{name!r} was requested but {why} -- it was not started")


def _mcp_server_entry_matches(cfg: "McpServerConfig", entry: dict) -> bool:
    """One `allowedMcpServers`/`deniedMcpServers` OBJECT entry against one
    resolved server config -- u2-h3b finding 10: 2.1.281's real schema (read
    from the binary) is a list of OBJECTS, each with EXACTLY ONE of:
      - `serverName`: exact name match (the common case: `{"serverName":
        "github"}`).
      - `serverCommand`: the server's `command` PLUS `args`, as an EXACT
        array match against `[command, *args]` -- lets a policy pin down a
        specific invocation, not just any server sharing a name.
      - `serverUrl`: a WILDCARD match (`fnmatch`-style `*`/`?`) against an
        http/sse server's `url` -- names never apply to a remote server.
    A malformed entry (not an object, none/more-than-one of the three keys
    set, or an empty list/string) matches nothing -- the caller reports a
    notice and moves on rather than crashing or silently allow/denying
    everything."""
    import fnmatch
    if not isinstance(entry, dict):
        return False
    keys_present = [k for k in ("serverName", "serverCommand", "serverUrl") if entry.get(k) not in (None, "", [])]
    if len(keys_present) != 1:
        return False
    key = keys_present[0]
    if key == "serverName":
        return isinstance(entry["serverName"], str) and cfg.name == entry["serverName"]
    if key == "serverCommand":
        want = entry["serverCommand"]
        if not isinstance(want, list) or not want:
            return False
        got = ([cfg.command] if cfg.command else []) + list(cfg.args or [])
        return [str(x) for x in want] == [str(x) for x in got]
    # serverUrl: wildcard match against an http/sse server's url only.
    want_url = entry["serverUrl"]
    return isinstance(want_url, str) and bool(cfg.url) and fnmatch.fnmatchcase(cfg.url, want_url)


def _apply_mcp_server_policy(resolved: dict, settings_raw: dict) -> "tuple[dict, list[str]]":
    """`(resolved, notices)`. Managed `allowedMcpServers`/`deniedMcpServers`
    [finding 10]: each is a LIST OF OBJECTS (`{"serverName"|"serverCommand"|
    "serverUrl": ...}`), never bare strings -- matched per-entry via
    `_mcp_server_entry_matches`.
    A non-empty `allowedMcpServers` narrows to exactly the servers SOME
    entry matches; an EMPTY `allowedMcpServers` list (`[]`, present but
    with zero entries) means lockdown -- "users can use no servers of
    their own" -- distinct from the key being entirely ABSENT (`None`,
    meaning no restriction at all). A `deniedMcpServers` match is removed
    regardless of anything else (deny always wins). A malformed entry
    (not an object, or not exactly one of the three keys) is skipped with
    a notice rather than silently matching everything or crashing.
    Applied on every return path, including the managed-mcp.json-exclusive
    and `--strict-mcp-config` early returns."""
    allowed = settings_raw.get("allowedMcpServers")
    denied = settings_raw.get("deniedMcpServers")
    notices: list = []

    def _valid_entries(raw_list, field_name: str) -> list:
        out = []
        for entry in raw_list:
            if isinstance(entry, dict) and len([k for k in ("serverName", "serverCommand", "serverUrl")
                                                 if entry.get(k) not in (None, "", [])]) == 1:
                out.append(entry)
            else:
                notices.append(f"{field_name}: malformed entry {entry!r} (must have exactly one of "
                                f"serverName/serverCommand/serverUrl) -- skipped")
        return out

    if isinstance(allowed, list):
        # present (even []) -- [] itself is lockdown, matching NO server.
        allow_entries = _valid_entries(allowed, "allowedMcpServers")
        resolved = {name: cfg for name, cfg in resolved.items()
                    if any(_mcp_server_entry_matches(cfg, e) for e in allow_entries)}
    if isinstance(denied, list) and denied:
        deny_entries = _valid_entries(denied, "deniedMcpServers")
        resolved = {name: cfg for name, cfg in resolved.items()
                    if not any(_mcp_server_entry_matches(cfg, e) for e in deny_entries)}
    return resolved, notices


def _apply_lazy_defaults(resolved: dict, settings_raw: dict) -> dict:
    """H13 Part A ("make lazy the default"): resolves every surviving
    server's tri-state `McpServerConfig.lazy` to a concrete bool, IN PLACE
    of the tri-state -- every downstream reader (`doctor._check_mcp_servers`,
    `mcp_setup.build_manager`'s own `{name for name, cfg in configs.items()
    if cfg.lazy}`) keeps reading a plain bool and needs no changes.

    Precedence: an `alwaysLoad` server is always eager (preloading its
    tools needs a live connection at session start regardless of any
    `mcpLazy` value someone also set on it) -> else an explicit per-server
    `mcpLazy` (True or False) always wins -> else the GLOBAL default from
    `settings.json`'s own top-level `"mcpLazy"` key (same key name, top
    level instead of per-entry) -> else True (lazy is now the DEFAULT,
    reversing the pre-H13 "eager unless mcpLazy: true" behaviour)."""
    global_raw = settings_raw.get("mcpLazy")
    global_default = True if global_raw is None else bool(global_raw)
    out = {}
    for name, cfg in resolved.items():
        if cfg.always_load:
            effective = False
        elif cfg.lazy is not None:
            effective = cfg.lazy
        else:
            effective = global_default
        out[name] = cfg if cfg.lazy is effective else dataclass_replace_config(cfg, lazy=effective)
    return out


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


_DEAD_TRANSPORT_MARKERS = ("connection closed", "closedresourceerror", "brokenresourceerror")


def _looks_like_dead_transport(exc: BaseException) -> bool:
    """finding 6: "on `Connection closed`, `ClosedResourceError` or
    `BrokenResourceError`... mark the handle failed" -- matched by
    exception TYPE NAME (anyio raises its own `ClosedResourceError`/
    `BrokenResourceError` classes, not caught by `isinstance` without an
    anyio import this module otherwise avoids at module scope) as well as
    by message text (the SDK's own "Connection closed" `McpError`)."""
    if type(exc).__name__ in ("ClosedResourceError", "BrokenResourceError"):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _DEAD_TRANSPORT_MARKERS)


# ---- McpServerHandle ---------------------------------------------------------

# state vocabulary: pending | pending_approval | cached | connecting |
# connected | failed | disabled | needs_auth | closed [D-CFG]. H13 Part A
# adds "cached": a lazy server whose tools were seeded from
# `mcp.tools_cache` without ever connecting -- see `McpManager.mark_cached`.
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

    def __init__(self, config: McpServerConfig, loop, *, tool_env: dict, cwd, trusted: bool = True) -> None:
        self.config = config
        self._loop = loop
        self._tool_env = tool_env
        self._cwd = cwd
        self._trusted = trusted
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
        # finding 6: set by _mark_dead() when a call/the lifecycle task
        # itself detects the transport died -- McpManager.call() consumes
        # this to reconnect ONCE on the NEXT call, never on every call to a
        # server that simply never connected in the first place (that case
        # keeps failing fast, unchanged).
        self._reconnect_on_next_call = False
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
        # H4 fix: a `session.initialize()` failure's `stack.aclose()` (see
        # `_connect_once`) is scheduled via `self._loop.spawn(...)` rather
        # than awaited inline, so `_connect_once`/`_lifecycle_task` -- and
        # `McpManager.start_all()`, which is bounded by ~MCP_TIMEOUT --
        # still return promptly even though the SDK's own shielded stdio
        # shutdown (closing stdin, waiting out the grace period, then the
        # Job Object/process-group kill) can take a few seconds. This
        # handle-visible `concurrent.futures.Future` is what lets `close()`/
        # `_await_close()` below actually WAIT for that real kill to finish
        # (bounded by the SAME close deadline) instead of a detached task
        # nobody downstream ever waited for -- verified on Windows: without
        # this, the spawned child outlived `close_all()` and a 5s poll.
        self._connect_cleanup_future: "Optional[concurrent.futures.Future]" = None

    async def _resolved_headers(self) -> dict:
        """H4 must-do: run the (optional) `headersHelper` off the event
        loop -- its `subprocess.run` is blocking, and this coroutine runs
        ON the McpLoop daemon thread's loop (inside `_open_transport`,
        inside `_connect_once`), so calling it inline would stall EVERY
        other server's connect/call for up to the helper's own 10s
        timeout. Also binary-facts sec.9: "repo-resident config needs
        persisted trust" -- only a PROJECT-scope (`.mcp.json`, repo-
        resident) helper is trust-gated; user/managed/flag/dynamic/plugin
        scope is the operator's own global config either way."""
        headers = dict(self.config.headers or {})
        if not self.config.headers_helper:
            return headers
        if self.config.scope == "project" and not self._trusted:
            log.debug("mcp %s: headersHelper skipped -- untrusted project", self.config.name)
            return headers
        try:
            import asyncio
            from rolo_claude.mcp.http_sse import merged_headers, run_headers_helper
            helper_headers = await asyncio.to_thread(
                run_headers_helper, self.config.headers_helper, env=self._tool_env,
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
            headers = await self._resolved_headers()
            try:
                return await http_sse_mod.connect_http(
                    url=self.config.url, headers=headers, connect_timeout=connect_timeout)
            except Exception as http_exc:
                # OpenCode-H9 MCP-compatibility item: "Streamable HTTP ->
                # SSE fallback" -- a server declared `type: "http"`
                # (Streamable HTTP, the current spec) that only actually
                # speaks the OLDER `sse` transport rejects the Streamable
                # HTTP handshake outright (typically a 4xx/405 on the very
                # first POST); retry the SAME url over the deprecated `sse`
                # transport before giving up, same as every other MCP
                # client's own documented fallback. Costs nothing when the
                # server DOES speak Streamable HTTP -- this branch is only
                # ever reached after that already failed.
                log.debug("mcp %s: streamable-http connect failed (%s), retrying as sse",
                          self.config.name, http_exc)
                try:
                    return await http_sse_mod.connect_sse(
                        url=self.config.url, headers=headers, connect_timeout=connect_timeout)
                except Exception:
                    # ruff B904: `from None` is deliberate -- http_exc is the
                    # ORIGINAL failure (more informative, per the comment
                    # above) and already carries its own traceback; chaining
                    # this sse-fallback's own exception onto it would only
                    # add noise about a fallback attempt nobody needs to see.
                    raise http_exc from None
        if self.config.type == "sse":
            log.debug("mcp %s: 'sse' transport is deprecated (use 'http')", self.config.name)
            headers = await self._resolved_headers()
            return await http_sse_mod.connect_sse(
                url=self.config.url, headers=headers, connect_timeout=connect_timeout)
        raise ValueError(f"unsupported transport: {self.config.type!r}")

    async def _connect_once(self) -> None:
        """Open the transport, initialize, list_tools -- raises on any
        connect/initialize failure (list_tools failing on its own is
        caught and recorded as `tools_fetch_failed` instead, same as
        before). Called from inside `_lifecycle_task`, never directly.

        finding 15: the overall-timeout bound uses `client.task_timeout`
        (a plain `Task.cancel()` scheduled via `loop.call_later`, run in
        THIS coroutine's own task -- see its own docstring for the full
        reasoning) instead of `asyncio.wait_for(_do(), ...)` -- `wait_for`
        runs its awaitable by wrapping it in a NEW Task (`ensure_future`)
        when it isn't already one, so the anyio cancel scopes
        `_open_transport`'s `stdio_client`/TaskGroup open on Python 3.11
        and earlier end up entered in that short-lived wait_for-Task but
        exited later, from `_lifecycle_task`'s own (different) Task when
        `close()` finally runs `stack.aclose()` -- verified to raise
        "Attempted to exit cancel scope in a different task than it was
        entered in" (silently swallowed by `close()`'s bare `except`, so
        nothing leaked, but the RuntimeWarning is real). `anyio.fail_after`
        does NOT fix this despite being the obvious anyio-native swap: it
        nests its OWN cancel scope around `stdio_client`'s, which is
        deliberately left OPEN past this method's return (the caller owns
        it from here) -- verified (in a minimal repro, fully within ONE
        task) that anyio then refuses to exit `fail_after`'s scope while
        the more-nested one is still open ("not the current task's current
        cancel scope"). `task_timeout` creates neither a new task nor a
        new scope, so it has neither problem; open and close now agree on
        3.10 through 3.13, not just 3.12+. `_do()` still has its OWN try/except
        around `initialize()` for the same reason as before: a `stack`
        `_open_transport` already opened (a real, live subprocess + its
        pipes) must never be silently dropped with nothing left to close
        it -- an OS-level leak that (verified on Windows) CRASHES rather
        than merely leaks once the owning loop is later closed or GC'd
        during interpreter shutdown [tests/test_mcp_manager.py::
        test_manager_start_all_is_bounded_by_mcp_timeout reproduces this].
        The close is scheduled via `self._loop.spawn(...)` (see `_do()`
        below), STILL detached from this coroutine's own return -- the
        installed mcp SDK's own `stdio_client` shutdown is already
        shielded + bounded (close stdin, a grace period, then a hard kill
        -- mcp/client/stdio.py), but awaiting it inline HERE would turn
        THIS failed connect attempt's latency into that same multi-second
        wait, breaking `start_all()`'s own ~MCP_TIMEOUT bound. H4 fix: the
        detached cleanup's `concurrent.futures.Future` is now stored on
        `self._connect_cleanup_future` (`McpLoop.spawn`, not a bare
        `asyncio.create_task` -- a real thread-safe Future any caller can
        `.result(timeout=)` on, unlike an `asyncio.Task`), so `close()`/
        `_await_close()` below can actually WAIT for the real kill to
        finish before this handle is considered closed -- the ORIGINAL bug
        (verified on Windows): nothing downstream ever waited for the old
        fire-and-forget task, so `close_all()` returned, and a caller's own
        post-close poll expired, while the spawned child was still alive."""
        from rolo_claude.mcp.client import task_timeout
        connect_timeout = mcp_connect_timeout_s()
        overall_timeout = mcp_timeout_s()
        # must-do: a genuine RECONNECT reuses this same handle object --
        # never carry a stale error/tools_fetch_failed/cleanup-future from
        # the PREVIOUS attempt into a freshly-successful one.
        self.error = None
        self.tools_fetch_failed = False
        self._connect_cleanup_future = None

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
                # H4 fix: `self._loop.spawn(...)` (a thread-safe
                # `concurrent.futures.Future`), not a bare
                # `asyncio.create_task` (a plain `asyncio.Task` has no
                # thread-safe `.result(timeout=)` a foreign closing thread
                # could ever wait on) -- see this method's own docstring.
                self._connect_cleanup_future = self._loop.spawn(_cleanup())
                raise
            return stack, session, init_result

        async with task_timeout(overall_timeout):
            stack, session, init_result = await _do()
        self._stack, self._session = stack, session
        self.instructions = getattr(init_result, "instructions", None)
        try:
            async with task_timeout(overall_timeout):
                tools_result = await session.list_tools()
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
        serves both call sites.

        H3c-regression fix (verified on Windows): the final cleanup below
        used to re-read `self._stack` instead of capturing it locally right
        here. `McpManager.close_all()`'s own per-handle `_await_close()`
        wait has a budget of its own (bounded by `close_all(timeout=...)`,
        default 5s total across every handle) that can genuinely be
        SHORTER than a still-connecting server needs -- when it gives up
        waiting on THIS task's `_lifecycle_future`, it unconditionally
        nulls `self._stack` right then (so a caller sees "closed" instead
        of a stale connected/session object), regardless of whether this
        task has actually finished. If this task were still reading
        `self._stack` at that point, its own `finally` below would then
        find it already `None` and silently skip closing the real, still-
        open stack it alone owns -- a live child process + an unclosed
        `BaseSubprocessTransport` left for the interpreter to crash on at
        shutdown (`test_controller_reconnect_mcp_threads_abort_through_to_
        a_slow_reconnect`: the abort only cuts the CALLER's wait short,
        never the underlying connect, so `_await_close()` racing ahead of
        a slow connect is the norm here, not an edge case). Every task
        must own and close exactly the stack IT opened, independent of
        whatever any other thread does to this handle's shared fields
        concurrently -- same principle `_do()`'s own detached `_cleanup()`
        closure (in `_connect_once` above) already followed by closing
        over ITS local `stack`, never `self._stack`."""
        self._close_event = asyncio.Event()
        try:
            await self._connect_once()
        except Exception as e:
            if not connect_future.done():
                connect_future.set_exception(e)
            return
        stack = self._stack
        if not connect_future.done():
            connect_future.set_result(None)
        try:
            await self._close_event.wait()
        finally:
            if stack is not None:
                await stack.aclose()

    def start(self, abort=None) -> None:
        """`abort` (u2-h3b finding 9): when given, the WAIT for this
        connect attempt (never the connect attempt itself -- same
        "abandoned, not stopped" caveat as everywhere else abort-aware
        waiting is used in this codebase) is cut short as soon as it
        fires, via `wait_future_abortable` -- lets a `/mcp` reconnect
        started from the TUI honour Esc instead of freezing the whole app
        for up to MCP_TIMEOUT+4s."""
        if self.state in ("disabled", "pending_approval"):
            return
        from rolo_claude.mcp import http_sse as http_sse_mod
        self.state = "connecting"
        connect_future: "concurrent.futures.Future" = concurrent.futures.Future()
        self._lifecycle_future = self._loop.spawn(self._lifecycle_task(connect_future))
        self._lifecycle_future.add_done_callback(self._on_lifecycle_done)
        try:
            self._loop.wait_future_abortable(connect_future, timeout=mcp_timeout_s() + 4, abort=abort)
        except Exception as e:
            self.state = "needs_auth" if http_sse_mod.looks_like_auth_required(e) else "failed"
            self.error = f"{type(e).__name__}: {e}"
            return
        self.state = "connected"

    def _mark_dead(self, exc: BaseException) -> None:
        """finding 6: on a detected dead transport, mark `failed` and drop
        `_session` so every SUBSEQUENT call fails fast (instead of hanging
        its own full timeout against a session that will never answer
        again) and `mcp list`/`/mcp`/the prompt's server list stop
        claiming this server is connected. `_stack` is deliberately left
        alone -- the owning `_lifecycle_task` still needs it for its own
        `finally: await self._stack.aclose()` once `close()`/process exit
        eventually runs; clearing it here too would leak the subprocess."""
        self.state = "failed"
        self.error = f"{type(exc).__name__}: {exc} (connection lost)"
        self._session = None
        self._reconnect_on_next_call = True

    def _on_lifecycle_done(self, fut: "concurrent.futures.Future") -> None:
        """finding 6's other half: notice when the lifecycle task itself
        ends WITHOUT an ordinary `close()` ever being requested (the
        server process died, or `_stack.aclose()`/something else inside it
        raised) -- otherwise `state` stays "connected" forever with
        nothing left actually driving the connection. `_close_event.
        is_set()` is the ordinary-close signal: by the time this done-
        callback can fire, an ordinary `close()` has ALREADY called
        `self._loop.call_soon(self._close_event.set)` (that's what let the
        lifecycle task's own `await self._close_event.wait()` return in
        the first place), so checking it here -- rather than `self.state`,
        which `close()` only updates AFTER waiting on this same future,
        racing this very callback -- reliably tells "expected" apart from
        "unexpected" regardless of which thread runs first."""
        if self._close_event is not None and self._close_event.is_set():
            return
        if self.state != "connected":
            return
        exc: Optional[BaseException] = None
        try:
            exc = fut.exception()
        except BaseException:
            pass
        self._mark_dead(exc if exc is not None else RuntimeError("mcp lifecycle task ended unexpectedly"))

    def call_tool(self, name: str, arguments: dict, *, timeout: Optional[float] = None, abort=None):
        if self._session is None:
            raise RuntimeError(f"mcp server {self.config.name!r} is not connected (state={self.state})")
        from rolo_claude.mcp.client import McpAborted, ProgressKeepalive
        # OpenCode-H9 MCP-compatibility item: "progress notifications reset
        # the call timeout" -- `keepalive` is handed to the SDK as the
        # request's own `progress_callback` AND to `run_abortable` below,
        # so a `notifications/progress` message during a long-but-healthy
        # call pushes run_abortable's OWN `overall` deadline back out, not
        # just whatever the SDK's internal `read_timeout_seconds` does on
        # its own.
        keepalive = ProgressKeepalive()
        # H9 bug fix (MCP compatibility matrix, "progress keepalive" live
        # check): this used to ALSO hand the SDK `read_timeout_seconds=
        # timeout` -- a single `anyio.fail_after` armed once inside the
        # SDK's own `send_raw_request` that NO progress notification ever
        # resets -- while the outer, keepalive-extendable bound below was
        # `timeout + 3`. The inner bound was therefore always 3s shorter
        # and always fired first, so a progressing call was killed at
        # exactly `timeout` every single time no matter how many progress
        # pings had arrived (deterministic repro: timeout_ms=1000, a tool
        # sending progress every 0.5s over 1.5s -> dead at 1.0s). The
        # keepalive could never actually rescue anything. Leave the SDK's
        # own bound OFF (None) and let `run_abortable`'s bound -- the one
        # `keepalive` really extends, and the one `abort` really cuts --
        # be the only wall-clock guard: a genuinely hung server still
        # times out at the SAME `overall` moment as before.
        coro = self._session.call_tool(name, arguments or {}, read_timeout_seconds=None,
                                        progress_callback=keepalive)
        overall = (timeout + 3) if timeout else mcp_timeout_s() + 3
        try:
            return self._loop.run_abortable(coro, timeout=overall, abort=abort, keepalive=keepalive)
        except McpAborted:
            raise
        except concurrent.futures.TimeoutError:
            raise
        except Exception as e:
            if _looks_like_dead_transport(e):
                self._mark_dead(e)
            raise

    def list_resources(self) -> list:
        if self._session is None:
            return []
        try:
            result = self._loop.run(self._session.list_resources(), timeout=mcp_timeout_s())
        except Exception as e:
            if _looks_like_dead_transport(e):
                self._mark_dead(e)
            return []
        return list(getattr(result, "resources", None) or [])

    def read_resource(self, uri: str):
        if self._session is None:
            raise RuntimeError(f"mcp server {self.config.name!r} is not connected (state={self.state})")
        try:
            return self._loop.run(self._session.read_resource(uri), timeout=mcp_timeout_s())
        except Exception as e:
            if _looks_like_dead_transport(e):
                self._mark_dead(e)
            raise

    def list_prompts(self) -> list:
        if self._session is None:
            return []
        try:
            result = self._loop.run(self._session.list_prompts(), timeout=mcp_timeout_s())
        except Exception as e:
            if _looks_like_dead_transport(e):
                self._mark_dead(e)
            return []
        return list(getattr(result, "prompts", None) or [])

    def get_prompt(self, name: str, arguments: Optional[dict] = None):
        if self._session is None:
            raise RuntimeError(f"mcp server {self.config.name!r} is not connected (state={self.state})")
        try:
            return self._loop.run(self._session.get_prompt(name, arguments or {}), timeout=mcp_timeout_s())
        except Exception as e:
            if _looks_like_dead_transport(e):
                self._mark_dead(e)
            raise

    def _signal_close(self) -> None:
        """Non-blocking half of `close()`: just requests the shutdown.
        U2 must-do: `McpManager.close_all()` calls this on EVERY handle
        FIRST, so all of them start unwinding concurrently, before waiting
        on any -- one slow server must not eat the whole quit budget and
        leave the rest 0.1s each."""
        if self._close_event is not None:
            try:
                self._loop.call_soon(self._close_event.set)
            except Exception:
                pass

    def _await_close(self, timeout: float = 5.0, abort=None) -> None:
        """Blocking half of `close()`: wait (up to `timeout`) for the SAME
        task that opened the connection to run its own `aclose()` -- never
        a separate `run()` call (that would recreate the cross-task
        cancel-scope bug the `_lifecycle_task` design fixes). H4 fix: ALSO
        waits (out of the SAME overall `timeout` budget) for a still-
        detached `_connect_cleanup_future` (a failed connect's own
        `stack.aclose()`, scheduled by `_connect_once`/`_do()`) -- without
        this, `close()`/`McpManager.close_all()` could return while that
        real kill was still in flight, which is exactly what let a spawned
        child outlive `close_all()` on Windows (verified). `abort` (u2-h3b
        finding 9, default None): waits via `wait_future_abortable` instead
        of a plain `.result(timeout=)` -- with `abort=None` (every
        `close_all()` call site, unchanged) this behaves identically, just
        polled in short slices; a real Event (the TUI's `/mcp` reconnect)
        lets Esc cut the wait short without changing `close_all()`'s own
        timing at all."""
        deadline = time.monotonic() + timeout
        if self._lifecycle_future is not None:
            try:
                self._loop.wait_future_abortable(self._lifecycle_future,
                                                  timeout=max(0.0, deadline - time.monotonic()), abort=abort)
            except Exception:
                pass
        if self._connect_cleanup_future is not None:
            try:
                self._loop.wait_future_abortable(self._connect_cleanup_future,
                                                  timeout=max(0.0, deadline - time.monotonic()), abort=abort)
            except Exception:
                pass
        if self._errlog is not None:
            self._errlog.close()
        self.state = "closed"
        self._stack = None
        self._session = None

    def close(self, timeout: float = 5.0, abort=None) -> None:
        self._signal_close()
        self._await_close(timeout=timeout, abort=abort)


# ---- McpManager ---------------------------------------------------------------

class McpManager:
    """Owns one `McpServerHandle` per configured server name and the ONE
    `McpLoop` they all share. `start_all()` is the only method that blocks
    for real wall-clock time (bounded by MCP_TIMEOUT); every other method
    is a thin, fast, synchronous wrapper a tool call or CLI command can use
    directly."""

    def __init__(self, configs: "dict[str, McpServerConfig]", *, loop=None,
                 tool_env: Optional[dict] = None, cwd=None, lazy_names: Optional[set] = None,
                 trusted: bool = True) -> None:
        from rolo_claude.mcp.client import McpLoop
        self.loop = loop or McpLoop()
        self.configs = configs
        self._lazy_names = set(lazy_names or ())
        self._closed = False
        # H13 Part A: names whose real, post-connect `tools/list` turned out
        # to differ from what a stale-but-hash-matching cache had promised
        # (`ensure_started`/`reconnect` populate this) -- drained by
        # `SessionCatalog.refresh_if_stale` so the model's own catalog gets
        # refreshed and told about it exactly once per drift.
        self._stale_after_connect: set = set()
        # H9 bug fix (item 11, MCP compatibility matrix): kept around ONLY
        # for `resync_from()` below, which needs to build a brand-new
        # `McpServerHandle` the same way the constructor does for a name
        # that shows up later (a server added to the config file mid-
        # session) -- the constructor itself never stored these before.
        self._tool_env = tool_env or {}
        self._cwd = cwd
        self._trusted = trusted
        self.handles: "dict[str, McpServerHandle]" = {
            name: McpServerHandle(cfg, self.loop, tool_env=tool_env or {}, cwd=cwd, trusted=trusted)
            for name, cfg in configs.items()
        }

    def resync_from(self, new_configs: "dict[str, McpServerConfig]") -> "list[str]":
        """H9 bug fix (item 11, MCP compatibility matrix): `reconnect()`
        alone only ever restarts an ALREADY-KNOWN handle using its
        ORIGINALLY-parsed `McpServerConfig` -- a real on-disk edit to an
        existing server's command/args/env/url, or a brand-new server name
        added to `~/.claude.json`/`.mcp.json` after this session started,
        was previously invisible no matter how many times `/mcp` reconnect
        ran, since neither `Controller.reconnect_mcp` nor this manager ever
        re-resolved the config FILES at all. The caller re-runs
        `resolve_server_configs(...)` against a freshly re-read
        `~/.claude.json` (and re-passes it here) -- this method then
        reconciles: a name with no existing handle gets a brand new one
        (added server); a name whose config genuinely differs from what's
        live now has its OLD handle closed and REPLACED with a fresh one
        built from the new config (changed server); a name already present
        with an UNCHANGED config is left alone (no needless reconnect,
        and no interruption of an in-flight call). A name that no longer
        appears in `new_configs` at all is left running untouched -- this
        never tears a working connection down just because it dropped out
        of the file; an explicit `reconnect()`/`close_all()` still can.
        Returns the names actually added or updated; `start()` on the
        result is the caller's job (eager vs. lazy is a caller decision,
        same contract the constructor already has)."""
        from dataclasses import asdict
        touched = []
        for name, cfg in new_configs.items():
            existing_cfg = self.configs.get(name)
            if existing_cfg is not None and asdict(existing_cfg) == asdict(cfg):
                continue  # unchanged -- nothing to do, don't disturb a live connection
            old_handle = self.handles.get(name)
            if old_handle is not None:
                old_handle.close(timeout=mcp_timeout_s() + 5.0)
            self.configs[name] = cfg
            self.handles[name] = McpServerHandle(cfg, self.loop, tool_env=self._tool_env,
                                                  cwd=self._cwd, trusted=self._trusted)
            touched.append(name)
        return touched

    # ---- H13 Part A: tool cache (lazy-by-default) --------------------------

    def mark_cached(self, name: str, tools: list, instructions: Optional[str]) -> None:
        """Seed a still-`pending` handle's tools from `mcp.tools_cache`
        WITHOUT connecting anything -- `tools` is a list of cache-shaped
        objects (`tools_cache.CachedTool`, or any object with `.name`/
        `.description`/`.input_schema`/`.meta`). A no-op on a handle that
        isn't `pending` (already cached/connecting/connected/failed/etc --
        never clobber a real, live state with stale cache data)."""
        h = self.handles.get(name)
        if h is None or h.state != "pending":
            return
        h.tools = list(tools or [])
        h.instructions = instructions
        h.state = "cached"

    def was_cache_stale(self, name: str) -> bool:
        return name in self._stale_after_connect

    def clear_cache_stale(self, name: str) -> None:
        self._stale_after_connect.discard(name)

    def _refresh_tools_cache(self, name: str, h: "McpServerHandle",
                              cached_tools_before: Optional[list] = None) -> None:
        """Writes a fresh `mcp.tools_cache` entry for `h` (only meaningful
        for a `_lazy_names` member -- an eager server's tools are never read
        back from the cache, so caching them would be pure unused disk IO)
        and, when `cached_tools_before` is given (a handle that just moved
        from `cached` to `connected` for real), records whether the live
        `tools/list` actually matches what the cache had promised -- H13
        Part A: "stale cache + changed tools on connect -> refresh the
        catalog entries and emit a notification" (the notification itself
        is `SessionCatalog.refresh_if_stale`'s job, driven by
        `was_cache_stale` above). Best-effort: a cache-module import/write
        failure never breaks the connection that already succeeded."""
        if name not in self._lazy_names:
            return
        try:
            from rolo_claude.mcp import tools_cache
            if cached_tools_before is not None:
                before = tools_cache.tool_signature_set(cached_tools_before)
                after = tools_cache.tool_signature_set(h.tools)
                if before != after:
                    self._stale_after_connect.add(name)
            key = tools_cache.config_cache_key(h.config)
            tools_cache.write_cache(name, key=key, tools=h.tools, instructions=h.instructions)
        except Exception:
            pass

    def start_all(self) -> None:
        """Start every eligible server (not lazy, not disabled, not
        pending_approval) IN PARALLEL, the whole phase bounded by
        MCP_TIMEOUT [D-CFG]; a per-server failure is recorded on its own
        handle and never aborts the others, and this method itself never
        raises -- `status()` afterward is how a caller learns what
        happened. Safe to call with zero eligible servers (no-op)."""
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
        self._start_targets_parallel(targets)

    def _start_targets_parallel(self, targets: "list[McpServerHandle]", *, abort=None) -> None:
        """Shared by `start_all()` and `ensure_lazy_started_all()` (H9
        must-do: the lazy-start path used to be a plain serial `for name in
        self._lazy_names: h.start()` loop -- up to `len(lazy_names) *
        MCP_TIMEOUT` of wall-clock time, worst case, and no `abort` at all,
        so Esc/Ctrl+C could do nothing while ToolSearch's first call waited
        on it. This starts every target CONCURRENTLY (same
        one-lifecycle-task-per-handle mechanism `McpServerHandle.start()`
        uses) and, when `abort` is given, waits on the combined future the
        SAME abortable way `McpServerHandle.start()` already waits on a
        single one -- an Esc during a lazy-discovery burst now returns
        promptly instead of blocking for up to MCP_TIMEOUT per server."""
        from rolo_claude.mcp import http_sse as http_sse_mod

        async def _start_one(h: McpServerHandle) -> None:
            # Same lifecycle-task mechanism `McpServerHandle.start()` uses
            # (never the old direct `await h._connect_once()`, which would
            # reintroduce the cross-task cancel-scope bug for THIS, the
            # parallel-startup path -- `close_all()` later signals this
            # SAME spawned task via `h._close_event`/`h._lifecycle_future`).
            h.state = "connecting"
            connect_future: "concurrent.futures.Future" = concurrent.futures.Future()
            h._lifecycle_future = self.loop.spawn(h._lifecycle_task(connect_future))
            h._lifecycle_future.add_done_callback(h._on_lifecycle_done)
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
            if abort is None:
                self.loop.run(_start_all_coro(), timeout=overall + 3)
            else:
                # abortable variant: spawn (don't block-run) the gather
                # coroutine, then WAIT on it the same interruptible way
                # McpServerHandle.start() waits on a single connect --
                # abandoned-not-stopped on abort/timeout, same as
                # everywhere else in this module (see wait_future_abortable's
                # own docstring).
                fut = self.loop.spawn(_start_all_coro())
                self.loop.wait_future_abortable(fut, timeout=overall + 3, abort=abort)
        except Exception as e:
            # the OUTER bound itself lapsed/was aborted (belt-and-suspenders
            # past each target's own inner wait_for) -- anything still
            # mid-connect never got to record its own terminal state; do it
            # here so a caller never observes a permanently-stuck
            # "connecting" (an abort leaves it running in the background,
            # same "abandoned, not stopped" caveat as everywhere else).
            for h in targets:
                if h.state == "connecting":
                    h.state = "failed"
                    h.error = f"startup exceeded MCP_TIMEOUT ({overall:.1f}s): {type(e).__name__}: {e}"

    def ensure_started(self, name: str, abort=None) -> None:
        """`mcpLazy` servers (and a not-yet-approved `.mcp.json` server
        that just became approved) connect on FIRST USE instead of at
        `start_all()` time. H13 Part A: a `cached` handle (tools known from
        `mcp.tools_cache`, never actually connected) is started here the
        exact same way a `pending` one is -- this is the ONE place a real
        connection actually happens for a tool call against either state
        (`McpManager.call` always runs this first). On a successful
        cached->connected transition, the fresh `tools/list` is compared
        against what the cache had promised and a new cache entry is
        written either way (see `_refresh_tools_cache`)."""
        h = self.handles.get(name)
        if h is None or h.state not in ("pending", "cached"):
            return
        was_cached = h.state == "cached"
        cached_tools_before = list(h.tools) if was_cached else None
        h.start(abort=abort)
        if was_cached and h.state == "connected":
            self._refresh_tools_cache(name, h, cached_tools_before)

    def ensure_lazy_started_all(self, abort=None) -> "list[str]":
        """Linux/H4 must-do: start EVERY still-`pending` `mcpLazy` server
        now -- a lazy server otherwise never appears in `all_tools()`
        (that only ever looks at `connected` handles), so its tools can
        never enter `SessionCatalog.deferred` (built once, from
        `all_tools()`, at session start) and are permanently
        undiscoverable through ToolSearch even by keyword, no matter how
        long the session runs. `ToolSearchTool`'s own first call in a
        session (agent/catalog.py's `SessionCatalog.ensure_lazy_discovered`)
        calls this once so a lazy server's tools actually become
        searchable/loadable at all -- still never PRELOADED, only
        discoverable. Returns the names actually (attempted to be)
        started, so the caller knows whose tools to (re)fetch via
        `all_tools()`; a no-op (returns []) once every lazy server has
        already been started, or there are none.

        H9 must-do: this used to start each target with a plain SERIAL
        `h.start()` loop -- worst case `len(lazy_names) * MCP_TIMEOUT` of
        wall-clock time on ToolSearch's very first call, with no `abort`
        parameter at all, so Esc could do nothing while it ran. Now shares
        `start_all()`'s concurrent-gather machinery via
        `_start_targets_parallel()`, bounded by ONE MCP_TIMEOUT window for
        every lazy server together, and honours `abort` (pass
        `ctx.abort` from a tool call) so an interrupt returns promptly
        instead of blocking."""
        targets_by_name = [(name, self.handles[name]) for name in self._lazy_names
                            if self.handles.get(name) is not None and self.handles[name].state == "pending"]
        if not targets_by_name:
            return []
        self._start_targets_parallel([h for _, h in targets_by_name], abort=abort)
        return [name for name, _ in targets_by_name]

    def start_many(self, names, abort=None) -> None:
        """H13 Part A: start every still-`pending` name in `names` IN
        PARALLEL, one shared MCP_TIMEOUT window for the whole batch --
        `mcp_setup.bootstrap_lazy_from_cache`'s own public entry point for
        this, so a box with N lazy servers that have no cache yet (a first
        run, or a box whose config just changed) bootstraps in roughly ONE
        slow server's connect time, not N of them back to back. A name not
        in `self.handles`, or not currently `pending`, is silently skipped
        (already connected/cached/disabled -- nothing to do)."""
        targets = [self.handles[n] for n in names
                   if self.handles.get(n) is not None and self.handles[n].state == "pending"]
        if not targets:
            return
        self._start_targets_parallel(targets, abort=abort)

    def all_tools(self) -> "list[tuple[str, str, object]]":
        """`[(server_name, wire_tool_name, sdk_tool), ...]` across every
        CONNECTED or CACHED server, name-sorted by WIRE name -- the
        candidate pool `agent/catalog.py` selects preload/deferred from, and
        what ToolSearch searches over for a not-yet-loaded name. H13 Part A:
        a `cached` handle's tools (seeded from `mcp.tools_cache`, never
        actually connected) are included here on purpose -- that's the
        whole point of the cache: the frozen catalog/ToolSearch can find and
        preload/defer a lazy server's tools before it is ever connected. A
        `McpTool` built from one of these calls `McpManager.call()` on
        invocation exactly like any other, which connects it for real on
        first use via `ensure_started` -- nothing downstream needs to know
        which state a tool's own server was in when this list was built."""
        out = []
        for server_name, h in self.handles.items():
            if h.state not in ("connected", "cached"):
                continue
            for t in h.tools:
                out.append((server_name, mcp_tool_name(server_name, t.name), t))
        out.sort(key=lambda triple: triple[1])
        return out

    def call(self, server: str, tool: str, arguments: dict, *, timeout: Optional[float] = None, abort=None):
        h = self.handles.get(server)
        if h is None:
            raise RuntimeError(f"unknown mcp server: {server!r}")
        self.ensure_started(server, abort=abort)
        if h._reconnect_on_next_call:
            # finding 6: a PREVIOUSLY-working connection died mid-session
            # (_mark_dead set this) -- reconnect once, here, on its next
            # use. A server that never connected in the first place never
            # sets this flag, so it keeps failing fast as before.
            # u2-h3b finding 9: this reconnect used to be a PLAIN blocking
            # call, ignoring `abort` entirely -- a hung/slow server's
            # reconnect-on-next-call could hold the whole turn (Esc did
            # nothing) for up to `mcp_timeout_s()*2 + 9`s. Threading
            # `abort` through makes it honour Esc same as the tool call
            # that follows it.
            h._reconnect_on_next_call = False
            self.reconnect(server, abort=abort)
        effective_timeout = timeout if timeout is not None else tool_timeout_s(h.config.timeout_ms)
        return h.call_tool(tool, arguments, timeout=effective_timeout, abort=abort)

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
                # H13 Part A: a "cached" server's tool count is known (from
                # mcp.tools_cache) even though it was never actually
                # connected -- reported the same as a real "connected" one.
                "tool_count": len(h.tools) if h.state in ("connected", "cached") else 0,
                "instructions": h.instructions, "scope": h.config.scope,
            })
        return out

    def reconnect(self, name: str, abort=None) -> bool:
        """U2 must-do: unlike a plain `h.close(); h.start()`, this (a)
        waits LONG ENOUGH for the OLD lifecycle task to genuinely finish
        (not just `close()`'s default 5s budget) before spawning a new
        one -- a still-running old task that finishes late would otherwise
        set `_stack`/`_session` itself, AFTER the new one already did,
        silently clobbering the fresh connection; and (b) resets `error`/
        `tools_fetch_failed` so a successful reconnect doesn't keep
        showing the PREVIOUS attempt's stale failure. `abort` (u2-h3b
        finding 9): threaded through both the close-wait and the
        start-wait, so a caller on a thread that honours it (the `call()`
        reconnect-on-next-call path above, and the TUI's `/mcp` `r`
        worker thread) can cut either wait short instead of blocking for
        up to `mcp_timeout_s()*2 + 9` seconds -- the underlying close/
        connect keeps running regardless (same "abandoned, not stopped"
        caveat as every other abort-aware wait here); a caller that
        aborts mid-reconnect simply sees whatever transient state the
        handle is in when it gives up waiting."""
        h = self.handles.get(name)
        if h is None:
            return False
        # H13 Part A: a manual `/mcp` reconnect of a `cached` (never
        # actually connected) handle is how "or when /mcp asks for it"
        # connects a lazy server on demand -- captured BEFORE close()/
        # start() below so a real tools/list that turns out to differ from
        # what the cache promised is still detected (see
        # `_refresh_tools_cache`).
        cached_tools_before = list(h.tools) if h.state == "cached" else None
        h.close(timeout=mcp_timeout_s() + 5.0, abort=abort)
        h.error = None
        h.tools_fetch_failed = False
        h._reconnect_on_next_call = False
        h.state = "pending_approval" if h.config.pending_approval else "pending"
        h.start(abort=abort)
        ok = h.state == "connected"
        if ok:
            self._refresh_tools_cache(name, h, cached_tools_before)
        return ok

    def close_all(self, timeout: float = 5.0) -> None:
        """Close every handle, then hard-stop the shared loop -- the WHOLE
        operation is bounded by `timeout` (D-CFG: "close_all() with a 5s
        deadline"). U2 must-do: every handle's close is SIGNALLED first
        (cheap, non-blocking), and only THEN does this wait on any of
        them -- the old one-handle-at-a-time close() loop let one slow
        server eat the whole budget before the rest even got the chance to
        start closing, leaving them ~0.1s each. Idempotent: a second call
        -- including one made by the mcp.client process-exit safety net
        for a manager the caller already closed itself -- is a no-op
        rather than re-touching handles or spinning the shared loop back
        up."""
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + timeout
        for h in self.handles.values():
            h._signal_close()
        for h in self.handles.values():
            remaining = max(0.1, deadline - time.monotonic())
            h._await_close(timeout=remaining)
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
