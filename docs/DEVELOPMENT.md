# Development

Repo layout, how to run the three test suites on Windows/WSL/Kali, the
briefs/review/acceptance workflow this project's own history follows, how
to extend the harness in the four most common ways, and the coding rules
that keep it that way. See [ARCHITECTURE.md](ARCHITECTURE.md) for how the
pieces you'd be extending fit together, and
[harness/README.md](harness/README.md) for the build-history index.

## Repo layout

```
rolo_claude/            the package
  cli.py                 the rolo-claude entry point + flag table
  headless.py             -p print mode / the shared session builder
  controller.py            the TUI's glue between BridgeApp and Session
  agent/                   the session loop, log, derive/prune, compaction,
                           sub-agents, plan mode, the cc: bridge, jobs
  providers/               per-provider HTTP, compat profiles, model_table.json
  tools/                   built-in tools (Read/Write/Edit/Bash/...)
  mcp/                     the MCP client (stdio/http/sse), lazy start, cache
  config/                  settings/CLAUDE.md/memory/plugins/agents discovery
  commands/                slash-command registry, custom commands, skills
  tui/                     the Textual app, widgets, dialogs, dispatch
  improve/                 /improve: evidence clustering, drafting, apply
  ccbridge/                the cc: subprocess's own MCP tool bridge (child side)
  *_cli.py                 each `rolo-claude <subcommand>`'s own module
bridge.py                 the original claude-bridge proxy (kept as `rolo-claude proxy`)
tests/                    the harness's own suite (tests/run_all.py)
test_bridge.py            the proxy's own black-box suite
test_tui.py               Textual pilots + SVG snapshots
docs/                     this documentation
docs/harness/             the build history: briefs, reviews, acceptance records
```

## The three suites

```sh
python tests/run_all.py; echo exit=$?      # the harness's own suite (agent loop, tools, config, MCP, permissions, ...)
python test_tui.py; echo exit=$?           # Textual pilots + SVG snapshots (docs/harness/tui-snapshots/)
python test_bridge.py; echo exit=$?        # the claude-bridge proxy's own black-box suite
```

All three use mock upstreams throughout (`tests/helpers/mock_openai.py`,
`mock_anthropic.py`, `mock_databricks.py`, `fake_claude_cc.py` for the `cc:`
route, `fake_mcp_server.py`) -- no live network call happens as part of a
normal run. `tests/run_all.py` discovers every `tests/test_*.py` module
automatically (`tests/helpers/runner.py`'s `new_registry()`/`@test`
pattern -- see any existing `test_*.py` file for the shape a new one
should follow) and prints one combined pass/fail count; it also snapshots
the *real* `~/.rolo-claude/sessions` directory before and after the run and
fails loudly if anything leaked there (a test that built a real `Session`
without scoping `BRIDGE_TEST_HOME` away from your actual machine state).

Every milestone in this project's history was required to stay green on
**Windows, WSL Ubuntu, and the Kali VM** -- Kali is the primary target
platform, Windows is the build host. Cross-checking from a Windows
checkout:

```sh
# WSL (rsync avoids 9p/DrvFS filesystem quirks a symlink/bind-mount would hide):
wsl -e bash -lc 'ulimit -n 4096; rsync -a --delete --exclude .git --exclude __pycache__ --exclude wheels --exclude build \
  /mnt/c/path/to/rolo-claude/ ~/rolo-claude-wt/ \
  && cd ~/rolo-claude-wt && source ~/rolo-claude-wt-venv/bin/activate \
  && python3 tests/run_all.py && python3 test_bridge.py && python3 test_tui.py'
```

```sh
# Kali (from Git Bash, over SSH; tar avoids the same filesystem-sharing quirks):
tar --exclude=.git --exclude=__pycache__ --exclude=wheels --exclude=build -czf - . \
  | ssh user@<host> 'rm -rf ~/rolo-claude-wt && mkdir -p ~/rolo-claude-wt && tar -xzf - -C ~/rolo-claude-wt'
ssh user@<host> 'export PATH="$HOME/.local/bin:$HOME/bin:$PATH"; ulimit -n 4096; \
  cd ~/rolo-claude-wt && source ~/rolo-claude-wt-venv/bin/activate && \
  python3 tests/run_all.py && python3 test_bridge.py && python3 test_tui.py'
```

Both recipes need a one-time venv (`python3 -m venv ~/rolo-claude-wt-venv &&
~/rolo-claude-wt-venv/bin/pip install -e ~/rolo-claude-wt`) created once
before the first run. `test_tui.py` regenerates
`docs/harness/tui-snapshots/*.svg` on every run (normalized, not
byte-diffed -- box-drawing/font metrics legitimately differ by terminal).

## The briefs/review/acceptance workflow

`docs/harness/` is this project's own build history, not user
documentation -- `docs/harness/README.md` indexes it. Each milestone (an
`H<n>-brief.md` or `U<n>-brief.md`) states scope and constraints *before*
the work; a `review-findings-*.md` is an independent bug-hunt pass over one
or more milestones afterward, each finding severity-tagged with a file:line,
a failure scenario, and a fix; an `ACCEPTANCE-<date>.md` is the
cross-platform (Windows/WSL/Kali) verification record for a batch of
milestones, with real command output. A new milestone's own brief is the
right place to record *why* a design decision was made -- this docs/
directory (the one you're reading) explains *what the code does now*, and
should never need to repeat that reasoning; when the two would say the same
thing, this docs/ directory wins on current behavior and the brief is left
as the historical record of intent.

## How to add a tool

1. Subclass `rolo_claude.tools.base.Tool`: set `name`/`description`/
   `input_schema` (together, the exact Anthropic tool definition sent on
   the wire), implement `run(self, input: dict, ctx: ToolContext) ->
   ToolResult`, and optionally override `summary()` (a one-line card
   header) and `permission_content()` (what a permission rule matches
   against -- a path, a command, a URL; defaults to `""`).
2. Add it to `tools/registry.py::default_tools()` (guard a
   platform-specific tool, e.g. `PowerShellTool`, behind `sys.platform`
   inside that function, imported lazily so the other platform never even
   imports the module). A tool that should exist only for certain
   providers (`WebSearchTool` is only registered for an OpenRouter-backed
   session, not unconditionally) is added at session-construction time
   instead -- `default_tools()` is for every-session built-ins.
3. `ToolContext` carries everything a `run()` might need (cwd, the
   session's read-cache, an abort event, background-job registry, MCP
   manager, permission engine, ...) -- add a new field there, with a
   `None`/safe default, rather than smuggling state through a global.
4. Never raise out of `run()` for an ordinary failure -- return
   `ToolResult(content=<message>, is_error=True)`; `ToolRegistry.dispatch()`
   catches an actual exception anyway and turns it into an error result,
   but a clear, quoted message the model can act on is always better than
   a traceback string.
5. Add a `tests/test_tools_<name>.py` following the existing
   `tests/helpers/runner.py` pattern (see `tests/test_tools_read.py` for a
   simple example, `tests/test_tools_bash.py` for one with subprocess
   mocking).

## How to add a provider family

Request-shaping is entirely data-driven (`providers/model_table.json`),
never a per-model code branch:

1. Add a row under the right host (`"openrouter"` or `"databricks"`) keyed
   by the exact upstream model id, with whatever fields differ from the
   family default (`docs/MODELS.md` lists every field
   `providers/profiles.py::ProviderProfile` understands). Leave a field out
   entirely to inherit the coarse family default
   (`providers/profiles.py::_fallback_family_defaults`, or a family's own
   default in `resolve_profile`) -- most rows only need 3-5 fields.
2. If the family is genuinely new (not `claude`/`deepseek`/`kimi`/`glm`/
   `qwen`/`gemini`/`grok`/`minimax`/`gpt`), add a substring match to
   `providers/profiles.py::model_family()` first.
3. Add per-family system-prompt notation to `providers/prompt.py`'s
   `_FAMILY_NOTATION` table if the model needs specific tool-calling
   guidance (most open-weight families do -- see the existing Kimi entry,
   adapted from OpenCode's own).
4. If the model needs a genuinely new *mechanism* (not just a data value --
   a new `reasoning_replay`/`tool_id_format` shape), that logic lives in
   `providers/hooks.py` (reasoning replay/tool-id normalization/leak
   parsing), keyed off the same field names `model_table.json` rows set.
5. Add a `tests/test_profiles.py`-style row test asserting
   `resolve_profile()` produces what you expect for the new id.

## How to add a slash command

- **A user-authored one needs no code at all**: drop a `.md` file in
  `.claude/commands/` (project) or `~/.claude/commands/` (user) -- the
  filename (minus `.md`) is the invocation. See `docs/SLASH-COMMANDS.md`
  for the `$ARGUMENTS`/`` !`cmd` ``/`@path` body-expansion pipeline every
  custom command and skill shares.
- **A new built-in** goes in `rolo_claude/commands/builtins.py`: write a
  `_cmd_<name>(args: str, facade: HeadlessFacade) -> str` function (the
  `HeadlessFacade` is a read-only view of session state -- see its own
  docstring for every field available), then add `"<name>": (kind,
  description, argument_hint, _cmd_<name>)` to `_BUILTIN_SPECS`. `kind` is
  `"core"` (works identically headless and in the TUI), `"ui"` (the
  headless version prints an honest "needs the interactive TUI" string;
  give it *real* interactive behavior by adding a handler to
  `rolo_claude/tui/slash.py`'s own dispatch dict in `handle_slash`), or
  `"prompt"` (the returned string becomes the turn's actual prompt, not
  printed directly -- see `/init`).
- Add the new command's section to `docs/SLASH-COMMANDS.md`
  (`tests/test_docs_slash_commands.py` fails the suite otherwise).

## How to add a `doctor` check

Add a `_check_<name>() -> str` (or `-> Optional[str]` if it only sometimes
applies, e.g. only inside tmux) function to `rolo_claude/doctor.py`
returning one line built with the module's own `OK`/`WARN`/`MISSING`
constants -- use `_fix(line, cmd=...)` or `_fix(line, see=...)` to attach
the exact fix command/reference a `WARN`/`MISSING` line always ends with.
Register it with a stable id in `_check_entries()`'s list (the id is what
`doctor --json` and `rolo-claude init`'s own summary key off -- never
reuse or reorder an existing id). Never let a check raise: wrap anything
that touches the network/filesystem/a subprocess in its own `try/except`
and degrade to a `WARN` naming what went wrong, since one bad check must
never take down the rest of `doctor`.

## Coding rules

- **OS-neutral by default**: `rolo_claude/config/paths.py` is the one place
  that knows about Windows vs. POSIX home-directory shapes, Git Bash vs.
  `/bin/bash`, drive letters, etc. -- a new module should go through it
  (`home()`,
  `git_bash()`, `to_posix()`/`from_posix()`) rather than hardcoding a
  separator or a shell. A Windows-only or POSIX-only code path is guarded
  by `sys.platform`/`os.name`, imported lazily so the other platform never
  pays for (or crashes on) an import that assumes a binary/module that
  doesn't exist there.
- **No secrets in a child process's environment**: any subprocess this
  harness spawns (Bash, PowerShell, an MCP stdio server, a hook, the `cc:`
  `claude` subprocess) gets `providers/config.py::tool_child_env()` (strips
  the env-file's own keys plus the fixed provider-secret list) or, for the
  `cc:` route specifically, `cc_child_env()` (additionally strips every
  `ANTHROPIC_*`/`CLAUDE_CODE_*`/`CLAUDE*` variable) -- never a raw
  `os.environ` passthrough.
- **Never write a Claude Code file** outside the one documented exception
  (`rolo-claude mcp add`/`add-json`/`remove` writing `~/.claude.json`'s
  `mcpServers` keys, byte-compatible with what `claude mcp add` itself
  writes) -- see `docs/CONFIG.md`'s "What is never written" section. A new
  feature that wants to persist something belongs in
  `~/.rolo-claude/config.json` (via `rolo_claude.theme.get_config_value`/
  `set_config_value`, which already supports dotted paths).
- **No safety/refusal/classifier logic, anywhere** -- this is a deliberate,
  repeatedly-reaffirmed project decision (see `docs/ARCHITECTURE.md`'s
  Permissions section), not an oversight to "fix" later. A permission
  decision is grammar and mode only; a hook is entirely the user's own
  gate.
- **`≤ 250 lines per Write/Edit tool call` is a worker-process rule for
  whoever is editing this repo with an AI coding agent, not a repo
  convention** -- there's no file-length limit on the code itself (see
  `agent/loop.py`, over 4000 lines, because splitting the session loop
  across files would scatter one coherent state machine).
- **`tests/run_all.py`'s own hygiene guards are load-bearing**: a new test
  that constructs a real `Session`/`SessionLog` must set `BRIDGE_TEST_HOME`
  (or otherwise scope itself away from your actual `~/.rolo-claude`) --
  the suite fails the whole run if it detects a new file under your real
  session directory afterward.
