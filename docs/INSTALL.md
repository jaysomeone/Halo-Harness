# Install

One install, one command, then `halo` works from any directory -- the same
way `claude` does once it's on PATH. This page is the short version;
[docs/harness/INSTALL.md](harness/INSTALL.md) is the exhaustive one (PEP
668 detail, an offline work-box recipe, per-terminal notes, reproducible
installs via `requirements.lock`).

**Upgrading from `rolo-claude` 1.0.1?** The plain install commands below
work as-is, whether or not the 1.0.1 tool is still installed under that
name -- see "Upgrading from rolo-claude 1.0.1" below for why, and for the
(still recommended, just no longer required) old-tool uninstall command.

## One line per platform

Prerequisite everywhere: Python 3.10+. [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
is recommended but optional -- every command below has a plain-`pip`
alternative that needs no `uv` at all.

### From a clone (recommended -- this is also how you get `git pull` updates)

```sh
git clone https://github.com/roloVibes/Halo-Harness.git
cd Halo-Harness
uv tool install --reinstall .
```

`--reinstall` works whether or not `halo` is already installed, so it is
the ONE command you run both the first time and every later time -- see
"After every `git pull`" below.

No `uv`? `pipx install --force -e .` is the alternative (`pip install
--user -e .` works too) -- `halo`'s own distribution installs exactly one
executable, `halo` itself, so none of these ever touch a separate,
already-installed `rolo-claude` 1.0.1 tool (see the upgrade note above).
**Kali
2024.x+/Debian trixie+ enable PEP 668**, so a bare `pip install --user`
refuses with `error: externally-managed-environment` on system Python;
`uv tool install`/`pipx install` both sidestep it entirely (they install
into an isolated environment, never system/apt Python), which is why
they're listed first.

### Directly from GitHub (no clone at all)

```sh
uv tool install git+https://github.com/roloVibes/Halo-Harness
```

Nothing to `cd` into first -- good for a box that only ever needs to RUN
`halo`, never to `git pull` it. You cannot update this install with `git
pull` (there is no local clone); re-run the same command to pick up a new
release instead.

## Then: cd anywhere, type `halo`

```sh
cd ~                 # or /tmp, or any other project directory at all
halo init            # one time: pick a provider, credentials, default model, doctor, live pong
halo                 # full-screen TUI -- reads THIS directory's CLAUDE.md, rules, settings, .mcp.json
```

That's the whole point of installing it: `halo`, once on PATH, is a real
console script, not a script that only works from inside this checkout --
run it from your home directory, `/tmp`, or any project, and it reads
*that* directory's `CLAUDE.md` chain, `.claude/rules`, project/local
settings, `.mcp.json`, skills, commands and agents, exactly the way
`claude` does, plus the user-level files under `~/.claude` as always.
`halo doctor`'s "command on PATH" check (and `init`'s own summary line)
names which copy of `halo` you're actually running and says so plainly
when it ISN'T the installed one -- see docs/harness/INSTALL.md's own
"Verify" section.

## The clone directory is for `git pull` only

Once installed, the checkout you cloned has exactly one job left: pulling
future updates. It is never needed again to RUN `halo` -- there is no
"run it from inside the checkout" mode you're supposed to use day to day
(`bin/halo`/`bin/halo.cmd` inside it are a fallback for a box with no
install done yet at all, not the normal path).

### After every `git pull`

```sh
cd /path/to/Halo-Harness   # back in the clone, for this one step only
git pull
uv tool install --reinstall .      # or: pipx install --force -e .
cd ~                                # done -- back to running `halo` from anywhere
```

The exact same reinstall command as the first install, every time --
`halo doctor`/`halo init`'s PATH check names this command for your box if
you ever forget and the installed copy drifts out of date.

## Upgrading from rolo-claude 1.0.1

`halo` is a rename, not a fresh product -- 1.0.1's installed console
script is still named `rolo-claude`. As of 2.0.1, `halo`'s own
distribution ships exactly one executable (`halo` itself, never a second
`rolo-claude` entry), so installing it never aborts or conflicts with a
still-installed 1.0.1 `rolo-claude` tool. Uninstalling the old tool first
is still recommended, so the stale command can't run by mistake -- `halo
doctor` WARNs if one is still on PATH, naming the exact fix. See
[docs/harness/INSTALL.md's "Upgrading from rolo-claude 1.0.1"](harness/INSTALL.md#upgrading-from-rolo-claude-101)
for the exact uninstall command per installer and what happens to
`~/.rolo-claude` (renamed to `~/.halo`, no link left behind, so the
separate 1.0.1 install must not be run again afterward).

## Verify

```sh
halo --version          # halo 2.0.1
halo doctor              # read-only environment check -- every WARN/MISSING line names its own fix
```

See the [README](../README.md) for the 10-minute walkthrough and
[docs/harness/INSTALL.md](harness/INSTALL.md) for everything this page
doesn't cover: offline/work-box installs, Windows detail, PATH
troubleshooting, and per-terminal notes for the TUI.
