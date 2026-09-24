# Installing rolo-claude

`rolo-claude` is a standalone, Claude-Code-compatible harness (full-screen TUI +
`-p` print mode + the older `claude-bridge` proxy as a subcommand) driving
OpenRouter/Databricks models. **Kali Linux is the primary target platform** --
install there first and treat Windows as the secondary/build host.

Prerequisites everywhere: Python 3.10+ (`python3 --version`). `uv` is optional
but recommended (both launchers below fall back to plain `pip`/`python3` when
it's absent).

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

`bin/rolo-claude` (POSIX shell) works with zero install step -- it prefers
`~/.local/bin/rolo-claude` when present (Option 1/2 above), otherwise falls
back to `PYTHONPATH=<repo> python3 -m rolo_claude` from this checkout:

```sh
cp bin/rolo-claude ~/bin/          # or symlink it; keep it executable
chmod +x ~/bin/rolo-claude
```

## Windows

```powershell
cd C:\path\to\rolo-claude
uv tool install --editable .
```

Produces `%USERPROFILE%\.local\bin\rolo-claude.exe`. `C:\Users\user\bin\rolo-claude.cmd`
is a PATH wrapper that prefers that exe and falls back to
`python -m rolo_claude` from this checkout when the exe isn't installed yet --
copy/adapt it for another machine (a teammate substitutes their own repo
path in the `else` branch).

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
rolo-claude --version          # rolo-claude 0.3.0
rolo-claude doctor             # read-only environment check: Python, ~/.claude,
                                # env file, OpenRouter/Databricks, claude/node on
                                # PATH, a WSL/Kali hint
```

## Configuration

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
