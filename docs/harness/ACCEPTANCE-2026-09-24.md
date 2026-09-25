# Acceptance report -- H8 (2026-09-24)

This is both H8's own acceptance record and the template for future
milestones: copy this file's structure (`ACCEPTANCE-<date>.md`, one per
milestone) rather than inventing a new shape each time. Sections below
mirror `docs/harness/H8-brief.md`'s own "Tests"/"Acceptance" wording.

- **Milestone**: H8 (background jobs, images/vision, notebooks, catalog
  vendoring, truncation/prompts, hooks leftovers, work-box packaging,
  README)
- **Worker**: H8 worker (Sonnet)
- **Base commit**: `1111f38` ("docs: H5c brief ... + H9 carried-over
  must-dos") -- working tree only, nothing committed by this pass
- **Environment**: Windows 11 (build/test host) + WSL Ubuntu (`~/rolo-
  claude-wt`, `~/rolo-claude-wt-venv`) via `docs/harness/INSTALL.md`'s
  cross-check recipe; OpenRouter key configured; Databricks NOT configured
  on this box (no VPN route from here)

## Suites

Final numbers, both platforms, after every change in this report (three
separate WSL sync+run passes total during this milestone, all green;
`docs/harness/INSTALL.md`'s "Cross-checking on WSL" recipe):

| Suite | Windows | WSL |
|---|---|---|
| `python tests/run_all.py` | 1314 tests, 1310 passed, 0 failed, 4 skipped | 1314 tests, 1310 passed, 0 failed, 4 skipped |
| `python test_bridge.py` | 97 tests, 391 checks, 97 passed, 0 failed, 0 skipped | 97 tests, 391 checks, 97 passed, 0 failed, 0 skipped |
| `python test_tui.py` | 42 tests, 42 passed, 0 failed, 0 skipped | 42 tests, 42 passed, 0 failed, 0 skipped |

Windows and WSL agree exactly. The 4 skips are the same on both platforms
except one Windows-only skip (`os.mkfifo` unavailable) that WSL runs for
real instead.

## Scope A -- Background Bash

`Bash(run_in_background: true)` + foreground-timeout -> background handoff,
`BashOutput`/`TaskStop` (Claude Code's real 2.1.281 tool names, confirmed
via binary string search, not guessed), a per-session `JobRegistry`,
`/tasks`, jobs killed on quit (`Controller.quit()`, `headless.py`'s
`finally` block). Tests: `tests/test_bash_background_jobs.py` (25 tests,
incl. two real concurrency-bug regressions: a `taskkill /T /F`
just-forked-grandchild race, and a drain-loop kill-signal race).

**Live**: `rolo-claude --model or:deepseek/deepseek-v3.2 -p "start sleep 20
in the background, then read README.md and tell me the line count, then
tell me when the background job finishes" --permission-mode auto` against
real OpenRouter --
- Job started for real (`shell_id bash_ea12cd7ed2`).
- Correct line count reported: **324** (README.md's real line count at
  the time, confirmed independently via `wc -l`).
- Completion delivered: `[Background job bash_ea12cd7ed2 (Start sleep 20
  seconds command in background) finished, exit code 0]` -- printed by
  `headless.py`'s `_drain_background_jobs_for_print_mode` (a single `-p`
  prompt has no later turn for the notice to arrive as a user-role message
  inside, so it's flushed to stdout before the process exits instead of
  silently dropped). PASS.

## Scope B -- Images/vision, notebooks

Read/MCP-tool image blocks gated on `profile.vision`, resized/downscaled to
OpenCode's 1568px/5MB rule (pure-stdlib dimension sniffing, optional
Pillow-based real resize -- `pip install 'rolo-claude[vision]'`), `@path`
image mentions (TUI and skill/command bodies) and `--file PATH [PATH ...]`
(a local path; Claude Code's own `file_id:relative_path` cloud-resource
form gets a clear "not available in this standalone harness" notice
instead of pretending to work), `NotebookEdit` (.ipynb cell editor, pure
JSON, no nbformat dependency).

**Found and fixed during acceptance**: a Playwright/Chrome screenshot (or
any all-image tool result) reached the TUI's `ToolCard` as the bare literal
string `"[image]"` -- genuinely un-built, not just hard to live-test (no
`playwright` package is installed in this environment to run a real
end-to-end screenshot check, which is what surfaced it: tracing the event
path in `rolo_claude/tui/dispatch.py`/`widgets/cards.py` by hand instead).
`ToolCard` renders with `Static(markup=False)` (deliberately, so untrusted
tool output can't inject Rich markup) and has no raster-image widget, so
real terminal pixel rendering was scoped OUT (would need a cross-terminal
graphics-protocol layer -- Kitty/iTerm2/Sixel -- new widget plumbing, a
much larger and riskier change than this pass should make unreviewed).
Instead, `agent/loop.py`'s `_summary_text_for_blocks` now gives an
all-image result a real caption per image (media type, dimensions sniffed
from the actual bytes, human byte size -- e.g. `"[image: image/png,
1280x800, 84.2 KB]"`), through the exact same plain-text card body/pager
pipeline every other tool result already uses. Tests: `tests/
test_loop_tools.py` (4 new: real caption, graceful degradation with
partial/bad data, multiple images, mixed-kind fallback) and `tests/
test_tools_read.py::test_loop_tools_image_result_reaches_the_tool_result_
event` (updated from asserting the old bare placeholder to asserting the
real caption, through a REAL `Session._dispatch_tools` call, not a unit
test in isolation).

Also found and fixed: `Controller.ingest_at_mentions`'s `@path` handling
didn't thread `vision` into its `ToolContext` at all (every image mention
silently got the "no vision" text note regardless of the model) and
stringified ANY list content (including a real image block) into unreadable
dict-repr text before logging it. Both fixed; regression-tested against a
test double session with no `model_profile` attribute at all (`test_tui.
py`'s own `_real_controller` fixture), which the first version of this fix
crashed against.

`--file`: moved out of `_NOT_YET_FLAGS` into `_REAL_FLAGS`; wired into both
`-p` (`headless.run_print_mode`) and the TUI (`tui/bootstrap.build_
controller`) via a new shared `headless.attach_cli_files`. Tests: `tests/
test_cli_file_attach.py` (11 tests, incl. the Windows-drive-letter-vs-
cloud-resource-spec ambiguity: `C:\...` must resolve as a real local file
before the `file_id:relative_path` heuristic ever gets a chance at it).
`tests/test_tools_notebook_edit.py` (18 tests, previously untested):
replace/insert/delete, brand-new-file creation, pre-4.5 notebooks with no
per-cell `id` getting one assigned on touch, both `source` shapes
(string vs. list of lines) on read.

## Scope C -- Catalog vendoring

`rolo-claude models --refresh`, vendored fallback in `rolo_claude/
providers/catalog/`, `doctor` catalog-age lines. **Live**: `rolo-claude
models --refresh` against real OpenRouter + models.dev -- succeeded,
wrote `~/.rolo-claude/models.json` (full real catalog, hundreds of
models) and `~/.rolo-claude/models-dev.json` (223 providers). Databricks
section correctly produced NO output (not configured on this box --
`cmd_models`'s own `if dbx is not None` gate, by design, not a bug).

## Scope D -- Truncation and prompts

2000-line/50KB backstop (OpenCode's saved-file hint) layered on top of
the per-tool cap, never looser than it; Kimi-specific prompt block; `export
--sanitize` / `stats` CLI subcommands. `export --sanitize` had a real
security-relevant bug found and fixed during this work: the secret-value
regex (`\S+`) over-matched through a JSON string's closing quote into
surrounding structure, corrupting it, and the corruption-recovery fallback
then silently returned the UNSANITIZED original node -- tightened the
regex and changed the fallback to a safe placeholder. Tests: `tests/
test_export_stats_cli.py`.

## Scope E -- Hooks leftovers

`prompt`/`agent`/`http`/`mcp_tool` hook handlers verified (already built,
confirmed against the fake MCP server); `@server:resource` mentions
(`rolo_claude/mcp/mentions.py`, parallel to the existing `@path` file-
mention convention) and `/mcp__server__prompt` slash commands
(`commands/registry.register_mcp_prompts`) newly built -- both were
genuinely unbuilt, deferred from H3. Wired into three call sites:
`Controller.ingest_at_mentions`, `headless._append_at_mention_snapshots`,
and the slash-command-expansion path. Tests: `tests/
test_mcp_resource_mentions_and_prompts.py` (11 tests against the REAL fake
MCP server -- `fake://note` resource, `greet` prompt -- not mocked).

## Scope F -- Work-box packaging

`tools/vendor_wheels.py` (new): downloads `requirements.lock`'s full
pinned closure as manylinux cp311/cp312 wheels (plus the setuptools/wheel
build backend, for `--no-build-isolation`) into a gitignored `wheels/`,
with a hand-written PEP 508 marker evaluator so a Windows build host
correctly EXCLUDES `pywin32 ; sys_platform == 'win32'` when vendoring for
a Linux work box (verified: dry-run + a direct call confirmed `pywin32`
absent from the Linux-filtered requirement list, everything else present).
`docs/harness/INSTALL.md` gained a "Work box (offline install)" section
(the vendor/install/`uv --offline` recipe, `ug`/`ucode-settings.json`
compatibility, `doctor --work`) and a "Cross-checking on WSL" section (the
rsync+venv recipe other briefs already referenced as living here).

**Live**: `rolo-claude doctor --work` on this (unconfigured, off-VPN) box
correctly reports `[MISSING] Databricks config: not configured` plus a
`ucode-settings.json: not found` WARN and `[MISSING] cannot probe --
Databricks not configured` for the reasoning-replay/route-split probes --
exit code 1, exactly the documented offline behavior (see `tests/
test_work_box.py`'s `test_doctor_work_no_databricks_configured` and
`test_doctor_work_unreachable_host_reports_vpn_hint` for the
configured-but-unreachable sibling case). A real duplicate-function-
definition bug (`_dbx_probe_target` defined twice, byte-for-byte
identical, Python silently using the later one) was found and removed
during this pass's own AST-based duplicate-name sweep of `doctor.py`.

## Scope G -- README

Full rewrite (`README.md`, 329 lines): what rolo-claude is now (its own
agent loop, not a `claude` wrapper, no local server in the harness itself),
install (Kali/Linux first, Windows second), config reuse from Claude Code,
models/providers, permissions and auto mode (quoting `permissions.py`'s
own "no classifier, no destructive-command list, no protected paths"
language directly), steering, hooks/skills/commands/MCP/browser,
images/vision (incl. the image-card caption fix above), sessions, print
mode, troubleshooting, security posture, tests, and a proxy-mode section
for `rolo-claude proxy`/`claude-bridge`. Every concrete claim in it
(subcommand list, flag choices, hook event names, permission mode count,
model-reference table) was checked against `--help` output or the
relevant source file while writing it, not carried over from memory of the
old proxy-era draft.

## Must-dos carried over from `review-findings-h4-h5-h3c.md`

- Databricks Claude passthrough auth/both-paths/thinking-replay: covered
  by existing mock coverage (`tests/test_anthropic_native.py` and
  friends); genuinely live-untestable from this box (no VPN route) --
  `doctor --work`'s VPN checklist is the documented substitute, confirmed
  above.
- `count_tokens` relay wired into the compaction gate: `agent/loop.py`'s
  `_count_tokens_via_api`, used by `_maybe_auto_compact` for
  Databricks/Anthropic-passthrough routes before falling back to the
  estimator.
- H5b's three cheap items verified landed: `compactionModel` read from
  `~/.rolo-claude/config.json` by the summariser; `anthropic-ratelimit-*-
  reset` parsed as RFC 3339; `_step`'s `Retry-After` cap at 300s (logged).

## Known gaps / explicitly deferred

- Real terminal pixel rendering for image cards (see scope B) -- a
  captioned card, not literal pixels; would need a new cross-terminal
  graphics-protocol layer.
- A live Playwright screenshot end-to-end run was not performed in this
  environment (`playwright` package not installed here) -- the fix above
  was verified via the event/log pipeline instead (`tests/
  test_tools_read.py::test_loop_tools_image_result_reaches_the_tool_
  result_event`), which exercises the identical code path a real
  screenshot tool result would.
- `--ide`, `--worktree`, `--remote-control`, `--teleport` intentionally
  remain in `_NOT_YET_FLAGS` per the brief.
