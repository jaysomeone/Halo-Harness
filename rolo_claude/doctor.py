"""rolo_claude.doctor -- `rolo-claude doctor` subcommand + the `/doctor`
slash command's shared implementation (U0 scope A). Checks: Python version,
`~/.claude` layout, the env file, an OpenRouter key, Databricks discovery,
`claude.exe`/`claude` on PATH (needed for `--chrome`), node/npx on PATH
(needed for `--playwright`), and a WSL/Kali hint. Read-only: never writes
anything, never raises on a missing/misconfigured piece -- each check
degrades to a "not configured" line instead.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

OK, WARN, MISSING = "[OK]", "[WARN]", "[MISSING]"


def _check_python() -> str:
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    status = OK if (info.major, info.minor) >= (3, 9) else WARN
    return f"{status} Python {version}"


def _check_claude_layout() -> list:
    from rolo_claude.config.paths import claude_config_dir, claude_json_path
    lines = []
    cfg_dir = claude_config_dir()
    lines.append(f"{OK if cfg_dir.is_dir() else MISSING} ~/.claude directory: {cfg_dir}")
    settings_path = cfg_dir / "settings.json"
    lines.append(f"{OK if settings_path.exists() else WARN} settings.json: {settings_path}")
    cj_path = claude_json_path()
    lines.append(f"{OK if cj_path.exists() else WARN} .claude.json: {cj_path}")
    return lines


def _check_env_file() -> str:
    from rolo_claude.config.paths import home
    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    return f"{OK if env_path.exists() else WARN} env file: {env_path}"


def _load_env_file_best_effort() -> None:
    """Same env file `run_print_mode` loads (`BRIDGE_ENV_FILE` or
    `~/.config/vibes-hacker/env`) -- without this, doctor would report a
    key "not configured" even when the real harness would happily find it
    there (an env-var-only check would silently disagree with reality)."""
    try:
        import os
        from rolo_claude.config.paths import home
        from rolo_claude.providers.config import load_env_file
        load_env_file(Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env")))
    except Exception:
        pass


def _check_openrouter() -> str:
    _load_env_file_best_effort()
    try:
        from rolo_claude.providers.config import resolve_openrouter
        orc = resolve_openrouter()
    except Exception as e:  # never let a doctor check crash the whole command
        return f"{WARN} OpenRouter: could not check ({type(e).__name__}: {e})"
    if orc is None:
        return f"{WARN} OpenRouter: not configured (no OPENROUTER_API_KEY found)"
    return f"{OK} OpenRouter: key found ({orc.base_url})"


def _check_databricks() -> str:
    _load_env_file_best_effort()
    try:
        from rolo_claude.providers.config import resolve_databricks
        dbx = resolve_databricks()
    except Exception as e:
        return f"{WARN} Databricks: could not check ({type(e).__name__}: {e})"
    if dbx is None:
        return f"{WARN} Databricks: not configured (no host/token found)"
    return f"{OK} Databricks: configured ({dbx.host})"


def _check_chrome() -> str:
    found = shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
    if found:
        return f"{OK} claude on PATH (needed for --chrome): {found}"
    return f"{WARN} claude not found on PATH -- --chrome will print its not-yet-supported line"


def _check_playwright() -> str:
    node = shutil.which("node")
    npx = shutil.which("npx")
    if node and npx:
        return f"{OK} node/npx on PATH (needed for --playwright): {node}"
    missing = ", ".join(n for n, p in (("node", node), ("npx", npx)) if not p)
    return f"{WARN} missing on PATH for --playwright: {missing}"


def _check_platform() -> str:
    system = platform.system()
    if system == "Linux":
        try:
            release = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            release = ""
        if "microsoft" in release:
            return f"{OK} Linux (WSL) -- {platform.release()}"
        try:
            os_release = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            os_release = ""
        if "kali" in os_release:
            return f"{OK} Linux (Kali) -- rolo-claude's primary target platform"
        return f"{OK} Linux -- {platform.release()}"
    if system == "Windows":
        wsl = shutil.which("wsl")
        hint = " (wsl.exe found -- verify parity there too)" if wsl else " (no wsl.exe found on PATH)"
        return f"{OK} Windows -- build/test host, not the primary target{hint}"
    return f"{OK} {system} -- {platform.release()}"


def run_checks(cwd: Optional[Path] = None) -> "tuple[list, bool]":
    """Returns (lines, ok) -- `ok` is True iff nothing came back MISSING
    (a WARN is informational, e.g. "no Databricks configured", and never
    fails doctor as a whole)."""
    lines = [_check_python()]
    lines.extend(_check_claude_layout())
    lines.append(_check_env_file())
    lines.append(_check_openrouter())
    lines.append(_check_databricks())
    lines.append(_check_chrome())
    lines.append(_check_playwright())
    lines.append(_check_platform())
    ok = not any(line.startswith(MISSING) for line in lines)
    return lines, ok


def cmd_doctor(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude doctor", add_help=True,
                                      description="Check the health of your rolo-claude installation.")
    parser.parse_args(argv)
    lines, ok = run_checks()
    print("rolo-claude doctor")
    for line in lines:
        print(f"  {line}")
    return 0 if ok else 1
