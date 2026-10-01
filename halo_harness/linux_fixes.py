"""halo_harness.linux_fixes -- the two Linux setup fixes `doctor` has always
known about (RECOMMENDATIONS.md P0/H12 brief Part A step 6): a static `rg`
install into `~/.local/bin`, and the `~/.local/bin`-on-PATH rc-file line for
a non-interactive shell. Shared by `halo init` (which actually
performs them) and `doctor.py` (which only reports whether they're needed,
with the exact fix command). Every function here is a plain, injectable-seam
helper -- nothing prompts, nothing touches a shell rc file or downloads
anything unless explicitly called to do so, and every network/subprocess
call is best-effort (never raises; a failure degrades to a clear fallback).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# ~/.local/bin on PATH for a non-interactive shell
# ---------------------------------------------------------------------------

PATH_MARKER = "# halo: put ~/.local/bin on PATH (added by `halo init`)"
# 2.0.0 fixpass finding 6: a line 1.0.1's `rolo-claude init` already
# appended carries THIS marker -- `ensure_local_bin_on_rc`'s presence check
# must still recognize it, or a 2.0.0 `halo init` re-run appends a SECOND
# PATH block right below the first one instead of treating it as already
# done. Only ever CHECKED, never written -- a fresh append always writes
# PATH_MARKER (the new one).
PATH_MARKER_LEGACY = "# rolo-claude: put ~/.local/bin on PATH (added by `rolo-claude init`)"
PATH_LINE = 'export PATH="$HOME/.local/bin:$HOME/bin:$PATH"'


def _home(home: Optional[Path] = None) -> Path:
    return home if home is not None else Path(os.environ.get("HOME") or Path.home())


def rc_file_for_shell(shell: Optional[str] = None, *, home: Optional[Path] = None) -> Path:
    """`~/.zshenv` for zsh, `~/.profile` for bash/anything else -- chosen
    from `$SHELL` (brief: "rc-file line for zsh (~/.zshenv) or bash
    (~/.profile) chosen from $SHELL"). `.zshenv` (not `.zshrc`) is read by
    EVERY zsh invocation, including a non-interactive/non-login one (a cron
    job, `ssh host cmd`, a subprocess) -- exactly the case this fix targets."""
    shell = shell if shell is not None else os.environ.get("SHELL", "")
    name = Path(shell).name if shell else ""
    if "zsh" in name:
        return _home(home) / ".zshenv"
    return _home(home) / ".profile"


def local_bin_on_noninteractive_path(*, shell: Optional[str] = None, home: Optional[Path] = None,
                                      run: Optional[Callable] = None) -> bool:
    """True iff `~/.local/bin` is already on PATH for a freshly-started
    NON-interactive `$SHELL` (re-invoked with `-c 'echo $PATH'`, never
    trusting THIS process's own inherited PATH, which may already have been
    widened by whatever interactive shell launched halo itself).
    `run` is a test seam (defaults to a real bounded subprocess call);
    any failure to even run the shell degrades to checking this process's
    own os.environ PATH instead of raising."""
    local_bin = str(_home(home) / ".local" / "bin")
    shell_path = shell if shell is not None else (os.environ.get("SHELL") or "/bin/sh")
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5))
    try:
        proc = run([shell_path, "-c", "echo $PATH"])
        path_value = (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        path_value = ""
    if not path_value:
        path_value = os.environ.get("PATH", "")
    return local_bin in (path_value or "").split(os.pathsep)


def ensure_local_bin_on_rc(*, shell: Optional[str] = None, home: Optional[Path] = None) -> "tuple[bool, Path]":
    """Idempotently appends `PATH_MARKER`+`PATH_LINE` to the rc file
    `rc_file_for_shell` picks -- a no-op (returns `(False, path)`) when the
    marker is already present, so calling this on every `halo init`
    re-run never duplicates the line. Preserves every existing line in the
    file. Returns `(written_this_call, path)`."""
    path = rc_file_for_shell(shell, home=home)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    if PATH_MARKER in existing or PATH_MARKER_LEGACY in existing:
        return False, path
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{sep}{PATH_MARKER}\n{PATH_LINE}\n")
    return True, path


# ---------------------------------------------------------------------------
# Static ripgrep install
# ---------------------------------------------------------------------------

RIPGREP_LATEST_API = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"

_ARCH_ASSET_SUFFIX = {
    ("x86_64", "linux"): "x86_64-unknown-linux-musl.tar.gz",
    ("amd64", "linux"): "x86_64-unknown-linux-musl.tar.gz",
    ("aarch64", "linux"): "aarch64-unknown-linux-gnu.tar.gz",  # ripgrep ships no linux-arm64-musl asset
    ("arm64", "linux"): "aarch64-unknown-linux-gnu.tar.gz",
}

_PACKAGE_MANAGER_HINTS = (
    ("apt", "sudo apt install ripgrep"), ("dnf", "sudo dnf install ripgrep"),
    ("pacman", "sudo pacman -S ripgrep"), ("apk", "sudo apk add ripgrep"),
    ("zypper", "sudo zypper install ripgrep"),
)


def ripgrep_fallback_message() -> str:
    """The exact package-manager command for whichever one is actually on
    this PATH; a generic pointer to ripgrep's own install docs when none
    of the common ones are found."""
    for exe, cmd in _PACKAGE_MANAGER_HINTS:
        if shutil.which(exe):
            return cmd
    return "install ripgrep via your package manager (see https://github.com/BurntSushi/ripgrep#installation)"


def ripgrep_asset_suffix(*, system: str, machine: str) -> Optional[str]:
    """The release-asset filename SUFFIX (after `ripgrep-<version>-`) for
    this (system, machine) pair, preferring a static musl build so the
    binary runs on any glibc version -- None for a combination ripgrep
    doesn't publish a prebuilt asset for (Linux-only: this fix is never
    offered on Windows/macOS -- see `install_static_ripgrep`'s own guard)."""
    if (system or "").lower() != "linux":
        return None
    return _ARCH_ASSET_SUFFIX.get(((machine or "").lower(), "linux"))


def pick_ripgrep_asset(release_json: dict, *, system: str, machine: str) -> Optional[dict]:
    """The one asset dict (`{"name": ..., "browser_download_url": ...}`)
    matching this (system, machine) out of a GitHub releases API response's
    own `assets` list; None when there's no matching suffix or no asset
    whose name ends with it."""
    suffix = ripgrep_asset_suffix(system=system, machine=machine)
    if not suffix:
        return None
    for asset in (release_json.get("assets") or []):
        name = asset.get("name") if isinstance(asset, dict) else None
        if isinstance(name, str) and name.endswith(suffix):
            return asset
    return None


def _default_fetch_json(url: str) -> dict:
    # 1.0.1 hotfix 11: urlopen_tls -- see providers/http.py's own docstring.
    import json
    import urllib.request
    from halo_harness.providers.http import urlopen_tls
    req = urllib.request.Request(url, headers={"User-Agent": "halo"})
    with urlopen_tls(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _default_fetch_bytes(url: str) -> bytes:
    import urllib.request
    from halo_harness.providers.http import urlopen_tls
    req = urllib.request.Request(url, headers={"User-Agent": "halo"})
    with urlopen_tls(req, timeout=60) as resp:
        return resp.read()


def _default_verify(path: Path) -> bool:
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_static_ripgrep(dest_dir: Path, *, system: Optional[str] = None, machine: Optional[str] = None,
                            fetch_json: Optional[Callable[[str], dict]] = None,
                            fetch_bytes: Optional[Callable[[str], bytes]] = None,
                            verify: Optional[Callable[[Path], bool]] = None) -> "tuple[bool, str]":
    """`(installed, message)`. Downloads ripgrep's own latest static-musl
    Linux release asset for this machine's architecture straight from
    ripgrep's GitHub releases (never a package manager), extracts `rg` into
    `dest_dir` (mode 0755), and smoke-checks it actually runs before
    declaring success. `system`/`machine`/`fetch_json`/`fetch_bytes`/
    `verify` are test seams -- a real caller leaves every one at its live
    default. Never raises: an unknown arch, no network, a malformed
    archive, or a binary that doesn't actually run once extracted all
    return `(False, ripgrep_fallback_message())` instead -- `message` is
    the extracted binary's path on success, the fallback command otherwise."""
    system = system if system is not None else platform.system()
    machine = machine if machine is not None else platform.machine()
    fetch_json = fetch_json or _default_fetch_json
    fetch_bytes = fetch_bytes or _default_fetch_bytes
    verify = verify or _default_verify
    try:
        release = fetch_json(RIPGREP_LATEST_API)
        asset = pick_ripgrep_asset(release if isinstance(release, dict) else {}, system=system, machine=machine)
        if asset is None:
            return False, ripgrep_fallback_message()
        data = fetch_bytes(asset["browser_download_url"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="halo-rg-") as tmp:
            archive_path = Path(tmp) / asset["name"]
            archive_path.write_bytes(data)
            with tarfile.open(archive_path, "r:gz") as tf:
                member = next((m for m in tf.getmembers() if Path(m.name).name == "rg"), None)
                if member is None:
                    return False, ripgrep_fallback_message()
                extracted = tf.extractfile(member)
                if extracted is None:
                    return False, ripgrep_fallback_message()
                payload = extracted.read()
        out_path = dest_dir / "rg"
        out_path.write_bytes(payload)
        out_path.chmod(0o755)
        if not verify(out_path):
            return False, ripgrep_fallback_message()
        return True, str(out_path)
    except Exception:
        return False, ripgrep_fallback_message()
