"""halo_harness.update -- Halo 2.0.2 round 6: what is installed (PEP 610
`direct_url.json` plus a live checkout's own git HEAD) and what is available
upstream (git ls-remote / GitHub API, cached 24h in `~/.halo/update-check.json`).
Read by `halo --version`, `halo doctor`'s "install" line, `halo update`
(`update_cli.py`), and `/update` (`tui/slash.py`). Every subprocess and HTTP
call goes through `run()`/`http_get_json()` below -- nothing else in this
module (or its callers) shells out or opens a socket directly, so a test can
fake both seams and never touch the real network or install anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

REPO_URL = "https://github.com/roloVibes/Halo-Harness"
API_BASE = "https://api.github.com/repos/roloVibes/Halo-Harness"
DIST_NAME = "halo-harness"
DEFAULT_BRANCH = "master"
CACHE_TTL_S = 24 * 3600
# TUI -> cli.main: run_tui() returning this exact int means "the user chose
# Enter: update and restart halo" -- cli.main runs the update (now past the
# TUI, output visible in the plain terminal) and relaunches with --continue.
# The Windows order is always quit (this), then update, then relaunch --
# never update while any halo process (this one included) still has the
# install open; see update_cli.apply_update/other_halo_pids.
RESTART_EXIT_CODE = 91

# Halo 2.0.2 round C: "update.cache_ttl_s default 3600 for the startup
# note" -- was 24*3600 before this round, applied to EVERY non-refresh
# caller (today, only the startup note is one -- see `latest_available`'s
# own docstring). The config knob's own fallback default, and (unchanged)
# what `tests/test_update.py` seeds a deliberately-expired cache entry
# relative to directly (`upd.CACHE_TTL_S`), so that test adapts to
# whatever this is set to with no edit of its own needed.
CACHE_TTL_S = 3600

_UNSET = object()
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _quote_for_shell(path: str) -> str:
    """Double-quote a real filesystem path for a `shell=True` command on
    both cmd.exe and POSIX shells -- only when it needs it (a space), so
    every existing plain-URL spec (never has one) keeps printing exactly
    as before. Good enough for a checkout's own path (never has an
    embedded `"`)."""
    return f'"{path}"' if path and (" " in path or "\t" in path) else path


def run(cmd, **kwargs):
    """The one subprocess seam this module uses -- tests monkeypatch
    `halo_harness.update.run`, never `subprocess.run` directly."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 10)
    return subprocess.run(cmd, **kwargs)


def http_get_json(url: str, *, timeout: float = 5.0) -> Optional[dict]:
    """The one network seam this module uses -- the GitHub REST API, no
    auth. Never raises; `None` on any failure (offline, rate-limited, a
    malformed body). Tests monkeypatch this, never urllib.

    Round 5e: `urlopen_tls` itself refuses this call under `network.offline`
    (github.com is never loopback/allow-listed) -- caught by the same
    blanket `except Exception` every other failure already was, so the
    update check keeps its pre-5e "just comes back None" contract; one
    DEBUG line names the reason instead of folding silently into "any
    failure"."""
    try:
        import urllib.request
        from halo_harness.providers.http import OfflineBlocked, urlopen_tls
        req = urllib.request.Request(url, headers={"User-Agent": "halo",
                                                     "Accept": "application/vnd.github+json"})
        with urlopen_tls(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OfflineBlocked as e:
        import logging
        logging.getLogger("bridge").debug("update: skipping update check -- %s", e)
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. What is installed
# ---------------------------------------------------------------------------

def _direct_url() -> Optional[dict]:
    try:
        import importlib.metadata as im
        text = im.distribution(DIST_NAME).read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _checkout_root_on_pythonpath() -> Optional[Path]:
    """The checkout this very source file lives in, when it looks like a
    real git repo -- true for a bare PYTHONPATH run (`bin/halo`'s own
    fallback, this repo's test suite) AND for most editable installs
    (their `__file__` already resolves straight back to the checkout);
    `installed_build`'s editable branch prefers direct_url.json's own
    checkout URL first and only falls back to this."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


def _git_head(checkout: Path, run_fn) -> "tuple[Optional[str], Optional[str]]":
    commit = branch = None
    try:
        r = run_fn(["git", "rev-parse", "--short", "HEAD"], cwd=str(checkout),
                   capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            commit = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        r = run_fn(["git", "branch", "--show-current"], cwd=str(checkout),
                   capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            branch = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return commit, branch


def _url_to_path(url: Optional[str]) -> Optional[Path]:
    if not url or not url.startswith("file:"):
        return None
    from urllib.parse import urlparse
    from urllib.request import url2pathname
    parsed = urlparse(url)
    path = url2pathname(parsed.path)
    return Path(path) if path else None


def installed_build(*, direct_url: "object" = _UNSET, run_fn=None) -> dict:
    """{"version", "commit", "requested_revision", "checkout", "branch",
    "editable"}. `commit`/`branch` are short/plain strings or None --
    None everywhere means "could not be determined", never a crash
    (`halo --version`/`doctor` must still print something). `commit` is
    always the short (7-char) form so it compares equal to
    `latest_available()`'s own commits."""
    from halo_harness import __version__
    run_fn = run_fn or run
    info = {"version": __version__, "commit": None, "requested_revision": None,
            "checkout": None, "branch": None, "editable": False}
    d = _direct_url() if direct_url is _UNSET else direct_url
    if d is None:
        checkout = _checkout_root_on_pythonpath()
        if checkout is not None:
            info["checkout"] = str(checkout)
            info["commit"], info["branch"] = _git_head(checkout, run_fn)
        return info
    vcs_info = d.get("vcs_info") if isinstance(d, dict) else None
    dir_info = d.get("dir_info") if isinstance(d, dict) else None
    if isinstance(vcs_info, dict) and vcs_info.get("vcs") == "git":
        commit = vcs_info.get("commit_id")
        info["commit"] = commit[:7] if commit else None
        info["requested_revision"] = vcs_info.get("requested_revision")
    elif isinstance(dir_info, dict):
        # Finding 16: BOTH installers' default (`uv tool install
        # --reinstall .`, no --editable) produce this same `dir_info`
        # shape with no "editable" key at all -- resolving checkout/
        # commit/branch must not depend on that key, or a plain (never
        # editable) checkout install always reports commit=None, which
        # made `halo update`/doctor treat it as "always behind" forever.
        info["editable"] = bool(dir_info.get("editable"))
        checkout = _url_to_path(d.get("url")) or _checkout_root_on_pythonpath()
        if checkout is not None:
            info["checkout"] = str(checkout)
            info["commit"], info["branch"] = _git_head(checkout, run_fn)
    return info


def format_version_line(build: dict) -> str:
    """`halo 2.0.2 (c93480d, master)` -- the branch/revision parenthetical
    only appears once the commit itself is known; branch falls back to the
    git-vcs install's own `requested_revision` when there is no live
    checkout to ask `git branch --show-current`, and is left off entirely
    when NEITHER is known (a `git+URL` install with no explicit ref asked
    for -- true of this very box's own real install)."""
    from halo_harness import __version__
    version = build.get("version") or __version__
    commit = build.get("commit")
    if not commit:
        return f"halo {version}"
    branch = build.get("branch") or build.get("requested_revision")
    return f"halo {version} ({commit}, {branch})" if branch else f"halo {version} ({commit})"


def _uv_tool_has(name: str, run_fn) -> bool:
    if not shutil.which("uv"):
        return False
    try:
        r = run_fn(["uv", "tool", "list"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and name in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _pipx_has(name: str, run_fn) -> bool:
    if not shutil.which("pipx"):
        return False
    try:
        r = run_fn(["pipx", "list"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and f"package {name}" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _spec_from_direct_url(d: dict) -> str:
    """`<spec>` for a reinstall command, kept exactly as the box's own
    install would resolve it: `git+<url>[@<requested_revision>]` for a
    git-vcs install, else the plain checkout path it was installed from."""
    vcs_info = d.get("vcs_info") if isinstance(d, dict) else None
    url = d.get("url") if isinstance(d, dict) else None
    if isinstance(vcs_info, dict) and vcs_info.get("vcs") == "git":
        base = f"git+{url}" if url else f"git+{REPO_URL}"
        rev = vcs_info.get("requested_revision")
        return f"{base}@{rev}" if rev else base
    return url or REPO_URL


def _tool_reinstall_cmd(prefix: str, run_fn, *, editable: bool, path: str = ".") -> str:
    """Which of the three installers THIS install actually used, by the
    same two signals every branch below checks: the tool's own marker file
    sitting right next to the running venv (no TOML/JSON parsing needed --
    existence alone is the uv-tool/pipx tell), or (if that venv isn't the
    one we're even running from right now) `uv tool list`/`pipx list`
    naming this distribution. Falls back to plain pip."""
    path = _quote_for_shell(path)
    if Path(prefix, "uv-receipt.toml").exists() or _uv_tool_has(DIST_NAME, run_fn):
        return f"uv tool install --reinstall{' --editable' if editable else ''} {path}"
    if Path(prefix, "pipx_metadata.json").exists() or _pipx_has(DIST_NAME, run_fn):
        return f"pipx install --force{' -e' if editable else ''} {path}"
    # Finding 16: whichever bare `python` happens to be first on PATH is
    # not necessarily THIS install's own interpreter (a pyenv/conda shim,
    # a different venv) -- `sys.executable` is always the one halo itself
    # is running under right now, same interpreter pip would need to
    # target anyway.
    return f"{_quote_for_shell(sys.executable)} -m pip install --upgrade{' -e' if editable else ''} {path}"


def install_kind(*, direct_url: "object" = _UNSET, prefix: Optional[str] = None, run_fn=None) -> dict:
    """{"kind", "spec", "reinstall_cmd"} -- `kind` is one of "uv_tool",
    "pipx", "pip", "editable_checkout", "dir_checkout", "bare_checkout",
    "source_dir", "unknown".
    `reinstall_cmd` is the exact command `halo update`/doctor's fix line
    runs or prints; always a plain string meant to be run with a shell
    (it may contain `&&`), never argv split for you. `None` for
    "source_dir" and "unknown" -- see those two kinds' own notes."""
    run_fn = run_fn or run
    prefix = sys.prefix if prefix is None else prefix
    d = _direct_url() if direct_url is _UNSET else direct_url
    if d is None:
        checkout = _checkout_root_on_pythonpath()
        if checkout is None:
            # Halo 2.0.2 round 6/C: no dist metadata (`direct_url.json`)
            # AND no `.git` here either -- a plain source directory (the
            # tar copy the test suite runs from being the known real
            # case) rather than a genuine install of any kind. Its own
            # named kind, not "unknown" -- `apply_update`/doctor's
            # install line both say something useful instead of the old
            # bare "could not determine how halo was installed" (there is
            # nothing to `git pull` and no package manager to reinstall
            # through; replacing the directory IS the only real fix).
            return {"kind": "source_dir", "spec": str(Path(__file__).resolve().parent.parent),
                    "reinstall_cmd": None}
        spec = str(checkout)
        # Finding 3: never a bare "git pull" -- `-C <checkout>` keeps the
        # pull tied to the real checkout no matter what directory `halo
        # update`/`/update` is actually invoked from.
        return {"kind": "bare_checkout", "spec": spec,
                "reinstall_cmd": f"git -C {_quote_for_shell(spec)} pull --ff-only"}
    dir_info = d.get("dir_info") if isinstance(d, dict) else None
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        checkout = _url_to_path(d.get("url")) or _checkout_root_on_pythonpath()
        spec = str(checkout) if checkout else "."
        tool_cmd = _tool_reinstall_cmd(prefix, run_fn, editable=True, path=spec)
        git_cmd = f"git -C {_quote_for_shell(spec)} pull --ff-only" if checkout else "git pull"
        return {"kind": "editable_checkout", "spec": spec, "reinstall_cmd": f"{git_cmd} && {tool_cmd}"}
    if isinstance(dir_info, dict):
        # Finding 16: both installers' default (`uv tool install
        # --reinstall .`, no --editable) produce exactly this
        # non-editable local-dir_info shape. Without this branch it fell
        # through to the generic `spec = _spec_from_direct_url(d)` path
        # below, which reinstalls the frozen `file://` snapshot from the
        # moment of install -- no pull, ever -- even though the clone
        # right next to it keeps moving.
        checkout = _url_to_path(d.get("url")) or _checkout_root_on_pythonpath()
        if checkout is not None and (checkout / ".git").exists():
            spec = str(checkout)
            tool_cmd = _tool_reinstall_cmd(prefix, run_fn, editable=False, path=spec)
            git_cmd = f"git -C {_quote_for_shell(spec)} pull --ff-only"
            return {"kind": "dir_checkout", "spec": spec, "reinstall_cmd": f"{git_cmd} && {tool_cmd}"}
    spec = _spec_from_direct_url(d)
    tool_cmd = _tool_reinstall_cmd(prefix, run_fn, editable=False, path=spec)
    kind = "uv_tool" if tool_cmd.startswith("uv ") else ("pipx" if tool_cmd.startswith("pipx") else "pip")
    return {"kind": kind, "spec": spec, "reinstall_cmd": tool_cmd}


# ---------------------------------------------------------------------------
# 2. What is available
# ---------------------------------------------------------------------------

def default_channel(build: Optional[dict] = None) -> str:
    """A config `update.channel` (written by `halo update --channel ...`,
    "remembers it in config" per the brief) always wins first. Otherwise:
    "stable" when the install was pinned to a `v*` tag (`--to v2.0.3`, or
    a release wheel's own `requested_revision`), else "main" (tracks
    whatever branch the install came from, `master` by default)."""
    from halo_harness.theme import get_config_value
    remembered = get_config_value("update.channel", None)
    if remembered in ("stable", "main"):
        return remembered
    build = build if build is not None else installed_build()
    rev = build.get("requested_revision") or build.get("branch") or ""
    return "stable" if _TAG_RE.match(rev) else "main"


def _cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / "update-check.json"


def _load_cache(state_dir: Path) -> dict:
    try:
        data = json.loads(_cache_path(state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(state_dir: Path, cache: dict) -> None:
    path = _cache_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _newest_tag(names) -> Optional[str]:
    best = None
    for name in names or []:
        m = _TAG_RE.match(name or "")
        if not m:
            continue
        key = tuple(int(x) for x in m.groups())
        if best is None or key > best[0]:
            best = (key, name)
    return best[1] if best else None


def _parse_ls_remote(output: str, channel: str) -> Optional[dict]:
    heads, tags = {}, {}
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):  # an annotated tag's own dereferenced-commit line
            ref = ref[:-3]
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha
        elif ref.startswith("refs/tags/"):
            tags[ref[len("refs/tags/"):]] = sha
    if channel == "stable":
        tag = _newest_tag(list(tags.keys()))
        return {"channel": channel, "commit": tags[tag][:7], "ref": tag, "reason": None} if tag else None
    sha = heads.get(DEFAULT_BRANCH)
    return {"channel": channel, "commit": sha[:7], "ref": DEFAULT_BRANCH, "reason": None} if sha else None


def _fetch_latest(channel: str, *, run_fn, fetch_json) -> dict:
    if shutil.which("git"):
        try:
            r = run_fn(["git", "ls-remote", "--heads", "--tags", REPO_URL],
                       capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                parsed = _parse_ls_remote(r.stdout, channel)
                if parsed:
                    return parsed
        except (OSError, subprocess.SubprocessError):
            pass
    if channel == "stable":
        data = fetch_json(f"{API_BASE}/tags", timeout=5)
        names = [t.get("name") for t in data if isinstance(t, dict)] if isinstance(data, list) else []
        tag = _newest_tag(names)
        if tag:
            sha = next((t.get("commit", {}).get("sha") for t in data if t.get("name") == tag), None)
            if sha:
                return {"channel": channel, "commit": sha[:7], "ref": tag, "reason": None}
        return {"channel": channel, "commit": None, "ref": None, "reason": "no tags found"}
    data = fetch_json(f"{API_BASE}/commits/{DEFAULT_BRANCH}", timeout=5)
    if isinstance(data, dict) and data.get("sha"):
        return {"channel": channel, "commit": data["sha"][:7], "ref": DEFAULT_BRANCH, "reason": None}
    return {"channel": channel, "commit": None, "ref": None, "reason": "network unavailable"}


def latest_available(channel: Optional[str] = None, *, refresh: bool = False,
                      state_dir: Optional[Path] = None, build: Optional[dict] = None,
                      run_fn=None, fetch_json=None) -> dict:
    """{"channel", "commit", "ref", "reason", "source"} -- never raises,
    `commit` is None with a `reason` when nothing could be determined
    (offline, no git, rate-limited). Cached per channel in
    `~/.halo/update-check.json`, for `update.cache_ttl_s` seconds (config,
    default `CACHE_TTL_S` -- Halo 2.0.2 round C: "the 24h cache is for the
    startup note only, and its TTL should be 1h" -- this is the ONLY
    passive/launch-time caller, `tui/app.py`'s `_update_check_startup_
    worker`; every explicit caller, `--check`/`/update`, now passes
    `refresh=True` instead of relying on a short TTL alone). `refresh=True`
    ignores the cache's own freshness entirely and always queries live
    (still 5s-capped -- see `_fetch_latest`'s own `timeout=5` call sites)
    -- but the cache is still consulted as the FALLBACK when that live
    query genuinely fails (offline, rate-limited): a stale-but-real
    answer beats reporting "unknown" when something WAS known moments
    ago. Honours `BRIDGE_TEST_NO_BACKGROUND_NET=1` and config `update.
    check: false` -- both return the LAST cached answer (or "unknown")
    rather than ever touching the network from a test."""
    from halo_harness.config.paths import bridge_home, background_net_disabled
    from halo_harness.theme import get_config_value
    run_fn = run_fn or run
    fetch_json = fetch_json or http_get_json
    state_dir = state_dir if state_dir is not None else bridge_home()
    if channel is None:
        channel = default_channel(build)
    cache = _load_cache(state_dir)
    cached = cache.get(channel) if isinstance(cache, dict) else None
    now = time.time()
    try:
        cache_ttl_s = float(get_config_value("update.cache_ttl_s", default=CACHE_TTL_S))
    except (TypeError, ValueError):
        cache_ttl_s = CACHE_TTL_S
    if not refresh and isinstance(cached, dict) and (now - cached.get("checked_at", 0)) < cache_ttl_s:
        return {**cached, "source": "cache"}
    checking_disabled = get_config_value("update.check", True) is False
    if checking_disabled or background_net_disabled():
        if isinstance(cached, dict):
            return {**cached, "source": "cache"}
        reason = "update.check is off" if checking_disabled else "background network disabled"
        return {"channel": channel, "commit": None, "ref": None, "reason": reason, "source": "disabled"}
    result = _fetch_latest(channel, run_fn=run_fn, fetch_json=fetch_json)
    if result.get("commit") is None and isinstance(cached, dict):
        # round C: the live query (5s-capped) genuinely failed -- fall
        # back to the last known-good cached answer rather than reporting
        # "unknown" over something that WAS known moments ago. Never
        # overwrites the cache file with this failure either (the stale
        # entry is still the most recent REAL data this channel has).
        return {**cached, "source": "cache"}
    result["checked_at"] = now
    cache[channel] = result
    _save_cache(state_dir, cache)
    return {**result, "source": "live"}


def commit_is_ancestor(old_commit: Optional[str], new_commit: Optional[str], *, checkout: Optional[Path] = None,
                        run_fn=None) -> Optional[bool]:
    """Halo 2.0.2 round C: "'differs from <channel>' when the ordering
    is unknown and `git merge-base --is-ancestor` when a checkout
    exists" -- without this, `old_commit != new_commit` alone was always
    presented as "an update is available" (old is BEHIND new), which is
    simply assumed, never actually checked; a diverged or rebased-
    backward local checkout (or one deliberately pinned to an older/
    different ref) would print a misleading "update available" for a
    commit it can never cleanly fast-forward onto. True when `old_commit`
    really is a git ancestor of `new_commit` in a real local checkout
    (the ordinary, overwhelmingly common "genuinely behind" case); False
    when a checkout IS available and confirms it is NOT (`git merge-base
    --is-ancestor`'s own exit code 1) -- a real, checked "no" rather than
    a guess. None when there is no local checkout to ask at all (every
    caller keeps its EXISTING "assume available means ahead" wording in
    that case, unchanged -- a plain uv_tool/pip install's own commit
    comes from querying this SAME remote branch, so there is no new
    ambiguity to introduce just because there is no local git history to
    double-check it against), or when either commit is missing/equal."""
    run_fn = run_fn or run
    if checkout is None:
        checkout = _checkout_root_on_pythonpath()
    if checkout is None or not old_commit or not new_commit or old_commit == new_commit:
        return None
    try:
        r = run_fn(["git", "merge-base", "--is-ancestor", old_commit, new_commit],
                   cwd=str(checkout), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    # `git merge-base --is-ancestor` exits 0 (yes)/1 (no) by design; any
    # OTHER code (128 "not a valid object", a shallow clone missing one
    # of the two commits, ...) means the question genuinely couldn't be
    # answered here, not a confirmed "no" -- stays None, never a false
    # "diverged".
    return True if r.returncode == 0 else (False if r.returncode == 1 else None)


def commits_between(old_commit: Optional[str], new_commit: Optional[str], *, checkout: Optional[Path] = None,
                     limit: int = 15, run_fn=None, fetch_json=None) -> "tuple[list, Optional[int]]":
    """(lines, count) -- up to `limit` `git log --oneline` lines (newest
    first) from a real local checkout when one is available, else an empty
    list with just the ahead-by `count` from the GitHub compare API.
    `(None, None)`-shaped as `([], None)` when neither source can answer
    (no checkout, no network, or either commit unknown)."""
    run_fn = run_fn or run
    if checkout is None:
        checkout = _checkout_root_on_pythonpath()
    if checkout is not None and old_commit and new_commit:
        try:
            r = run_fn(["git", "log", "--oneline", f"-{limit}", f"{old_commit}..{new_commit}"],
                       cwd=str(checkout), capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
                return lines, len(lines)
        except (OSError, subprocess.SubprocessError):
            pass
    if old_commit and new_commit:
        fetch_json = fetch_json or http_get_json
        data = fetch_json(f"{API_BASE}/compare/{old_commit}...{new_commit}", timeout=5)
        if isinstance(data, dict) and isinstance(data.get("ahead_by"), int):
            return [], data["ahead_by"]
    return [], None


def note_due_today(state_dir: Path) -> bool:
    """True at most once per calendar day (UTC) -- flips the cache's own
    `notified_date` immediately, so a second call the same day (another
    launch, a concurrent worker) returns False without showing the note
    twice."""
    import datetime
    today = datetime.date.today().isoformat()
    cache = _load_cache(state_dir)
    if cache.get("notified_date") == today:
        return False
    cache["notified_date"] = today
    _save_cache(state_dir, cache)
    return True


# ---------------------------------------------------------------------------
# 3. Applying an update: another halo process, and the relaunch itself
# ---------------------------------------------------------------------------

def _is_halo_cmdline(cmdline: str) -> bool:
    """True only for a process that IS halo: the literal `halo`/`halo.exe`
    entry point as any whitespace-separated token (by its own basename,
    so a full path still matches), or a `-m halo_harness`-style module
    invocation -- never a mere substring hit. Finding 2: the old `"halo"
    in cmdline.lower()` also matched an editor window titled after this
    repo (`...\\Halo-Harness`) and `tail -f ~/.halo/bridge.log`; neither
    names a `halo`/`halo.exe` TOKEN, just a longer word/path containing
    those letters."""
    for tok in (cmdline or "").split():
        if tok == "halo_harness":
            return True
        name = tok.strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in ("halo", "halo.exe"):
            return True
    return False


def _ancestor_pids(this_pid: int, pid_to_ppid: "dict[int, int]") -> "set[int]":
    """`this_pid` plus every PID above it in the process tree (parent,
    grandparent, ...). Finding 2: on Windows, a `uv tool`/pipx/pip
    console-script install's own launcher stub (`~/.local/bin/halo.exe`)
    stays alive as the PARENT of the real `python.exe` that runs halo --
    that parent's own command line also names `halo`/`halo.exe` (it IS
    one), so excluding only `os.getpid()` left it counted as "another"
    halo process, and every real `halo update`/`/update` on Windows
    refused forever. Stops at the first pid missing from the map (the
    root, or a race where ps/CIM already lost it) or on a cycle
    (defensive; real process trees never have one)."""
    seen = {this_pid}
    pid = pid_to_ppid.get(this_pid)
    while pid is not None and pid not in seen:
        seen.add(pid)
        pid = pid_to_ppid.get(pid)
    return seen


def other_halo_pids(*, run_fn=None) -> "list[int]":
    """Every OTHER process on this machine that looks like a `halo`
    invocation (by command line) -- this process AND its whole ancestor
    chain (see `_ancestor_pids`) always excluded, never just `os.getpid()`
    -- `update_cli.apply_update`'s own refusal check. Best-effort: any
    failure to even list processes returns `[]` (the brief's refusal is a
    courtesy, never a hard guarantee this can't still race)."""
    run_fn = run_fn or run
    this_pid = os.getpid()
    pids: "list[int]" = []
    try:
        if os.name == "nt":
            r = run_fn(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | "
                        "ConvertTo-Json -Compress"], capture_output=True, text=True, timeout=10)
            rows = json.loads(r.stdout) if (r.stdout or "").strip() else []
            rows = [rows] if isinstance(rows, dict) else (rows if isinstance(rows, list) else [])
            pid_to_ppid = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid, ppid = row.get("ProcessId"), row.get("ParentProcessId")
                if pid is not None and ppid is not None:
                    pid_to_ppid[int(pid)] = int(ppid)
            excluded = _ancestor_pids(this_pid, pid_to_ppid)
            for row in rows:
                pid = row.get("ProcessId") if isinstance(row, dict) else None
                if pid and int(pid) not in excluded and _is_halo_cmdline(row.get("CommandLine")):
                    pids.append(int(pid))
        else:
            r = run_fn(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True, timeout=10)
            rows = []  # (pid, args)
            pid_to_ppid = {}
            for line in (r.stdout or "").splitlines()[1:]:
                parts = line.strip().split(None, 2)
                if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                    continue
                pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
                rows.append((pid, args))
                pid_to_ppid[pid] = ppid
            excluded = _ancestor_pids(this_pid, pid_to_ppid)
            for pid, args in rows:
                if pid not in excluded and _is_halo_cmdline(args):
                    pids.append(pid)
    except Exception:
        return []
    return pids


def relaunch_halo(extra_args: "list[str]", *, exec_fn: Optional[Callable] = None,
                   call_fn: Optional[Callable] = None) -> int:
    """Replaces THIS process with a fresh `halo <extra_args>` (`--continue`
    is always one of them, from the caller) -- never returns on a real
    `exec_fn` (the default, `os.execv`, POSIX only -- see below).
    `exec_fn`/`call_fn` are the test seams: a fake records the call and
    returns instead of truly exec'ing/launching, which is why this still
    has a (dead-code-in-production-on-POSIX) `return` for `exec_fn` to
    see.

    2.0.2 review finding 30: `os.execv` REPLACES this process outright --
    on Windows, that also exits the uv launcher `halo.exe` wrapper that
    started this one (its own parent), since replacing a process image
    doesn't keep a PARENT alive. The shell then takes the console back
    immediately, racing the relaunched TUI that's also trying to read/
    write it. `exec_fn` passed explicitly (every existing test) always
    wins outright, on any platform -- only the DEFAULT varies: on Windows
    it's `sys.exit(subprocess.call(argv))` (waits for a real child, same
    as the uv launcher itself already does for every other halo
    invocation), on POSIX it stays `os.execv`."""
    halo_path = shutil.which("halo") or shutil.which("halo.exe")
    if halo_path:
        argv = [halo_path, *extra_args]
    else:
        halo_path = sys.executable
        argv = [halo_path, "-m", "halo_harness", *extra_args]
    if exec_fn is None and sys.platform == "win32":
        call_fn = call_fn or subprocess.call
        sys.exit(call_fn(argv))
    exec_fn = exec_fn or os.execv
    exec_fn(halo_path, argv)
    return 0
