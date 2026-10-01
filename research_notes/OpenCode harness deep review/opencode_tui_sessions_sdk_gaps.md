# OpenCode harness deep review: TUI, sessions, headless/server/SDK, agents, plugins, Linux, and gap analysis vs Claude Code

Researched 2026-09-24. OpenCode = github.com/anomalyco/opencode (formerly sst/opencode), latest release v1.18.32 (2026-09-21). Claude Code reference = code.claude.com docs for 2.1.x (the box has 2.1.281 per `docs/harness/claude-help-2.1.281.txt`). "Verified" = read on a primary page (opencode.ai docs, the repo, the plugin source, GitHub issues/releases API); "secondary" = DeepWiki/blog/aggregator; inferences are marked as such. The halo plan column in the final matrix comes from the local briefs (`docs/harness/U0-brief.md`, `U2-brief.md`, `H1-brief.md`, `H5-brief.md`).

## Key question 1: The TUI (technology, layout, keybinds, commands, undo/redo, themes, mouse, images, permissions, terminals)

### Takeaway
OpenCode's TUI is no longer Go/Bubble Tea: it is a SolidJS app on OpenTUI (TypeScript API over a Zig renderer) running inside the Bun-compiled `opencode` binary, talking to its own in-process HTTP server over the SDK + SSE. Its UX signature is a leader-key (`ctrl+x`) keymap with ~150 remappable actions, a `ctrl+p` command palette, `@file#L10-20` mentions, `!` shell escape, git-snapshot-backed `/undo` `/redo`, JSON themes with a terminal-adaptive `system` theme, and inline once/always/reject permission prompts; multi-session is a session-list dialog plus parent/child navigation, not tabs (tabs exist only in the desktop app).

### Cited Findings
**Technology**
- OpenTUI is "a library to build terminal user interfaces in TypeScript, React, or Solid, on a native Zig renderer", built by Anomaly (the OpenCode team); OpenCode "uses OpenTUI in production for millions of users" — [anomalyco/opentui README](https://github.com/anomalyco/opentui/blob/main/README.md); [Better Stack OpenTUI guide](https://betterstack.com/community/guides/scaling-nodejs/opentui-react/)
- The TUI "utilizes SolidJS with the OpenTUI rendering engine", uses "a worker process pattern that isolates the rendering thread from the backend server", and communicates "via the OpencodeClient SDK using EventSource" with a `useSync` hook for reactive session state — [DeepWiki 3.1 TUI](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)) (secondary)
- Composio's 100-hour comparison (2026-06-11) credits OpenTUI ("Zig core with Bun runtime") with "60fps+ streaming", Flexbox layout reflow, JSON themes via `/theme`, mouse support and smooth scroll acceleration, versus Ink which "caps at 30fps and eats 50MB+" — [Composio](https://composio.dev/content/claude-code-vs-open-code) (secondary)
- Grokipedia claims the Go/Bubble Tea TUI was replaced because it "exhibited severe performance issues at scale", with OpenTUI prototypes appearing around August 2025 — [Grokipedia OpenTUI](https://grokipedia.com/page/OpenTUI) (AI-generated tertiary source; treat with caution)
- DeepWiki's package inventory states "No Go TUI package exists" and the TUI lives in the legacy `opencode` package; its TUI page separately refers to "legacy code in `packages/opencode/src/cli/cmd/tui.ts` and the newer standalone `packages/tui`" — [DeepWiki 1.1](https://deepwiki.com/anomalyco/opencode/1.1-repository-structure-and-packages); [DeepWiki 3.1](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)) (secondary; the two pages are not fully consistent)

**Layout and rendering**
- Main structure: a `ScrollBoxRenderable` transcript with sticky scroll, a `Prompt` component with autocomplete, and a "dynamic footer" ("split-footer direct mode": prompt + status in a dedicated footer while content scrolls above); `SubagentFooter` shows subagent status; `PermissionPrompt` and `QuestionPrompt` components intercept subagent permission/question requests — [DeepWiki 3.1](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)) (secondary)
- Sidebar and status views exist as keybinds: `sidebar_toggle` = `<leader>b`, `status_view` = `<leader>s`, `scrollbar_toggle` = none — [Keybinds docs](https://opencode.ai/docs/keybinds/)
- `tui.json` options: `diff_style` `"auto"` (adapts to terminal width) or `"stacked"` (single column); `cursor.style` block/underline/line/default + `blinking`; `mouse` (default true); `scroll_speed` (default 3) and `scroll_acceleration.enabled`; `leader_timeout` 2000 ms; `attention` (desktop notifications + sounds, volume 0–1, sound packs, per-event overrides; notifications fire when the terminal is blurred, not for subagent events); path override `OPENCODE_TUI_CONFIG` — [TUI docs](https://opencode.ai/docs/tui/)
- `/thinking` "only controls display — doesn't toggle actual reasoning. Use `ctrl+t` to cycle model variants" (variants = reasoning-effort presets); `/details` toggles tool execution details — [TUI docs](https://opencode.ai/docs/tui/)
- Command palette entries include `session.share`, `session.fork`, `session.compact`, `session.undo`, `session.redo`, `session.toggle.thinking`, `session.toggle.actions`, `session.message.next/previous`, `session.first/last`, `model.list`, `agent.list`, `theme.switch` — [DeepWiki 3.1](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)) (secondary)
- The @opencode-ai/ui shared library uses `@pierre/diffs` as its diff viewer (web/desktop, not the TUI) — [DeepWiki 1.1](https://deepwiki.com/anomalyco/opencode/1.1-repository-structure-and-packages) (secondary)

**Slash commands (TUI docs table)** — [TUI docs](https://opencode.ai/docs/tui/)
- `/connect` (add provider keys), `/compact` (`/summarize`, `ctrl+x c`), `/details`, `/editor` (`ctrl+x e`, uses `$EDITOR`; GUI editors need `--wait`), `/exit` (`/quit`, `/q`, `ctrl+x q`), `/export` (Markdown, `ctrl+x x`), `/help`, `/init` (creates/updates `AGENTS.md`), `/models` (`ctrl+x m`), `/new` (`/clear`, `ctrl+x n`), `/redo` (`ctrl+x r`, "Git-backed"), `/sessions` (`/resume`, `/continue`, `ctrl+x l`), `/share`, `/themes` (`ctrl+x t`), `/thinking`, `/undo` (`ctrl+x u`, "Git-backed"), `/unshare`
- Custom commands with the same name override built-ins — [Commands docs](https://opencode.ai/docs/commands/)

**Mentions, shell escape, editor**
- `@` = fuzzy file search (e.g. `@packages/functions/src/api/index.ts`); configured references autocomplete via `@alias`; `!` prefix runs shell commands whose output "integrates as tool results" — [TUI docs](https://opencode.ai/docs/tui/)
- Line-range mention syntax `@file.ts#10-20` is parsed by `getEditorRangeLabel`; the VS Code extension inserts references like `@File#L37-42` with `Alt+Ctrl+K` — [DeepWiki 3.1](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)); [IDE docs](https://opencode.ai/docs/ide/)
- Custom commands support `$ARGUMENTS`, `$1..$n`, `` !`cmd` `` shell injection and `@file` injection; frontmatter `description`, `template`, `agent`, `model`, `subtask` — [Commands docs](https://opencode.ai/docs/commands/)

**Keybinds (complete default table, `tui.json` → `keybinds`)** — [Keybinds docs](https://opencode.ai/docs/keybinds/)
- Leader `ctrl+x`, `leader_timeout` 2000 ms; formats: string ("a,b"), array, or object `{key, event, preventDefault, fallthrough}`; disable with `"none"` or `false`
- App: `app_exit` `ctrl+c,ctrl+d,<leader>q`; `command_list` `ctrl+p`; `editor_open` `<leader>e`; `theme_list` `<leader>t`; `sidebar_toggle` `<leader>b`; `status_view` `<leader>s`; `tips_toggle` `<leader>h`; `which_key_toggle` `ctrl+alt+k` (plus which-key layout/pending/group/scroll keys); `terminal_suspend` `ctrl+z`; hidden/no-default: `app_debug`, `app_console`, `app_heap_snapshot`, `app_toggle_animations`, `app_toggle_file_context`, `app_toggle_diffwrap`, `app_toggle_paste_summary`, `app_toggle_session_directory_filter`, `help_show`, `docs_open`, `theme_switch_mode`, `theme_mode_lock`, `scrollbar_toggle`, `terminal_title_toggle`, `plugin_manager`, `plugin_install`
- Session: `session_new` `<leader>n`; `session_list` `<leader>l`; `session_timeline` `<leader>g`; `session_rename` `ctrl+r`; `session_delete` `ctrl+d`; `session_export` `<leader>x`; `session_interrupt` `escape`; `session_compact` `<leader>c`; `session_child_first` `<leader>down`; `session_child_cycle` `right`; `session_child_cycle_reverse` `left`; `session_parent` `up`; no default: `session_fork`, `session_share`, `session_unshare`, `session_copy`, `session_move`, `session_toggle_timestamps`, `session_toggle_generic_tool_output`; `stash_delete` `ctrl+d`
- Models/agents: `model_list` `<leader>m`; `model_provider_list` `ctrl+a`; `model_favorite_toggle` `ctrl+f`; `model_cycle_recent` `f2` / `shift+f2`; `agent_list` `<leader>a`; `agent_cycle` `tab`; `agent_cycle_reverse` `shift+tab`; `variant_cycle` `ctrl+t`; no default: `model_cycle_favorite(_reverse)`, `variant_list`, `mcp_list`, `provider_connect`, `console_org_switch`
- Messages: `messages_page_up` `pageup,ctrl+alt+b`; `messages_page_down` `pagedown,ctrl+alt+f`; `messages_line_up/down` `ctrl+alt+y` / `ctrl+alt+e`; `messages_half_page_up/down` `ctrl+alt+u` / `ctrl+alt+d`; `messages_first` `ctrl+g,home`; `messages_last` `ctrl+alt+g,end`; `messages_copy` `<leader>y`; `messages_undo` `<leader>u`; `messages_redo` `<leader>r`; `messages_toggle_conceal` `<leader>h`; no default: `messages_next/previous/last_user`, `tool_details`, `display_thinking`
- Input (Emacs-style): `input_submit` `return`; `input_newline` `shift+return,ctrl+return,alt+return,ctrl+j`; `input_clear` `ctrl+c`; `input_paste` `ctrl+v`; `input_line_home/end` `ctrl+a`/`ctrl+e`; `input_delete_to_line_end` `ctrl+k`; `input_delete_to_line_start` `ctrl+u`; `input_delete_word_backward` `ctrl+w,ctrl+backspace,alt+backspace`; `input_word_forward/backward` `alt+f,alt+right,ctrl+right` / `alt+b,alt+left,ctrl+left`; `input_undo` `ctrl+-,super+z`; `input_redo` `ctrl+.,super+shift+z`; `input_select_*` shift+arrows; `input_select_all` `super+a`; `history_previous/next` `up`/`down`; prompt stash (`prompt_stash`, `prompt_stash_pop`, `prompt_stash_list`) and `prompt_skills` have no defaults
- Dialogs/autocomplete: `dialog.select.prev/next` `up,ctrl+p` / `down,ctrl+n`; `dialog.select.submit` `return`; `dialog.mcp.toggle` `space`; `prompt.autocomplete.prev/next/hide/select/complete` `up,ctrl+p` / `down,ctrl+n` / `escape` / `return` / `tab`; `permission.prompt.fullscreen` `ctrl+f`; `plugins.toggle` `space`; `dialog.plugins.install` `shift+i`
- Windows differences: `input_undo` includes `ctrl+z` and `terminal_suspend` is forced to `"none"`; Windows Terminal needs `\u001b[13;2u` mapped for Shift+Enter — [Keybinds docs](https://opencode.ai/docs/keybinds/)

**Undo/redo of file changes**
- `/undo` and `/redo` are "Git-backed" message undo/redo — [TUI docs](https://opencode.ai/docs/tui/); SDK exposes `session.revert()` / `session.unrevert()` — [SDK docs](https://opencode.ai/docs/sdk/)
- Revert "restore[s] prior states using snapshot metadata and git worktree mechanics", storing diff info in the session's `revert` field; "git shadow repositories (managed via `packages/opencode/src/worktree/index.ts`) enable safe file restoration without corrupting the primary working directory" (`packages/opencode/src/session/revert.ts`); the tool lifecycle "captures `initialSnapshot` before LLM stream starts" — [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system); [DeepWiki 2.5](https://deepwiki.com/anomalyco/opencode/2.5-tool-system-and-permissions) (secondary)
- Config key `snapshot` enables/disables file-change snapshots — [Config docs](https://opencode.ai/docs/config/)
- Over ACP, "slash commands like `/undo` and `/redo` remain unsupported" — [ACP docs](https://opencode.ai/docs/acp/)

**Themes**
- Built-ins include `system`, `tokyonight`, `everforest`, `ayu`, `catppuccin`, `gruvbox`, `kanagawa`, `nord`, `matrix`, `one-dark`; `system` "generates gray scale based on your terminal's background color" and "uses ANSI colors (0-15) for syntax highlighting"; custom JSON themes load from `~/.config/opencode/themes/*.json`, `<project>/.opencode/themes/*.json`, `./.opencode/themes/*.json`; values may be hex, ANSI 0–255, references, `{dark, light}` pairs, or `"none"`; a `defs` block gives reusable colors; 60+ color properties — [Themes docs](https://opencode.ai/docs/themes/)

**Mouse, selection, clipboard**
- `mouse: true` by default (capture on); copy via `messages_copy` `<leader>y`; on Linux copy/paste needs `xclip`/`xsel` (X11) or `wl-clipboard` (Wayland) — [TUI docs](https://opencode.ai/docs/tui/); [Troubleshooting](https://opencode.ai/docs/troubleshooting/)

**Images**
- Config key `attachment.image` sets image size limits for LLM attachments; `opencode run --file/-f` attaches files — [Config docs](https://opencode.ai/docs/config/); [CLI docs](https://opencode.ai/docs/cli/)
- v1.18.32 "Fixed Bedrock image attachments for Claude, Nova, and Llama 4 models" — [Changelog](https://opencode.ai/changelog)

**Permission prompts**
- The TUI offers `once`, `always` (whitelists matching patterns for the session), `reject`; DeepWiki lists "reject", "allow", "once", "always"; `permission.prompt.fullscreen` = `ctrl+f` expands the prompt — [Permissions docs](https://opencode.ai/docs/permissions/); [DeepWiki 2.5](https://deepwiki.com/anomalyco/opencode/2.5-tool-system-and-permissions); [Keybinds docs](https://opencode.ai/docs/keybinds/)
- `--auto` "Auto-approve non-denied permissions" on `opencode` and `opencode run` — [CLI docs](https://opencode.ai/docs/cli/)

**Terminal/tmux/SSH bugs (Linux)**
- Issue #16566 "TUI not rendering LLM response in tmux (opentui rendering bug, v1.2.21)": backend completes but the TUI never updates — [#16566](https://github.com/anomalyco/opencode/issues/16566)
- Issue #16967 "TUI broken inside Linux tmux" (Ubuntu 22.04, xterm-256color): after 1.2.24 users "sometimes cannot input messages and the UI glitches"; last working 1.2.16; opened 2026-03-11, closed 2026-04-27 by the reporter with no maintainer fix reference — [#16967 via GitHub API](https://api.github.com/repos/anomalyco/opencode/issues/16967)
- Issue #8484: screen lag/freezing when typing under WSL2 + tmux + Alacritty — [#8484](https://github.com/anomalyco/opencode/issues/8484)
- Ghostty discussion: mouse cursor flickers between caret and pointer while using OpenCode — [ghostty #10511](https://github.com/ghostty-org/ghostty/discussions/10511)
- For contrast, Claude Code's own tmux flicker issue (#37076) attributes full-viewport redraws to Ink; a differential renderer and DEC 2026 synchronized output were the fixes — [anthropics/claude-code #37076](https://github.com/anthropics/claude-code/issues/37076); [HN "Claude Chill"](https://news.ycombinator.com/item?id=46699072)

### Inferences
- The TUI has no session tabs; concurrency is modelled as separate sessions (list dialog) plus a parent→child navigation axis (`<leader>down`, `left/right`, `up`). Tabs are a desktop-app concept (see Q2). halo's "one Textual app = one session + picker" plan is therefore already at OpenCode-TUI parity; a child-session navigation axis for sub-agents would be the cheap next step.
- Both `packages/tui` references and the "worker process" note suggest the TUI has been split into its own process/package during 2026 while still shipping inside the single binary; for a Python harness the transferable idea is the strict TUI ↔ engine split over an event stream, which halo already has (Controller/queue in U2).
- `permission.prompt.fullscreen` (ctrl+f) implies permission prompts are compact inline cards by default with an expand affordance; halo's `PermissionCard` matches, but should add an expand key for long diffs/commands.
- The "system" theme (derive greys from the terminal background, use ANSI 0–15 for syntax) is the lowest-effort way to look native in kitty/xterm/tmux on Kali; Textual's `ansi_color` mode approximates it.

### Gaps
- No primary documentation found for the exact footer/status-bar fields (model, context %, cost, git branch) or the sidebar's contents; DeepWiki's TUI page explicitly lacks them.
- No primary documentation of image paste/drag-and-drop mechanics in the TUI (only the `attachment.image` config and `--file` flag).
- No documentation of kitty-keyboard-protocol handling or SSH-specific behaviour; only issue reports for tmux.
- Tool-call rendering details (collapsed vs expanded, side-by-side diff at wide widths) are only implied by `diff_style: auto|stacked` and `/details`.

## Key question 2: Sessions (storage, share, fork, resume, titles, export, child sessions, projects, worktrees)

### Takeaway
Sessions live in one SQLite file (`~/.local/share/opencode/opencode.db`) with a 2026 event-sourced schema (`session_v2`, `session_message`) layered over frozen v1 tables; the JSON→SQLite migration lost sessions for some users. Sessions bind to a project derived from the git root (a "global" project catches non-git dirs), get titles from `small_model`, can be forked (`--fork`, `POST /session/:id/fork`), resumed (`-c`, `-s`), shared to public `opncd.ai/s/<id>` links, exported/imported as JSON, and spawn child sessions for sub-agents.

### Cited Findings
- SQLite path: `~/.local/share/opencode/opencode.db`; legacy per-file JSON under `~/.local/share/opencode/storage/session`; the schema source is `packages/opencode/src/session/session.sql.ts` — [Agent Sessions guide](https://jazzyalex.github.io/agent-sessions/guides/opencode-sqlite-history.html) (secondary; the raw file 404'd on the `dev` branch when fetched 2026-09-24, so it has moved)
- "the legacy session / message / part tables are frozen and all new sessions and messages are written to new event-sourced tables (`session_v2`, `session_message`) in the same SQLite database"; `session_message` rows carry `seq`, `type` (role), `tokens`, `cost`, `model: {providerID, id}`; assistant content is projected from `content[]` — [memex PR #195](https://github.com/nicosuave/memex/pull/195) (secondary, third-party reader)
- Migration bugs: "The migration gate assumes DB exists = sessions already migrated" so incremental upgraders' JSON sessions were "permanently orphaned" — [#13654](https://github.com/anomalyco/opencode/issues/13654); "[BUG] SQLite Migration Ate My Sessions" — [#13636](https://github.com/anomalyco/opencode/issues/13636); Windows path normalisation changed project hashes — [#36178](https://github.com/anomalyco/opencode/issues/36178); v1 sessions from non-git dirs landed in project `global` and lost the leading `/` — [#50521](https://github.com/anomalyco/opencode/issues/50521)
- Sessions use "descending ULIDs for chronological sorting", link to projects via `projectID`, and reference parents via `parentID` "enabling fork/child session hierarchies"; projects are "resolved from git repository roots", with "a special 'global' project", and "standard projects map to worktrees" (`packages/opencode/src/project/project.ts`) — [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system) (secondary)
- Session IDs look like `ses_XXXXXXXXXXXXXXXXXXXX` — [takopi cheatsheet](https://takopi.dev/reference/runners/opencode/stream-json-cheatsheet/)
- App data dir holds `auth.json`, logs, and per-project data in `./<project-slug>/storage/` or `./global/storage/` — [Troubleshooting](https://opencode.ai/docs/troubleshooting/)
- Message parts: `text`, `reasoning`, `tool`, `file`, `agent`, `compaction`, `snapshot`, `subtask` (`packages/opencode/src/session/message-v2.ts`), plus step-start/step-finish markers — [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system); [DeepWiki overview](https://deepwiki.com/anomalyco/opencode) (secondary)
- Titles: `small_model` = "Lighter model for tasks like title generation"; title generation "leverages a smaller model" (`packages/opencode/src/session/prompt.ts`) — [Config docs](https://opencode.ai/docs/config/); [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system)
- Resume/fork: `opencode --continue/-c`, `--session/-s <id>`, `--fork` ("Fork session when continuing"); same flags on `opencode run`; TUI `/sessions` (aliases `/resume`, `/continue`) — [CLI docs](https://opencode.ai/docs/cli/); [TUI docs](https://opencode.ai/docs/tui/); HTTP `POST /session/:sessionID/fork` — [DeepWiki 2.6](https://deepwiki.com/anomalyco/opencode/2.6-http-server-and-rest-api)
- Share: `/share` copies a unique public URL at `opncd.ai/s/<share-id>`; `/unshare` "remove[s] the share link and delete[s] the data"; modes `"share": "manual"|"auto"|"disabled"`; uploads "full conversation history, messages, responses, and session metadata"; enterprises can disable, SSO-restrict, or self-host — [Share docs](https://opencode.ai/docs/share/); `opencode run --share` — [CLI docs](https://opencode.ai/docs/cli/)
- Export/import: `opencode export [sessionID] --sanitize` (JSON, redacts secrets), `opencode import <file|url>` (JSON or share URL), `opencode session list [-n] [--format table|json]`, `opencode session delete <id>`, TUI `/export` → Markdown — [CLI docs](https://opencode.ai/docs/cli/); [TUI docs](https://opencode.ai/docs/tui/)
- Stats: `opencode stats --days --tools --models --project` — [CLI docs](https://opencode.ai/docs/cli/)
- Child sessions: the `task` tool "creates child sessions with fresh context (empty message history), inherited `parentID`, assigned agent and model" and "returns text summary, task ID, and resumability status to parent" (`packages/opencode/src/tool/task.ts`) — [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system); v1.18.20: "Subagent tool failures now surface with resumable `task_id`" — [Changelog](https://opencode.ai/changelog)
- Compaction: config `compaction` (auto, prune, reserved tokens); v1.18.17 "Session compaction keeps complete recent turns with clearer summaries"; overflow check `SessionCompaction.isOverflow` (`compaction.ts:166-169`); tool outputs truncated to 2,000 chars during compaction with `skill` protected — [Config docs](https://opencode.ai/docs/config/); [Changelog](https://opencode.ai/changelog); [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system)
- Worktrees: plugins receive `worktree` (git worktree root) and `directory`; `experimental_workspace.register(type, adapter)` exists in `PluginInput` — [plugin/src/index.ts](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/plugin/src/index.ts). A July 2026 blog claims CLI `opencode worktree create|list|remove|reset` and a TUI "warp dialog", and that the desktop "does not support Worktrees yet — coming soon" — [explainx](https://www.explainx.ai/blog/opencode-desktop-tabs-sessions-worktrees-july-2026) (secondary; the official CLI docs page fetched 2026-09-24 lists no `worktree` subcommand). Community plugins fill the gap: kdcokenny/opencode-worktree, felixAnhalt/opencode-worktree-session, saitakarcesme/open-trees — [kdcokenny/opencode-worktree](https://github.com/kdcokenny/opencode-worktree); [opencode-worktree-session](https://github.com/felixAnhalt/opencode-worktree-session)
- Desktop app: rebuilt around tabs in July 2026 (each tab a new or reopened session from any project) — [explainx](https://www.explainx.ai/blog/opencode-desktop-tabs-sessions-worktrees-july-2026) (secondary); issues show a Projects sidebar and exact-match `session.directory` filtering hiding older sessions — [#49032](https://github.com/anomalyco/opencode/issues/49032); [#49401](https://github.com/anomalyco/opencode/issues/49401); [#49561](https://github.com/anomalyco/opencode/issues/49561)

### Inferences
- OpenCode's project keying (git-root hash, with a `global` bucket and worktree mapping) is the same shape as Claude Code's `~/.claude/projects/<slug>` and halo's `~/.halo/sessions/<slug>/<id>.jsonl` (H1). The migration incidents argue for keeping halo's append-only JSONL as the source of truth and adding a derived SQLite index only for listing/search, never as the only copy.
- "Descending ULIDs" is a cheap trick worth copying: newest-first ordering falls out of a plain sort of IDs.
- The `snapshot`/`revert` fields on messages mean OpenCode's undo is message-granular (undo the last user message and its file effects), not file-granular; Claude Code's `/rewind` checkpoints are the same idea. halo can implement it with a shadow git repo (`git --git-dir=~/.halo/snapshots/<slug> --work-tree=<cwd>`) without touching the user's `.git`.

### Gaps
- Exact current table/column list of `session_v2`/`session_message` and where the schema file moved could not be read (404 on `session.sql.ts`); the version that introduced SQLite is only reported by third parties (an oh-my-openagent issue says "beta v1.1.53+"; not verified on a primary source).
- No primary source confirms an upstream `opencode worktree` CLI subcommand or TUI "warp dialog"; treat as unverified.
- Share-link retention policy beyond "until you unshare" is not documented.

## Key question 3: Headless, server, SDK, plugin hooks, ACP, web/desktop

### Takeaway
OpenCode is server-first: every client (TUI, `run`, web, desktop, IDE plugins, ACP) drives the same Hono HTTP server with an OpenAPI 3.1 spec at `/doc` and SSE at `/event` / `/global/event`; `opencode run --format json` is a lossy JSONL projection of that stream (no text deltas, missing user prompt, occasional missing final `step_finish`), while the JS SDK gives full fidelity. Plugins are JS/TS modules with ~21 typed mutating hooks (`chat.message`, `chat.params`, `permission.ask`, `tool.execute.before/after`, compaction/system-prompt transforms) plus a catch-all `event` stream; `opencode acp` speaks JSON-RPC over stdio for Zed/JetBrains/Neovim.

### Cited Findings
**`opencode run`** — [CLI docs](https://opencode.ai/docs/cli/)
- Flags: `--command`, `--continue/-c`, `--session/-s`, `--fork`, `--share`, `--model/-m provider/model`, `--agent`, `--file/-f`, `--format default|json`, `--title`, `--attach <url>` (connect to a running server), `--dir`, `--port`, `--variant`, `--thinking`, `--auto`; global `--print-logs`, `--log-level`, `--pure`, `--help`, `--version`
- `--format json` writes JSONL; event types `step_start`, `tool_use` (only when `status == "completed"`), `text`, `step_finish` (`reason` stop|tool-calls, `snapshot`, `cost`, `tokens` {input, output, reasoning, cache read/write}), `error` — [takopi cheatsheet](https://takopi.dev/reference/runners/opencode/stream-json-cheatsheet/) (secondary, derived from observed output)
- Known defects: no real-time `message.part.delta` events (only one complete `text` event per part) — [#38638](https://github.com/anomalyco/opencode/issues/38638); the user prompt is never emitted — [#29997](https://github.com/anomalyco/opencode/issues/29997); the process can exit before the final `step_finish` — [#26855](https://github.com/anomalyco/opencode/issues/26855); subagent parts are dropped because parts are filtered against the root session — [#49300](https://github.com/anomalyco/opencode/issues/49300); text/step-finish events dropped in containers — [#31435](https://github.com/anomalyco/opencode/issues/31435)

**Server** — [Server docs](https://opencode.ai/docs/server/)
- `opencode serve --port 4096 --hostname 127.0.0.1 --mdns --mdns-domain opencode.local --cors <origin>...`; `opencode web` takes the same flags — [CLI docs](https://opencode.ai/docs/cli/)
- Auth: `OPENCODE_SERVER_PASSWORD` enables HTTP basic auth (username `opencode` or `OPENCODE_SERVER_USERNAME`); DeepWiki adds `auth_token` query param support and the `x-opencode-directory` header for multi-project use — [Server docs](https://opencode.ai/docs/server/); [DeepWiki 2.6](https://deepwiki.com/anomalyco/opencode/2.6-http-server-and-rest-api)
- OpenAPI 3.1 at `http://<host>:<port>/doc`; endpoint groups: global (health, event stream), project, path/VCS, config (+providers), provider (+OAuth), sessions (CRUD, fork, share, diff, permissions), messages (list/send, async prompts, shell), commands, files (search files/symbols, read, status), tools (experimental), LSP/formatter/MCP status (dynamic MCP add), agents, TUI control (`/tui` endpoints used by IDE plugins), auth; SSE via `/event` or `/global/event` with `{type, properties}` payloads; WebSocket PTY for remote shells — [Server docs](https://opencode.ai/docs/server/); [DeepWiki 2.6](https://deepwiki.com/anomalyco/opencode/2.6-http-server-and-rest-api)
- Remote config: organisations can serve defaults from a `.well-known/opencode` endpoint (lowest precedence); managed config at `/etc/opencode/` on Linux; macOS MDM highest — [Config docs](https://opencode.ai/docs/config/)

**SDK** — [SDK docs](https://opencode.ai/docs/sdk/)
- `@opencode-ai/sdk`: `createOpencode()` (starts server + client; hostname, port, signal, timeout, config) and `createOpencodeClient()` (baseUrl, fetch, throwOnError…); methods `session.list/create/get/prompt (optional structured output)/messages/message/revert/unrevert/share/unshare/delete/abort/command/shell`, `find.text/files/symbols`, `file.read/status`, `config.get/providers`, `app.agents`, `global.health`, `tui.appendPrompt/submitPrompt/clearPrompt/openHelp/openSessions/openModels/openThemes/executeCommand/showToast`, `event.subscribe()`, `auth.set()`; types generated from the OpenAPI spec; only JS/TS documented
- Community lists `opencode-sdk-go` and `opencode-sdk-python` as official repos — [awesome-opencode](https://github.com/awesome-opencode/awesome-opencode) (secondary; not on opencode.ai)
- Monorepo (per DeepWiki 1.1): `opencode` (Bun/TS, Hono, Drizzle ORM, Effect), V2 packages `@opencode-ai/{cli,core,schema,protocol,client,llm}`, `@opencode-ai/plugin`, `@opencode-ai/{ui,app,session-ui}` (SolidJS; `app` embeds a `ghostty-web` terminal), `@opencode-ai/web` (Astro docs), `@opencode-ai/desktop` ("Electron-based desktop client" with `@lydell/node-pty`), console/enterprise/stats packages — [DeepWiki 1.1](https://deepwiki.com/anomalyco/opencode/1.1-repository-structure-and-packages) (secondary). Conflict: issue #16874 (early 2026) and search summaries call the desktop app "Tauri-based" — [#16874](https://github.com/anomalyco/opencode/issues/16874)

**Plugin hooks (`packages/plugin/src/index.ts`, `Hooks` interface)** — [plugin source](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/plugin/src/index.ts)
- `event` ({event}), `config` (Config), `tool` (custom tools map), `auth`, `provider`, `dispose`
- `chat.message` → mutate `{message: UserMessage, parts: Part[]}`; `chat.params` → `{temperature, topP, topK, maxOutputTokens, options}`; `chat.headers` → `{headers}`
- `permission.ask` (Permission → `{status: "ask"|"deny"|"allow"}`)
- `command.execute.before` ({command, sessionID, arguments} → `{parts}`)
- `tool.execute.before` ({tool, sessionID, callID} → `{args}`); `tool.execute.after` (… → `{title, output, metadata}`); `tool.definition` ({toolID} → `{description, parameters}`)
- `shell.env` ({cwd, sessionID?, callID?} → `{env}`)
- Experimental: `experimental.chat.messages.transform`, `experimental.chat.system.transform` (→ `{system: string[]}`), `experimental.provider.small_model`, `experimental.session.compacting` (→ `{context: string[], prompt?}`), `experimental.compaction.autocontinue`, `experimental.text.complete`
- `PluginInput`: `client`, `project`, `directory`, `worktree`, `serverUrl`, `$` (Bun shell), `experimental_workspace.register`
- Event bus names exposed to `event` hooks: `command.executed`, `file.edited`, `file.watcher.updated`, `installation.updated`, `lsp.client.diagnostics`, `lsp.updated`, `message.part.removed/updated`, `message.removed/updated`, `permission.asked/replied`, `server.connected`, `session.created/compacted/deleted/diff/error/idle/status/updated`, `shell.env`, `todo.updated`, `tui.prompt.append`, `tui.command.execute`, `tui.toast.show` — [Plugins docs](https://opencode.ai/docs/plugins/)
- Discovery order: global `opencode.json` `plugin` array → project `opencode.json` → `~/.config/opencode/plugins/` → `.opencode/plugins/`; npm packages are installed with Bun into `~/.cache/opencode/node_modules/`; `opencode plugin <module> [-g] [-f]` installs — [Plugins docs](https://opencode.ai/docs/plugins/); [CLI docs](https://opencode.ai/docs/cli/)

**ACP** — [ACP docs](https://opencode.ai/docs/acp/)
- `opencode acp` runs OpenCode "as an ACP-compatible subprocess" over JSON-RPC on stdio; editors: Zed (ACP Registry via `zed: acp registry`, or custom executable in `~/.config/zed/settings.json`), JetBrains (`acp.json`), Avante.nvim (`acp_providers`), CodeCompanion.nvim; full tools/MCP/AGENTS.md/formatters/permissions work, `/undo` `/redo` do not — [ACP docs](https://opencode.ai/docs/acp/); [Zed ACP agent page](https://zed.dev/acp/agent/opencode)
- v1.18.31 "Restored ACP session model, effort, mode, and reasoning chunk boundaries" on resume — [Changelog](https://opencode.ai/changelog)

**IDE, web, desktop**
- VS Code/Cursor/Windsurf/VSCodium extension auto-installs on first `opencode` run in the integrated terminal; `Ctrl+Esc` opens/focuses a split-terminal session, `Ctrl+Shift+Esc` new session, `Alt+Ctrl+K` inserts `@File#L37-42`; requires `code`/`cursor`/`windsurf`/`codium` CLI on PATH — [IDE docs](https://opencode.ai/docs/ide/)
- Desktop app is "available as beta through releases page or opencode.ai/download"; Linux assets in v1.18.32: `.deb` 117.49 MB (amd64), `.AppImage` 151.68 MB (x86_64), `.rpm` 101.52 MB — [README](https://github.com/anomalyco/opencode); [releases/latest API](https://api.github.com/repos/anomalyco/opencode/releases/latest)
- Community alternatives to the web UI exist (OpenGUI Electron app, pk-opencode-webui prefix-aware reimplementation), implying the official web UI has been a pain point — [DEV: OpenGUI](https://dev.to/akemmanuel/i-built-a-native-desktop-gui-for-opencode-in-4-days-with-ai-p44); [pk-opencode-webui](https://github.com/prokube/pk-opencode-webui) (secondary)

### Inferences
- The lossy `--format json` is the single most-cited pain point in OpenCode's headless story; Claude Code's `stream-json` (with `--include-partial-messages` deltas and `--input-format stream-json` for bidirectional control) is strictly ahead. halo's U0 `--output-format stream-json` design should keep deltas and echo the user prompt line, precisely because OpenCode users file bugs when those are missing.
- OpenCode's "every client is an HTTP client of the same server" architecture is what makes `--attach`, the web UI, IDE `/tui` control and ACP nearly free. For halo, exposing the existing Controller queue over a loopback HTTP+SSE server (halo already runs a loopback bridge server) would unlock remote/attach and a future web view without a rewrite; ACP would be a separate adapter on top.
- `permission.ask` and `tool.execute.before` are the two plugin hooks that carry most of the value of Claude Code's `PreToolUse`/`PermissionRequest`; a Python harness gets the same by letting hook scripts return a JSON decision, which is what Claude Code's hook contract already specifies.

### Gaps
- The full list of SSE event types and their `properties` schemas was not extracted (needs a running server's `/doc`).
- Whether `opencode run --format json` gained delta events after the cited issues could not be confirmed on a primary page.
- Electron vs Tauri for the desktop app is unresolved between sources (likely a 2026 migration to Electron, but not verified).

## Key question 4: Agents and modes (primary vs sub-agents, config, Tab switching, task tool)

### Takeaway
OpenCode's "modes" are agents: `build` (all tools) and `plan` (edit/bash default to ask) are primary agents cycled with Tab; `general`, `explore`, `scout` are sub-agents invoked by `@name` or the `task` tool into fresh child sessions with restricted permissions; agents are Markdown files with frontmatter (model, prompt, permission, temperature/top_p, steps, mode, hidden, color) and can be generated with `opencode agent create`.

### Cited Findings
- Primary agents: **Build** (default, all tools) and **Plan** ("file edits and bash commands default to 'ask' mode"); sub-agents: **General** (full access, multi-step research/parallel work), **Explore** (read-only codebase explorer), **Scout** (read-only external dependency/library research) — [Agents docs](https://opencode.ai/docs/agents/)
- Switching: **Tab** cycles primary agents (`agent_cycle`/`agent_cycle_reverse` = `tab`/`shift+tab`, `agent_list` = `<leader>a`); sub-agents are invoked by typing `@general …`; child-session navigation via `session_child_first`, `session_child_cycle`, `session_parent` — [Agents docs](https://opencode.ai/docs/agents/); [Keybinds docs](https://opencode.ai/docs/keybinds/)
- Config: `opencode.json` `agent` key or Markdown in `.opencode/agents/` and `~/.config/opencode/agents/`; fields `model`, `prompt` (file), `permission` (allow/ask/deny per tool), `temperature`, `top_p`, `steps` ("Limit agentic iterations before text-only response"), `description` (required), `mode` (`primary|subagent|all`), `hidden`, `color`; `opencode agent create [--path] [--description] [--mode] [--permissions] [-m model]`; `default_agent` and `subagent_depth` are top-level config keys — [Agents docs](https://opencode.ai/docs/agents/); [CLI docs](https://opencode.ai/docs/cli/); [Config docs](https://opencode.ai/docs/config/)
- Task tool: child sessions with fresh context and inherited `parentID`; returns "text summary, task ID, and resumability status"; "Background Subagents" require `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`; subagent permissions are "derived/restricted: disallows `todowrite` and `task` unless explicitly granted"; `subagent_depth` limits recursion — [DeepWiki 2.3](https://deepwiki.com/anomalyco/opencode/2.3-session-and-agent-system); [DeepWiki 2.5](https://deepwiki.com/anomalyco/opencode/2.5-tool-system-and-permissions) (secondary)
- Permission grammar: keys `read`, `edit`, `glob`, `grep`, `bash`, `task`, `skill`, `lsp`, `question`, `webfetch`, `websearch`, `external_directory`, `doom_loop`; values `allow|ask|deny`; patterns with `*`, `?`, `~`/`$HOME`; "Last matching rule wins"; defaults mostly allow except `doom_loop` ask, `external_directory` ask, `.env` blocked from read; agent rules override global — [Permissions docs](https://opencode.ai/docs/permissions/)
- Tool registry (DeepWiki 2.5): `read`, `edit`, `write`, `grep` (ripgrep), `glob`, `bash` (`tool/shell.ts`), `task`, `skill`, `apply_patch`, `question`, `webfetch`, `execute` (CodeMode sandbox); tools are `Tool.define(id, init)` with `execute(params, ctx)` where ctx has `sessionID`, `messageID`, `agent`, `abort`, `metadata()`, `ask()`; file locks via a path-keyed semaphore, line-ending/BOM preservation, LSP `touchFile()` after edits — [DeepWiki 2.5](https://deepwiki.com/anomalyco/opencode/2.5-tool-system-and-permissions) (secondary)
- Composio reports OpenCode's bash permission matching uses "shell parsing via tree-sitter" — [Composio](https://composio.dev/content/claude-code-vs-open-code) (secondary; not confirmed on a primary page)

### Inferences
- "Plan" in OpenCode is just an agent whose permission table says ask; there is no plan file, no `ExitPlanMode`, no plan-approval card. Claude Code's plan mode (and halo's H6 `PlanCard`) is richer. The reusable part is the *Tab-to-cycle* affordance and showing the active agent in the footer.
- `steps` (max iterations per agent) is a small, useful safety knob halo can expose per agent alongside `--max-turns`.
- Sub-agent output returning as "summary + task_id + resumable" plus a navigable child session is a better UX than a flat text return; halo's H6 "sub-agent nesting placeholder" should carry the child's transcript so the user can dive in (`<leader>down`) and later resume by task id.

### Gaps
- Whether `steps` defaults exist or whether the `task` tool's background mode is still gated behind the experimental env var in 1.18.x was not verifiable on a primary page.

## Key question 5: Plugins and integrations (ecosystem, LSP, formatters, GitHub Action, Anthropic auth, Databricks/enterprise)

### Takeaway
The plugin ecosystem is large and JS-only (notify, DCP context pruning, oh-my-opencode, worktree managers, memory plugins, OTel exporters, a "Claude Code-compatible hooks" plugin); LSP (30+ servers, some auto-downloaded) and formatters (~20+) are built in but off by default; the GitHub Action reacts to `/oc` comments; Anthropic Pro/Max login was removed in 1.3.0 after Anthropic's January 2026 block, so Anthropic access is API-key or gateway only; Databricks reaches OpenCode via `ucode`, which registers only `databricks-anthropic` and `databricks-google` providers.

### Cited Findings
- Notable plugins (awesome-opencode): Opencode Notify (native OS notifications), OpenCode ntfy.sh, Opencode Telemetry (local SQLite cost rollups), Token Tracker, Dynamic Context Pruning, Opencode Worktree, Background/Background Agents, Oh My Opencode, Agent Memory / Harness Memory / Opencode Mem / Honcho, CC Safety Net, Envsitter Guard, opencode-review, Opencode Hooks Plugin ("Claude Code-compatible hooks"), opencode-plugin-otel, Opencode Agents Sidebar, OpenCode Agent Tmux, Autotitle, Antigravity/Gemini/Codex/Kilo auth plugins, OpenCode LiteLLM — [awesome-opencode](https://github.com/awesome-opencode/awesome-opencode)
- opencode-notify: "native OS notifications when tasks complete, errors occur, or the AI needs your input", 37+ terminals supported, installed via `ocx add kdco/notify --from https://registry.kdco.dev` — [kdcokenny/opencode-notify](https://github.com/kdcokenny/opencode-notify)
- DCP: "replaces pruned content with placeholders before sending requests"; a model-invoked `compress` tool; automatic strategies (deduplication, write superseding, error purging) at zero LLM cost; config at `$OPENCODE_CONFIG_DIR/dcp.jsonc` or `.opencode/dcp.jsonc` — [Opencode-DCP README](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning/blob/master/README.md)
- LSP: 30+ built-in servers (pyright, gopls, jdtls, clangd, …); "LSP is disabled by default. When enabled, servers start when one of the above file extensions is detected"; auto-install for Astro, Bash, Kotlin, Lua, PHP Intelephense, Svelte, Terraform, Tinymist, Vue, YAML; `OPENCODE_DISABLE_LSP_DOWNLOAD` stops downloads; config `lsp.<name>: {command, extensions, disabled, env, initialization}`; the docs themselves warn servers "can get out of sync, use significant memory … and slow down agent workflows" and suggest CLI linters instead — [LSP docs](https://opencode.ai/docs/lsp/)
- Diagnostics reach the model as feedback after edits (edit/write call `LSP.Service.touchFile()`); `lsp.client.diagnostics` is a bus event — [DeepWiki 2.5](https://deepwiki.com/anomalyco/opencode/2.5-tool-system-and-permissions); [Plugins docs](https://opencode.ai/docs/plugins/)
- Formatters: disabled by default; `"formatter": true` enables built-ins (Prettier, Biome, gofmt, ruff, rustfmt, clang-format, ~20 others); run "after files are written or edited"; custom `{command: [..., "$FILE"], extensions, environment, disabled}`; `formatter: false` disables all — [Formatters docs](https://opencode.ai/docs/formatters/)
- GitHub Action: `opencode github install` sets up app + workflow + secrets; triggers on `/opencode` or `/oc` comments, schedules, PR/issue events, manual dispatch; needs `ANTHROPIC_API_KEY` (or other provider) + `model:`; `uses: anomalyco/opencode/github@latest`; triages issues, implements fixes and opens PRs — [GitHub docs](https://opencode.ai/docs/github/)
- Anthropic auth: the providers page says of Pro/Max plugins "Anthropic explicitly prohibits this" and "Previous versions of OpenCode came bundled with these plugins but that is no longer the case as of 1.3.0"; it contrasts ChatGPT Plus, GitHub Copilot and GitLab Duo, which support subscription login; 75+ providers via AI SDK + Models.dev; local providers (Ollama `http://localhost:11434/v1`, LM Studio, llama.cpp) via custom provider config; OpenCode Zen (curated models) and OpenCode Go (low-cost subscription) — [Providers docs](https://opencode.ai/docs/providers/)
- Timeline of the block: "On January 9, 2026, Anthropic deployed server-side checks that began rejecting OAuth tokens from third-party tools"; February ToS added an "Authentication and credential use" section banning subscription OAuth in third-party tools — [NxCode](https://www.nxcode.io/resources/news/opencode-blocked-anthropic-2026); [ZBuild](https://www.zbuild.io/resources/news/opencode-blocked-anthropic-2026); [DEV](https://dev.to/mcrolly/anthropic-kills-claude-subscription-access-for-third-party-tools-like-openclaw-what-it-means-for-3ipc) (secondary news)
- Databricks: `ucode` is "a lightweight launcher for running Codex, Claude Code, Gemini CLI, OpenCode, GitHub Copilot CLI, and Pi through Databricks"; `ucode configure --agents opencode` registers only `databricks-anthropic` and `databricks-google` and silently drops GPT-5/Codex Responses models — [databricks/ucode #97](https://github.com/databricks/ucode/issues/97); `ug opencode` has no `--provider` option for Unity Catalog Model Provider Services — [unity-gateway #608](https://github.com/databricks/unity-gateway/issues/608); official page — [Databricks: Connect OpenCode](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-opencode)
- Skills: `SKILL.md` discovered in `.opencode/skills/`, `.claude/skills/`, `.agents/skills/` (project, walking up the worktree) and `~/.config/opencode/skills/`, `~/.claude/skills/`, `~/.agents/skills/`; frontmatter `name` (`^[a-z0-9]+(-[a-z0-9]+)*$`, must equal the directory), `description`, optional `license`, `compatibility`, `metadata`; a native `skill` tool (`skill({name})`); `permission.skill` patterns like `internal-*` — [Skills docs](https://opencode.ai/docs/skills/)
- Rules: `AGENTS.md` (project, walking up) and `~/.config/opencode/AGENTS.md`; falls back to `CLAUDE.md` and `~/.claude/CLAUDE.md` unless `OPENCODE_DISABLE_CLAUDE_CODE` is set; `instructions` config array accepts globs and remote URLs (5 s timeout); `/init` generates `AGENTS.md` — [Rules docs](https://opencode.ai/docs/rules/)
- MCP: `mcp.<name>: {type: local|remote, command|url, environment|headers, enabled, timeout (5000 ms), oauth}`; OAuth via Dynamic Client Registration (RFC 7591) with `opencode mcp auth/list/logout/debug`; tokens in `~/.local/share/opencode/mcp-auth.json`; tools named `servername_toolname`; disable globally via `tools: {"my-mcp*": false}` or per agent — [MCP docs](https://opencode.ai/docs/mcp-servers/)
- Config precedence: remote `.well-known/opencode` → `~/.config/opencode/opencode.json` → `OPENCODE_CONFIG` → project `opencode.json(c)` → `.opencode/` dirs → `OPENCODE_CONFIG_CONTENT` → managed (`/etc/opencode/` on Linux) → macOS MDM; files merge; `{env:VAR}` and `{file:path}` substitution; schema at `https://opencode.ai/config.json` — [Config docs](https://opencode.ai/docs/config/)

### Inferences
- OpenCode reads `~/.claude/skills`, `.claude/skills` and `CLAUDE.md`, i.e. it deliberately consumes Claude Code's on-disk conventions. halo, which targets `~/.claude` natively, is at parity by construction; adding `AGENTS.md` as a fallback (the inverse of OpenCode's fallback) costs almost nothing and makes halo usable in OpenCode-configured repos.
- The DCP plugin's placeholder-pruning ("session history never modified, pruned content replaced before send") is the same design as halo's H5 "OpenCode pruning" item; the model-invoked `compress` tool is the novel bit worth copying.
- Because Anthropic Pro/Max OAuth is now contractually off-limits for third-party harnesses, halo's existing stance (subscription untouched, bridge only for non-Claude/gateway routes; API key or Databricks passthrough for Claude) is the only defensible one; no OpenCode trick to copy here.

### Gaps
- The providers page summary also listed "Claude Pro/Max OAuth login (opens browser)" among Anthropic auth methods while stating the plugins were removed in 1.3.0; whether a browser-login path still exists for API-console accounts (not consumer subscriptions) could not be disambiguated from the fetched text.
- No primary source for how LSP diagnostics are formatted in the tool result (position/severity/limit).

## Key question 6: Gap analysis — Claude Code vs OpenCode, both directions

### Takeaway
Claude Code leads on the harness "operating system": 33+ hook events with five handler types and seven config scopes, plan mode as a first-class permission mode, background bash + `/tasks`, native worktrees with hooks, full-fidelity `stream-json` in and out, budget caps, MCP scopes, Remote Control/cloud/agent teams, and a fullscreen renderer. OpenCode leads on openness and surfaces: 75+ providers with OAuth for non-Anthropic subscriptions, server/OpenAPI/SSE-first design with a JS SDK and ACP, share links, bundled LSP/formatters, a fully remappable leader-key TUI with JSON themes, `stats`/`export`/`import`, and a desktop/web app.

### Cited Findings
**Claude Code (verified on code.claude.com, 2.1.x)**
- Hooks: 33 named events (`SessionStart`, `SessionEnd`, `Setup`, `UserPromptSubmit`, `UserPromptExpansion`, `Stop`, `StopFailure`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied`, `SubagentStart/Stop`, `TaskCreated/Completed`, `TeammateIdle`, `InstructionsLoaded`, `ConfigChange`, `CwdChanged`, `DirectoryAdded`, `FileChanged`, `WorktreeCreate/Remove`, `PreCompact/PostCompact`, `PreModelSwitch/PostModelSwitch`, `Notification`, `MessageDisplay`, `Elicitation/ElicitationResult`; the page says 35 total); handler types `command`, `http`, `mcp_tool`, `prompt`, `agent`; scopes user/project/local/managed/plugin/skill-frontmatter/subagent-frontmatter; matchers, `if: "Bash(rm *)"` filters, exit-code-2 blocking, `/hooks` viewer — [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- CLI: `-p`, `--output-format text|json|stream-json`, `--input-format text|stream-json`, `--json-schema`, `--continue`, `--resume`, `--fork-session`, `--name`, `--model`, `--effort`, `--agent`, `--agents` (JSON), `--permission-mode default|acceptEdits|plan|auto|bypassPermissions`, `--allowedTools/--disallowedTools`, `--max-turns`, `--max-budget-usd`, `--mcp-config`, `--add-dir`, `--settings`, `--bare`, `--chrome`, `--cloud`, `--background/--bg`; subcommands `auth`, `mcp`, `plugin`, `agents`, `doctor`, `update`, `install`, `setup-token`, `remote-control`, `project purge`, `ultrareview` — [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- Interactive: fullscreen rendering with `?` help, `{`/`}` prompt jumps, `[` dump to native scrollback, `v` open transcript in `$EDITOR`, mouse on `/` and `@` lists; `Ctrl+B` backgrounds a running Bash call ("Tmux users must press `Ctrl+B` twice"), `/tasks` lists background shells/subagents, memory-pressure reaping (v2.1.193+); `Ctrl+R` history search with session/project/all scopes; `/diff` panel; `/btw` side questions (fork into a background subagent with `f`); `/teleport`, `/tui`, Remote Control, agent teams; away-recap; next-prompt suggestions — [Claude Code interactive mode](https://code.claude.com/docs/en/interactive-mode)
- LSP: officially in the 2.0.74 changelog (previously `ENABLE_LSP_TOOL=1`), servers delivered via plugin marketplaces such as Piebald-AI/claude-code-lsps — [Scott Spence](https://scottspence.com/posts/enable-lsp-in-claude-code); [claude-code-lsps](https://github.com/Piebald-AI/claude-code-lsps) (secondary)
- Composio: Claude Code "provides OS-level sandboxing and safety layers but offers no mechanism for users to 'see it or switch it off'"; OpenCode "permits inspection and modification of compaction logic, permission rules, and shell parsing" — [Composio](https://composio.dev/content/claude-code-vs-open-code) (secondary)

**Adoption/market signals (secondary)**
- GitHub stars 209.8k / forks 27.7k / 4.7k open issues on the OpenCode repo (2026-09) — [anomalyco/opencode](https://github.com/anomalyco/opencode); an aggregator put OpenCode at 191,904 stars vs Claude Code 139,886, but Claude Code's npm downloads at 44.3M/month vs 8.2M — [tech-insider](https://tech-insider.org/opencode-vs-claude-code-2026/) (unverified aggregator)
- Builder.io head-to-head (early 2026, Sonnet 4.5 on both): "OpenCode took 78 percent longer overall" because it ran full test suites and safety checks by default — [Parallel.ai](https://parallel.ai/articles/opencode-vs-claude-code-a-2026-comparison-for-developers) (secondary, quoting Builder.io)

### Inferences (the gap analysis proper)
**Where Claude Code is ahead (OpenCode lacks or is weaker)**
1. Hook surface: 33+ lifecycle events × 5 handler types × 7 scopes, shell/HTTP-native, versus ~21 JS-only plugin hooks + a JS event stream. OpenCode has no `UserPromptSubmit`-equivalent blocking hook for shell scripts, no `SessionStart` context injection contract, and no per-skill/per-agent hooks. (A community "Opencode Hooks Plugin" reimplements Claude-style hooks — evidence of demand.)
2. Memory: Claude Code's CLAUDE.md hierarchy + `.claude/rules/*.md` + auto-memory vs OpenCode's AGENTS.md + `instructions` globs/URLs; persistent memory in OpenCode is plugin-land (Agent Memory, Honcho, Mem…).
3. Plan mode: a permission mode with plan files and approval, vs an agent with ask-permissions.
4. Background bash with `Ctrl+B`, `/tasks`, memory-pressure reaping, vs experimental background subagents and a "Background" plugin.
5. Worktrees: `--worktree`, `WorktreeCreate/Remove` hooks vs unverified CLI + plugins.
6. Headless I/O: `stream-json` with partial deltas and `--input-format stream-json` bidirectional control, `--json-schema`, vs a lossy JSONL projection with open bugs.
7. Budget/cost governance: `--max-budget-usd`, `--max-turns` in core; OpenCode has `steps` per agent and post-hoc `opencode stats`.
8. Permission grammar: `Tool(pattern)` rules with modes and `if:` hook filters; OpenCode's `<permission, pattern, action>` triples are comparable but lack modes like `acceptEdits`.
9. MCP: three scopes (local/project/user) + managed + elicitation hooks vs config-file merge only.
10. Remote/cloud: Remote Control, `--cloud`, `/teleport`, agent teams; OpenCode has `serve`/`--attach`/mDNS and a desktop app instead.
11. Renderer: Claude Code's fullscreen renderer now has scrollback dump (`[`) and editor view (`v`); OpenCode relies on OpenTUI's alternate-screen scroll box.

**Where OpenCode is ahead (Claude Code lacks)**
1. Multi-provider (75+) with OAuth for ChatGPT Plus/Pro, GitHub Copilot, GitLab Duo, plus local Ollama/LM Studio/llama.cpp configs and gateway (Cloudflare, Databricks via ucode, Bedrock/Vertex/Azure with Entra ID).
2. Server-first: OpenAPI 3.1 at `/doc`, SSE bus, basic-auth, mDNS, `--attach`, remote `.well-known` config, and an official JS SDK with TUI-control endpoints; Claude Code's equivalent is the Agent SDK + Remote Control, not an open HTTP API.
3. ACP for Zed/JetBrains/Neovim (Claude Code has IDE extensions but, on the pages read, no ACP server).
4. Share links (`opncd.ai/s/<id>`) with `/unshare`, `export --sanitize`, `import` from a share URL, `opencode stats`.
5. Bundled LSP (30+ servers, auto-download) and formatters, off by default; Claude Code needs marketplace LSP plugins.
6. TUI ergonomics: leader-key keymap with ~150 remappable actions in `tui.json`, which-key overlay, JSON themes + `system` theme, model favourites/recent cycling (F2), variant cycling (ctrl+t), prompt stash, `question` tool with a dedicated dialog.
7. Git-backed `/undo` `/redo` on messages — although Claude Code's `/rewind` checkpoints are close, OpenCode exposes it via the API (`session.revert/unrevert`).
8. Web/desktop app with tabs and a ghostty-web terminal, built from the same server.
9. Config as merged JSONC with `{env:}`/`{file:}` substitution and a published JSON schema.

**What this means for halo (Python/Textual, Kali-first)** — see the matrix and ranked list in Key question 8.

### Gaps
- Morphllm's "September 2026" comparison page returned HTTP 429 and could not be read; only its search snippet ("closed-source, model-locked" vs "open-source, model-agnostic") is available.
- No benchmark with both tools on identical 2026 models beyond the Builder.io citation.

## Key question 7: Linux/Kali operational details (install, size, config dirs, bundled tools, known Debian issues)

### Takeaway
On Kali the practical path is `curl -fsSL https://opencode.ai/install | bash` (a ~58 MB `opencode-linux-x64.tar.gz`, or the ~60 MB musl build), with state split across `~/.config/opencode` (config, agents, commands, skills, plugins, themes), `~/.local/share/opencode` (`auth.json`, `opencode.db`, logs, `mcp-auth.json`) and `~/.cache/opencode` (plugin `node_modules`, auto-downloaded `bin/rg`); the recurring Linux failures are ripgrep auto-download (proxy/offline/aarch64-musl), glibc on old Ubuntu (not a Kali problem), tmux rendering regressions, and clipboard needing `xclip`/`wl-clipboard`.

### Cited Findings
- Install: `curl -fsSL https://opencode.ai/install | bash`; `npm|bun|pnpm install -g opencode-ai` / `yarn global add opencode-ai`; `brew install anomalyco/tap/opencode`; `paru -S opencode-bin` (AUR); `mise use -g github:anomalyco/opencode`; `docker run -it --rm ghcr.io/anomalyco/opencode`; Windows: Chocolatey/Scoop, "Bun support on Windows is currently in development; WSL is recommended"; Nix and Pacman are listed in the README — [Install docs](https://opencode.ai/docs/); [README](https://github.com/anomalyco/opencode)
- Binary sizes (v1.18.32, 2026-09-21): `opencode-linux-x64.tar.gz` 57.79 MB, `opencode-linux-x64-musl.tar.gz` 60.09 MB, `opencode-linux-arm64.tar.gz` 57.62 MB; desktop `.deb` 117.49 MB, `.AppImage` 151.68 MB, `.rpm` 101.52 MB — [releases/latest API](https://api.github.com/repos/anomalyco/opencode/releases/latest)
- Directories: config `~/.config/opencode/` (`opencode.json`, `tui.json`, `agents/`, `commands/`, `skills/`, `plugins/`, `themes/`, `AGENTS.md`); data `~/.local/share/opencode/` (`auth.json`, `log/` keeping the 10 most recent files, `opencode.db`, `mcp-auth.json`, per-project `storage/`); cache `~/.cache/opencode` (provider packages, plugin `node_modules/`, `bin/rg`); managed `/etc/opencode/` — [Troubleshooting](https://opencode.ai/docs/troubleshooting/); [Plugins docs](https://opencode.ai/docs/plugins/); [MCP docs](https://opencode.ai/docs/mcp-servers/); [Config docs](https://opencode.ai/docs/config/); [#51022](https://github.com/anomalyco/opencode/issues/51022)
- ripgrep: downloaded to `~/.cache/opencode/bin/rg`; a manually placed binary is picked up; failures behind proxies (#51022), offline (#21646), aarch64 containers requesting a non-existent musl build (#589), broken after 1.14.18 (#23411); docs did not say rg is required (#4420) — [#51022](https://github.com/anomalyco/opencode/issues/51022); [#21646](https://github.com/anomalyco/opencode/issues/21646); [#589](https://github.com/anomalyco/opencode/issues/589); [#23411](https://github.com/anomalyco/opencode/issues/23411); [#4420](https://github.com/anomalyco/opencode/issues/4420)
- glibc: Ubuntu 20.04 users hit missing `GLIBC_2.34`/`GLIBC_2.32` — [#238](https://github.com/anomalyco/opencode/issues/238); a musl asset now ships (release list above)
- Clipboard: X11 needs `xclip` or `xsel`, Wayland `wl-clipboard` — [Troubleshooting](https://opencode.ai/docs/troubleshooting/)
- Logs: `~/.local/share/opencode/log/`; `--print-logs`, `--log-level` global flags; `OPENCODE_DISABLE_AUTOUPDATE`, `autoupdate: true|false|"notify"` — [Troubleshooting](https://opencode.ai/docs/troubleshooting/); [CLI docs](https://opencode.ai/docs/cli/); [Config docs](https://opencode.ai/docs/config/)
- Shell: config key `shell` selects the shell for terminal execution (pwsh, bash, zsh…) — [Config docs](https://opencode.ai/docs/config/)
- tmux regressions on Linux: #16566 (1.2.21), #16967 (1.2.24, Ubuntu 22.04), #8484 (WSL2) — cited in Q1
- Desktop-app resets: delete `opencode.settings.dat` / `opencode.global.dat`, clear cache, disable plugins — [Troubleshooting](https://opencode.ai/docs/troubleshooting/)

### Inferences
- Kali (Debian testing-based, current glibc) will not hit the glibc issue; the musl tarball is the safe choice for containers/VMs. The one Linux dependency to pre-install on the Kali VM is ripgrep (`apt install ripgrep` then symlink or copy to `~/.cache/opencode/bin/rg`) and a clipboard tool.
- No evidence of an `fzf` dependency; fuzzy matching is in-process (the TUI's own autocomplete).
- For halo, the XDG split (config / data / cache) is the right convention on Kali; today halo keeps everything under `~/.halo/` (H1), which is simpler but mixes secrets, logs and sessions.

### Gaps
- Install-script env vars (`OPENCODE_INSTALL_DIR`, `XDG_BIN_DIR`) were not visible in the fetched install page text; not verified.
- No Kali-specific reports found (searches returned Ubuntu/Debian/WSL issues only).
- Peak RSS of the TUI on Linux is not documented anywhere found.

## Key question 8: Feature-parity matrix and what halo should adopt

### Takeaway
Of ~40 compared capabilities, halo's briefs already target Claude Code parity on the core loop (permissions, hooks, compaction, stream-json, slash commands, skills, memory); the highest-value OpenCode ideas to fold in are the leader-key/which-key keymap with a `tui.json`-style remap file, git-shadow undo/redo, a loopback HTTP+SSE server with `--attach`, child-session navigation for sub-agents, `stats`/`export --sanitize`, a `system` theme, formatter/LSP-diagnostic post-edit feedback, and OpenCode's XDG directory split.

### Cited Findings
- halo plan facts come from the local briefs: Textual 8.2.8 TUI with `Transcript`, `PromptInput` (1–8 lines), `StatusBar` (model, context %, cost, mode glyph, cwd + git branch, MCP n/m, spinner), `PermissionCard` (1/y once, 2/a session, 3 always), `QuestionCard`, `PlanCard` (H6), dialogs ModelPicker/SessionPicker/McpStatus/PermissionsDialog/Help/HistorySearch (Ctrl+R), keys Enter/`\`+Enter/Ctrl+J/Alt+Enter/Esc — `C:\Users\user\Documents\vibes\appDev\halo\docs\harness\U2-brief.md`; CLI flag parity with Claude Code 2.1.281 incl. `--permission-mode acceptEdits|auto|bypassPermissions|manual|dontAsk|plan`, slash registry (`/help /clear /compact /cost /context /model /mcp /memory /permissions /plan /resume /status /config /skills /agents /effort /init /doctor /export /add-dir /theme /exit`), custom `.claude/commands/**/*.md` with `$ARGUMENTS`, `` !`cmd` `` gated by `allowed-tools`, skills as `/name`, `--output-format stream-json` — `docs\harness\U0-brief.md`; append-only JSONL session log `~/.halo/sessions/<slug>/<id>.jsonl` — `docs\harness\H1-brief.md`; compaction "dsh replay + OpenCode pruning", PreCompact/PostCompact/SessionStart(compact) hooks, `CostMeter`, `/cost` and `/context` — `docs\harness\H5-brief.md`
- All OpenCode/Claude Code cells below are sourced in Key questions 1–7 above (same URLs).

### Inferences
**Feature-parity matrix (CC = Claude Code 2.1.x; OC = OpenCode 1.18.x; RC = halo plan per briefs; Rec = recommendation)**

| # | Capability | Claude Code 2.1.x | OpenCode 1.18.x | halo plan (briefs) | Recommendation |
|---|---|---|---|---|---|
| 1 | TUI framework | Ink/React + newer fullscreen renderer | OpenTUI (SolidJS + Zig) in Bun binary | Textual 8.2.8 (Python) | Keep Textual; enforce 30 Hz drain + delta coalescing (U2) to avoid Ink-style flicker |
| 2 | Layout: transcript + prompt + status | Yes; fullscreen adds `?`, `[` scrollback dump, `v` editor | ScrollBox + prompt + split footer; sidebar `<leader>b`; status view `<leader>s` | Transcript, PromptInput, StatusBar | Add `[`-style "dump transcript to native scrollback" and `v` open-in-editor (cheap, huge for tmux users) |
| 3 | Session tabs in TUI | No (background sessions + attach) | No (desktop app has tabs) | No | Defer; add child-session navigation instead |
| 4 | Keybind remapping | `~/.claude/keybindings.json`, chords | `tui.json` `keybinds`, ~150 actions, leader `ctrl+x`, which-key overlay | `tui/keys.py` fixed defaults | Adopt a keymap file + leader key + which-key hint overlay |
| 5 | Command palette | `/` autocomplete lists | `ctrl+p` palette + `/` | `/` and `@` completion | Add `ctrl+p` palette over the U0 command registry |
| 6 | `@file` mentions | Yes, fuzzy | Yes, fuzzy, `@file#10-20` ranges, `@alias` | Yes | Add `#L10-20` range syntax and configured aliases |
| 7 | `!` shell escape | Yes (`!` bash mode) | Yes (output becomes tool result) | Planned via commands `!` injection | Add interactive `!` prefix in the prompt |
| 8 | Undo/redo file changes | `/rewind` checkpoints, Esc Esc | `/undo` `/redo` git shadow repo; API revert/unrevert | Not in briefs | Adopt shadow-git snapshots per user turn |
| 9 | Themes | `/theme` (dark/light/daltonized/ANSI) | JSON themes, `system` theme from terminal colours | claude-dark/light/daltonized/ansi (U0) | Add `system` theme (ANSI 0–15 + derived greys) |
| 10 | Mouse / native selection | Fullscreen: mouse on lists | `mouse: true` capture toggle; copy `<leader>y` | Not specified | Ship `mouse` toggle + copy-last-message key; document xclip/wl-clipboard |
| 11 | Image attachments | Paste/Ctrl+V | `--file`, `attachment.image` limits | Not in briefs | Defer to post-U2 (Textual paste of image bytes is limited) |
| 12 | Permission prompt UX | Inline y/n/always, `Shift+Tab` modes | once/always/reject, `ctrl+f` fullscreen, `--auto` | PermissionCard 1/2/3 + free-text "do differently" | Add expand key for long diffs; keep Claude modes |
| 13 | Permission grammar | `Tool(pattern)` allow/ask/deny + modes | `<perm, pattern, action>`, last-match wins, `external_directory`, `doom_loop` | Claude-style rules (`permissions.py`) | Add `doom_loop` detector and `external_directory` ask |
| 14 | Plan mode | First-class mode + plan file | `plan` agent (edits/bash ask) | PlanCard + `/plan` (H6) | Keep Claude semantics |
| 15 | Agent switching | `--agent`, `/agents` | Tab cycles primary agents; `@sub` mention | `--agent` flag | Add Tab-cycle + footer badge |
| 16 | Sub-agents | Agent tool, teams, background | `task` → child session, fresh ctx, resumable `task_id`, nav keys | Nesting placeholder (H6) | Model sub-agents as child sessions with `<leader>down`/`up` navigation |
| 17 | Background bash | `Ctrl+B`, `/tasks`, reaping | Experimental background subagents; plugins | Not in briefs | Adopt Claude's `Ctrl+B` + `/tasks` |
| 18 | Hooks | 33+ events, 5 types, 7 scopes | ~21 JS plugin hooks + event bus | Claude-compatible hooks (H4) | Keep Claude contract; expose `permission.ask`-like decision path |
| 19 | Plugins | Marketplaces, hooks/skills/agents bundles | JS modules via npm/Bun, `opencode plugin` | Not in briefs | Defer; skills + hooks cover most needs |
| 20 | Skills | `SKILL.md`, frontmatter hooks | `SKILL.md` incl. `~/.claude/skills`, `skill` tool | Skills as `/name` (U0) | Parity; optionally read `.opencode/skills` too |
| 21 | Memory / rules | CLAUDE.md chain, rules, auto-memory | AGENTS.md (+ CLAUDE.md fallback), `instructions` URLs | CLAUDE.md chain + memory index (H1/H5) | Add `AGENTS.md` fallback |
| 22 | Custom slash commands | `.claude/commands`, `$ARGUMENTS` | `.opencode/commands`, `$1..`, `!`cmd``, `@file`, `subtask` | `.claude/commands` with `$0..$9`, `!` gating | Parity; consider `subtask:` to run a command in a child session |
| 23 | MCP | local/project/user scopes, elicitation | config merge, OAuth DCR, `servername_tool`, per-agent | `mcp` subcommand parity (U0) | Parity |
| 24 | LSP diagnostics | Since 2.0.74 via plugins | 30+ bundled, auto-download, off by default | None | Cheap subset: run `ruff`/`pyright`/`tsc` after edits and append diagnostics |
| 25 | Formatters after edit | No built-in | ~20 built-ins, `$FILE` custom | None | Add opt-in formatter map |
| 26 | Sessions storage | JSONL under `~/.claude/projects/<slug>` | SQLite `opencode.db` (v2 event-sourced) | JSONL `~/.halo/sessions/<slug>/<id>.jsonl` | Keep JSONL; add SQLite index only for search |
| 27 | Resume/fork | `-c`, `-r`, `--fork-session`, `--name` | `-c`, `-s`, `--fork`, `/sessions`, `ctrl+r` rename | `--continue/--resume`, SessionPicker | Add fork + rename |
| 28 | Session titles | Auto (small model) | `small_model` | `--small-model` exists | Wire title generation |
| 29 | Share links | No public share | `/share` → `opncd.ai/s/<id>`, `/unshare` | No | Skip (privacy); offer `export --sanitize` |
| 30 | Export/import | `/export` | `opencode export --sanitize`, `import <file|url>`, `/export` md | `/export` (U0) | Add `--sanitize` and import |
| 31 | Cost/usage | `/cost`, `/usage`, `--max-budget-usd` | `step_finish.cost`, `opencode stats` | CostMeter, `/cost`, `/context` (H5) | Add `stats` subcommand over JSONL; add `--max-budget-usd` |
| 32 | Headless output | `stream-json` w/ deltas, `--json-schema`, `--input-format stream-json` | `--format json` JSONL, lossy | `stream-json` (U0) | Keep deltas + user echo + final result line; consider `--json-schema` |
| 33 | Server / attach | Remote Control, cloud | `serve`, OpenAPI `/doc`, SSE, `--attach`, mDNS, basic auth | Loopback bridge server only | Expose Controller over loopback HTTP+SSE; `--attach` |
| 34 | SDK | Agent SDK (TS/Python) | `@opencode-ai/sdk` (TS), community Go/Python | None | A thin Python client over the HTTP API once #33 lands |
| 35 | ACP (editors) | IDE extensions | `opencode acp` (Zed/JetBrains/Neovim) | None | Defer; low value on a Kali TUI box |
| 36 | Worktrees | `--worktree`, hooks | Plugins; CLI unverified | Not in briefs | Adopt Claude's `--worktree` semantics later |
| 37 | Multi-provider auth | Anthropic + Bedrock/Vertex/Foundry gateways | 75+ providers, OAuth for ChatGPT/Copilot/GitLab | Bridge routes (OpenRouter, Databricks, `ant:`) | Parity for rolo's needs |
| 38 | Compaction | Auto + `/compact`, Pre/PostCompact hooks | `compaction` config, prune, DCP plugin | dsh replay + pruning + hooks (H5) | Add model-invoked `compress` tool (DCP idea) |
| 39 | Notifications | `Notification` hook, terminal bell | `attention` sounds/desktop notifications when blurred | Not in briefs | Add bell + optional `notify-send` on Linux when unfocused |
| 40 | Config format | settings.json chain (user/project/local/managed) | JSONC merge chain incl. remote `.well-known`, `{env:}`/`{file:}` | Claude settings chain | Parity; maybe `{env:}` substitution |
| 41 | Linux state dirs | `~/.claude` | XDG config/data/cache split | `~/.halo` | Consider XDG split on Kali |
| 42 | tmux/SSH robustness | Historic flicker; sync-output fix | Regressions 1.2.21–1.2.24 | Untested | Test in tmux + kitty + xterm on Kali; use Textual's synchronized output |

**Ranked: what halo should adopt from OpenCode (effort: S ≈ ≤1 day, M ≈ 2–4 days, L ≈ 1–2 weeks)**
1. **Leader-key keymap + which-key overlay + `keybinds` remap file** (M). Mirrors OpenCode's `tui.json`; keep Claude Code's defaults where they exist (Ctrl+R, Ctrl+B, Shift+Tab) and put everything else under a leader (`ctrl+x`) so nothing collides with tmux/readline. Source: Q1 keybinds table.
2. **Git-shadow snapshots → `/undo` `/redo`** (M). Snapshot the work tree in `~/.halo/snapshots/<slug>` before each assistant turn; `/undo` reverts files and truncates the JSONL log; expose as Controller commands so headless can call them. Source: Q1/Q2 revert findings.
3. **Sub-agents as navigable child sessions** (M, builds on H6). Store child logs as separate JSONL with `parentID`; return `summary + task_id + resumable`; keys `<leader>down` / `left/right` / `up`. Source: Q4.
4. **Loopback HTTP+SSE server with `--attach`** (L). Wrap the existing Controller queue in a small ASGI app (starlette/uvicorn are already in the venv); `halo serve` and `halo --attach http://…`; publish an OpenAPI doc; later a web view. Source: Q3.
5. **`stats`, `export --sanitize`, `import`** (S–M). Walk the JSONL logs: per-day/model/tool token+cost tables; sanitised export for bug reports. Source: Q2.
6. **`system` theme** (S). Textual `ansi_color=True` plus greys derived from the terminal background; solves kitty/xterm/tmux colour mismatches on Kali. Source: Q1 themes.
7. **Post-edit diagnostics/formatter feedback** (M). Not full LSP: after Write/Edit, run a configured linter/formatter (`ruff`, `pyright`, `tsc`, `prettier`) and append a ≤N-line diagnostics block to the tool result, matching OpenCode's `touchFile()` + formatter pattern; keep it off by default as OpenCode does. Source: Q5.
8. **`doom_loop` and `external_directory` permission guards** (S). Identical-call detection → ask; absolute paths outside cwd/add-dirs → ask. Source: Q4 permissions.
9. **`ctrl+p` command palette + `@file#L10-20` + interactive `!` prefix** (S each). Source: Q1.
10. **Attention notifications** (S). Terminal bell + `notify-send` when the Textual app is unfocused, for permission/question/idle events; mirrors OpenCode's `attention` block. Source: Q1.
11. **Session title via small model + `/rename` + fork** (S). Source: Q2.
12. **Model-invoked `compress` tool (DCP)** (M, extends H5). Lets the model prune stale tool outputs itself instead of waiting for the compaction threshold. Source: Q5.
13. **XDG directory split + `ripgrep` preflight on Kali** (S). `~/.config/halo`, `~/.local/share/halo`, `~/.cache/halo`; `halo doctor` checks `rg`, `xclip`/`wl-copy`, `$EDITOR`. Source: Q7.
14. **`AGENTS.md` fallback and `.opencode/skills` discovery** (S). Makes halo drop-in for OpenCode-configured repos. Source: Q5.
15. **ACP adapter** (L). Only if Zed/Neovim use on the Kali box becomes real; otherwise skip.

**Explicitly not worth copying**: public share links (privacy; Claude Code deliberately lacks them), the JS-only plugin model (halo's Claude-compatible shell/HTTP hooks are more portable), the lossy `--format json` shape, and a desktop/web app before the TUI is complete.

### Gaps
- The matrix's "halo plan" column reflects the briefs available on disk (U0, U2, H1, H5, H6 references), not the implemented code; items marked "Not in briefs" may exist in `wip/spec*.md` files that were not read.
- Effort estimates are judgement calls for a Python/Textual codebase and are not sourced.
