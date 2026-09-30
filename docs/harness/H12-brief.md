# H12 brief — RECOMMENDATIONS P0: `rolo-claude init`, prescriptive doctor, Edit context line per family, README quick start

rolo (2026-09-28): "go ahead with the P0 items, start with rolo-claude init". Source: `docs/harness/
RECOMMENDATIONS.md` §2, §3, §4 and §7 (P0). Repo `~\Documents\vibes\appDev\rolo-claude\`
(Windows build host; **Kali Linux is primary**). Baseline = tag `v0.4.0` (`57eb133`): run_all 1585,
test_bridge 97, test_tui 47, green on Windows, WSL and the Kali VM. Only worker on the tree; no
commits; no sub-agents that edit files.

## Read first
`docs/harness/RECOMMENDATIONS.md`; `rolo_claude/{doctor.py,config_cli.py,model.py,headless.py,
cli.py,catalog_cli.py}`; `rolo_claude/providers/{config.py,profiles.py,model_table.json,cc_models.py}`
(`load_env_file` and the env-file path `BRIDGE_ENV_FILE` → `~/.config/vibes-hacker/env`; the family
detection that prints `family=deepseek` on stderr; `DEFAULT_MODEL_REF` in model.py); `rolo_claude/
tools/edit.py` (`DESCRIPTION`) and `tools/registry.py` (how tool definitions are built per session);
`README.md`; `docs/harness/INSTALL.md`; `tests/helpers/fake_home.py`.

## A. `rolo-claude init` (new subcommand; the whole first run in one command)
- `rolo-claude init [--preset home|work|claude] [--model REF] [--yes] [--no-live] [--no-fixes]`.
  Interactive by default (plain prompts on stdin/stdout via `rich`, no Textual); `--yes` accepts the
  defaults; fully non-interactive with `--preset … --yes`.
- Steps, each printed as it runs, each idempotent (re-running shows the current state and lets the
  user change things):
  1. **Preset**: `home` = OpenRouter with `or:deepseek/deepseek-v4.1-flash`; `work` = Databricks with
     `dbx:databricks-deepseek-v4-1-flash` (Kimi K3 and GLM-5.3 listed as alternatives); `claude` =
     the subscription route with `cc:sonnet` (offered only when `claude auth status` reports a
     claude.ai login, using the existing cc_models helper — never the credentials file). Detect a
     sensible default: existing key → home; Databricks host/ucode settings → work; login and nothing
     else → claude.
  2. **Credentials**: OpenRouter — if no key is discoverable, ask for it with hidden input
     (`getpass`) and write `OPENROUTER_API_KEY=…` into the env file the harness already reads
     (`BRIDGE_ENV_FILE` or `~/.config/vibes-hacker/env`), creating the directory 0700 and the file
     0600 on POSIX, preserving other lines, never echoing the value, never touching shell rc files.
     Databricks — ask for host and token the same way (or accept `~/.databrickscfg` /
     `ucode-settings.json` when present). `claude` — nothing stored.
  3. **Default model**: write `model` into `~/.rolo-claude/config.json` (via the config_cli store)
     and make the TUI and headless entry points honour it: precedence `--model` → config.json
     `model` → `DEFAULT_MODEL_REF`. (Claude Code's own `settings.json` `model` stays unused, as
     designed.)
  4. **Checks**: run `doctor` (in-process) and `models --refresh` (skip refresh with `--no-live`).
  5. **Live pong** with the chosen default unless `--no-live`: `reply with the single word pong`;
     print the answer, model, provider and estimated cost line.
  6. **Linux fixes** (ask first; `--yes` accepts; `--no-fixes` skips): install a static `rg` into
     `~/.local/bin` when missing (download the ripgrep musl release for the machine's arch, verify
     it runs, else print the apt/dnf command); add `export PATH="$HOME/.local/bin:$HOME/bin:$PATH"`
     guarded by a marker comment to `~/.zshenv` (zsh) or `~/.profile` (bash/other) when
     `~/.local/bin` is missing from a non-interactive shell's PATH; mention `$EDITOR` if unset.
     On Windows: check that the `rolo-claude` launcher is on PATH and say how to add it.
  7. **Summary**: what was written (paths only), and "run `rolo-claude`" or the next fix command.
- Never writes `~/.claude.json` or `~/.claude/settings.json`; never prints a key or token; exit 0 on
  success, 2 on a usage/config error, non-zero when the pong fails (with the doctor hint).
- Wire into `cli.py` (help epilog, `rolo-claude --help`), `not_yet` untouched, README.

## B. Prescriptive doctor
- Every `[WARN]`/`[MISSING]` line ends with `-> fix: <exact command>` (or `-> see: <URL/section>`
  when there is no command). Audit every existing line.
- New checks: `~/.local/bin` on PATH for non-interactive shells (Linux; suggest the exact rc line);
  `$VISUAL`/`$EDITOR`; `tmux` mouse mode when `$TMUX` is set (`tmux show -g mouse`); `xclip` or
  `wl-copy` on Linux (clipboard fallback); `claude` presence + login state for `--chrome` and `cc:`
  (existing helper); `npx` for `--playwright`; configured MCP servers with their last measured
  connect time from the telemetry/logs when available and the `mcpLazy` hint when the total
  exceeds ~2 s; the default model in config.json and whether its provider is configured.
- `doctor --json` (machine-readable list of checks: id, status, message, fix) for `init` to reuse.

## C. Edit context line per family
- `model_table.json` gains an optional per-model/per-family `edit_hint` string; the Edit tool's
  description appended with it when the session's model family has one (computed once per session
  when the frozen catalog is built, so the cache prefix stays stable). Defaults: DeepSeek, Kimi, GLM,
  Qwen, MiniMax families: "Include at least three lines of surrounding context in old_string so the
  match is unique; if the tool reports multiple matches, add more context rather than guessing."
  Claude and GPT families: none.
- Test: a DeepSeek-profile session's Edit definition carries the line; an `ant:`/`cc:` session's
  does not; the wire request is otherwise byte-identical.

## D. README quick start
- New first section "Quick start (Kali / Linux)": install (`pipx install` or `uv tool install` from
  the repo), `rolo-claude init`, `rolo-claude`; then Windows in five lines; the rest of the README
  unchanged below it. INSTALL.md points to `init`. CHANGELOG entry; `__version__` 0.4.1.

## Tests (≥ 30, OS-neutral; temp HOME + `BRIDGE_TEST_HOME`, never the real files)
`init --preset home --yes --no-live` under a temp HOME with a fake key on stdin/env writes the env
file (POSIX mode 0600, dir 0700) and config.json `model`, never `~/.claude.json`, never prints the
key (capture stdout/stderr); re-run is idempotent and reports the current state; `--preset claude`
only offered/accepted with a faked claude.ai login (fake_claude_cc); `--preset work` with host/token;
model precedence `--model` → config.json → default in headless and TUI bootstrap; Linux fixes are
asked, skipped with `--no-fixes`, rc-file line written once with the marker (temp HOME, fake shell);
`rg` install path exercised with a fake download; doctor lines all carry a fix; `doctor --json`
schema; Edit hint per family; README first section present.

## Acceptance (Fable re-runs)
Suites green on Windows, WSL and the Kali VM. Live: on WSL under a TEMP HOME, `rolo-claude init
--preset home --yes` with the key provided via env → env file 0600, config model set, doctor OK,
pong; on the Kali VM the real `rolo-claude init` (interactive-free with `--yes`) reports the
existing key and rg/PATH as already done and pongs; `doctor` on Windows shows fixes on every WARN;
a DeepSeek session's Edit tool description carries the context line; README opens with the quick
start. Report ≤ 50 lines. Rules as in the other briefs.
