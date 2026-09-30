# H13 brief — RECOMMENDATIONS P1: lazy MCP by default, inline images, /resume search, family baselines

rolo (2026-09-29): "go ahead with the lazy MCP and the P1 items". Source: `docs/harness/
RECOMMENDATIONS.md` §3, §4, §7 (P1). Repo `~\Documents\vibes\appDev\rolo-claude\`
(Windows build host; **Kali Linux is primary**). Baseline = tag `v0.4.1` (`9d1ed9d`): run_all 1629,
test_bridge 97, test_tui 47, green on Windows, WSL and the Kali VM. Only worker on the tree; no
commits; no sub-agents that edit files. The repo is about to become PUBLIC: no home paths, LAN
addresses, usernames or key fragments in anything you add (docs, tests, fixtures, comments).

## Read first
`docs/harness/RECOMMENDATIONS.md`; `rolo_claude/mcp/{manager,client,stdio,http_sse}.py`,
`rolo_claude/agent/catalog.py`, `tools/tool_search.py`, `tui/widgets/statusbar.py`, `tui/dialogs/
{mcp_status,session_picker}.py`, `agent/sessions.py`, `tui/widgets/cards.py` (image captions),
`tools/imageutil.py`, `providers/model_table.json` + `providers/profiles.py`, `telemetry.py`,
`docs/harness/ACCEPTANCE-2026-09-25.md` (format), `reports/Open weight model adapter rules.md`.

## A. Lazy MCP start by default
- Today every configured server is spawned at session start (H9 made the lazy path parallel and
  abortable, but `mcpLazy` is opt-in). Make lazy the default: a server is connected the first time
  one of its tools is called or loaded by ToolSearch, or when `/mcp` asks for it; `alwaysLoad`
  servers and servers with `mcpLazy: false` (per-server or global) connect eagerly as today.
- The frozen catalog still needs tool names and descriptions before any server is connected: add a
  per-server tool cache under `~/.rolo-claude/mcp/tools-cache/<server>.json` keyed by a hash of the
  server's config entry (command/args/env/url/headers), written after every successful
  `tools/list`, read at session start. No cache yet (first session, or config changed) → connect
  that server eagerly once so its tools enter the catalog, then cache. Stale cache + changed tools
  on connect → refresh the catalog entries (existing `resync_from` path) and emit a notification.
- Status bar: "MCP 2/6" = connected/configured, updated live; `/mcp` shows per-server state
  (cached, connecting, connected, failed) and connect time; `doctor` reports the cache ages.
- Print mode identical. Tests: fake servers with and without cache; a tool call on a lazy server
  connects it and runs; ToolSearch finds cached tools of an unconnected server; cache invalidated
  by a config change; `alwaysLoad` eager; a server that fails on lazy connect gives the model an
  `is_error` result naming the server; status counts; startup with 6 configured servers and a full
  cache makes zero connections until a tool is used (assert).

## B. Inline images in the terminal
- Render image tool results (Read of an image, Playwright/Chrome screenshots, MCP image content)
  inline in the TUI via the kitty graphics protocol and sixel, with the current caption as the
  fallback. Evaluate the `textual-image` package (permissive licence) as an optional dependency
  (`pip install rolo-claude[images]`); if it does not fit, implement the kitty and sixel encoders
  directly (PNG/JPEG/GIF/WebP via Pillow when present). Detection: kitty, WezTerm, Ghostty, foot
  (kitty protocol), xterm/mlterm/others with sixel via the terminal query, tmux only with
  `allow-passthrough on` — otherwise caption. Config `images: inline|caption|off` in
  ~/.rolo-claude/config.json; `--no-inline-images`. Max rendered size bounded (downscale).
- Tests: encoder output shape for a small PNG (kitty APC framing, sixel header), detection matrix
  from env, fallback to caption, config off; a TUI pilot with a fake image result.

## C. `/resume` search
- The session picker (TUI `/resume` and `rolo-claude --resume` without an id) gets a text filter:
  fuzzy over title, first prompt, cwd and model, live as you type; `--resume <text>` on the CLI
  picks the unique match or opens the picker filtered. Titles come from U5; index.json holds the
  first prompt. Tests: filter ranking, unique-match shortcut, ambiguous → picker, TUI pilot.

## D. Family baselines (the data the model-table pass needs)
- Live-run, on OpenRouter, one short acceptance set per family that has no real volume yet:
  GLM-5.3 (`z-ai/glm-5.3` or the current id in models.json), Qwen (the current `qwen/qwen3.8-*`
  coding-capable id), MiniMax M3, Kimi K2.7-code and Kimi K3: pong; Read a 200-line file and answer
  a line; a Write→Edit→Bash chain on a scratch file in auto mode; one steer. Keep it cheap (Kimi K3
  is the expensive one: one pong + one Read only).
- Record per family in `docs/harness/FAMILY-BASELINE-2026-09-29.md`: model id, provider that
  answered, tool-call success, repair hits, edit failures, latency, cost (from `stats --models`),
  and any family-specific bug — fix each bug found with a pinning test (tool ids, reasoning replay,
  sampling params, edit format). Adjust `model_table.json` rows ONLY where a live failure proves
  the current value wrong; note everything else as "no change, n=1".

## E. Release
`__version__` 0.5.0; CHANGELOG; README sections for lazy MCP, inline images, `/resume` search;
`pyproject` extra `images`. Suites green on Windows, WSL and the Kali VM (`ulimit -n 4096` on
Linux); tests leave /tmp unchanged and never write the real sessions dir.

## Acceptance (Fable re-runs)
On this box with rolo's real config: a fresh session starts with "MCP 0/N" and no server
processes until a tool is used, then "MCP 1/N"; `/mcp` shows cached servers; a DeepSeek session
calls an MCP tool of a lazy server and it connects and answers; a Playwright screenshot renders
inline in kitty/WezTerm (or a pilot proves the encoder path) and captions elsewhere; `/resume`
filter finds a session by a word from its first prompt; the family baseline doc exists with real
numbers; suites green on all three platforms. Report ≤ 60 lines. Rules as in the other briefs.
