"""rolo_claude.providers.config -- environment discovery, OpenRouter/
Databricks config resolution, the settings-env chain, logging setup, and a
handful of small pure helpers (jdumps/estimate_tokens/dump_debug). Moved out
of bridge.py unchanged in the H0 package split; see wip/SIGNATURES.md part1
("Constants / env") for the full contract. No behavior change from v0.2.1
except bridge_py_sha1(), which now hashes the whole bridge.py + providers
tree instead of just this one file (see its docstring).
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from rolo_claude import __version__

def home() -> Path:
    """Return BRIDGE_TEST_HOME if set, else user home directory."""
    if "BRIDGE_TEST_HOME" in os.environ:
        return Path(os.environ["BRIDGE_TEST_HOME"])
    return Path.home()


def default_state_dir() -> Path:
    """Return BRIDGE_STATE_DIR if set, else ~/.rolo-claude (rolo-claude harness data dir; shared with the harness's own session/MCP/trust state -- see config/paths.py)."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return Path(os.environ["BRIDGE_STATE_DIR"])
    return home() / ".rolo-claude"


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
    """Tree hash: sha1 over the running proxy's bridge.py bytes, followed by
    every rolo_claude/providers/*.py file's bytes in sorted-filename order
    (each entry prefixed by its own name so a rename changes the digest too).

    bridge.py's path is resolved from sys.modules["__main__"] whenever the
    running process WAS launched as a bridge.py-shaped script (detected via
    the cmd_serve attribute, which only a real -- or byte-mutated -- copy of
    bridge.py defines, regardless of what the file is actually named) so a
    byte-mutated copy run directly for the launcher's stale-server-restart
    test hashes as ITSELF, not the installed original. Every other caller
    (``python -m rolo_claude ...``, a direct ``import bridge``) falls back
    to the real bridge.py installed as this package's sibling, which the
    packaging layout guarantees (py-modules=["bridge"] alongside the
    rolo_claude package, in the repo root or in site-packages alike).
    """
    providers_dir = Path(__file__).resolve().parent
    main_mod = sys.modules.get("__main__")
    main_file = getattr(main_mod, "__file__", None)
    if main_mod is not None and hasattr(main_mod, "cmd_serve") and main_file:
        bridge_path = Path(main_file)
    else:
        bridge_path = providers_dir.parents[1] / "bridge.py"
    h = hashlib.sha1()
    try:
        h.update(b"bridge.py\x00")
        h.update(bridge_path.read_bytes())
        for p in sorted(providers_dir.glob("*.py")):
            h.update(p.name.encode("utf-8") + b"\x00")
            h.update(p.read_bytes())
    except OSError:
        return "0" * 40
    return h.hexdigest()


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
