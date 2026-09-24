# H8 brief — background jobs, images/vision, notebooks, catalog vendoring, README (rolo-claude)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux primary**).
Baseline = the U5 commit on master, all suites green on Windows and WSL. Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → milestone table row H8/H6 ("Background Bash,
   images/vision, notebooks, `prompt`/`http`/`mcp_tool` hooks, `@server:resource`, work-box acceptance,
   `tools/vendor_wheels.py`"), "Primary use case", "Closing milestone H9".
2. `reports/OpenCode harness deep review.md` → H8 rows (2000-line / 50 KB truncation cap with the
   saved-file hint text, image resize/omit rule, models.dev `api.json` vendoring, Kimi prompt block,
   `export --sanitize` + `stats`) and the dsh report's background-job pattern (foreground timeout →
   background, completion as a user-role notice in the next step, `job_output/list/kill`).
3. `docs/harness/claude-help-2.1.281.txt` (any flags still printing "not supported yet" — this
   milestone should clear all that can be cleared: `--ide`, `--worktree`, `--remote-control`,
   `--teleport` stay unsupported with a clear notice), `docs/harness/INSTALL.md`, `README.md`
   (still describes the proxy era — rewrite).
4. Current `tools/{bash,_proc,read,webfetch}.py`, `agent/loop.py` (dispatch), `providers/
   databricks.py` (probe), `model.py` (`models.json`), `doctor.py`, `not_yet.py`.

## Scope
A. **Background Bash** (`run_in_background: true`, and foreground commands that exceed their timeout
   are moved to the background instead of killed — dsh): `BashOutput`/`KillShell`-style tools with
   Claude Code's names (`TaskOutput`? check the 2.1.281 tool names via `claude --help`/binary strings:
   Claude Code uses `Bash(run_in_background)` + `TaskOutput`/`TaskStop` in this era — mirror the real
   names), job registry per session, completion delivered as a user-role notice at the next step,
   `/tasks` in the TUI, jobs killed on quit.
B. **Images/vision**: Read returns `image` blocks for png/jpg/gif/webp when `profile.vision`; resize/
   downscale to ≤ 1 568 px and ≤ 5 MB (OpenCode's rule), omit with a note otherwise; pasted/attached
   images in the TUI (`--file`-style attach via `@path` for images), MCP image results, screenshots
   from Playwright/Chrome shown as cards; `NotebookEdit` tool (Claude Code's `.ipynb` cell editor).
C. **Catalog vendoring**: `rolo-claude models --refresh` pulls OpenRouter `/api/v1/models` and
   models.dev `api.json` into `~/.rolo-claude/` with a vendored fallback copy in the package
   (`rolo_claude/providers/catalog/`), so profiles resolve offline (work box); Databricks endpoint
   list cached by `probe`; `doctor` shows catalog ages.
D. **Truncation and prompts**: Bash/tool output cap 2 000 lines / 50 KB with OpenCode's saved-file
   hint text (in addition to the token-based spill); Kimi-specific prompt block from OpenCode's
   `kimi.txt` adapted into the per-family notation section; `export --sanitize` and `stats` CLI
   subcommands (headless versions of U5's slash commands).
E. **Hooks leftovers**: `prompt`/`agent` hook handlers already exist — verify; `http` and `mcp_tool`
   handlers verified against a local server + the fake MCP server; `@server:resource` mentions and
   `/mcp__server__prompt` expansion (H3 deferred them).
F. **Packaging for the work box**: `tools/vendor_wheels.py` (manylinux cp311/cp312 wheels for
   textual/rich/mcp/pydantic-core into a gitignored `wheels/`), `pip install --no-index --find-links`
   recipe in INSTALL.md, `uv tool install` path, `ug`-compatibility note (Databricks' unity-gateway CLI
   writes `~/.claude/ucode-settings.json`; read it if present for the gateway URL/token — same
   discovery chain), a `rolo-claude doctor --work` preset that checks VPN reachability of the
   Databricks host, token validity (`GET /api/2.0/serving-endpoints` or a 1-token completion), and the
   two open questions from the plan (reasoning replay forwarded? route split per model?) as
   runnable probes with clear output.
G. **README rewrite** for the harness (what it is, install on Kali/Linux and Windows, first run,
   config reuse from Claude Code, models and providers incl. Databricks, permissions and auto mode,
   steering, hooks/skills/commands/MCP/browser, sessions, print mode, troubleshooting, proxy mode
   section, security posture, tests), plus `docs/harness/ACCEPTANCE-<date>.md` template.

## Tests
Background job lifecycle (start/poll/kill/quit), timeout → background conversion, completion notice
ordering; image block gating + resize; NotebookEdit round trip on a fixture notebook; catalog
refresh + vendored fallback; truncation cap text; export sanitiser removes keys/tokens; stats math;
http/mcp_tool hook handlers; `@server:resource` expansion; doctor --work output shape with a mocked
Databricks host.

## Acceptance
All suites green on Windows and WSL; live: `-p "start sleep 20 in the background, then read
README.md and tell me the line count, then tell me when the background job finishes" --permission-mode
auto` → line count first, completion notice later; a Playwright screenshot rendered as an image
card in the TUI; `rolo-claude models --refresh`; `rolo-claude doctor --work` (offline here: reports
unreachable with the VPN hint); README reviewed by Fable.
Report ≤ 50 lines. Rules as in the other briefs.

## Must-dos carried over from `review-findings-h4-h5-h3c.md` (H8 section) — added 2026-09-24
- Work-box acceptance for the Databricks Claude passthrough: auth (H5b added the bearer header
  inside the databricks branch — verify with the headers `build_session` produces), both paths
  (`/ai-gateway/anthropic/v1/messages?beta=true` first, `/serving-endpoints/<name>/invocations`
  fallback), thinking replay with signatures across a tool loop. Offline here: keep the mock
  coverage and write the VPN checklist into `doctor --work` output.
- Wire the `count_tokens` relay (`providers/http.py` has it, nothing calls it): use it for the
  compaction gate when the route supports it (Databricks/Anthropic), with the estimator as fallback.
- Verify H5b's three cheap items landed: `compactionModel` from `~/.rolo-claude/config.json` used by
  the summariser; `anthropic-ratelimit-*-reset` parsed as RFC 3339; `_step` caps `Retry-After` at
  300 s instead of 60 s (logged).
