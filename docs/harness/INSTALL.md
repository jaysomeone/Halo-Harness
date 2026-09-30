# Installing rolo-claude

`rolo-claude` is a standalone, Claude-Code-compatible harness (full-screen TUI +
`-p` print mode + the older `claude-bridge` proxy as a subcommand) driving
OpenRouter/Databricks models. **Kali Linux is the primary target platform** --
install there first and treat Windows as the secondary/build host.

Prerequisites everywhere: Python 3.10+ (`python3 --version`). `uv` is optional
but recommended (both launchers below fall back to plain `pip`/`python3` when
it's absent).

**Short version**: install with one of the two options below, then run
`rolo-claude init` -- it picks a preset, sets up credentials, sets a default
model, runs `doctor`, sends a live pong, and (on Linux) offers the `rg`/PATH
fixes, all in one command, and is always safe to re-run. Everything else in
this file is the manual/reference version of what `init` automates, plus the
offline/work-box and reproducible-install recipes it doesn't cover.

## Kali / Linux

### Option 1 -- `uv tool install` (recommended)

Installs into an isolated tool environment (never touches system/apt Python
packages, sidesteps Debian/Kali's PEP 668 "externally-managed-environment"
guard entirely) and puts a console script on `~/.local/bin`:

```sh
cd /path/to/rolo-claude
uv tool install --editable .
```

This produces `~/.local/bin/rolo-claude`. `--editable` means it keeps reading
this checkout's source directly -- pull/edit the repo and the installed
command picks it up immediately, no reinstall needed (only a NEW/removed
dependency in `pyproject.toml` needs a re-run of the install command).

If `uv` isn't on PATH yet, see <https://docs.astral.sh/uv/getting-started/installation/>
(or just use Option 2 below -- nothing here requires `uv`).

### Option 2 -- `pip install --user -e .`

```sh
cd /path/to/rolo-claude
pip install --user -e .
```

Also lands on `~/.local/bin/rolo-claude` (pip's own `--user` console-script
location), editable the same way. **Kali 2024.x+ / Debian trixie+ enable PEP
668** and this will refuse with `error: externally-managed-environment` on a
system Python. Three ways around it, in order of preference:

1. Use Option 1 (`uv tool install`) instead -- it never hits this at all.
2. Install into a venv: `python3 -m venv ~/.venvs/rolo-claude && ~/.venvs/rolo-claude/bin/pip install -e .`, then symlink or wrap `~/.venvs/rolo-claude/bin/rolo-claude` onto your PATH.
3. Last resort: `pip install --user -e . --break-system-packages`.

### Make sure `~/.local/bin` is on PATH

Common gotcha on a fresh Kali box: `~/.local/bin` is on PATH for an
interactive login shell but not always for scripts/non-login shells/tmux. If
`rolo-claude --version` says "command not found" right after installing,
add to `~/.bashrc`/`~/.zshrc`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

### No install at all (dev checkout)

`bin/rolo-claude` (POSIX shell) works with zero PACKAGING step -- it
prefers `~/.local/bin/rolo-claude` when present (Option 1/2 above),
otherwise falls back to `PYTHONPATH=<repo> python3 -m rolo_claude` from
this checkout:

```sh
ln -s "$(pwd)/bin/rolo-claude" ~/bin/rolo-claude   # a symlink, not a copy -- see below
chmod +x bin/rolo-claude
```

Symlink it, don't copy it (H9 whole-tree review finding 24): a bare `cp`
severs the connection back to this checkout entirely -- there is no
`rolo_claude/` package sitting next to a lone copied file in `~/bin`, so
it can never work no matter what. A symlink keeps pointing at the real
checkout, and the script itself resolves that symlink (`readlink -f`)
back to this directory before setting `PYTHONPATH`.

This step alone does **not** install this harness's Python dependencies
(textual, rich, mcp, ...) -- it only makes the `rolo-claude` COMMAND
reachable on PATH. Something still has to have installed those
dependencies somewhere `python3` can import them from: either run one of
the `pip install -e .`/`uv tool install --editable .` recipes above at
least once in this same checkout (their own console-script entry point is
what line 1 of this section prefers -- once that exists, THIS symlink is
redundant, though harmless to keep), or point `python3` at a venv that
already has them (activate it before running `rolo-claude`, or hardcode
its interpreter on this script's own shebang line).

## Windows

```powershell
cd C:\path\to\rolo-claude
uv tool install --editable .
```

Produces `%USERPROFILE%\.local\bin\rolo-claude.exe`. `~\bin\rolo-claude.cmd`
is a PATH wrapper that prefers that exe and falls back to
`python -m rolo_claude` from this checkout when the exe isn't installed yet --
copy/adapt it for another machine (a teammate substitutes their own repo
path in the `else` branch).

## Work box (offline install)

Some boxes only reach Databricks (over a VPN) and have no route to PyPI at
all. `tools/vendor_wheels.py` pre-downloads every wheel `rolo-claude` needs
(`requirements.lock`'s full pinned closure -- textual, rich, mcp,
pydantic-core and everything under them -- plus the setuptools/wheel build
backend) for Linux/cp311/cp312/cp313 (Debian 13 and Kali rolling both ship
3.13 by default), so the work box never has to reach PyPI:

```sh
# on a machine WITH internet access (the Windows build host is fine --
# this cross-downloads Linux wheels regardless of what it's running on):
python tools/vendor_wheels.py                      # -> ./wheels (gitignored)
python tools/vendor_wheels.py --platform manylinux2014_aarch64   # arm64 work box

# copy wheels/ to the work box (shared drive, scp once the VPN is up, a USB
# stick -- whatever side channel reaches it), then there, INSIDE A VENV
# (a fresh Python >=3.12 venv has no setuptools/pip preinstalled at all --
# see the note below on why this must be a venv AND must NOT pass
# --no-build-isolation):
python3 -m venv ~/.venvs/rolo-claude && source ~/.venvs/rolo-claude/bin/activate
pip install --no-index --find-links=wheels -e .
```

`--no-index` refuses to reach PyPI even if a route momentarily exists;
`--find-links=wheels` is the only source of packages -- pip passes BOTH of
these through to the isolated build environment it creates for `-e .`
itself (the standard, documented behaviour of pip's build isolation: the
build env is populated using the SAME index options as the install
command), so that isolated env finds `wheels/`'s own setuptools/wheel
without ever reaching PyPI.
H9 whole-tree review finding 23: do **not** add `--no-build-isolation`
here, even though `wheels/` also has a setuptools/wheel pair sitting in
it -- that flag skips creating the isolated build env ENTIRELY, so pip
never looks in `--find-links` for the build backend at all; it instead
requires setuptools to ALREADY be import-able in whatever environment
`pip install` itself is running in. A fresh Python (>=3.12 stopped
bundling setuptools into a new venv by default) or a bare `pip install
--user` on Debian/Kali (PEP 668) has neither -- verified on WSL, a fresh
3.12 venv: `ModuleNotFoundError: No module named 'setuptools'`, pip exit
2. Dropping the flag (and installing inside a venv, never `--user`
system-wide, to sidestep PEP 668 entirely) fixes both at once. `uv` works
the same offline, pointed at the same directory:

```sh
uv tool install --editable . --offline --find-links wheels
```

Re-run `vendor_wheels.py` after any dependency change (`pyproject.toml`'s
`dependencies` plus a fresh `uv pip compile pyproject.toml --universal -o
requirements.lock`) -- the wheels directory is a point-in-time snapshot of
that lock file, not something that stays in sync on its own.

### `ug`/unity-gateway compatibility

Databricks' own `ug` CLI writes `~/.claude/ucode-settings.json` (gateway URL
and token) when a user has already set up unity-gateway access for the
stock Claude Code CLI on that box. `rolo-claude` reads it as one more source
in the same Databricks-credential discovery chain the main README's Install
section documents (env vars first, then this file) -- a work box already
configured for `ug` needs no separate rolo-claude setup step at all.

### `rolo-claude doctor --work`

A preset for exactly this box: VPN reachability of the Databricks host, an
actual token-validity probe (`GET /api/2.0/serving-endpoints`, or a 1-token
completion), and the two open questions from the project plan --- does
Databricks forward replayed `reasoning_content` back through a tool call,
and does the invocations-vs-gateway route split hold for the configured
model -- as runnable probes with a clear OK/WARN/MISSING line each, not just
prose. Run it any time this box's Databricks setup is in doubt:

```sh
rolo-claude doctor --work
```

Off the VPN (including from the build host that produced `wheels/`), every
line reports MISSING/WARN with a hint to get on the VPN or run `ug` first --
that's the expected, correct offline result, not a bug. It also prints the
derived workspace root, the gateway path, the header NAMES it will send
(never values), the resolved default model/effort, and which config source
supplied the token, and distinguishes a bad token (401) from the IP access
list (403 with Databricks' own wording) from a token that can run inference
but not list endpoints (403 without it) from a wrong path (404).

`rolo-claude doctor --work --probe-all [--both] [--tools] [--only <glob>]`
goes further: one short pong through every chat-shaped endpoint the catalog
knows about, on its own chosen path (`--both` also probes the anthropic
gateway for Claude/GLM/Kimi; `--tools` adds a one-tool-call check), and a
JSON report at `~/.rolo-claude/work-matrix-<date>.json` naming endpoints
only -- no host, no token -- to paste back for review. See the README's
"Databricks at work" section for `team.json` (shared host/default-model/
gateway-preference setup) and `/models refresh`.

## Reproducible installs (`requirements.lock`)

`requirements.lock` is a cross-platform (`uv pip compile --universal`) pin of
every dependency, including markers like `pywin32==312 ; sys_platform ==
'win32'` -- safe to install on Linux (it correctly skips Windows-only
packages there). Regenerate it after changing `pyproject.toml`'s
`dependencies`:

```sh
uv pip compile pyproject.toml --universal -o requirements.lock
```

`uv tool install --editable .` and `pip install --user -e .` both resolve
straight from `pyproject.toml` (not this file) and already do the right
thing per-platform; `requirements.lock` is for a venv you want pinned
exactly, e.g. `uv pip sync requirements.lock` inside one.

## Verify

```sh
rolo-claude --version          # rolo-claude 0.4.1
rolo-claude doctor             # read-only environment check: Python, ~/.claude,
                                # env file, OpenRouter/Databricks, claude/node/rg
                                # on PATH, $VISUAL/$EDITOR, tmux mouse mode, a
                                # usable Bash shell, clipboard backend, MCP
                                # servers, the default model, a WSL/Kali hint --
                                # every WARN/MISSING line names its own fix;
                                # `doctor --json` for the machine-readable form
```

## Configuration

`rolo-claude init` does everything below for you in one command (see the top
of this file) -- read on for the manual/reference version of the same steps.

Put `OPENROUTER_API_KEY` in `~/.config/vibes-hacker/env` (`KEY=value`, `#`
comments, optional leading `export`; override the path with `BRIDGE_ENV_FILE`)
or export it yourself. Databricks credentials are discovered automatically
the same way `rolo-claude proxy`/`bridge.py` always has -- see the main
README's Install section for the full discovery order.

## Running

```sh
rolo-claude                              # full-screen TUI (needs a real terminal)
rolo-claude "read README.md"             # TUI, prompt pre-filled as the first turn
rolo-claude -p "reply with the word pong"  # print mode, scriptable/headless
rolo-claude --demo                       # scripted TUI walkthrough, no network/model needed
rolo-claude --demo -p                    # same script through print mode instead
rolo-claude proxy --serve                # the older claude-bridge proxy subcommand
```

**The full-screen TUI needs a real interactive terminal.** Bare `rolo-claude`
(no `-p`) checks `stdin.isatty()` before it ever imports `textual`; when
stdin isn't a tty (piped input, a subprocess, a cron/systemd job with no
console) it prints one line to stderr and exits 2 instead of hanging. This is
expected and by design -- use `-p` for anything non-interactive. To exercise
the real TUI from a wrapper/non-login-shell context that doesn't itself
attach a tty (e.g. proving it renders inside CI, or piping its output through
another tool), allocate a pty explicitly:

```sh
script -q -c "rolo-claude --demo" /dev/null
```

## Terminal notes (Linux acceptance)

rolo-claude's TUI is built on Textual, which targets any modern terminal
(xterm, kitty, gnome-terminal, tmux, and others) -- this project's own
acceptance record has directly exercised WSL Ubuntu's tmux over SSH (see
`docs/harness/ACCEPTANCE-2026-09-25.md`); a mouse on/off matrix across the
other terminals named above, and a local (non-SSH) Kali console session,
have not yet been separately recorded. This is what to expect, and how to
get the most out of it, on any of them.

- **Mouse**: on by default (Textual captures it for click-to-focus, drag-
  scroll and drag-select-to-copy). **Hold Shift while dragging** to bypass
  the app's own mouse capture and use the terminal's NATIVE text selection
  instead -- this works the same way in xterm, kitty, gnome-terminal and
  inside a tmux pane, and is the right move any time you want to select
  text some OTHER way than what a Ctrl+C-on-selection copy gives you (e.g.
  selecting across a scrolled-off region, or copying into a completely
  different app). Inside tmux specifically, mouse mode is tmux's own
  setting (`set -g mouse on`, on by default in modern tmux) -- rolo-claude
  just receives whatever tmux forwards; toggle it in tmux itself
  (`tmux set -g mouse off` for a session) if you want the terminal's native
  selection to be the DEFAULT instead of needing Shift.
- **Clipboard (OSC 52)**: Ctrl+C on a selected transcript run copies via
  OSC 52, which works over SSH and through tmux (with tmux's own
  `set -g set-clipboard on`, the default) without any extra tooling. As a
  second, best-effort mechanism alongside it, rolo-claude also pipes the
  same text through `wl-copy` (Wayland) or `xclip`/`xsel` (X11) if one is
  on PATH -- useful on a terminal/multiplexer config that doesn't relay OSC
  52. Neither is required for the primary OSC 52 path to work; install one
  (`apt install xclip` or `wl-clipboard`) only if copy isn't reaching your
  system clipboard and you want the fallback active too.
- **Resize**: the layout re-flows live (Textual's own resize handling);
  nothing rolo-claude does needs a restart after resizing a pane/window,
  including the prompt's own auto-grow (1-8 rows, wrap-aware).
- **Ctrl+Z / `fg`**: rolo-claude never binds Ctrl+Z itself, so it reaches
  your shell as the normal Unix job-control suspend (SIGTSTP); `fg` resumes
  the TUI cleanly with no hang or redraw glitch (Textual repaints on
  resume).
- **Chords and the which-key overlay** (`Ctrl+X ...`) work identically
  across all four terminals above; `tmux`'s own prefix key (default
  `Ctrl+B`) doesn't collide with rolo-claude's `Ctrl+X` chord prefix, but
  if you've remapped tmux's prefix to `Ctrl+X` yourself, remap
  rolo-claude's instead via `~/.claude/keybindings.json` (see
  `/keybindings` for the live list, and the `keybindings-help` skill for
  the file format).
- **SSH**: everything above (mouse, OSC 52 clipboard, resize, chords) is
  verified over SSH into the Kali VM inside tmux, not just on a local
  console -- there's nothing SSH-specific to configure.

## Tests

```sh
python3 tests/run_all.py; echo exit=$?      # the core suite (agent loop, tools, config, MCP, ...)
python3 test_tui.py; echo exit=$?           # the TUI suite (Textual pilots + SVG snapshots)
python3 test_bridge.py; echo exit=$?        # the claude-bridge proxy's own black-box suite
```

`test_tui.py` regenerates `docs/harness/tui-snapshots/*.svg` on every run
(normalised, not byte-diffed against a golden copy -- box-drawing/font
metrics legitimately differ between terminals); pass `UPDATE_SNAPSHOTS=1` if
a future version of that suite adds a byte-comparison mode that needs it.

### Cross-checking on WSL from the Windows build host

Every milestone's suites must stay green on Linux, not just Windows. From a
Windows checkout, with a one-time `python3 -m venv ~/rolo-claude-wt-venv &&
~/rolo-claude-wt-venv/bin/pip install -e /mnt/c/path/to/rolo-claude` done
once inside WSL to create the venv:

```sh
wsl -e bash -lc 'rsync -a --delete --exclude .git --exclude __pycache__ --exclude wheels \
  ~/Documents/vibes/appDev/rolo-claude/ ~/rolo-claude-wt/ \
  && cd ~/rolo-claude-wt && source ~/rolo-claude-wt-venv/bin/activate \
  && python3 tests/run_all.py | tail -5 \
  && python3 test_bridge.py | tail -5 \
  && python3 test_tui.py | tail -5'
```

`rsync` (not a symlink/bind-mount) because `/mnt/c/...` is a 9p/DrvFS mount
-- editable installs and some file-watching code behave differently over
it than on a native Linux filesystem, and the whole point of this check is
to catch that class of bug before it reaches the Kali VM.

## Troubleshooting

- **`externally-managed-environment` from pip** -- see the PEP 668 note under
  Option 2 above; easiest fix is `uv tool install --editable .` instead.
- **`rolo-claude: command not found` right after installing** -- `~/.local/bin`
  isn't on PATH yet; see the PATH note above.
- **Bare `rolo-claude` exits 2 immediately with a "not a tty" message** --
  expected outside a real terminal; see Running above.
- **A different `python3`/version than expected gets used** -- both
  `uv tool install` and `pip install --user -e .` bind to whichever
  interpreter ran the install command; use `python3.X -m pip install --user
  -e .` (or `uv tool install --python 3.X --editable .`) to pin one
  explicitly.
- Everything else (proxy port conflicts, VPN/Databricks reachability, stale
  server hash, `BRIDGE_DUMP=1` request dumps, ...) -- see the main
  `README.md`'s own Troubleshooting section; `rolo-claude proxy` is the exact
  same `bridge.py` underneath.
