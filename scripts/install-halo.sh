#!/usr/bin/env bash
# install-halo.sh -- install Halo Harness (the successor of rolo-claude) on
# Linux or macOS and make sure an old rolo-claude install can never shadow it.
#
#   bash install-halo.sh            # clone to ~/Halo-Harness, install, verify
#   bash install-halo.sh --yes      # no questions (uninstalls old rolo-claude)
#   bash install-halo.sh --no-clone # install straight from GitHub, no clone
#   HALO_CLONE_DIR=~/src/Halo-Harness bash install-halo.sh
#
# Needs: git, curl, Python 3.10+. Installs uv if it is missing.
set -euo pipefail

REPO_URL="https://github.com/roloVibes/Halo-Harness.git"
CLONE_DIR="${HALO_CLONE_DIR:-$HOME/Halo-Harness}"
YES=0
NO_CLONE=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --no-clone) NO_CLONE=1 ;;
    --dir=*) CLONE_DIR="${a#--dir=}" ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
confirm() {
  [ "$YES" = 1 ] && return 0
  local r
  read -r -p "$1 [y/N] " r </dev/tty || return 1
  [[ "$r" =~ ^[Yy] ]]
}

# 1. Python 3.10+
PY=""
for c in python3 python; do
  if have "$c" && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is required (check: python3 --version)."
  exit 1
fi
say "Python: $("$PY" --version)"

# 2. uv (isolated tool installs; sidesteps PEP 668 on Kali/Debian)
if ! have uv; then
  say "uv not found: installing it from https://astral.sh/uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  have uv || { echo "uv still not on PATH; open a new shell and re-run."; exit 1; }
fi
say "uv: $(uv --version)"

# 3. Remove the old rolo-claude tool so it can never run by mistake.
#    (halo migrates ~/.rolo-claude to ~/.halo by itself on first run.)
if uv tool list 2>/dev/null | grep -q '^rolo-claude '; then
  say "Old rolo-claude uv tool found"
  if confirm "Uninstall it (recommended)?"; then uv tool uninstall rolo-claude; fi
fi
if have pipx && pipx list 2>/dev/null | grep -q 'package rolo-claude'; then
  say "Old rolo-claude pipx install found"
  if confirm "Uninstall it (recommended)?"; then pipx uninstall rolo-claude; fi
fi
if "$PY" -m pip show rolo-claude >/dev/null 2>&1; then
  say "Old rolo-claude pip install found"
  if confirm "Uninstall it (recommended)?"; then "$PY" -m pip uninstall -y rolo-claude; fi
fi

# 4. Install halo (2.0.1+ ships exactly one executable: halo)
if [ "$NO_CLONE" = 1 ]; then
  say "Installing halo straight from GitHub"
  uv tool install --reinstall "git+${REPO_URL%.git}"
else
  if [ -d "$CLONE_DIR/.git" ]; then
    say "Updating the clone at $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    say "Cloning to $CLONE_DIR"
    git clone "$REPO_URL" "$CLONE_DIR"
  fi
  say "Installing halo from the clone"
  (cd "$CLONE_DIR" && uv tool install --reinstall .)
fi

# 5. PATH for the uv tools directory
uv tool update-shell >/dev/null 2>&1 || true
export PATH="$HOME/.local/bin:$PATH"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'Add this to your shell rc, then open a new shell:  export PATH="$HOME/.local/bin:$PATH"' ;;
esac

# 6. Verify: halo first on PATH, no rolo-claude left anywhere on PATH
say "Verify"
if ! have halo; then
  echo "halo is not on PATH yet: open a new shell (uv tools live in ~/.local/bin) and run: halo doctor"
  exit 1
fi
echo "halo: $(command -v halo)  ($(halo --version))"
if have rolo-claude; then
  echo "WARNING: an old rolo-claude is still on PATH at $(command -v rolo-claude)."
  echo "         Remove it so it cannot run by mistake (halo doctor names the exact command)."
fi
halo doctor || true

say "Done. Next:"
echo "  cd ~ && halo init      # pick a provider (Databricks at work, OpenRouter at home), then a live pong"
echo "  halo                   # the TUI, from any directory"
if [ "$NO_CLONE" = 0 ]; then
  echo "Updates later:  cd $CLONE_DIR && git pull && uv tool install --reinstall ."
else
  echo "Updates later:  uv tool install --reinstall git+${REPO_URL%.git}"
fi
