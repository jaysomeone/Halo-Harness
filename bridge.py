# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

"""
claude-bridge: A local bridge that routes Claude Code requests to Databricks
or OpenRouter models while preserving Claude Code's settings, MCPs, and tools.

This tool intercepts Claude Code's API calls (via ANTHROPIC_BASE_URL override)
and translates between Anthropic's Messages API and OpenAI-compatible or
Databricks-specific endpoints. It maintains Claude Code's full environment
(memories, skills, permissions) while allowing non-Claude models to drive
the interface.

See SIGNATURES.md for the complete cross-section contract.
"""

from __future__ import annotations

import os
import sys
import io
import re
import json
import time
import uuid
import select
import socket
import ssl
import ipaddress
import hashlib
import secrets
import shutil
import signal
import argparse
import threading
import queue
import subprocess
import logging
import logging.handlers
import http.client
import http.server
import urllib.request
import urllib.parse
import socketserver
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

__version__ = "0.2.1"
DEFAULT_PORT = 8787
PROG = "claude-bridge"

log = logging.getLogger("bridge")


class InvalidModelError(Exception):
    pass


class WebSearchUnavailable(Exception):
    pass


class UpstreamConnectError(Exception):
    pass


class ClaudeNotFoundError(Exception):
    pass


class BridgeStartError(Exception):
    pass


def home() -> Path:
    """Return BRIDGE_TEST_HOME if set, else user home directory."""
    if "BRIDGE_TEST_HOME" in os.environ:
        return Path(os.environ["BRIDGE_TEST_HOME"])
    return Path.home()


def default_state_dir() -> Path:
    """Return BRIDGE_STATE_DIR if set, else ~/.claude-bridge."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return Path(os.environ["BRIDGE_STATE_DIR"])
    return home() / ".claude-bridge"


def load_env_file(path: Path) -> dict[str, str]:
    """
    Load KEY=value lines from file, setdefault into os.environ, return loaded dict.
    """
    loaded = {}
    if not path.exists():
        return loaded
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip optional 'export ' prefix
                if line.startswith("export "):
                    line = line[7:].strip()
                # Parse KEY=value
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    # Remove surrounding quotes
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    if key:
                        loaded[key] = value
                        os.environ.setdefault(key, value)
    except (OSError, UnicodeDecodeError):
        pass
    return loaded


def load_settings_env_chain(cwd: Path) -> dict:
    """
    Merge env blocks from settings files in precedence order.
    Returns merged dict (later wins).
    """
    merged = {}
    # Managed settings (skip on non-Windows)
    if os.name == "nt":
        # Best-effort path for managed settings
        managed_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Claude" / "managed-settings.json"
        if managed_path.exists():
            try:
                with open(managed_path, encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data.get("env"), dict):
                        merged.update(data["env"])
            except (json.JSONDecodeError, OSError):
                pass

    # ~/.claude/settings.json
    user_settings = home() / ".claude" / "settings.json"
    if user_settings.exists():
        try:
            with open(user_settings, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data.get("env"), dict):
                    merged.update(data["env"])
        except (json.JSONDecodeError, OSError):
            pass

    # <cwd>/.claude/settings.json
    project_settings = cwd / ".claude" / "settings.json"
    if project_settings.exists():
        try:
            with open(project_settings, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data.get("env"), dict):
                    merged.update(data["env"])
        except (json.JSONDecodeError, OSError):
            pass

    # <cwd>/.claude/settings.local.json
    local_settings = cwd / ".claude" / "settings.local.json"
    if local_settings.exists():
        try:
            with open(local_settings, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data.get("env"), dict):
                    merged.update(data["env"])
        except (json.JSONDecodeError, OSError):
            pass

    return merged


@dataclass
class OrConfig:
    """OpenRouter configuration."""
    api_key: str
    base_url: str = "https://openrouter.ai/api/v1"


def resolve_openrouter() -> OrConfig | None:
    """
    Resolve OpenRouter config from environment.
    Returns None if OPENROUTER_API_KEY not set.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    return OrConfig(api_key=api_key, base_url=base_url)


@dataclass
class DbxConfig:
    """Databricks configuration."""
    host: str
    token: str


_DBX_HOST_SUFFIXES = (".databricks.com", ".azuredatabricks.net", ".gcp.databricks.com")


def looks_like_databricks_host(url_or_host: str) -> bool:
    """True if the hostname (bare host or full URL) ends with a known Databricks domain suffix, never loopback."""
    if not url_or_host:
        return False
    if url_or_host in ("127.0.0.1", "localhost", "::1"):
        return False
    if "://" in url_or_host:
        parsed = urllib.parse.urlparse(url_or_host)
        hostname = parsed.hostname or ""
    else:
        hostname = url_or_host
    hostname = hostname.lower()
    return any(hostname.endswith(suffix) for suffix in _DBX_HOST_SUFFIXES)


def load_databrickscfg(path: Path) -> DbxConfig | None:
    """Parse the [DEFAULT] section of a databricks-cli style config file; None if missing/unparseable/incomplete."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    in_default = False
    host = None
    token = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            in_default = (section == "DEFAULT")
            continue
        if not in_default or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if key.lower() == "host":
            host = value
        elif key.lower() == "token":
            token = value
    if host and token:
        return DbxConfig(host=host.rstrip("/"), token=token)
    return None


def resolve_databricks() -> DbxConfig | None:
    """
    Resolve Databricks host+token: BRIDGE_DBX_* override, then filtered
    ANTHROPIC_* (process env then settings chain, Databricks hosts only,
    never loopback), then DATABRICKS_HOST/TOKEN, then ~/.databrickscfg
    [DEFAULT]. Never raises.
    """
    # 1. Explicit override -- always wins, unfiltered.
    bridge_host = os.environ.get("BRIDGE_DBX_BASE_URL")
    bridge_token = os.environ.get("BRIDGE_DBX_TOKEN")
    if bridge_host and bridge_token:
        return DbxConfig(host=bridge_host.rstrip("/"), token=bridge_token)

    # 2. Process env ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (Databricks hosts only).
    anth_host = os.environ.get("ANTHROPIC_BASE_URL")
    anth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if anth_host and anth_token and looks_like_databricks_host(anth_host):
        return DbxConfig(host=anth_host.rstrip("/"), token=anth_token)

    # 3. Settings chain ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (same filter).
    settings_env = load_settings_env_chain(Path.cwd())
    anth_host = settings_env.get("ANTHROPIC_BASE_URL")
    anth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN")
    if anth_host and anth_token and looks_like_databricks_host(anth_host):
        return DbxConfig(host=anth_host.rstrip("/"), token=anth_token)

    # 4. DATABRICKS_HOST + DATABRICKS_TOKEN (process env, falling back to
    # settings chain per-field) -- unambiguous by name, no host filter.
    dbx_host = os.environ.get("DATABRICKS_HOST")
    dbx_token = os.environ.get("DATABRICKS_TOKEN")
    if not dbx_host or not dbx_token:
        dbx_host = settings_env.get("DATABRICKS_HOST") or dbx_host
        dbx_token = settings_env.get("DATABRICKS_TOKEN") or dbx_token
    if dbx_host and dbx_token:
        return DbxConfig(host=dbx_host.rstrip("/"), token=dbx_token)

    # 5. ~/.databrickscfg [DEFAULT].
    return load_databrickscfg(home() / ".databrickscfg")


def derive_workspace_root(base_url: str) -> str:
    """Strip a trailing /ai-gateway/anthropic[/v1[/messages]] suffix (and any trailing slash) from a Databricks base URL, returning the bare workspace root."""
    stripped = base_url.rstrip("/")
    root = re.sub(r"/ai-gateway/anthropic(?:/v1(?:/messages)?)?/?$", "", stripped)
    return root.rstrip("/")


def extract_custom_headers(incoming_headers) -> dict[str, str]:
    """Copy incoming request headers for upstream Databricks relay, dropping hop-by-hop/bridge-internal ones."""
    result = {}
    skip_lower = {"x-api-key", "authorization", "host", "content-length", "content-type",
                  "accept-encoding", "connection", "transfer-encoding"}
    for name, value in incoming_headers.items():
        lower_name = name.lower()
        if lower_name in skip_lower or lower_name.startswith("x-bridge-"):
            continue
        result[name] = value
    return result


def load_routes(path: Path | None) -> dict:
    """Load routes JSON file, return empty dict if missing/invalid."""
    if path is None or not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def redact(s: str | None) -> str:
    """Keep first 4 chars + '...' for non-empty strings, else '<unset>'."""
    if not s:
        return "<unset>"
    if len(s) <= 4:
        return s
    return s[:4] + "..."


@dataclass
class BridgeConfig:
    """Complete bridge configuration for --config output."""
    state_dir: Path
    openrouter: Optional[dict] = None
    databricks: Optional[dict] = None
    routes: dict = field(default_factory=dict)
    env_file_loaded: bool = False


def resolve_config() -> BridgeConfig:
    """
    Resolve all configuration without network calls.
    Secrets are redacted via redact().
    """
    # Load env file if present
    env_file_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    env_loaded = bool(load_env_file(env_file_path))

    state_dir = default_state_dir()
    routes_path = state_dir / "routes.json" if state_dir.exists() else None
    routes = load_routes(routes_path)

    # OpenRouter config (redacted)
    or_config = resolve_openrouter()
    or_dict = None
    if or_config:
        or_dict = {
            "api_key": redact(or_config.api_key),
            "base_url": or_config.base_url,
        }

    # Databricks config (redacted)
    dbx_config = resolve_databricks()
    dbx_dict = None
    if dbx_config:
        dbx_root = derive_workspace_root(dbx_config.host)
        dbx_dict = {
            "host": dbx_config.host,
            "token": redact(dbx_config.token),
            "workspace_root": dbx_root,
            "routes": {
                "invocations": f"{dbx_root}/serving-endpoints/<name>/invocations",
                "mlflow_chat": f"{dbx_root}/ai-gateway/mlflow/v1/chat/completions",
                "claude_passthrough": f"{dbx_root}/ai-gateway/anthropic/v1/messages",
            },
        }

    return BridgeConfig(
        state_dir=state_dir,
        openrouter=or_dict,
        databricks=dbx_dict,
        routes=routes,
        env_file_loaded=env_loaded,
    )


def ensure_token(state_dir: Path) -> str:
    """
    Read token from state_dir/token if exists, else generate and write.
    """
    token_file = state_dir / "token"
    if token_file.exists():
        try:
            return token_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    token = secrets.token_urlsafe(32)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        token_file.write_text(token, encoding="utf-8")
        if os.name != "nt":
            token_file.chmod(0o600)
    except OSError:
        pass
    return token


def bridge_py_sha1() -> str:
    """Return SHA1 hex digest of this file's bytes."""
    try:
        with open(__file__, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return "0" * 40


def write_server_json(state_dir: Path, pid: int, port: int) -> None:
    """Write server.json with service info."""
    data = {
        "service": "claude-bridge",
        "version": __version__,
        "hash": bridge_py_sha1(),
        "pid": pid,
        "port": port,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    server_file = state_dir / "server.json"
    try:
        with open(server_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive headers in log messages."""

    SENSITIVE_PATTERNS = [
        (r'(?i)(x-api-key|authorization|bearer)(\s*:?\s*)([^\s,;"]+)', r'\1\2[REDACTED]'),
        (r'(?i)"(api_key|token|key|secret)"\s*:\s*"[^"]*"', r'"\1":"[REDACTED]"'),
        (r'(?i)(password|passwd|pwd)\s*=\s*[^\s&]+', r'\1=[REDACTED]'),
    ]

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            msg = re.sub(pattern, replacement, msg)
        return msg


def setup_logging(state_dir: Path) -> logging.Logger:
    """
    Configure logging with RotatingFileHandler to state_dir/bridge.log.
    Returns the 'bridge' logger.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / "bridge.log"

    # Remove any existing handlers from the bridge logger
    logger = logging.getLogger("bridge")
    for hdlr in logger.handlers[:]:
        logger.removeHandler(hdlr)

    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = RedactingFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    return logger


def jdumps(obj: Any) -> bytes:
    """JSON dumps with ensure_ascii=False, encode to UTF-8 with replacement."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")


_IMAGE_FLAT_TOKENS = 1600


def _strip_image_payloads_for_estimate(obj):
    """Return (structurally-similar copy of obj, image_count): every embedded
    base64 image payload -- OpenAI-ish {"type":"image_url","image_url":
    {"url":"data:...;base64,..."}} or Anthropic-ish {"type":"image","source":
    {"data":...}} -- is replaced by a short placeholder and counted, so
    estimate_tokens can charge it a flat per-image cost instead of its raw
    (post-base64-inflation) byte length (finding 1)."""
    count = 0

    def walk(o):
        nonlocal count
        if isinstance(o, dict):
            if o.get("type") == "image_url" and isinstance(o.get("image_url"), dict) \
                    and isinstance(o["image_url"].get("url"), str) and "base64," in o["image_url"]["url"]:
                count += 1
                return {"type": "image_url", "image_url": {"url": "(image)"}}
            if o.get("type") == "image" and isinstance(o.get("source"), dict) and "data" in o["source"]:
                count += 1
                new_source = dict(o["source"])
                new_source["data"] = "(image)"
                return {**o, "source": new_source}
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(item) for item in o]
        return o

    return walk(obj), count


def estimate_tokens(obj: Any) -> int:
    """Estimate tokens as len(json)/4 for everything except embedded base64
    image payloads, which are counted at a flat ~1600 tokens each instead of
    their raw byte length -- otherwise one large screenshot in history
    inflates the estimate so much the up-front clamp floors max_tokens at 1,
    giving 1-token replies every later turn (finding 1). Minimum 1."""
    stripped, image_count = _strip_image_payloads_for_estimate(obj)
    json_str = json.dumps(stripped, ensure_ascii=False)
    return max(1, len(json_str) // 4 + image_count * _IMAGE_FLAT_TOKENS)


def dump_debug(state_dir: Path, kind: str, payload: Any) -> None:
    """Write debug dump if BRIDGE_DUMP=1."""
    if os.environ.get("BRIDGE_DUMP") != "1":
        return
    dump_dir = state_dir / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_file = dump_dir / f"{time.time():.3f}-{kind}.json"
    try:
        with open(dump_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except OSError:
        pass
@dataclass
class Route:
    provider: str  # "openrouter"|"databricks"|"invalid"
    upstream_model: str
    dialect: str  # "openai-chat"|"anthropic-passthrough"


def _dbx_dialect(stripped_model: str) -> str:
    """Databricks dialect for an already dbx:-stripped model name: passthrough iff 'claude' appears in it."""
    return "anthropic-passthrough" if "claude" in stripped_model.lower() else "openai-chat"


def route_model(name: str, headers: dict[str, str], cfg) -> Route:
    """Resolve model name to route; raises InvalidModelError for invalid."""
    # Helper to strip a dbx:/or: prefix and return the matching Route, else
    # None. NOTE: dbx:'s dialect must be decided the SAME way as the
    # unprefixed databricks-*/system.ai. fallback further down (via
    # _dbx_dialect) -- a bug in an earlier draft hardcoded
    # "anthropic-passthrough" for every dbx: name regardless of whether it
    # was actually a Claude model, silently sending non-Claude Databricks
    # models down the raw-relay path instead of the openai-chat translation.
    def prefixed(name_to_check):
        if name_to_check.startswith("dbx:"):
            stripped = name_to_check[len("dbx:"):]
            return Route("databricks", stripped, _dbx_dialect(stripped))
        if name_to_check.startswith("or:"):
            return Route("openrouter", name_to_check[len("or:"):], "openai-chat")
        return None

    # Prefix wins first
    if r := prefixed(name):
        return r

    # Tier/alias resolution
    resolved = name
    if "/" not in name:  # not a vendor/model
        # Determine tier
        is_small = "haiku" in name.lower() or name.endswith("-small")
        header_key = "x-bridge-small" if is_small else "x-bridge-main"
        if header_key in headers:
            resolved = headers[header_key]
        else:
            env_key = "BRIDGE_MODEL_SMALL" if is_small else "BRIDGE_MODEL"
            resolved = os.environ.get(env_key, name)
        # Re-run prefix logic on resolved ref
        if r := prefixed(resolved):
            return r

    # Direct vendor/model
    if "/" in resolved and resolved.count("/") == 1:
        return Route("openrouter", resolved, "openai-chat")

    # Databricks patterns
    if resolved.startswith("databricks-") or resolved.startswith("system.ai."):
        return Route("databricks", resolved, _dbx_dialect(resolved))

    raise InvalidModelError(
        f"no route: {name!r} (accepted forms are dbx:, or:, vendor/model, or a configured tier alias)"
    )


def is_passthrough_ref(model: str) -> bool:
    """True if a launcher --model value resolves to the Databricks Claude
    passthrough dialect. Mirrors route_model's own two prefix-checking code
    paths exactly, reusing _dbx_dialect for the actual "claude" check in
    both, instead of independently requiring a databricks-/system.ai. prefix
    on a dbx:-prefixed name (finding 10) -- route_model sends ANY dbx:X name
    containing "claude" to passthrough regardless of what X starts with, so
    a bare `dbx:claude-sonnet-4-5` ref used to be misclassified here as a
    plain openai-chat ref, wrongly forcing MAX_THINKING_TOKENS=0 and
    ENABLE_TOOL_SEARCH on what the server actually treats as a raw Claude
    relay."""
    if model.startswith("dbx:"):
        return _dbx_dialect(model[len("dbx:"):]) == "anthropic-passthrough"
    if model.startswith("databricks-") or model.startswith("system.ai."):
        return _dbx_dialect(model) == "anthropic-passthrough"
    return False


def select_tools(tools: list[dict] | None, messages: list[dict]) -> list[dict]:
    """Select tools based on references, cap at 128, strip defer_loading."""
    if not tools:
        return []
    referenced = collect_tool_references(messages)
    core = []
    deferred_refd = []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        defer = t.get("defer_loading", False)
        if not defer:
            core.append(t)
        elif name in referenced:
            deferred_refd.append(t)
    selected = core + deferred_refd
    selected = selected[:128]
    # Strip defer_loading key
    for t in selected:
        t.pop("defer_loading", None)
    return selected


def collect_tool_references(messages) -> set[str]:
    """Recursively collect tool names from tool_reference blocks."""
    refs = set()

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "tool_reference":
                refs.add(obj.get("tool_name") or obj.get("name") or "")
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(messages)
    return refs


def sanitize_tool_schema(schema: dict) -> dict:
    """Shallow copy, pop "$schema" only."""
    copy = dict(schema)
    copy.pop("$schema", None)
    return copy


def anthropic_tool_to_openai(t: dict) -> dict:
    """Convert Anthropic tool definition to OpenAI format."""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": sanitize_tool_schema(t.get("input_schema") or {"type": "object", "properties": {}})
        }
    }


def map_tool_choice(tc: dict | str | None) -> str | dict | None:
    """Map Anthropic tool_choice to OpenAI format."""
    if tc is None or tc == "auto":
        return "auto"
    if isinstance(tc, str):
        return tc  # fallback
    if not isinstance(tc, dict):
        return None
    typ = tc.get("type")
    if typ == "auto":
        return "auto"
    if typ == "any":
        return "required"
    if typ == "none":
        return "none"
    if typ == "tool":
        name = tc.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def resolve_profile(model_id: str, state_dir, routes_cfg: dict | None) -> dict:
    """Resolve {'context_tokens': int, 'max_output_tokens': int} for a model id: models.json entry, else routes_cfg['profiles'] entry, else routes_cfg['profiles']['default'], else the hardcoded 16384/128000 fallback."""
    models = load_models_json(state_dir)
    entry = models.get(model_id)
    if entry:
        return {"context_tokens": entry.get("context_length") or 128000,
                "max_output_tokens": entry.get("max_output_tokens") or 16384}
    profiles = (routes_cfg or {}).get("profiles") or {}
    entry = profiles.get(model_id) or profiles.get("default")
    if entry:
        return {"context_tokens": entry.get("context_tokens") or 128000,
                "max_output_tokens": entry.get("max_output_tokens") or 16384}
    return {"context_tokens": 128000, "max_output_tokens": 16384}


def clamp_max_tokens(requested: int, profile: dict, prompt_estimate: int) -> int:
    """Up-front max_tokens clamp: min(requested, profile max output, context -
    1.1*estimate - 512), floor 1. If that headroom term collapses below a
    sane floor (min(requested, 4096) -- e.g. our own estimate got inflated
    by a large embedded image), the headroom term is skipped entirely rather
    than clamping max_tokens down to a near-useless value: a genuine overflow
    is then left for the real upstream 400/retry path, which has the actual
    numbers instead of our own estimate (finding 1)."""
    max_output = profile.get("max_output_tokens", 16384)
    headroom = profile.get("context_tokens", 128000) - int(1.1 * prompt_estimate) - 512
    floor = min(requested, 4096) if requested > 0 else 4096
    if headroom < floor:
        return max(1, min(requested, max_output))
    candidate = min(requested, max_output, headroom)
    return max(1, candidate)


def anthropic_to_openai(body: dict, route: Route, profile: dict | None = None) -> dict:
    """Convert Anthropic request to OpenAI format; raises WebSearchUnavailable on web_search."""
    # Check for web_search tools before any work
    tools = body.get("tools")
    if tools:
        for t in tools:
            if isinstance(t, dict) and t.get("type", "").startswith("web_search"):
                raise WebSearchUnavailable()

    # Build messages per algorithm
    messages = _flatten_messages(body.get("messages", []), body.get("system"))

    # Build tools list
    oai_tools = None
    if tools:
        selected = select_tools(tools, body.get("messages", []))
        if selected:
            oai_tools = [anthropic_tool_to_openai(t) for t in selected]

    # Up-front max_tokens clamp: min(requested, profile cap, context headroom).
    # `profile` defaults to the foundation's hardcoded 16384/128000 pair so
    # behavior is unchanged for callers that don't pass one.
    profile = profile or {"context_tokens": 128000, "max_output_tokens": 16384}
    prelim_estimate = estimate_tokens({"messages": messages, "tools": oai_tools or []})
    requested_max = body.get("max_tokens", profile["max_output_tokens"])
    clamped_max = clamp_max_tokens(requested_max, profile, prelim_estimate)

    # Build final body
    oai_body = {
        "model": route.upstream_model,
        "messages": messages,
        "stream": True,
        "max_tokens": clamped_max,
    }
    if oai_tools:
        oai_body["tools"] = oai_tools
        oai_body["provider"] = {"require_parameters": True}
        # tool_choice is only meaningful (and only accepted by some strict
        # backends) when tools are actually present in the forwarded body.
        tc = map_tool_choice(body.get("tool_choice"))
        if tc is not None:
            oai_body["tool_choice"] = tc
    for key in ["temperature", "top_p", "stop"]:
        if key in body:
            oai_body[key] = body[key]
    # Explicitly drop
    for key in ["top_k", "metadata", "thinking"]:
        oai_body.pop(key, None)
    return oai_body


def _system_text_parts(value) -> list:
    """Extract system-message text parts from a `system` field or an
    in-message `role: system` content value: a plain string is used as-is;
    a list yields the text of each text-type block, dropping any block whose
    text starts with "x-anthropic-billing-header" and ignoring non-text
    blocks. Shared by both call sites in `_flatten_messages` below so the
    top-level `system` field and any in-message `role: system` entry are
    filtered identically."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text.startswith("x-anthropic-billing-header"):
                    continue
                parts.append(text)
        return parts
    return []


def _flatten_messages(messages: list, system) -> list:
    """Implement the message flattening algorithm."""
    # Flatten system: start from the top-level `system` field, then fold in
    # the text of every in-message `role: system` entry encountered below (in
    # encounter order -- see the `role == "system"` branch inside the loop).
    # Claude Code 2.1.280 appends a trailing "# Environment ..." system
    # message AFTER the user turn; forwarding that in place as a trailing
    # `role: system` OpenAI message makes some upstreams treat it as the last
    # word and continue writing environment text instead of answering, so
    # every system-role message is hoisted into one leading system message
    # instead of being forwarded where it appeared.
    system_parts = _system_text_parts(system)

    pending_ids = []  # tool_use ids from preceding assistant turn
    protos = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])
        if role == "assistant":
            # Split into text and tool_use blocks
            text_parts = []
            tool_use_blocks = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_use_blocks.append(block)
            text = "\n".join(text_parts).strip() if text_parts else None
            tool_calls = None
            if tool_use_blocks:
                tool_calls = []
                for tu in tool_use_blocks:
                    tool_calls.append({
                        "id": tu["id"],
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu.get("input") or {})
                        }
                    })
            # Emit assistant proto; content is explicitly present (None for a
            # tool-only turn) to match the canonical {"content": null, ...} shape.
            proto = {"role": "assistant", "content": text}
            if tool_calls:
                proto["tool_calls"] = tool_calls
            # Only emit if there was at least one original content block
            if content or (isinstance(content, list) and content):
                protos.append(proto)
            # Set pending_ids for next user turn
            pending_ids = [tc["id"] for tc in tool_calls] if tool_calls else []
        elif role == "user":
            # Gather tool_results
            result_by_id = {}
            other_blocks = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        result_by_id[tid] = block
                else:
                    other_blocks.append(block)
            # Emit tool messages for pending_ids in order. Keep a `consumed`
            # record (tid -> popped result, incl. None for a missing one) so
            # the image-hoisting pass below can still see what was matched
            # here -- result_by_id itself is emptied by the .pop() calls.
            consumed = {}
            for tid in pending_ids:
                result = result_by_id.pop(tid, None)
                consumed[tid] = result
                content_text = "(no result)"
                if result:
                    cnt = result.get("content")
                    text = extract_text(cnt)
                    if result.get("is_error"):
                        text = "[tool error] " + text
                    # Check for image
                    if isinstance(cnt, list):
                        for part in cnt:
                            if isinstance(part, dict) and part.get("type") == "image":
                                text = "(image in next message)"
                                break
                    content_text = text
                protos.append({"role": "tool", "tool_call_id": tid, "content": content_text})
            # Build one trailing user proto
            user_parts = []
            # (a) images from consumed ids
            for tid in pending_ids:
                result = consumed.get(tid)
                if result and isinstance(result.get("content"), list):
                    for part in result["content"]:
                        if isinstance(part, dict) and part.get("type") == "image":
                            source = part.get("source", {})
                            if source.get("type") == "base64":
                                user_parts.append({"type": "text", "text": f"Image from {tid}:"})
                                user_parts.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{source.get('media_type','image/jpeg')};base64,{source.get('data','')}"
                                    }
                                })
            # (b) orphaned results
            for tid, result in result_by_id.items():
                cnt = result.get("content")
                text = extract_text(cnt)
                if text:
                    user_parts.append({"type": "text", "text": text})
            # (c) other blocks from this user message
            for block in other_blocks:
                if isinstance(block, dict):
                    typ = block.get("type")
                    if typ == "text":
                        txt = block.get("text", "").strip()
                        if txt:
                            user_parts.append({"type": "text", "text": txt})
                    elif typ == "image":
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            user_parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{source.get('media_type','image/jpeg')};base64,{source.get('data','')}"
                                }
                            })
            if user_parts:
                protos.append({"role": "user", "content": user_parts})
            pending_ids = []  # reset after user turn
        elif role == "system":
            # Hoisted into the leading system message (below) instead of
            # being forwarded in place; must NOT touch pending_ids, so a
            # preceding assistant tool_use turn still pairs correctly with
            # the tool_result in the next real user turn even when a system
            # message sits between them.
            system_parts.extend(_system_text_parts(content))
        else:
            # Unknown role, pass through as-is
            protos.append({"role": role, "content": content})

    system_text = "\n\n".join(system_parts).strip()
    if system_text:
        protos.insert(0, {"role": "system", "content": system_text})

    # Final pass: drop empty, merge consecutive same-role
    filtered = []
    for proto in protos:
        content = proto.get("content")
        # Drop empty protos
        if content is None:
            if "tool_calls" not in proto:
                continue
        elif isinstance(content, str):
            if not content.strip():
                if "tool_calls" not in proto:
                    continue
        elif isinstance(content, list):
            if not content:
                if "tool_calls" not in proto:
                    continue
            # Check if all text parts are whitespace-only
            all_ws = True
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    if part.get("text", "").strip():
                        all_ws = False
                        break
                else:
                    all_ws = False
                    break
            if all_ws and "tool_calls" not in proto:
                continue
        filtered.append(proto)

    # Merge consecutive same-role (user+user, assistant+assistant)
    merged = []
    for proto in filtered:
        if not merged:
            merged.append(proto)
            continue
        prev = merged[-1]
        if prev["role"] == proto["role"] and prev["role"] not in ("tool", "system"):
            # Merge content
            prev_content = prev.get("content")
            curr_content = proto.get("content")
            # Normalize to list of parts
            def to_parts(c):
                if c is None:
                    return []
                if isinstance(c, str):
                    return [{"type": "text", "text": c}]
                if isinstance(c, list):
                    return c
                return []
            prev_parts = to_parts(prev_content)
            curr_parts = to_parts(curr_content)
            merged_parts = prev_parts + curr_parts
            # Convert back if single text part
            if len(merged_parts) == 1 and merged_parts[0].get("type") == "text":
                prev["content"] = merged_parts[0]["text"]
            else:
                prev["content"] = merged_parts
            # Merge tool_calls
            if "tool_calls" in proto:
                if "tool_calls" not in prev:
                    prev["tool_calls"] = []
                prev["tool_calls"].extend(proto.get("tool_calls", []))
        else:
            merged.append(proto)

    # Final conversion: an all-text content list on a user/assistant message
    # becomes one string joined by "\n\n" (a list containing any non-text --
    # e.g. image_url -- part is left as a list).
    for proto in merged:
        if proto["role"] not in ("user", "assistant"):
            continue
        content = proto.get("content")
        if isinstance(content, list) and content and all(
            isinstance(part, dict) and part.get("type") == "text" for part in content
        ):
            proto["content"] = "\n\n".join(part.get("text", "") for part in content)
    return merged


def extract_text(content) -> str:
    """Extract text from content block."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
        return "\n".join(texts)
    return ""


@dataclass
class OverflowInfo:
    limit: int
    prompt_tokens: int | None
    fixable: bool
    total: int | None


def _coerce_text(value) -> str:
    """Best-effort str() that never raises, for a JSON field (an error
    message, error.metadata.raw, a text content-block's "text") that a
    misbehaving upstream may send as the wrong type -- a dict, null, a
    number -- instead of the expected string (finding 6)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def upstream_error_text(body) -> str:
    """Best-effort upstream error message, checked in order: OpenAI/OpenRouter
    error.message -> top-level message (a Databricks serving-endpoint 400 is
    shaped {"error_code":...,"message":...}, with NO nested "error" object at
    all) -> a bare string "error" field -> str(body). Accepts a dict, bytes,
    or str and never raises regardless of shape, e.g. a bare {"error":"boom"}
    used to crash every caller that assumed error was always a dict
    (findings 2 and 6)."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return body.decode("utf-8", "replace")
    if not isinstance(body, dict):
        return _coerce_text(body)
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            return msg
    msg = body.get("message")
    if isinstance(msg, str) and msg:
        return msg
    if isinstance(err, str) and err:
        return err
    return str(body)


def parse_context_overflow(status: int, err_msg: str, raw_meta: str | None,
                            requested_max_tokens: int | None = None) -> OverflowInfo | None:
    """Parse an upstream context-overflow error from OpenAI/vLLM, OpenRouter,
    error.metadata.raw, or Databricks wording. None if status != 400 or no
    limit found. `requested_max_tokens` (the max_tokens actually sent on the
    failing request) feeds two fixes: (a) finding 4's fallback derivation of
    the prompt-token count A = T - requested_max_tokens when a wording only
    ever states a total T with no per-part breakdown; (b) finding 1's floor
    below which a mathematically-fixable retry (A <= L) is still treated as
    unfixable, because the resulting retry budget (L - A - 256) would be too
    small to be a useful response (e.g. near/below zero when A sits close to
    L, or ~1 when a huge inlined image inflated A) -- better to let Claude
    Code compact than burn a round trip on a near-empty reply."""
    if status != 400:
        return None
    err_msg = _coerce_text(err_msg)
    raw_meta_text = _coerce_text(raw_meta)
    text = err_msg + ((" " + raw_meta_text) if raw_meta_text else "")

    limit = None
    prompt_tokens = None
    total = None

    # Databricks real wording: "... exceed context limit: A + B > L" -- A, B,
    # L all in one match. Tried FIRST: the older, looser "context limit ...
    # (\d+)" pattern below (kept as a fallback for other Databricks wordings)
    # matched A (6000) instead of L (8192) against this exact wording
    # (finding 3) since "max_tokens" and a number both appear right after
    # "context limit" in the sentence.
    dbx_full = re.search(r"exceed context limit:?\s*(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)", text)
    if dbx_full:
        prompt_tokens = int(dbx_full.group(1))
        limit = int(dbx_full.group(3))
    else:
        for pattern in (
            r"maximum context length is (\d+)",           # OpenAI/vLLM and OpenRouter (superset phrase)
            r"context limit(?: of)?[^\d]{0,20}?(\d+)",     # looser Databricks fallback wording
        ):
            m = re.search(pattern, text)
            if m:
                limit = int(m.group(1))
                break
        if limit is None:
            return None

        prompt_match = re.search(r"\((\d+) in the messages", text)
        if not prompt_match:
            prompt_match = re.search(r"\((\d+) of text input", text)  # OpenRouter (finding 4)
        if prompt_match:
            prompt_tokens = int(prompt_match.group(1))
        else:
            dbx_match = re.search(r"(\d+)\s*input tokens\s*\+\s*(\d+)\s*max_tokens", text)
            prompt_tokens = int(dbx_match.group(1)) if dbx_match else None

        total_match = re.search(r"requested about (\d+)", text)
        total = int(total_match.group(1)) if total_match else None

        # Fallback (finding 4): a wording that only ever states a total T with
        # no per-part breakdown -- derive A = T - requested max_tokens so a
        # fixable overflow is still recognized instead of an unconditional
        # compaction rewrite.
        if prompt_tokens is None and total is not None and isinstance(requested_max_tokens, int):
            prompt_tokens = total - requested_max_tokens

    # Only fixable (worth a silent max_tokens-clamped retry) if the PROMPT
    # itself still fits under the limit (a prompt that alone exceeds it can
    # never be fixed by shrinking max_tokens) AND the resulting retry budget
    # clears a sane floor (finding 1) -- min(requested_max_tokens, 4096), or
    # a flat 4096 if the original request's max_tokens isn't known.
    fixable = False
    if prompt_tokens is not None and prompt_tokens <= limit:
        retry_budget = limit - prompt_tokens - 256
        if isinstance(requested_max_tokens, int) and requested_max_tokens > 0:
            floor = min(requested_max_tokens, 4096)
        else:
            floor = 4096
        fixable = retry_budget >= floor
    return OverflowInfo(limit, prompt_tokens, fixable, total)


def build_prompt_too_long_message(total: int, limit: int) -> str:
    """Build prompt too long error message."""
    return f"prompt is too long: {max(total, limit + 1)} tokens > {limit} maximum"


def map_upstream_error(status: int, body: dict | bytes | str, provider: str,
                       resp_headers: dict | None = None) -> tuple[int, dict, dict]:
    """Map upstream error to client error."""
    # Parse message (finding 2/6: never assume body["error"] is a dict --
    # Databricks has no nested "error" object at all, and a bare
    # {"error": "boom"} used to crash this with AttributeError).
    msg = upstream_error_text(body)
    # Table mapping: (upstream_status, error_type, client_status, should_retry)
    table = [
        (401, "authentication_error", 401, False),
        (402, "permission_error", 402, False),
        (403, "permission_error", 403, False),
        (404, "not_found_error", 404, False),
        (400, "invalid_request_error", 400, False),
        (429, "rate_limit_error", 429, True),
        (500, "api_error", 500, True),
        (502, "overloaded_error", 529, True),
        (503, "overloaded_error", 529, True),
        (504, "overloaded_error", 529, True),
    ]
    err_type = "api_error"
    client_status = status
    should_retry = True
    for st, et, cs, retry in table:
        if status == st:
            err_type, client_status, should_retry = et, cs, retry
            break
    else:
        if status >= 500:
            err_type, client_status, should_retry = "overloaded_error", 529, True
    # Build response
    extra_headers = {"x-should-retry": "true" if should_retry else "false"}
    if resp_headers:
        for k, v in resp_headers.items():
            if k.lower() == "retry-after":
                extra_headers["Retry-After"] = v
                break
    json_body = {"error": {"type": err_type, "message": msg}}
    return client_status, json_body, extra_headers


def flatten_content_parts(content) -> str:
    """Flatten a Databricks-style list of {'type':'text'|'reasoning',...} content parts into plain text; reasoning parts are logged at DEBUG with their length and never emitted."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            # finding 6: a malformed upstream can send {"type":"text","text":null}
            # -- part.get("text", "") only substitutes the default when the KEY
            # is missing, not when it's present-but-null, and "".join() on a
            # None item raises TypeError inside this stream loop.
            texts.append(_coerce_text(part.get("text")))
        elif ptype == "reasoning":
            for s in part.get("summary") or []:
                if isinstance(s, dict):
                    rtext = s.get("text", "")
                    log.debug("databricks reasoning part (not emitted), length=%d", len(rtext))
    return "".join(texts)


class OpenAIStreamToAnthropic:
    """State machine converting OpenAI streaming chunks to Anthropic SSE events."""
    def __init__(self, requested_model: str, input_tokens_estimate: int, msg_id: str | None = None):
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.requested_model = requested_model
        self.input_tokens_estimate = input_tokens_estimate
        self.next_index = 0
        self.text_index = None
        self.text_open = False
        self.tool_order = []
        self.tool_buf = {}
        self._id_to_key = {}
        self._last_key = None
        self._next_auto = 0
        self.finish_reason = None
        self.usage = {}
        self.done = False
        # Rough proxy for output size (chars of text + tool-call argument
        # fragments actually emitted), used ONLY as a fallback estimate for
        # message_delta.usage.output_tokens when the upstream never sends a
        # real "usage" chunk (several dialects/scenarios never do -- but an
        # Anthropic SDK-shaped stream must always carry a non-null int here).
        self._output_units = 0

    def message_start_event(self) -> dict:
        """Return message_start event with usage based on input estimate."""
        return {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "model": self.requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": self.input_tokens_estimate,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }

    def _key_for(self, tc: dict) -> tuple:
        """Return a stable key for a tool-call delta dict."""
        idx = tc.get("index")
        if idx is not None:
            key = ("idx", idx)
        else:
            id_ = tc.get("id")
            if id_ is not None:
                if id_ not in self._id_to_key:
                    self._id_to_key[id_] = ("auto", self._next_auto)
                    self._next_auto += 1
                key = self._id_to_key[id_]
            else:
                key = self._last_key or ("auto", self._next_auto)
                if key == ("auto", self._next_auto):
                    self._next_auto += 1
        self._last_key = key
        return key

    def feed_chunk(self, chunk: dict) -> dict:
        """Process one upstream chunk, return {"kind":"events"/"error","events":list}."""
        if "error" in chunk and "choices" not in chunk:
            # finding 6: a mid-stream {"error": "boom"} chunk (bare string,
            # not the usual {"message": ...} dict) crashed this with
            # AttributeError since only dicts support .get() -- this loop
            # only catches OSError, so an uncaught exception here died with
            # neither an error event nor message_stop ever reaching the client.
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else None
            if not isinstance(msg, str):
                msg = err if isinstance(err, str) else _coerce_text(err)
            return {"kind": "error", "events": [self.error_event(msg)]}

        events = []
        if chunk.get("choices"):
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}

            # Text content -- OpenAI/OpenRouter send a plain string; Databricks
            # can instead send a list of {"type":"text"|"reasoning",...} parts
            # (serving-endpoints/mlflow responses) -- flatten text parts here
            # and log-only any reasoning part found inside the list, so both
            # shapes funnel through one code path.
            content = delta.get("content")
            if isinstance(content, list):
                content = flatten_content_parts(content)
            if content:
                if not self.text_open:
                    self.text_index = self.next_index
                    self.next_index += 1
                    events.append(content_block_start(self.text_index, {"type": "text", "text": ""}))
                    self.text_open = True
                events.append(content_block_delta(self.text_index, {"type": "text_delta", "text": content}))
                self._output_units += len(content)

            # Reasoning content (log only, never emitted to Claude Code)
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                log.debug("reasoning delta (not emitted), length=%d", len(reasoning))

            # Tool calls
            for tc in delta.get("tool_calls") or []:
                key = self._key_for(tc)
                if key not in self.tool_buf:
                    self.tool_buf[key] = {"name": None, "args": ""}
                    if key not in self.tool_order:
                        self.tool_order.append(key)
                buf = self.tool_buf[key]
                func = tc.get("function") or {}
                if "name" in func:
                    buf["name"] = func["name"]
                if "arguments" in func:
                    buf["args"] += func["arguments"]
                    self._output_units += len(func["arguments"])

            # Finish reason
            fr = choice.get("finish_reason")
            if fr is not None:
                self.finish_reason = fr

        # Usage
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage.update(map_usage(usage))

        return {"kind": "events", "events": events}

    def feed_sse_line(self, line: str) -> dict:
        """Parse one SSE line, return {"kind":"events"/"done","events":list}."""
        if not line or line.startswith(":"):
            return {"kind": "events", "events": []}

        # Strip "data:" prefix if present
        if line.startswith("data:"):
            payload = line[5:].lstrip()
        else:
            payload = line

        if payload == "[DONE]":
            return {"kind": "done", "events": self._finalize()}

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            return {"kind": "events", "events": []}
        return self.feed_chunk(chunk)

    def on_eof(self) -> list[dict]:
        """Call when upstream stream ends; returns final events if not already finalized."""
        return [] if self.done else self._finalize()

    def error_event(self, message: str, err_type: str = "overloaded_error") -> dict:
        """Return an error event dict."""
        return {"type": "error", "error": {"type": err_type, "message": message}}

    def _finalize(self) -> list[dict]:
        """Finalize all pending blocks, return list of events. Idempotent."""
        if self.done:
            return []
        self.done = True

        events = []
        if self.text_open:
            events.append(content_block_stop(self.text_index))

        # Drop last tool call if length cutoff
        if self.finish_reason == "length" and self.tool_order:
            self.tool_order = self.tool_order[:-1]

        # Emit tool calls
        for key in self.tool_order:
            buf = self.tool_buf[key]
            args_str = buf["args"] or "{}"
            try:
                parsed = json.loads(args_str) if args_str.strip() else {}
            except json.JSONDecodeError:
                log.warning(f"Invalid JSON in tool arguments: {args_str}")
                parsed = {}

            idx = self.next_index
            self.next_index += 1
            tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
            events.append(content_block_start(idx, {"type": "tool_use", "id": tool_id, "name": buf["name"] or "unknown", "input": {}}))
            events.append(content_block_delta(idx, {"type": "input_json_delta", "partial_json": json.dumps(parsed)}))
            events.append(content_block_stop(idx))

        stop_reason = decide_stop_reason(self.finish_reason, bool(self.tool_order))
        final_usage = dict(self.usage)
        if not isinstance(final_usage.get("output_tokens"), int) or isinstance(final_usage.get("output_tokens"), bool):
            final_usage["output_tokens"] = max(1, self._output_units // 4)
        events.append(message_delta_event(stop_reason, final_usage))
        events.append({"type": "message_stop"})
        return events


def decide_stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    """Map upstream finish_reason to Anthropic stop_reason."""
    if finish_reason == "length":
        return "max_tokens"
    if has_tool_calls:
        return "tool_use"
    if finish_reason in (None, "stop", "content_filter"):
        return "end_turn"
    return "end_turn"


def map_usage(u: dict) -> dict:
    """Map OpenAI usage dict to Anthropic fields, dropping None values."""
    result = {}
    if "prompt_tokens" in u:
        result["input_tokens"] = u["prompt_tokens"]
    if "completion_tokens" in u:
        result["output_tokens"] = u["completion_tokens"]
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        result["cache_read_input_tokens"] = cached
    return result


def content_block_start(index: int, content_block: dict) -> dict:
    """Return content_block_start event."""
    return {"type": "content_block_start", "index": index, "content_block": content_block}


def content_block_delta(index: int, delta: dict) -> dict:
    """Return content_block_delta event."""
    return {"type": "content_block_delta", "index": index, "delta": delta}


def content_block_stop(index: int) -> dict:
    """Return content_block_stop event."""
    return {"type": "content_block_stop", "index": index}


def message_delta_event(stop_reason: str, usage: dict) -> dict:
    """Return message_delta event with stop_reason and usage."""
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    }


class MessageCollector:
    """Collect a non‑streaming response into events."""
    @staticmethod
    def to_events(body: dict, sm: OpenAIStreamToAnthropic) -> list[dict]:
        """Convert a plain JSON response body to events via the state machine."""
        choice = body["choices"][0]
        msg = choice.get("message") or {}
        delta = {}
        content = msg.get("content")
        if content is not None:
            delta["content"] = content
        tool_calls = msg.get("tool_calls")
        if tool_calls is not None:
            delta["tool_calls"] = tool_calls
        chunk = {
            "choices": [{"index": 0, "delta": delta, "finish_reason": choice.get("finish_reason")}],
            "usage": body.get("usage"),
        }
        events = list(sm.feed_chunk(chunk).get("events") or [])
        events += sm.on_eof()
        return events


def sse_frame(event_type: str, data: dict) -> bytes:
    """Return a complete SSE frame bytes."""
    return b"event: " + event_type.encode() + b"\r\ndata: " + jdumps(data) + b"\r\n\r\n"
def pick_proxy(host: str) -> str | None:
    """Return proxy URL from env if host not bypassed, else None."""
    # Check bypass first
    if urllib.request.proxy_bypass_environment(host):
        return None
    
    # Check environment variables in order
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        proxy = os.environ.get(var)
        if proxy:
            return proxy
    return None


def open_upstream(host: str, port: int, tls: bool, connect_timeout: int = 10) -> http.client.HTTPConnection | http.client.HTTPSConnection:
    """Open HTTP(S) connection to upstream, respecting proxy and TLS settings."""
    proxy_url = pick_proxy(host)
    
    # Create TLS context if needed
    ssl_context = None
    if tls:
        ssl_context = ssl.create_default_context()
        # Load custom CA bundle if specified
        for env_var in ("NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "BRIDGE_CA_BUNDLE"):
            ca_path = os.environ.get(env_var)
            if ca_path:
                try:
                    ssl_context.load_verify_locations(ca_path)
                    break
                except Exception:
                    log.warning(f"Failed to load CA bundle from {ca_path}", exc_info=True)
    
    # Determine connection parameters
    if proxy_url:
        proxy_parts = urllib.parse.urlparse(proxy_url)
        proxy_host = proxy_parts.hostname
        proxy_port = proxy_parts.port or (443 if proxy_parts.scheme == "https" else 80)
        
        if tls:
            # HTTPS via proxy tunnel
            conn = http.client.HTTPSConnection(
                proxy_host, proxy_port, timeout=connect_timeout, context=ssl_context
            )
            conn.set_tunnel(host, port)
        else:
            # HTTP via proxy
            conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=connect_timeout)
    else:
        # Direct connection
        if tls:
            conn = http.client.HTTPSConnection(
                host, port, timeout=connect_timeout, context=ssl_context
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    
    # Connect with timeout, then set idle timeout
    conn.connect()
    if conn.sock:
        conn.sock.settimeout(300)  # 5 minutes idle timeout
    
    return conn


@dataclass
class UpstreamResult:
    """Result of an upstream HTTP call."""
    status: int
    headers: dict[str, str]
    resp: http.client.HTTPResponse | None
    conn: http.client.HTTPConnection | http.client.HTTPSConnection | None
    # Already-read raw response bytes when the producer had to consume `resp`
    # itself (e.g. call_databricks_chat peeking at a 400 body to look for a
    # max_tokens-limit wording before deciding whether to retry) -- None
    # means "resp has not been read yet, read it yourself".
    body_bytes: bytes | None = None


def call_openai_chat(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir: Path) -> UpstreamResult:
    """POST to OpenAI-compatible chat completions endpoint."""
    # Normalize base_url
    if base_url.endswith("/"):
        base_url = base_url.rstrip("/")
    
    # Parse URL to get host/port/tls
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    
    # Prepare request
    path = parsed.path + "/chat/completions"
    if not path.startswith("/"):
        path = "/" + path
    
    body_bytes = jdumps(body)
    dump_debug(state_dir, "upstream-request", body)
    
    # Build headers
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "HTTP-Referer": "https://github.com/rolo/claude-bridge",
        "X-Title": "claude-bridge",
        "Content-Length": str(len(body_bytes)),
    }
    # Merge extra_headers on top (overriding defaults)
    headers.update(extra_headers)
    
    try:
        # Open connection and send request
        conn = open_upstream(host, port, tls)
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        
        # Collect headers (lowercase keys)
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        
        return UpstreamResult(
            status=resp.status,
            headers=resp_headers,
            resp=resp,
            conn=conn,
        )
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        # Wrap connection-level errors
        raise UpstreamConnectError(f"Upstream connection failed: {e}") from e


def databricks_route_candidates(model: str) -> list[tuple[str, bool]]:
    """Ordered (path, body_has_model) candidates for a resolved Databricks
    model name (dbx: prefix already stripped). The invocations path uses the
    model name IN FULL, prefix included -- Databricks pay-per-token serving
    endpoint names keep "databricks-" as part of their real, literal name
    (documented form /serving-endpoints/databricks-meta-llama-3-3-70b-instruct
    /invocations; the Claude passthrough route already sends
    "databricks-claude-..." in full). Stripping it 404s on the primary route
    for every such model (finding 8 -- the brief itself was wrong here)."""
    invocations = (f"/serving-endpoints/{model}/invocations", False)
    mlflow = ("/ai-gateway/mlflow/v1/chat/completions", True)
    if model.startswith("system.ai."):
        return [mlflow, invocations]
    return [invocations, mlflow]


def build_databricks_body(oai_body: dict, include_model: bool, model: str) -> dict:
    """Filter an OpenAI-chat body down to the Databricks-accepted key allowlist, adding/omitting 'model'."""
    allowed = ("messages", "max_tokens", "temperature", "top_p", "stop", "stream", "tools", "tool_choice")
    result = {key: oai_body[key] for key in allowed if key in oai_body}
    if include_model:
        result["model"] = model
    return result


def parse_databricks_max_tokens_limit(err_msg: str) -> int | None:
    """Parse a Databricks 400 error naming a max_tokens ceiling, e.g.
    'max_tokens must be <= 4096'. Deliberately anchored on the '<=' ceiling
    wording: a looser 'max_tokens ... (\\d+)' pattern also matches the
    UNRELATED "... exceed context limit: A + B > L" overflow wording (it
    contains the substring "max_tokens" too), misreading A as if it were a
    max_tokens ceiling and triggering a nonsensical clamp-retry instead of
    the real overflow handling (finding 3)."""
    m = re.search(r"max[_ ]?tokens?\D{0,20}?<=\s*(\d+)", err_msg, re.IGNORECASE)
    return int(m.group(1)) if m else None


def databricks_unreachable_response(detail: str) -> tuple[int, dict, dict]:
    """Build the (status, json_body, extra_headers) tuple for a Databricks connect/DNS failure."""
    msg = f"{detail} (are you on the VPN? Databricks is whitelisted)"
    return 502, {"error": {"type": "api_error", "message": msg}}, {"x-should-retry": "false"}


# In-memory mirror of <state_dir>/routes-cache.json: model -> {"route": int, "max_tokens_limit": int|None}
_DBX_ROUTE_CACHE: dict = {}


def _dbx_cache_path(state_dir) -> Path:
    """routes-cache.json path under state_dir."""
    return Path(state_dir) / "routes-cache.json"


def _dbx_cache_load(state_dir) -> dict:
    """Merge routes-cache.json from disk into _DBX_ROUTE_CACHE (in-memory entries win) and return it."""
    path = _dbx_cache_path(state_dir)
    if not path.exists():
        return _DBX_ROUTE_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            disk = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _DBX_ROUTE_CACHE
    if isinstance(disk, dict):
        for model, entry in disk.items():
            if model not in _DBX_ROUTE_CACHE:
                _DBX_ROUTE_CACHE[model] = entry
    return _DBX_ROUTE_CACHE


def _dbx_cache_save(state_dir) -> None:
    """Write _DBX_ROUTE_CACHE to routes-cache.json. Best-effort; swallow OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        with open(_dbx_cache_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(_DBX_ROUTE_CACHE, f)
    except OSError:
        pass


def dbx_cache_get_route(model: str, state_dir) -> int | None:
    """Cached candidate index for model, or None."""
    if model not in _DBX_ROUTE_CACHE:
        _dbx_cache_load(state_dir)
    entry = _DBX_ROUTE_CACHE.get(model)
    return entry.get("route") if isinstance(entry, dict) else None


def dbx_cache_set_route(model: str, index: int, state_dir) -> None:
    """Record which candidate index worked for model, update memory + persist to disk."""
    _DBX_ROUTE_CACHE.setdefault(model, {})["route"] = index
    _dbx_cache_save(state_dir)


def dbx_cache_get_max_tokens_limit(model: str, state_dir) -> int | None:
    """Cached max_tokens ceiling for model, or None."""
    if model not in _DBX_ROUTE_CACHE:
        _dbx_cache_load(state_dir)
    entry = _DBX_ROUTE_CACHE.get(model)
    return entry.get("max_tokens_limit") if isinstance(entry, dict) else None


def dbx_cache_set_max_tokens_limit(model: str, limit: int, state_dir) -> None:
    """Record a discovered max_tokens ceiling for model, update memory + persist to disk."""
    _DBX_ROUTE_CACHE.setdefault(model, {})["max_tokens_limit"] = limit
    _dbx_cache_save(state_dir)


def _dbx_post(base_url: str, path: str, api_key: str, req_body: dict, extra_headers: dict, state_dir):
    """POST to a Databricks endpoint, returning (resp, conn) or raising UpstreamConnectError."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + path
    body_bytes = jdumps(req_body)
    dump_debug(state_dir, "upstream-request", req_body)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "Content-Length": str(len(body_bytes)),
    }
    headers.update(extra_headers)
    try:
        conn = open_upstream(host, port, tls)
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        return resp, conn
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"Databricks connection failed: {e}") from e


def call_databricks_chat(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir, model: str) -> UpstreamResult:
    """POST an OpenAI-chat body to a Databricks route, trying the cached/candidate paths with 404 fallback and a max_tokens-limit clamp-retry."""
    candidates = databricks_route_candidates(model)
    cached_idx = dbx_cache_get_route(model, state_dir)
    if cached_idx is not None and 0 <= cached_idx < len(candidates):
        order = [(cached_idx, *candidates[cached_idx])]
        order += [(i, *cand) for i, cand in enumerate(candidates) if i != cached_idx]
    else:
        order = [(i, *cand) for i, cand in enumerate(candidates)]

    chosen = None  # (orig_idx, path, include_model, resp, conn)
    for pos, (orig_idx, path, include_model) in enumerate(order):
        req_body = build_databricks_body(body, include_model, model)
        resp, conn = _dbx_post(base_url, path, api_key, req_body, extra_headers, state_dir)
        if resp.status == 404 and pos != len(order) - 1:
            resp.read()
            continue
        chosen = (orig_idx, path, include_model, resp, conn)
        break

    orig_idx, path, include_model, resp, conn = chosen
    dbx_cache_set_route(model, orig_idx, state_dir)
    headers = {k.lower(): v for k, v in resp.getheaders()}

    if resp.status == 400:
        raw = resp.read()
        try:
            err_obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except (json.JSONDecodeError, ValueError):
            err_obj = {"error": {"message": raw.decode("utf-8", "replace")}}
        # finding 2/6: Databricks' own shape is {"error_code":...,"message":...}
        # -- no nested "error" object at all -- and a bare {"error":"boom"}
        # used to crash the old ad hoc ".get('error') or {}).get('message')"
        # extraction with AttributeError.
        err_msg = upstream_error_text(err_obj)
        limit = parse_databricks_max_tokens_limit(err_msg)
        if limit is not None and isinstance(body.get("max_tokens"), int) and body["max_tokens"] > limit:
            # Done with the first attempt's connection -- close it before opening a second one.
            try:
                resp.close()
                conn.close()
            except Exception:
                pass
            retry_body = dict(body)
            retry_body["max_tokens"] = limit
            req_body = build_databricks_body(retry_body, include_model, model)
            resp2, conn2 = _dbx_post(base_url, path, api_key, req_body, extra_headers, state_dir)
            dbx_cache_set_max_tokens_limit(model, limit, state_dir)
            headers2 = {k.lower(): v for k, v in resp2.getheaders()}
            return UpstreamResult(status=resp2.status, headers=headers2, resp=resp2, conn=conn2, body_bytes=None)
        return UpstreamResult(status=400, headers=headers, resp=resp, conn=conn, body_bytes=raw)

    return UpstreamResult(status=resp.status, headers=headers, resp=resp, conn=conn, body_bytes=None)


def probe_databricks_endpoints(root: str, token: str) -> tuple[int, list[str]]:
    """GET <root>/api/2.0/serving-endpoints; returns (status, endpoint_names). Raises UpstreamConnectError on connect/DNS failure."""
    parsed = urllib.parse.urlparse(root)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/api/2.0/serving-endpoints"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
    try:
        conn = open_upstream(host, port, tls)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"Databricks connection failed: {e}") from e
    if resp.status == 200:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return 200, [e.get("name", "") for e in data.get("endpoints", [])]
        except (json.JSONDecodeError, ValueError):
            pass
    return resp.status, []


def probe_openrouter_models(base_url: str, api_key: str) -> list[dict]:
    """GET {base_url}/models; returns a list of {'id', 'context_length', 'max_output_tokens'} dicts."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"}
    try:
        conn = open_upstream(host, port, tls)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"OpenRouter connection failed: {e}") from e
    if resp.status != 200:
        raise RuntimeError(f"OpenRouter /models returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    models = []
    for entry in data.get("data", []):
        models.append({
            "id": entry.get("id"),
            "context_length": entry.get("context_length"),
            "max_output_tokens": (entry.get("top_provider") or {}).get("max_completion_tokens"),
        })
    return models


def models_json_path(state_dir) -> Path:
    """models.json path under state_dir."""
    return Path(state_dir) / "models.json"


def write_models_json(state_dir, models: list[dict]) -> None:
    """Write models.json as {"<id>": {"context_length":..., "max_output_tokens":...}, ...}. Best-effort."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {}
        for m in models:
            mid = m.get("id")
            if not mid:
                continue
            out[mid] = {"context_length": m.get("context_length"), "max_output_tokens": m.get("max_output_tokens")}
        with open(models_json_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except OSError:
        pass


def load_models_json(state_dir) -> dict:
    """Read models.json (dict keyed by id); {} if missing/unparseable."""
    path = models_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def cmd_probe(args) -> None:
    """--probe: report Databricks endpoint reachability and cache the OpenRouter model list."""
    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    dbx = resolve_databricks()
    if dbx is None:
        print("Databricks: not configured")
    else:
        root = derive_workspace_root(dbx.host)
        try:
            status, names = probe_databricks_endpoints(root, dbx.token)
            if status == 200:
                print(f"Databricks: {len(names)} serving endpoint(s) at {root}: {', '.join(names) if names else '(none)'}")
            elif status in (401, 403):
                print("Databricks: token can run inference but not list endpoints -- pass names explicitly")
            else:
                print(f"Databricks: unexpected status {status} listing endpoints at {root}")
        except UpstreamConnectError as e:
            print(f"Databricks: unreachable -- {e} (are you on the VPN? Databricks is whitelisted)")

    orc = resolve_openrouter()
    if orc is None:
        print("OpenRouter: not configured (no OPENROUTER_API_KEY)")
    else:
        try:
            models = probe_openrouter_models(orc.base_url, orc.api_key)
            state_dir = default_state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            write_models_json(state_dir, models)
            print(f"OpenRouter: cached {len(models)} model(s) to {models_json_path(state_dir)}")
        except (UpstreamConnectError, RuntimeError) as e:
            print(f"OpenRouter: probe failed -- {e}")

    # finding 9: --probe never reported whether the local bridge server's own
    # port was busy, free, or held by a stale/foreign process.
    port = getattr(args, "port", None) or DEFAULT_PORT
    port_status = check_server_status(port, bridge_py_sha1())
    port_labels = {
        "fresh": f"Port {port}: claude-bridge running (up to date)",
        "stale": f"Port {port}: claude-bridge running but stale (different version) -- launch will restart it",
        "foreign": f"Port {port}: in use by something that is not claude-bridge",
        "absent": f"Port {port}: free",
    }
    print(port_labels[port_status])


def strip_bridge_signed_thinking(messages: list) -> list:
    """Return a copy of messages with any assistant 'thinking' content block whose signature starts with 'bridge1.' removed."""
    result = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking" and \
                        str(block.get("signature", "")).startswith("bridge1."):
                    continue
                new_content.append(block)
            result.append({**msg, "content": new_content})
        else:
            result.append(dict(msg) if isinstance(msg, dict) else msg)
    return result


def filter_beta_header(value: str) -> str:
    """Drop any comma-separated anthropic-beta flag matching 'tool-search-tool-*'; return the rejoined remainder (possibly empty string)."""
    if not value:
        return ""
    parts = [p.strip() for p in value.split(",")]
    filtered = [p for p in parts if not p.lower().startswith("tool-search-tool-")]
    return ", ".join(filtered)


def replace_tool_reference_blocks(messages: list) -> list:
    """Return a copy of messages with every {"type":"tool_reference",...}
    content block replaced by a plain text block naming the tool. Databricks'
    native Claude endpoint has no tool-search-tool-* beta enabled on the
    passthrough relay (we strip it) and 400s on this unrecognized block type
    -- the first ToolSearch result in the history breaks every later
    passthrough request otherwise (finding 5)."""
    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "tool_reference":
                name = obj.get("tool_name") or obj.get("name") or "unknown"
                return {"type": "text", "text": f"(tool available: {name})"}
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(item) for item in obj]
        return obj

    return [walk(msg) for msg in messages]


def build_passthrough_body(body: dict, route: Route) -> dict:
    """Rewrite an Anthropic-format request body for Databricks Claude passthrough: model rewrite, tool cap/reference handling, thinking-block strip."""
    result = dict(body)
    result["model"] = route.upstream_model
    if body.get("tools"):
        result["tools"] = select_tools(body["tools"], body.get("messages", []))
    messages = strip_bridge_signed_thinking(body.get("messages", []))
    result["messages"] = replace_tool_reference_blocks(messages)
    return result


def build_passthrough_headers(incoming_headers, gateway_token: str) -> dict:
    """Build the outgoing header dict for a Databricks Claude passthrough request."""
    result = extract_custom_headers(incoming_headers)
    beta_key = None
    for k in list(result.keys()):
        if k.lower() == "anthropic-beta":
            beta_key = k
            break
    if beta_key is not None:
        filtered = filter_beta_header(result[beta_key])
        if filtered:
            result[beta_key] = filtered
        else:
            result.pop(beta_key, None)
    result["Authorization"] = f"Bearer {gateway_token}"
    return result


def proxy_anthropic(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir, path: str = "/v1/messages") -> UpstreamResult:
    """POST an already-shaped Anthropic-format body straight through to Databricks' native Claude endpoint; raw relay, no dialect translation."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    request_path = parsed.path.rstrip("/") + path + "?beta=true"
    body_bytes = jdumps(body)
    dump_debug(state_dir, "upstream-request", body)
    headers = {
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "Content-Length": str(len(body_bytes)),
    }
    headers.update(extra_headers)
    try:
        conn = open_upstream(host, port, tls)
        conn.request("POST", request_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return UpstreamResult(status=resp.status, headers=resp_headers, resp=resp, conn=conn, body_bytes=None)
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"Databricks connection failed: {e}") from e


def call_databricks_count_tokens(base_url: str, api_key: str, body: dict, extra_headers: dict, state_dir) -> UpstreamResult:
    """Blocking (non-streaming) relay to Databricks' count_tokens endpoint."""
    return proxy_anthropic(base_url, api_key, body, extra_headers, state_dir, path="/v1/messages/count_tokens")


def passthrough_reader_thread(resp, q) -> None:
    """Background thread: read the upstream Databricks Claude response in raw chunks and post them to q."""
    try:
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                q.put(("eof", None))
                break
            q.put(("raw", chunk))
    except Exception as e:
        q.put(("exc", e))
    finally:
        try:
            resp.close()
        except Exception:
            pass
class _Server(http.server.ThreadingHTTPServer):
    """Threading HTTP server with exclusive address binding on Windows."""
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        """Bind socket with SO_EXCLUSIVEADDRUSE on Windows."""
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for Claude Bridge."""
    protocol_version = "HTTP/1.1"
    timeout = 120
    # Applied by socketserver.StreamRequestHandler.setup() BEFORE handle()
    # runs -- unlike a manual setsockopt() from __init__, which (for
    # socketserver handlers) only executes AFTER the entire request has
    # already been processed, since BaseRequestHandler.__init__ itself calls
    # setup()/handle()/finish() synchronously.
    disable_nagle_algorithm = True

    def check_auth(self) -> str | None:
        """Extract token from x-api-key or Authorization header."""
        auth = self.headers.get("x-api-key")
        if auth:
            return auth
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def send_json(self, status: int, obj: dict, extra_headers: dict | None = None):
        """Send JSON response with exact Content-Length."""
        body = jdumps(obj)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Connection": "keep-alive",
        }
        if extra_headers:
            headers.update(extra_headers)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _client_gone(self) -> bool:
        """Non-blocking peek at the client socket to detect it having closed
        its side. Writing small SSE frames alone does not reliably fail
        promptly on every platform (a graceful close over loopback can leave
        small writes silently accepted by the local kernel for a while), so
        the streaming loop also polls this directly every iteration."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def write_chunk(self, data: bytes):
        """Write a single chunk in chunked encoding."""
        try:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            log.debug("Client write error: %s", e)
            raise

    def _read_full_body(self) -> bytes:
        """Read entire request body using Content-Length."""
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _reader_thread(self, resp: http.client.HTTPResponse, q: queue.Queue):
        """Background thread reading upstream response."""
        try:
            if resp.getheader("content-type", "").startswith("text/event-stream"):
                while True:
                    line = resp.readline()
                    if not line:
                        # A clean readline() EOF happens BOTH for a properly
                        # terminated chunked body AND for a connection that
                        # died mid-chunk (empirically, http.client does not
                        # raise IncompleteRead here) -- resp.chunk_left is
                        # the only reliable signal after the fact: None means
                        # the terminating zero-chunk was actually seen; any
                        # other value means the body was cut off mid-stream.
                        if getattr(resp, "chunk_left", None) is None:
                            q.put(("eof", None))
                        else:
                            q.put(("exc", ConnectionError("upstream connection closed mid-stream")))
                        break
                    q.put(("line", line))
            else:
                data = resp.read()
                try:
                    obj = json.loads(data.decode("utf-8", "replace"))
                    q.put(("json", obj))
                except json.JSONDecodeError:
                    q.put(("exc", ValueError("Upstream returned non-JSON")))
                q.put(("eof", None))
        except Exception as e:
            q.put(("exc", e))
        finally:
            resp.close()

    def _handle_upstream_stream(self, result: UpstreamResult, sm: OpenAIStreamToAnthropic):
        """Handle streaming upstream response with SSE and pings."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        # Deliberately NOT "Connection: close" here (despite the design
        # review's literal wording): http.client.HTTPConnection.getresponse()
        # treats a "close" response as already-closed and nulls out its own
        # response reference right after parsing headers, so a client's
        # later conn.close() (with no explicit resp.close(), exactly the
        # pattern test_bridge.py's abort tests use) becomes a no-op that
        # never actually releases the socket's file object -- the peer close
        # then never becomes observable to us (verified empirically). We
        # still close from the SERVER's own side via close_connection=True.
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True

        # BRIDGE_DUMP=1 accumulators -- one dump_debug call per request per
        # kind at the end (dump_debug itself no-ops unless the env var is set).
        dumped_lines: list = []
        dumped_events: list = []

        def _write_event(ev):
            dumped_events.append(ev)
            self.write_chunk(sse_frame(ev["type"], ev))

        try:
            start_ev = sm.message_start_event()
            dumped_events.append(start_ev)
            self.write_chunk(sse_frame("message_start", start_ev))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            if result.conn and result.conn.sock:
                try:
                    result.conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            dump_debug(SERVER_STATE_DIR, "upstream-stream", {"lines": dumped_lines})
            dump_debug(SERVER_STATE_DIR, "emitted-events", {"events": dumped_events})
            return

        q = queue.Queue()
        reader = threading.Thread(target=self._reader_thread, args=(result.resp, q))
        reader.daemon = True
        reader.start()

        ping_interval = float(os.environ.get("BRIDGE_PING_INTERVAL", "15"))
        try:
            while True:
                if self._client_gone():
                    raise BrokenPipeError("client disconnected (detected via peek)")
                try:
                    item = q.get(timeout=ping_interval)
                except queue.Empty:
                    self.write_chunk(sse_frame("ping", {"type": "ping"}))
                    continue

                kind, value = item
                try:
                    if kind == "line":
                        line = value.decode("utf-8", "replace").rstrip("\n")
                        dumped_lines.append(line)
                        # NOTE: named `step`, not `result` -- `result` is the
                        # enclosing UpstreamResult (still needed below, in the
                        # write-error handler, to shut down the upstream socket).
                        step = sm.feed_sse_line(line)
                        if step["kind"] == "error":
                            for ev in step["events"]:
                                _write_event(ev)
                            break
                        elif step["kind"] == "done":
                            for ev in step["events"]:
                                _write_event(ev)
                            break
                        else:
                            for ev in step["events"]:
                                _write_event(ev)
                    elif kind == "json":
                        dumped_lines.append(value)
                        events = MessageCollector.to_events(value, sm)
                        for ev in events:
                            _write_event(ev)
                        break
                    elif kind == "eof":
                        events = sm.on_eof()
                        for ev in events:
                            _write_event(ev)
                        break
                    elif kind == "exc":
                        _write_event(sm.error_event(str(value)))
                        break
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    raise
                except Exception as e:
                    # finding 6: malformed upstream data (a mid-stream
                    # {"error":"boom"} chunk, a {"type":"text","text":null}
                    # part, ...) used to raise here uncaught -- this loop only
                    # caught connection errors, so the stream died with
                    # neither an error event nor message_stop, just the bare
                    # chunked terminator from the `finally` below. The server
                    # must never die because one request misbehaved.
                    log.warning("malformed upstream stream data: %s", e)
                    _write_event(sm.error_event(f"upstream sent malformed data: {e}"))
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            log.debug("Stream write error: %s", e)
            if result.conn and result.conn.sock:
                try:
                    result.conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        finally:
            dump_debug(SERVER_STATE_DIR, "upstream-stream", {"lines": dumped_lines})
            dump_debug(SERVER_STATE_DIR, "emitted-events", {"events": dumped_events})
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def _handle_passthrough_stream(self, result: UpstreamResult):
        """Raw byte relay of a Databricks Claude passthrough response back to the client (status + headers relayed, body bytes forwarded unparsed)."""
        self.send_response(result.status)
        skip_headers = {"content-length", "connection", "transfer-encoding"}
        for k, v in result.headers.items():
            if k.lower() in skip_headers:
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True

        # For passthrough there is no dialect translation -- what we relay IS
        # what we emit, so both dumps hold the same raw chunks.
        dumped_chunks: list = []

        q = queue.Queue()
        reader = threading.Thread(target=passthrough_reader_thread, args=(result.resp, q))
        reader.daemon = True
        reader.start()

        ping_interval = float(os.environ.get("BRIDGE_PING_INTERVAL", "15"))
        try:
            while True:
                if self._client_gone():
                    raise BrokenPipeError("client disconnected (detected via peek)")
                try:
                    item = q.get(timeout=ping_interval)
                except queue.Empty:
                    # No synthetic pings here -- injecting one would corrupt
                    # the real Anthropic byte stream. Upstream Claude already
                    # sends its own native ping events during long silences.
                    continue
                kind, value = item
                if kind == "raw":
                    dumped_chunks.append(value.decode("utf-8", "replace"))
                    self.write_chunk(value)
                elif kind == "eof":
                    break
                elif kind == "exc":
                    err_ev = {"type": "error", "error": {"type": "overloaded_error", "message": f"upstream: {value}"}}
                    dumped_chunks.append(json.dumps(err_ev))
                    # finding 11: the last relayed read1() chunk may end
                    # mid-event (no trailing blank line yet) -- writing the
                    # synthetic frame straight after it lets the SDK join
                    # "event: error" onto that partial "data:" line and throw
                    # a JSON parse error instead of surfacing our error. A
                    # leading blank line is harmless when the previous chunk
                    # WAS already on an event boundary.
                    self.write_chunk(b"\n\n" + sse_frame("error", err_ev))
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            log.debug("Passthrough stream write error: %s", e)
            if result.conn and result.conn.sock:
                try:
                    result.conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        finally:
            dump_debug(SERVER_STATE_DIR, "upstream-stream", {"lines": dumped_chunks})
            dump_debug(SERVER_STATE_DIR, "emitted-events", {"events": dumped_chunks})
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def _handle_messages_post(self, body: dict):
        """Handle POST /v1/messages."""
        try:
            route = route_model(body["model"], self.headers, SERVER_CFG)
        except InvalidModelError as e:
            self.send_json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            return
        except WebSearchUnavailable:
            self.send_json(400, {"error": {"type": "invalid_request_error",
                                           "message": "web search unavailable via claude-bridge"}})
            return

        dump_debug(SERVER_STATE_DIR, "claude-request", body)

        # -- Databricks Claude passthrough: raw relay, no dialect translation,
        # no overflow/error-shape rewriting (Databricks' native Claude
        # endpoint already speaks Anthropic's own error format). --
        if route.provider == "databricks" and route.dialect == "anthropic-passthrough":
            dbx = SERVER_DBX
            if dbx is None:
                self.send_json(502, {"error": {"type": "api_error", "message": "Databricks not configured"}},
                               {"x-should-retry": "false"})
                return
            root = derive_workspace_root(dbx.host)
            pbody = build_passthrough_body(body, route)
            log_mirrored_headers(extract_custom_headers(self.headers), "databricks passthrough")
            phdrs = build_passthrough_headers(self.headers, dbx.token)
            try:
                result = proxy_anthropic(base_url=f"{root}/ai-gateway/anthropic", api_key=dbx.token,
                                          body=pbody, extra_headers=phdrs, state_dir=SERVER_STATE_DIR)
            except UpstreamConnectError as e:
                status, jbody, hdrs = databricks_unreachable_response(str(e))
                self.send_json(status, jbody, hdrs)
                return
            self._handle_passthrough_stream(result)
            return

        try:
            profile = resolve_profile(route.upstream_model, SERVER_STATE_DIR,
                                       SERVER_CFG.routes if SERVER_CFG else {})
            oai_body = anthropic_to_openai(body, route, profile=profile)
        except WebSearchUnavailable:
            self.send_json(400, {"error": {"type": "invalid_request_error",
                                           "message": "web search unavailable via claude-bridge"}})
            return

        # finding 2: consume a max_tokens ceiling already discovered for this
        # Databricks model (routes-cache.json) up front, instead of paying the
        # same 400 + clamp-retry round trip on every later request -- the
        # cache was being written but never read back.
        if route.provider == "databricks":
            cached_limit = dbx_cache_get_max_tokens_limit(route.upstream_model, SERVER_STATE_DIR)
            if isinstance(cached_limit, int) and isinstance(oai_body.get("max_tokens"), int) \
                    and oai_body["max_tokens"] > cached_limit:
                oai_body["max_tokens"] = cached_limit

        estimate = estimate_tokens(oai_body)
        sm = OpenAIStreamToAnthropic(body.get("model"), estimate)

        # -- Databricks openai-chat and OpenRouter share one retry loop below
        # (connect-error one-shot retry, context-overflow clamp-retry); only
        # how the single upstream call is made, and how a final connect
        # failure is reported, differs per provider. --
        if route.provider == "databricks":
            dbx = SERVER_DBX
            if dbx is None:
                self.send_json(502, {"error": {"type": "api_error", "message": "Databricks not configured"}},
                               {"x-should-retry": "false"})
                return
            root = derive_workspace_root(dbx.host)
            mirrored_headers = extract_custom_headers(self.headers)
            log_mirrored_headers(mirrored_headers, "databricks chat")

            def _call_upstream():
                return call_databricks_chat(base_url=root, api_key=dbx.token, body=oai_body,
                                             extra_headers=mirrored_headers,
                                             state_dir=SERVER_STATE_DIR, model=route.upstream_model)
        else:
            base_url = SERVER_MOCK_BASE_URL or (SERVER_OPENROUTER.base_url if SERVER_OPENROUTER else "")
            api_key = SERVER_OPENROUTER.api_key if SERVER_OPENROUTER else ""
            if not base_url or not api_key:
                self.send_json(502, {"error": {"type": "api_error", "message": "OpenRouter not configured"}},
                               {"x-should-retry": "false"})
                return

            def _call_upstream():
                return call_openai_chat(base_url=base_url, api_key=api_key, body=oai_body,
                                         extra_headers={}, state_dir=SERVER_STATE_DIR)

        max_attempts = 2
        overflow_retries = 0
        result = None
        for attempt in range(max_attempts):
            try:
                result = _call_upstream()
            except UpstreamConnectError as e:
                if attempt == 0:
                    continue
                if route.provider == "databricks":
                    status, jbody, hdrs = databricks_unreachable_response(str(e))
                else:
                    status, jbody, hdrs = map_upstream_error(502, {"error": {"message": str(e)}}, route.provider)
                self.send_json(status, jbody, hdrs)
                return

            if 200 <= result.status < 300:
                break

            # Non-2xx: read the body ONCE (HTTPResponse.read() cannot be
            # replayed) -- call_databricks_chat may already have consumed it
            # itself while checking for a max_tokens-limit wording, in which
            # case it hands the bytes back via body_bytes instead.
            if result.body_bytes is not None:
                raw = result.body_bytes
            else:
                raw = result.resp.read() if result.resp else b""
            try:
                err_obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            except (json.JSONDecodeError, ValueError):
                err_obj = {"error": {"message": raw.decode("utf-8", "replace")}}

            # finding 6: this whole block used to assume err_obj["error"] was
            # always a dict (crashing with AttributeError on a bare
            # {"error": "boom"} body) and that metadata.raw was always a
            # string (crashing with TypeError if it came back as a dict) --
            # BEFORE any response has been sent to the client, so an uncaught
            # exception here dropped the socket with no status at all. Any
            # unexpected failure now maps to a plain 502 instead.
            try:
                err_msg = upstream_error_text(err_obj)
                raw_meta = None
                if isinstance(err_obj, dict):
                    err_field = err_obj.get("error")
                    if isinstance(err_field, dict):
                        meta = err_field.get("metadata")
                        if isinstance(meta, dict):
                            raw_meta = meta.get("raw")

                if result.status == 400:
                    overflow = parse_context_overflow(result.status, err_msg, raw_meta,
                                                       requested_max_tokens=oai_body.get("max_tokens"))
                    if overflow:
                        if overflow.fixable and overflow_retries < 1:
                            overflow_retries += 1
                            oai_body["max_tokens"] = max(1, overflow.limit - overflow.prompt_tokens - 256)
                            continue
                        # Not (or no longer) fixable -- rewrite to the exact
                        # message Claude Code's own compaction regex recognizes,
                        # with T > L guaranteed by build_prompt_too_long_message.
                        total_for_msg = overflow.total
                        if total_for_msg is None:
                            total_for_msg = overflow.prompt_tokens if overflow.prompt_tokens is not None else overflow.limit + 1
                        msg = build_prompt_too_long_message(total_for_msg, overflow.limit)
                        status, jbody, hdrs = map_upstream_error(400, {"error": {"message": msg}}, route.provider)
                        self.send_json(status, jbody, hdrs)
                        return

                status, jbody, hdrs = map_upstream_error(result.status, err_obj, route.provider, result.headers)
            except Exception as e:
                log.warning("malformed upstream error body, mapping to 502: %s", e)
                status, jbody, hdrs = 502, {"error": {"type": "api_error", "message": f"malformed upstream response: {e}"}}, {"x-should-retry": "true"}
            self.send_json(status, jbody, hdrs)
            return
        else:
            status, jbody, hdrs = map_upstream_error(502, {"error": {"message": "upstream failure after retries"}}, route.provider)
            self.send_json(status, jbody, hdrs)
            return

        # Only reachable on a genuine 2xx from the loop's `break` above.
        self._handle_upstream_stream(result, sm)

    def _handle_count_tokens_post(self, body: dict):
        """Handle POST /v1/messages/count_tokens: relay to Databricks passthrough refs, falling back to the local estimate on any non-2xx/connect failure or for every other route."""
        try:
            route = route_model(body.get("model", ""), self.headers, SERVER_CFG)
        except (InvalidModelError, WebSearchUnavailable):
            route = None

        if route is not None and route.provider == "databricks" and route.dialect == "anthropic-passthrough" and SERVER_DBX is not None:
            root = derive_workspace_root(SERVER_DBX.host)
            pbody = build_passthrough_body(body, route)
            phdrs = build_passthrough_headers(self.headers, SERVER_DBX.token)
            try:
                result = call_databricks_count_tokens(base_url=f"{root}/ai-gateway/anthropic", api_key=SERVER_DBX.token,
                                                       body=pbody, extra_headers=phdrs, state_dir=SERVER_STATE_DIR)
                raw = result.resp.read() if result.resp else b""
                if 200 <= result.status < 300:
                    try:
                        parsed = json.loads(raw.decode("utf-8", "replace"))
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict) and "input_tokens" in parsed:
                        self.send_json(200, parsed)
                        return
            except UpstreamConnectError:
                pass

        self.send_json(200, {"input_tokens": estimate_tokens(body)})

    def do_GET(self):
        """Handle GET requests."""
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self.send_json(200, {
                "service": "claude-bridge",
                "version": __version__,
                "hash": bridge_py_sha1(),
                "pid": os.getpid(),
                "port": SERVER_PORT
            })
            return
        token = self.check_auth()
        if token != SERVER_TOKEN:
            self.send_json(401, {"error": {"type": "authentication_error", "message": "invalid or missing token"}})
            return
        if path == "/v1/models":
            self.send_json(200, {"data": []})
        else:
            self.send_json(404, {"error": {"type": "not_found_error", "message": f"path {path} not found"}})

    def do_POST(self):
        """Handle POST requests. Body is always read first (even for auth
        failures / bodyless routes) so a keep-alive socket stays in sync."""
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/v1/messages", "/v1/messages/count_tokens", "/shutdown"):
            self._read_full_body()
            self.send_json(404, {"error": {"type": "not_found_error", "message": f"path {path} not found"}})
            return

        body_data = self._read_full_body()

        token = self.check_auth()
        if token != SERVER_TOKEN:
            self.send_json(401, {"error": {"type": "authentication_error",
                                           "message": "invalid or missing token"}})
            return

        if path == "/shutdown":
            self.send_json(200, {"ok": True})
            threading.Timer(0.2, self.server.shutdown).start()
            return

        if not body_data:
            self.send_json(400, {"error": {"type": "invalid_request_error", "message": "empty body"}})
            return
        try:
            body = json.loads(body_data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.send_json(400, {"error": {"type": "invalid_request_error", "message": "invalid JSON"}})
            return

        if path == "/v1/messages":
            self._handle_messages_post(body)
        elif path == "/v1/messages/count_tokens":
            self._handle_count_tokens_post(body)

    def log_message(self, format, *args):
        """Log messages via bridge logger."""
        log.info(format, *args)


SERVER_STATE_DIR = None
SERVER_PORT = None
SERVER_TOKEN = None
SERVER_CFG = None
SERVER_OPENROUTER = None
SERVER_MOCK_BASE_URL = None
SERVER_DBX = None


def cmd_serve(args):
    """Start the HTTP server."""
    global SERVER_STATE_DIR, SERVER_PORT, SERVER_TOKEN, SERVER_CFG, SERVER_OPENROUTER, SERVER_MOCK_BASE_URL, SERVER_DBX

    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    SERVER_STATE_DIR = Path(args.state_dir) if getattr(args, "state_dir", None) else default_state_dir()
    SERVER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(SERVER_STATE_DIR)

    port = args.port or DEFAULT_PORT
    SERVER_PORT = port
    SERVER_TOKEN = ensure_token(SERVER_STATE_DIR)
    SERVER_CFG = resolve_config()
    # SERVER_OPENROUTER/SERVER_DBX hold the REAL (unredacted) configs used to
    # actually authenticate upstream calls -- SERVER_CFG.openrouter/.databricks
    # are the redacted dicts meant only for `--config` display, never for auth.
    SERVER_OPENROUTER = resolve_openrouter()
    SERVER_DBX = resolve_databricks()
    SERVER_MOCK_BASE_URL = os.environ.get("BRIDGE_OPENROUTER_BASE_URL")

    write_server_json(SERVER_STATE_DIR, os.getpid(), port)

    try:
        server = _Server(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"claude-bridge: cannot bind port {port} -- already in use? ({e})", file=sys.stderr)
        sys.exit(1)
    log.info("Server listening on http://127.0.0.1:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped by keyboard interrupt")
    finally:
        server.server_close()
def find_claude_exe() -> str:
    """Return path to claude executable, raising ClaudeNotFoundError if not found."""
    env_exe = os.environ.get("BRIDGE_CLAUDE_EXE")
    if env_exe:
        return env_exe

    exe = shutil.which("claude.exe")
    if exe:
        return exe

    cmd = shutil.which("claude.cmd")
    if cmd:
        try:
            with open(cmd, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r'"([^"]+claude\.exe)"', content)
            if m:
                raw = m.group(1)
                # npm's generated shims reference a batch-local %dp0% (the
                # shim's own directory, always trailing-backslash) rather
                # than a literal path -- resolve it against the real file.
                dp0 = str(Path(cmd).resolve().parent) + os.sep
                # A callable replacement avoids re.sub() parsing backslashes
                # in the Windows path (dp0) as escape-sequence templates.
                resolved = re.sub(r"%~?dp0%?", lambda _m: dp0, raw, flags=re.IGNORECASE)
                if os.path.exists(resolved):
                    return resolved
                return raw
        except Exception:
            pass

    local_exe = home() / ".local" / "bin" / "claude.exe"
    if local_exe.exists():
        return str(local_exe)

    raise ClaudeNotFoundError("claude executable not found")


def is_server_alive(port: int, expected_hash: str | None) -> bool:
    """Return True if a bridge server is responding on port with matching hash."""
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        if resp.status != 200:
            return False
        data = json.loads(resp.read())
        if expected_hash is not None and data.get("hash") != expected_hash:
            return False
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def check_server_status(port: int, expected_hash: str) -> str:
    """Classify what's on a port: 'absent' (nothing/refused), 'fresh' (claude-bridge, matching hash), 'stale' (claude-bridge, different hash), 'foreign' (something else answered)."""
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException):
        # finding 9: a foreign listener that accepts the TCP connection but
        # never speaks valid HTTP (e.g. answers with garbage, or a status
        # line http.client can't parse) raised http.client.BadStatusLine --
        # a subclass of HTTPException, NOT OSError -- straight through this
        # function and crashed `launch`.
        return "absent"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "foreign"
    if not isinstance(data, dict) or data.get("service") != "claude-bridge":
        return "foreign"
    return "fresh" if data.get("hash") == expected_hash else "stale"


def port_is_free(port: int) -> bool:
    """True if nothing accepts a TCP connection on 127.0.0.1:port right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def spawn_server_detached(port: int, state_dir):
    """Start a bridge server in a detached subprocess."""
    log_path = Path(state_dir) / "server-stdio.log"
    logfile = open(log_path, "ab")
    argv = [sys.executable, __file__, "--serve", "--port", str(port), "--state-dir", str(state_dir)]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": logfile,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        )
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)
    logfile.close()


def _merge_custom_header_lines(process_value: str, chain_value: str) -> list[str]:
    """Merge ANTHROPIC_CUSTOM_HEADERS from the raw process env and the
    settings chain into one ordered list of "Name: Value" lines. The
    settings chain wins on a name collision; a name present only in one side
    is kept as-is; original order is preserved (process-env names first,
    then any new names from the chain) (finding 7)."""
    def parse(value):
        names = []
        values = {}
        for line in (value or "").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, _, val = line.partition(":")
            name = name.strip()
            if not name:
                continue
            if name not in values:
                names.append(name)
            values[name] = val.strip()
        return names, values

    proc_names, proc_values = parse(process_value)
    chain_names, chain_values = parse(chain_value)
    merged = dict(proc_values)
    merged.update(chain_values)
    order = list(proc_names)
    for name in chain_names:
        if name not in order:
            order.append(name)
    return [f"{name}: {merged[name]}" for name in order]


_SECRET_HEADER_NAME_HINTS = ("token", "key", "secret", "auth", "password", "credential")


def log_mirrored_headers(headers: dict, context: str) -> None:
    """Log the NAMES (and, unless the name looks secret, the values) of
    headers about to be mirrored upstream, at INFO -- with nothing logged at
    all when there's nothing to mirror. Without this there was no way to
    confirm from bridge.log that e.g. a work box's
    `x-databricks-use-coding-agent-mode` header actually made it through
    (finding 7)."""
    if not headers:
        return
    parts = []
    for name, value in headers.items():
        if any(hint in name.lower() for hint in _SECRET_HEADER_NAME_HINTS):
            parts.append(f"{name}=<redacted>")
        else:
            parts.append(f"{name}={value}")
    log.info("%s: mirroring %d custom header(s): %s", context, len(headers), ", ".join(parts))


def cmd_launch(args, forwarded_claude_args=None):
    """Launch Claude Code with bridge settings. `forwarded_claude_args` is
    the already-split argv to hand to `claude` untouched -- finding 14 moved
    that split into main()'s _parse_launch_argv (it has to scan the raw
    sys.argv token stream, not this function's already-parsed Namespace)."""
    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    port = args.port or DEFAULT_PORT
    expected_hash = bridge_py_sha1()
    status = check_server_status(port, expected_hash)

    if status == "foreign":
        raise BridgeStartError(
            f"port {port} is already in use by something that is not claude-bridge -- refusing to touch it"
        )

    if status == "stale":
        # A server from an older bridge.py is holding the port -- shut it
        # down with the token it itself wrote to this same state dir, wait
        # for the port to free, then fall through to spawn a fresh one.
        stale_token_path = state_dir / "token"
        stale_token = stale_token_path.read_text(encoding="utf-8").strip() if stale_token_path.exists() else None
        if stale_token:
            conn = None
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/shutdown", headers={"x-api-key": stale_token})
                conn.getresponse().read()
            except OSError:
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        free_deadline = time.time() + 5
        while time.time() < free_deadline and not port_is_free(port):
            time.sleep(0.2)
        status = "absent"

    if status != "fresh":
        # finding 9: don't blindly attempt to spawn a second server -- if
        # /shutdown was refused (wrong/missing token, stale server hung) or
        # an unresponsive-but-listening foreign process still holds the
        # port, the old code spawned anyway and just burned the whole 10s
        # healthz-poll timeout before failing with a generic "did not start"
        # message. Fail fast and name the port instead.
        if not port_is_free(port):
            raise BridgeStartError(
                f"port {port} is still in use (a stale server would not stop, or an unresponsive "
                f"listener is holding it) -- refusing to start a second server"
            )
        spawn_server_detached(port, state_dir)
        deadline = time.time() + 10
        while time.time() < deadline:
            if is_server_alive(port, expected_hash):
                break
            time.sleep(0.2)
        else:
            raise BridgeStartError(f"Server did not start within 10 seconds on port {port}")

    token = ensure_token(state_dir)

    forwarded_claude_args = list(forwarded_claude_args or [])

    user_settings_path = None
    user_settings = {}
    i = 0
    while i < len(forwarded_claude_args):
        arg = forwarded_claude_args[i]
        if arg == "--settings":
            if i + 1 < len(forwarded_claude_args):
                user_settings_path = forwarded_claude_args[i + 1]
                del forwarded_claude_args[i : i + 2]
                break
        elif arg.startswith("--settings="):
            user_settings_path = arg.split("=", 1)[1]
            del forwarded_claude_args[i]
            break
        else:
            i += 1

    if user_settings_path:
        try:
            with open(user_settings_path, "r", encoding="utf-8") as f:
                user_settings = json.load(f)
        except Exception:
            pass

    # finding 15: routes.json's `default`/`small` were accepted (documented
    # even) but no code path ever actually read them -- only `profiles` was
    # wired up. Full fallback chain: --model -> BRIDGE_MODEL -> routes.json
    # default -> built-in; small mirrors it, falling back to the main ref.
    routes_cfg = load_routes(state_dir / "routes.json")
    main_model = (getattr(args, "model", None) or os.environ.get("BRIDGE_MODEL")
                  or routes_cfg.get("default") or "or:deepseek/deepseek-v3.2")
    small_model = (getattr(args, "small_model", None) or os.environ.get("BRIDGE_MODEL_SMALL")
                   or routes_cfg.get("small") or main_model)

    settings = {"model": main_model, "env": {}}
    env = settings["env"]

    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_AUTH_TOKEN"] = token
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        env[key] = main_model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = small_model
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "true"
    env["ANTHROPIC_CUSTOM_MODEL_NAME"] = main_model
    env["ANTHROPIC_CUSTOM_MODEL_DESCRIPTION"] = main_model

    # NOTE: load_settings_env_chain() already returns the flat merged env
    # dict (see its own docstring/SIGNATURES.md) -- do NOT re-index it with
    # .get("env", {}), that would always yield {} and silently drop both the
    # NO_PROXY-preservation and custom-header-mirroring logic below.
    chain_env = load_settings_env_chain(Path.cwd())

    passthrough = is_passthrough_ref(main_model)
    if not passthrough:
        # openai-chat refs need tool search on (128-function cap) and no
        # native "thinking" (nothing implements it on that side yet).
        env["ENABLE_TOOL_SEARCH"] = "true"
        env["MAX_THINKING_TOKENS"] = "0"
    elif chain_env.get("ENABLE_TOOL_SEARCH"):
        # A real Claude model behaves like plain `claude` -- only turn tool
        # search on if the user's own settings chain already wanted it.
        env["ENABLE_TOOL_SEARCH"] = chain_env["ENABLE_TOOL_SEARCH"]

    # finding 10: strip only a LEADING dbx:/or: prefix -- an OpenRouter model
    # id can itself contain a colon (e.g. "qwen/qwen3-coder:free"), and a
    # naive split on the first ':' anywhere mistook that variant suffix for a
    # prefix separator, looking up the wrong models.json key ("free").
    bare_model = main_model
    for _prefix in ("dbx:", "or:"):
        if bare_model.startswith(_prefix):
            bare_model = bare_model[len(_prefix):]
            break
    models_entry = load_models_json(state_dir).get(bare_model) or {}
    if passthrough and not models_entry:
        # A real Claude model with no specific profile on file behaves like
        # plain `claude` -- omit the CLAUDE_CODE_MAX_* overrides entirely
        # instead of silently forcing the openai-chat 128000/16384 defaults
        # onto a model with a much larger real context window, which made a
        # 200k-context Claude session compact at ~100k unlike plain `claude`
        # (finding 10).
        pass
    else:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(models_entry.get("context_length") or 128000)
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(models_entry.get("max_output_tokens") or 16384)
    env["API_TIMEOUT_MS"] = "600000"

    no_proxy_val = chain_env.get("NO_PROXY") or chain_env.get("no_proxy") or ""
    if no_proxy_val and not no_proxy_val.endswith(","):
        no_proxy_val += ","
    no_proxy_val += "127.0.0.1,localhost"
    env["NO_PROXY"] = no_proxy_val
    env["no_proxy"] = no_proxy_val

    # finding 7: merge the settings chain's ANTHROPIC_CUSTOM_HEADERS with
    # whatever the shell itself exported -- a settings `env` value REPLACES
    # (not merges with) the process var once handed to the child, so a
    # shell-exported header (e.g. a work box's
    # `x-databricks-use-coding-agent-mode: true`) used to be silently
    # dropped just because the settings chain didn't also define it. The
    # settings chain wins on a name collision; bridge's own x-bridge-* lines
    # are appended last either way.
    lines = _merge_custom_header_lines(os.environ.get("ANTHROPIC_CUSTOM_HEADERS", ""),
                                        chain_env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
    lines.append(f"x-bridge-main: {main_model}")
    lines.append(f"x-bridge-small: {small_model}")
    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(lines)

    dbx = resolve_databricks()
    if dbx:
        env["WORK_MCP_BASE_URL"] = dbx.host
        env["WORK_MCP_TOKEN"] = dbx.token
        env["DATABRICKS_MCP_BASE_URL"] = dbx.host
        env["DATABRICKS_MCP_TOKEN"] = dbx.token

    if user_settings:
        for k, v in user_settings.items():
            if k == "env" and isinstance(v, dict) and isinstance(settings.get("env"), dict):
                settings["env"].update(v)
            else:
                settings[k] = v

    settings_path = state_dir / f"launch-{os.getpid()}.json"
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f)

    claude_settings_path = home() / ".claude" / "settings.json"
    snapshot_model = None
    snapshot_missing = False
    if claude_settings_path.exists():
        try:
            with open(claude_settings_path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            if "model" in snapshot:
                snapshot_model = snapshot["model"]
        except Exception:
            snapshot_missing = True
    else:
        snapshot_missing = True

    try:
        claude_exe = find_claude_exe()
        child_env = dict(os.environ)
        child_env.pop("CLAUDECODE", None)
        argv = [claude_exe, "--settings", str(settings_path)] + forwarded_claude_args

        if os.name == "nt":
            signal.signal(signal.SIGINT, lambda *_: None)
        p = subprocess.Popen(argv, env=child_env)
        while True:
            try:
                rc = p.wait()
                break
            except KeyboardInterrupt:
                continue
        if rc == -1073741510:
            rc = 130
    finally:
        try:
            settings_path.unlink(missing_ok=True)
        except Exception:
            pass

        if not snapshot_missing:
            try:
                if claude_settings_path.exists():
                    with open(claude_settings_path, "r", encoding="utf-8") as f:
                        current = json.load(f)
                else:
                    current = {}
                if snapshot_model is None:
                    current.pop("model", None)
                else:
                    current["model"] = snapshot_model
                with open(claude_settings_path, "w", encoding="utf-8") as f:
                    json.dump(current, f)
            except Exception:
                pass

    sys.exit(rc)


def cmd_config(args):
    """Print resolved configuration as JSON (no network, secrets redacted)."""
    cfg = resolve_config()
    out = {
        "state_dir": str(cfg.state_dir),
        "openrouter": cfg.openrouter,
        "databricks": cfg.databricks,
        "routes": cfg.routes,
        "env_file_loaded": cfg.env_file_loaded,
    }
    print(json.dumps(out, indent=2))


def cmd_stop(args):
    """Stop a running bridge server. finding 13: POST /shutdown answers 200
    immediately but the real server.shutdown() only fires ~200ms later from a
    background timer -- returning (and printing success) right after the 200
    let a `launch` issued immediately afterward see the dying server still
    answering /healthz and reuse it, handing its child `claude` an
    ECONNREFUSED mid-request. This now polls until the port actually refuses
    connections (up to 5s) before printing anything about success."""
    state_dir = default_state_dir()
    server_json = state_dir / "server.json"
    if not server_json.exists():
        print("No running server found", file=sys.stderr)
        return
    with open(server_json, "r", encoding="utf-8") as f:
        info = json.load(f)
    port = info["port"]
    token_path = state_dir / "token"
    if not token_path.exists():
        print("Token file missing", file=sys.stderr)
        return
    token = token_path.read_text().strip()

    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/shutdown", headers={"x-api-key": token})
        resp = conn.getresponse()
        resp.read()
        status, reason = resp.status, resp.reason
    except ConnectionRefusedError:
        print(f"no bridge server on port {port}")
        return
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if status != 200:
        print(status, reason)
        return

    deadline = time.time() + 5
    while time.time() < deadline and not port_is_free(port):
        time.sleep(0.05)
    print("stopped")


def _parse_launch_argv(argv: list[str]) -> tuple[str | None, str | None, str | None, list[str]]:
    """Parse `launch`'s own argv (everything after the literal "launch"
    token) by scanning from the front: consume --model/--small-model/--port
    (space- or "="-separated) while they lead; stop at the first token that
    doesn't match one of those (an optional literal "--" right there ends
    the scan and is itself consumed). Everything from that point on is
    forwarded to `claude` untouched -- a user `--model` typed after the
    bridge's own flags, or after an explicit "--", still goes to claude.
    Returns (model, small_model, port_str, forwarded_args); each of the
    first three is None when not given.

    A plain argparse.parse_args() cannot do this: it is strict and errors
    out on the first non-bridge-flag token. That mismatch is exactly what
    broke `claude-bridge --model X -p hi` (no "--") from PATH -- the
    wrapper hardcoded `launch -- %*`, so the bridge's own parser only ever
    saw an EMPTY argv (silently using its default model) while
    `--model X -p hi`, including the user's own `--model`, went straight to
    `claude` as ITS argv instead (finding 14)."""
    model = small_model = port_str = None
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--model" and i + 1 < n:
            model = argv[i + 1]
            i += 2
        elif tok.startswith("--model="):
            model = tok.split("=", 1)[1]
            i += 1
        elif tok == "--small-model" and i + 1 < n:
            small_model = argv[i + 1]
            i += 2
        elif tok.startswith("--small-model="):
            small_model = tok.split("=", 1)[1]
            i += 1
        elif tok == "--port" and i + 1 < n:
            port_str = argv[i + 1]
            i += 2
        elif tok.startswith("--port="):
            port_str = tok.split("=", 1)[1]
            i += 1
        else:
            break
    if i < n and argv[i] == "--":
        i += 1
    return model, small_model, port_str, argv[i:]


def main():
    """Main CLI entry point."""
    # Bridge's own flags are only ever recognized BEFORE the first literal
    # "--" (everything after that belongs to the forwarded `claude` args and
    # must never be mistaken for e.g. this tool's own --version/--config).
    # NOTE: this "--" heuristic is NOT used for `launch` (see below) -- a
    # "--" there is optional, and even when present may appear after a run
    # of leading bridge flags rather than immediately after "launch".
    sep = sys.argv.index("--") if "--" in sys.argv else len(sys.argv)
    own_argv = sys.argv[1:sep]

    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--serve", action="store_true", help="Run the bridge server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--state-dir", help="State directory path")
    parser.add_argument("--config", action="store_true", help="Print configuration and exit")
    parser.add_argument("--stop", action="store_true", help="Stop a running server")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--probe", action="store_true", help="Probe Databricks/OpenRouter reachability and cache model list")

    if "--version" in own_argv:
        print(f"claude-bridge {__version__}")
        return 0

    if own_argv and own_argv[0] == "launch":
        # sys.argv[1] == own_argv[0] == "launch" here, so index 1 IS its
        # position -- scan directly from the raw sys.argv, not `own_argv`
        # (whose "up to the first --" cut doesn't apply to `launch`).
        model, small_model, port_str, forwarded_claude_args = _parse_launch_argv(sys.argv[2:])
        try:
            port = int(port_str) if port_str is not None else None
        except ValueError:
            print(f"{PROG}: --port must be an integer, got {port_str!r}", file=sys.stderr)
            return 2
        launch_args = argparse.Namespace(model=model, small_model=small_model, port=port)
        cmd_launch(launch_args, forwarded_claude_args)
        return 0

    args, _remaining = parser.parse_known_args(own_argv)

    if args.config:
        cmd_config(args)
        return 0

    if args.stop:
        cmd_stop(args)
        return 0

    if args.probe:
        cmd_probe(args)
        return 0

    cmd_serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
