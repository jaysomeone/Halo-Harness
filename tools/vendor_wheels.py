"""tools/vendor_wheels.py -- H8 scope F: pre-download manylinux cp311/cp312/
cp313 (H9 whole-tree review finding 23: Debian 13 and Kali rolling both
ship 3.13) wheels for halo's full pinned dependency closure
(`requirements.lock`,
`uv pip compile pyproject.toml --universal -o requirements.lock`) into a
local directory, so a later `pip install --no-index --find-links=wheels -e .`
on a machine with NO PyPI access at all -- only a VPN path to Databricks --
still installs cleanly. That's the "work box" this exists for (see
`docs/harness/INSTALL.md`'s "Work box (offline install)" section).

Run this on a machine WITH internet access (the Windows build host is
fine -- pip's `--platform`/`--python-version`/`--implementation`/`--abi`
flags make it download wheels for a DIFFERENT target than the host actually
running pip), then copy the resulting `wheels/` directory to the work box
over whatever side channel is available there (a shared drive, `scp` once
the VPN is up, a USB stick -- this script doesn't care which).

    python tools/vendor_wheels.py                       # -> ./wheels, cp311+cp312+cp313, manylinux2014_x86_64
    python tools/vendor_wheels.py --out /tmp/wheels --python-versions 311
    python tools/vendor_wheels.py --platform manylinux2014_aarch64
    python tools/vendor_wheels.py --dry-run             # print the pip commands, download nothing

Only textual/rich/mcp/pydantic-core are named in the H8 brief, but this
vendors requirements.lock's FULL pinned closure (every transitive
dependency those actually need) -- a partial vendor directory would just
move the "no PyPI access" failure one dependency deeper. `--no-deps` is
passed to `pip download` because the lock file already IS that full
closure (one line per package, exactly pinned); letting pip re-resolve on
top of that would just re-invite the cross-platform marker problem this
script exists to avoid (see `marker_applies` below).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
LOCK_FILE = REPO_DIR / "requirements.lock"

# The handful of PEP 508 environment-marker variables requirements.lock
# actually uses (`uv pip compile`'s own output -- see the comparison list in
# docs/harness/H8-brief.md's scope F). Evaluated against a SYNTHETIC target
# environment (Linux/CPython/not-PyPy/not-emscripten), not whatever platform
# this script happens to be running on right now -- the whole point being
# that a Windows build host correctly EXCLUDES `pywin32 ; sys_platform ==
# 'win32'` when vendoring for a Linux work box (pip's own marker evaluation
# during `download` uses the HOST interpreter's environment regardless of
# --platform/--python-version, which would otherwise pull pywin32 in and
# then fail to find a manylinux wheel for it).
_MARKER_CLAUSE_RE = re.compile(r"""^\s*([\w.]+)\s*(==|!=|>=|<=|>|<)\s*['"]([^'"]+)['"]\s*$""")


def _version_tuple(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in v.split("."))


def _eval_clause(var: str, op: str, value: str, env: dict) -> bool:
    actual = env.get(var)
    if actual is None:
        return True  # unrecognised variable -- fail OPEN, see marker_applies
    if op in ("==", "!="):
        eq = actual == value
        return eq if op == "==" else not eq
    try:
        a, b = _version_tuple(actual), _version_tuple(value)
    except (TypeError, ValueError):
        return True
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a < b


def marker_applies(marker: str, *, env: dict) -> bool:
    """`True` unless `marker` (a `;`-stripped PEP 508 marker expression,
    `and`-only -- the only combinator `uv pip compile` ever emits into this
    file) clearly evaluates to False against `env`. A clause shape this tiny
    evaluator doesn't recognise fails OPEN (the requirement is kept) rather
    than silently dropping a dependency the work box actually needs -- an
    unnecessary wheel is a wasted download; a missing one is a broken
    offline install."""
    for clause in marker.split(" and "):
        m = _MARKER_CLAUSE_RE.match(clause.strip())
        if not m:
            continue
        var, op, value = m.groups()
        if not _eval_clause(var, op, value, env):
            return False
    return True


def linux_env(python_full_version: str) -> dict:
    return {
        "sys_platform": "linux",
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "python_full_version": python_full_version,
        "python_version": ".".join(python_full_version.split(".")[:2]),
    }


def filtered_requirements(python_full_version: str) -> "list[str]":
    """requirements.lock's package lines (comments, blank lines and the
    `    # via ...` provenance continuation lines dropped) that apply to a
    Linux/CPython target at `python_full_version`."""
    env = linux_env(python_full_version)
    out = []
    for raw in LOCK_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ";" in line:
            req, _, marker = line.partition(";")
            if not marker_applies(marker.strip(), env=env):
                continue
            line = req.strip()
        out.append(line)
    return out


def download(*, out_dir: Path, python_tag: str, platform_tag: str, dry_run: bool = False) -> int:
    """`python_tag` like `"311"` -- a bare cpXYZ tag, no dots, no `cp`
    prefix (matches `--python-versions` below and pip's own `--abi cp311`
    spelling)."""
    full_version = f"{python_tag[0]}.{python_tag[1:]}"
    reqs = filtered_requirements(full_version + ".0")
    if not reqs:
        print(f"[vendor_wheels] no requirements matched for cp{python_tag} -- "
              f"check {LOCK_FILE}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    req_file = out_dir / f".vendor-requirements-cp{python_tag}.txt"
    req_file.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    argv = [sys.executable, "-m", "pip", "download",
            "-r", str(req_file),
            "--dest", str(out_dir),
            "--platform", platform_tag,
            "--python-version", full_version,
            "--implementation", "cp",
            "--abi", f"cp{python_tag}",
            "--only-binary=:all:",
            "--no-deps"]
    print(f"[vendor_wheels] cp{python_tag} / {platform_tag}: {len(reqs)} pinned requirement(s) -> {out_dir}")
    if dry_run:
        print("  " + " ".join(argv))
        req_file.unlink(missing_ok=True)
        return 0
    try:
        rc = subprocess.call(argv)
    finally:
        req_file.unlink(missing_ok=True)
    return rc


# `pip install --no-index --find-links=wheels -e .` still needs a build
# backend to produce the editable install (setuptools/wheel -- see
# pyproject.toml's `[build-system]`); `--no-build-isolation` (the recipe
# INSTALL.md gives) skips pip's normal "fetch the build backend into a
# throwaway env" step, so that backend must already be installed OR sitting
# in `wheels/` too. Both are tiny, pure-Python, `py3-none-any` wheels --
# one platform-generic download covers every Python/OS combination, so
# these are fetched once, outside the per-cpXYZ/platform loop above.
_BUILD_BACKEND_PACKAGES = ["setuptools>=68", "wheel"]


def download_build_backend(*, out_dir: Path, dry_run: bool = False) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "pip", "download", *_BUILD_BACKEND_PACKAGES,
            "--dest", str(out_dir), "--only-binary=:all:"]
    print(f"[vendor_wheels] build backend ({', '.join(_BUILD_BACKEND_PACKAGES)}) -> {out_dir}")
    if dry_run:
        print("  " + " ".join(argv))
        return 0
    return subprocess.call(argv)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Vendor manylinux wheels for halo's pinned dependencies.")
    ap.add_argument("--out", default=str(REPO_DIR / "wheels"),
                     help="destination directory (default: ./wheels, gitignored)")
    ap.add_argument("--python-versions", default="311,312,313",
                     help="comma-separated cpXYZ tags, no dots (default: 311,312,313 -- H9 whole-tree "
                          "review finding 23: Debian 13 ships Python 3.13, and so does Kali rolling, "
                          "which tracks it, so cp313 wheels are needed by default too, not opt-in)")
    ap.add_argument("--platform", default="manylinux2014_x86_64",
                     help="pip --platform tag (default: manylinux2014_x86_64; "
                          "use manylinux2014_aarch64 for an arm64 work box)")
    ap.add_argument("--dry-run", action="store_true",
                     help="print the pip download command(s) instead of running them")
    args = ap.parse_args(argv)

    if not LOCK_FILE.exists():
        print(f"[vendor_wheels] {LOCK_FILE} not found -- run "
              f"`uv pip compile pyproject.toml --universal -o requirements.lock` first", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    worst = 0
    for tag in [t.strip() for t in args.python_versions.split(",") if t.strip()]:
        worst = max(worst, download(out_dir=out_dir, python_tag=tag, platform_tag=args.platform, dry_run=args.dry_run))
    worst = max(worst, download_build_backend(out_dir=out_dir, dry_run=args.dry_run))

    if worst == 0 and not args.dry_run:
        n = len(list(out_dir.glob("*.whl")))
        print(f"[vendor_wheels] done -- {n} wheel file(s) in {out_dir}")
        print(f"[vendor_wheels] on the work box: pip install --no-index "
              f"--find-links={out_dir} -e . --no-build-isolation")
    return worst


if __name__ == "__main__":
    sys.exit(main())
