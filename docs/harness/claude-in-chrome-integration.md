# Claude in Chrome — how Claude Code wires it, and how rolo-claude reuses it (verified 2026-09-23)

Verified read-only on rolo's box: Claude Code 2.1.281, Claude in Chrome extension 1.0.94
(id `fcoeoabgfenejglbffodgkkbkcdhcgfn`). Nothing modified.

## The chain Claude Code uses
```
agent ⇄ MCP/JSON-RPC over stdio ⇄ [claude.exe --claude-in-chrome-mcp]
      ⇄ named pipe \\.\pipe\claude-mcp-browser-bridge-<username>  (POSIX: /tmp/claude-mcp-browser-bridge-<user>/<pid>.sock)
      ⇄ [claude.exe --chrome-native-host]   (launched by Chrome via C:\Users\user\.claude\chrome\chrome-native-host.bat)
      ⇄ Chrome native messaging (4-byte little-endian length + UTF-8 JSON, max 1 MiB) ⇄ extension service worker ⇄ pages
```
- Claude Code registers an in-process MCP server definition
  `{type:"stdio", command: process.execPath, args:["--claude-in-chrome-mcp"], scope:"dynamic"}` named
  **`claude-in-chrome`** → tools `mcp__claude-in-chrome__*`.
- The native host (`claude.exe --chrome-native-host`) is a pure relay: any client on the pipe sends
  4-byte-LE-framed JSON `{method, params}`; it becomes `{type:"tool_request", method, params}` to the
  extension; `tool_response`/`notification` fan back to all connected clients. Control types:
  `ping/pong`, `get_status/status_response{native_host_version:"1.0.0"}`, `mcp_connected`,
  `mcp_disconnected`. Tool calls: `method:"execute_tool"`, `params:{tool, args, tabId, tabGroupId,
  client_id, permission_mode, …}`. Env `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_checks`
  is injected in bypass mode.
- No token, no session binding: the trust boundary is the OS user (per-user pipe; POSIX 0700/0600 +
  uid check) plus the extension's own per-site permission prompts.
- Registry (HKCU, Chrome + Edge + Brave + Chromium + Vivaldi):
  `com.anthropic.claude_code_browser_extension` → manifest
  `C:\Users\user\AppData\Roaming\Claude Code\ChromeNativeHost\com.anthropic.claude_code_browser_extension.json`
  (`type: stdio`, `path: …\.claude\chrome\chrome-native-host.bat`, `allowed_origins:
  ["chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/"]`). The Claude Desktop app has a separate
  host (`com.anthropic.claude_browser_extension`, standalone exe, Chrome only) using a sealed/paired
  channel — not needed.
- Extension (MV3, permissions incl. `debugger`, `nativeMessaging`, `<all_urls>`): on enable it probes
  both hosts with `{type:"ping"}` → `{type:"pong"}` (10 s), then `get_status`. Clear-text
  `execute_tool` is accepted unless the extension is in managed/third-party-desktop mode ("Rejected:
  pairing with Claude Desktop is required").
- Live tool namespace: `browser_batch, computer, navigate, find, form_input, get_page_text,
  read_page, read_console_messages, read_network_requests, javascript_tool, tabs_context_mcp,
  tabs_create_mcp, tabs_close_mcp, gif_creator, file_upload, upload_image, list_connected_browsers,
  select_browser, switch_browser, resize_window, shortcuts_list, shortcuts_execute`.
- `claude --help`: `--chrome  Enable Claude in Chrome integration`, `--no-chrome  Disable …`.
  `~/.claude.json`: `claudeInChromeDefaultEnabled=true`, `hasCompletedClaudeInChromeOnboarding=true`,
  `cachedChromeExtensionInstalled=true`.

## rolo-claude design (milestone H7a)
- `rolo-claude --chrome` (and `claudeInChromeDefaultEnabled` honoured like Claude Code; `--no-chrome`
  disables): add a dynamic MCP server `claude-in-chrome` = `{type: stdio, command: <claude.exe path>,
  args: ["--claude-in-chrome-mcp"]}` through the normal MCP manager. Tools appear as
  `mcp__claude-in-chrome__*` with Claude Code's exact schemas, so skills/prompts written for Claude
  Code work unchanged. Locate `claude.exe` via `find_claude_exe()` (already in `bridge.py`:
  `BRIDGE_CLAUDE_EXE` → `which claude.exe` → `claude.cmd` target → `~/.local/bin/claude.exe`).
- Preconditions surfaced in `/mcp` and `rolo-claude doctor`: Chrome running with the extension enabled
  (the pipe exists only while the extension holds the native host open); extension not in managed
  mode; per-site permissions granted in the extension UI (or `CLAUDE_CHROME_PERMISSION_MODE=
  skip_all_permission_checks` when rolo-claude runs in `auto`/`bypassPermissions`, matching Claude
  Code's bypass behaviour).
- Fallback without Claude Code installed (work box?): a small Python client speaking the pipe protocol
  directly (`\\.\pipe\claude-mcp-browser-bridge-<username>`, 4-byte-LE JSON `{method:"execute_tool",
  params:{tool, args, …}}`) exposed as the same tool names — later, only if needed.
- Chrome-less fallback = Playwright (H7b): `rolo-claude --playwright` registers
  `{type: stdio, command: "npx", args: ["-y", "@playwright/mcp@latest", …]}` as `playwright`
  (`mcp__playwright__*`), with `--cdp-endpoint` to drive the user's own Chrome started with
  `--remote-debugging-port`. Nothing Playwright is installed on this box yet (`npx` would download
  `@playwright/mcp@0.0.82`; no Python `playwright`; no `%LOCALAPPDATA%\ms-playwright`).
