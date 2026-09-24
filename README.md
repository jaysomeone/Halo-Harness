# claude-bridge

## What it is

Claude Code itself is the harness -- there is no separate agent CLI. `bridge.py` is a single Python file, stdlib only (`requires-python = ">=3.10"` PEP 723 header, so `uv run --script bridge.py ...` works with zero install step; `python`/`python3 bridge.py ...` work identically, no dependencies either way). It speaks the Anthropic Messages API to Claude Code and translates it to the Databricks AI Gateway (raw Anthropic passthrough for Claude endpoints, OpenAI chat-completions dialect for `databricks-*`/`system.ai.*` endpoints) and to OpenRouter. Plain `claude` is never touched: the subscription, `CLAUDE.md`, settings, memory, MCP servers, skills, hooks and permissions all ride along untouched, because the bridge only intercepts the one HTTP call Claude Code makes to its model.

## Install

### Home (Windows)

Prerequisites: Python 3.10+ and, optionally, `uv` (both launcher scripts fall back to plain `python`/`python3` when it's absent). Put `OPENROUTER_API_KEY` in `~/.config/vibes-hacker/env` (`KEY=value`, `#` comments, optional leading `export`, never overwrites an already-set variable) -- or just export it yourself. Override the path with `BRIDGE_ENV_FILE`.

`bin/claude-bridge.cmd` resolves `bridge.py` **relative to its own folder** (`%~dp0..\bridge.py`), so a *copy* of it on PATH would look for a `bridge.py` next to wherever you copied it, not this repo. Instead, drop a one-line wrapper on PATH (e.g. `C:\Users\user\bin`) calling this repo's `bridge.py` by absolute path (a teammate substitutes their own path):

```bat
@echo off
where uv >nul 2>nul
if %errorlevel% == 0 (
    uv run --script "C:\Users\user\Documents\vibes\appDev\claude-bridge\bridge.py" launch %*
) else (
    python "C:\Users\user\Documents\vibes\appDev\claude-bridge\bridge.py" launch %*
)
```

### Work (Linux, VPN only)

```sh
cp bin/claude-bridge ~/bin/
chmod +x ~/bin/claude-bridge
```

Databricks discovery is automatic from Claude Code's own settings, tried in order: explicit `BRIDGE_DBX_BASE_URL`+`BRIDGE_DBX_TOKEN` (wins outright) -> process `ANTHROPIC_BASE_URL`+`ANTHROPIC_AUTH_TOKEN` when the host is a real Databricks host (`*.databricks.com` / `*.azuredatabricks.net` / `*.gcp.databricks.com`, never loopback) -> the same check against the settings `env` chain (managed -> user -> project -> project-local, later wins) -> `DATABRICKS_HOST`+`DATABRICKS_TOKEN` -> `~/.databrickscfg`'s `[DEFAULT]` section. Nothing to configure if any of those already work.

## Usage

`bridge.py launch`'s own flags (`--model`, `--small-model`, `--port`) are read straight off the front of the argv that follows `launch`, consumed while they lead; an optional `--` there ends the scan, and everything from that point on is forwarded to `claude` untouched. The `--` is only needed to hand `claude` a flag of its own that happens to share a name with a bridge flag (a bare `--model` after the bridge's own flags, or after an explicit `--`, still goes to `claude`):

```sh
python bridge.py launch --model or:deepseek/deepseek-v3.2 -p "reply with the single word pong"
uv run --script bridge.py launch --model or:deepseek/deepseek-v3.2 -- -p "..."
```

The installed `claude-bridge` wrapper forwards its whole argument list to `bridge.py launch`, which reads its own flags (`--model`, `--small-model`, `--port`) from the front and hands everything else to `claude`, so the `--` is optional there. For day-to-day use, leave `--model` off and set the default in `BRIDGE_MODEL` or `routes.json`:

```sh
claude-bridge --model or:deepseek/deepseek-v3.2 -p "reply with the single word pong"
claude-bridge --model or:deepseek/deepseek-v3.2 -p "reply with the single word pong"
claude-bridge -p "reply with the single word pong"
claude-bridge                       # interactive
```

`--small-model` sets the model for Claude Code's haiku-tier background tasks (titles, classifiers), defaulting to the main model. `--port` picks the bridge's loopback port (default `8787`). Model reference forms (`route_model`):

| Form | Example | Resolves to |
|---|---|---|
| `or:vendor/model` | `or:deepseek/deepseek-v3.2` | OpenRouter |
| bare `vendor/model` (contains `/`) | `deepseek/deepseek-v3.2` | OpenRouter |
| `dbx:databricks-<name>` | `dbx:databricks-kimi-k3` | Databricks, OpenAI-chat dialect |
| `dbx:system.ai.<name>` | `dbx:system.ai.my_model` | Databricks, OpenAI-chat dialect |
| bare `databricks-<name>` / `system.ai.<name>` | `databricks-kimi-k3` | Databricks (alias only) |
| any of the above with `claude` in the name | `dbx:databricks-claude-sonnet` | Databricks, raw Anthropic passthrough |

Anything else (a tier name like `sonnet`/`haiku`, or whatever Claude Code substitutes for one) is resolved from that request's `x-bridge-main`/`x-bridge-small` header first, then the server's own `BRIDGE_MODEL`/`BRIDGE_MODEL_SMALL`; unresolved gets a 400 naming the accepted forms. `launch` sets those two headers once per session (inside `ANTHROPIC_CUSTOM_HEADERS`) to its `--model`/`--small-model` values, so concurrent sessions against one server resolve tier aliases independently.

Inside Claude Code, `/model` shows one extra custom entry named after the active `--model` ref; picking a different *built-in* entry there doesn't change the upstream, since the bridge resolves tier names from the session's `x-bridge-*` headers before the literal name Claude Code sent.

## Commands

`--config` -- resolve and print configuration as JSON, no network, secrets redacted:

```sh
python bridge.py --config
```
```json
{
  "state_dir": "C:\\Users\\rolo\\.claude-bridge",
  "openrouter": { "api_key": "sk-o...", "base_url": "https://openrouter.ai/api/v1" },
  "databricks": null,
  "routes": {},
  "env_file_loaded": true
}
```

`--probe` -- outbound-only reachability check, no server needed. Databricks: `GET <root>/api/2.0/serving-endpoints` -> `Databricks: not configured`, `Databricks: N serving endpoint(s) at <root>: ...`, `Databricks: token can run inference but not list endpoints -- pass names explicitly` (401/403), or `Databricks: unreachable -- <error> (are you on the VPN? Databricks is whitelisted)`. OpenRouter: `GET <base_url>/models`, caches `{"<id>": {context_length, max_output_tokens}}` per model to `<state_dir>/models.json` (`launch` reads it for `CLAUDE_CODE_MAX_CONTEXT_TOKENS`/`_OUTPUT_TOKENS`) -> `OpenRouter: not configured (no OPENROUTER_API_KEY)` or `OpenRouter: cached N model(s) to <path>`.

A busy port is only reported when the *server* tries to bind it (`--serve`, normally spawned by `launch`): `claude-bridge: cannot bind port 8787 -- already in use? (<error>)`. `launch` checks first and fails fast instead (`... refusing to touch it`) if something else owns the port.

`--stop` -- POST `/shutdown` (token-authenticated) to the server named in `server.json`:

```sh
python bridge.py --stop
```

Prints `No running server found` / `Token file missing` if either file is absent; on a successful shutdown request it polls until the port actually refuses connections (up to 5s) and only then prints `stopped` -- so a `launch` run immediately afterward never races a server that's still finishing its shutdown. `--version` prints the version, e.g. `claude-bridge 0.2.1`. `--serve [--port N] [--state-dir DIR]` runs the server in the foreground -- what `launch` spawns detached; not normally run directly.

State directory (`~/.claude-bridge/` by default, override with `BRIDGE_STATE_DIR`):

| File | Contents |
|---|---|
| `bridge.log` | Rotating server log (2 MB x 3 backups); auth headers/bearer tokens redacted |
| `server-stdio.log` | Raw stdout/stderr of the detached `--serve` subprocess |
| `token` | Bearer token, generated once, reused across restarts |
| `server.json` | `{service, version, hash, pid, port}` -- `hash` is the sha1 of the running `bridge.py`, used to detect a stale server after an edit |
| `models.json` | OpenRouter model -> `{context_length, max_output_tokens}`, written by `--probe` |
| `routes-cache.json` | Per-model Databricks route (`invocations` vs. `mlflow`) and any discovered `max_tokens` limit, learned at request time |
| `routes.json` | Optional, copy `routes.example.json` here. `default`/`small` are the fallback model refs for `launch` (after `--model`/`BRIDGE_MODEL`, before the built-in `or:deepseek/deepseek-v3.2`); `profiles` gives context/max-output token sizes per bare model id |
| `dumps/` | With `BRIDGE_DUMP=1`: one timestamped JSON file per `claude-request` / `upstream-request` / `upstream-stream` / `emitted-events` |

## How it works

1. `launch` checks `127.0.0.1:<port>/healthz`; if nothing answers, or a different `bridge.py` hash than the one running does, it spawns a fresh detached `--serve` and polls `/healthz` up to 10s (a foreign, non-bridge service on the port fails the launch instead). The server binds loopback-only and requires the bearer token on every route but `/healthz`.
2. `launch` writes a one-shot `--settings` JSON file (merged with any `--settings` the user already passed after `--`, user keys winning) and runs `claude --settings <path> ...` as a child process -- a settings *file*, never process env, so a work box's own `env.ANTHROPIC_BASE_URL` can't win. It points `ANTHROPIC_BASE_URL` at the loopback server, carries the bearer token, pins the model aliases plus `x-bridge-*` headers, and (non-passthrough refs) sets `ENABLE_TOOL_SEARCH=true`/`MAX_THINKING_TOKENS=0`. Deleted, and `~/.claude/settings.json`'s `model` key restored, when `claude` exits.
3. Each `/v1/messages` request is translated (`anthropic_to_openai`) and the response stream fed through `OpenAIStreamToAnthropic`, which buffers tool-call deltas until finalize (so split/interleaved/repeated-id chunks become clean `tool_use` blocks) and re-emits Anthropic SSE events, with a synthetic `ping` every `BRIDGE_PING_INTERVAL`s of silence. Databricks Claude passthrough skips translation and relays raw bytes instead.
4. Tool lists are capped at 128 (`select_tools`): every non-deferred tool plus any deferred tool already referenced by a `tool_reference` block, `defer_loading` stripped before the request leaves. `ENABLE_TOOL_SEARCH=true` is what keeps a 250+-tool setup under that cap.
5. `count_tokens` is a `len(json)/4` estimate for every route except Databricks Claude passthrough, which relays to the real endpoint and falls back to the estimate on failure.
6. A 400 that looks like a context-length error is parsed; if only `max_tokens` was the problem it's retried once with headroom subtracted, else rewritten to `prompt is too long: <T> tokens > <L> maximum` so Claude Code's own compaction fires instead of the session dying.
7. Reasoning content (`reasoning`, `reasoning_content`, Databricks reasoning parts) is logged at DEBUG (length only), never shown to Claude Code.

## Environment variables

Everything below is optional; only `OPENROUTER_API_KEY` (or a working Databricks discovery source -- `DATABRICKS_HOST`/`DATABRICKS_TOKEN`, `~/.databrickscfg`, see Install) is needed for the bridge to route anywhere.

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_MODEL` | `or:deepseek/deepseek-v3.2` | `launch --model` default; also the server's fallback resolving an unprefixed tier name with no `x-bridge-main` header |
| `BRIDGE_MODEL_SMALL` | (none) | Server-side fallback for the haiku/small tier, same mechanism |
| `BRIDGE_STATE_DIR` | `~/.claude-bridge` | Overrides the whole state directory |
| `BRIDGE_ENV_FILE` | `~/.config/vibes-hacker/env` | Path to the `KEY=value` file loaded before resolving OpenRouter/Databricks |
| `BRIDGE_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter base URL override (also the test/mock seam) |
| `BRIDGE_DBX_BASE_URL` + `BRIDGE_DBX_TOKEN` | (none) | Explicit Databricks host+token; set both together to win outright |
| `BRIDGE_DUMP` | (unset) | `1` writes per-request JSON dumps to `<state_dir>/dumps/` |
| `BRIDGE_PING_INTERVAL` | `15` | Seconds of upstream silence before a synthetic SSE `ping` (openai-chat dialect only) |
| `BRIDGE_CLAUDE_EXE` | (auto-detected) | Explicit path to the `claude` executable; first in the lookup order |
| `BRIDGE_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` / `REQUESTS_CA_BUNDLE` | (none) | Extra CA bundle for outbound TLS, checked in that order, first present wins |
| `HTTPS_PROXY` / `HTTP_PROXY` (+ lowercase) | (none) | Outbound proxy for the bridge's own upstream calls, subject to `NO_PROXY` |
| `NO_PROXY` / `no_proxy` | (none) | `launch` appends `127.0.0.1,localhost` to the settings chain's value, for the child `claude` process only -- the bridge's own calls still honour the box's real `HTTPS_PROXY` |

## Troubleshooting

- **VPN hint**: any Databricks connect failure (`--probe` or a live request) ends with `(are you on the VPN? Databricks is whitelisted)`.
- **Port 8787 busy**: `launch` fails fast (`... refusing to touch it`) if a foreign service owns the port; `--serve` prints `claude-bridge: cannot bind port 8787 -- already in use? (...)` if it can't bind. Pass `--port` for a different one.
- **Stale server**: `launch` detects a running server whose `bridge.py` hash doesn't match the current file, shuts it down with its own on-disk token, and starts fresh automatically -- `--stop` is only for killing a server you don't want auto-restarted right away.
- **128-tool cap / deferred tools**: if a tool isn't visible to the model, check whether it's `defer_loading` and unreferenced so far in the conversation.
- **WebSearch returns 400 by design**: `web search unavailable via claude-bridge`; no non-Claude equivalent exists. WebFetch is unaffected.
- **`uv`'s first-run interpreter download blocked**: both launchers fall back to plain `python`/`python3` automatically when `uv` isn't on PATH, or call that directly yourself.
- **"prompt is too long: N tokens > L maximum"**: expected -- the bridge rewrites a provider's context-overflow error into this shape so Claude Code's own compaction fires; not a bug.
- **Where to look**: `<state_dir>/bridge.log` and `server-stdio.log` first; set `BRIDGE_DUMP=1` and check `<state_dir>/dumps/` for exact request/response bodies.
- **Auto-mode classifier notice**: with `permissions.defaultMode: auto`, Claude Code asks the model to classify tool-call approval, visible as an extra turn behind a gateway -- expected.
- **`[claude-code:unrecognized_model]`**: Claude Code noting the model string isn't a known Claude ID. Expected for every non-`claude:` reference; harmless.

## Security posture

- Loopback-only bind (`127.0.0.1`); Windows also sets `SO_EXCLUSIVEADDRUSE` so another process can't silently share the port.
- Every route but `/healthz` requires the bearer token (`x-api-key` or `Authorization: Bearer`); wrong/missing gets a 401 with zero upstream calls.
- The token is generated once (`secrets.token_urlsafe(32)`), persisted at `<state_dir>/token` (mode `0600` on POSIX) and **reused across restarts**, not regenerated per launch -- anyone who can read that file can call the bridge until it's deleted.
- Credentials never land in the repo or on a command line: secrets come from the environment, settings chain, or `~/.databrickscfg`; `claude` gets them via a temporary per-PID `--settings` file deleted on exit; `bridge.log`/`--config` redact keys, tokens and auth headers.
- Each provider only sees its own traffic -- OpenRouter calls go to `BRIDGE_OPENROUTER_BASE_URL` (default `openrouter.ai`), Databricks calls go only to the resolved workspace root.

## Tests

```sh
python test_bridge.py; echo exit=$?
uv run --script test_bridge.py; echo exit=$?
```

Hermetic black-box suite: talks to `bridge.py` over HTTP/subprocess only (never imports it), runs mock OpenAI-dialect and Databricks/Anthropic-passthrough upstreams in-process, and gates on the exit code, not printed text. Bridge-dependent tests report SKIP (not FAIL) if `bridge.py` is missing or won't start; no live network happens in the suite itself. Two live acceptance checks exist outside it, manual/optional: `or:deepseek/deepseek-v3.2` replying `pong` over real OpenRouter, and `dbx:databricks-kimi-k3` replying `pong` over real Databricks on the VPN.

## Known limitations

- Weaker/non-Claude models can echo tool-call scaffolding or thinking-style preamble into visible text; the bridge doesn't yet repair malformed calls or extract text-embedded/fenced-JSON ones.
- No WebSearch (see Troubleshooting); WebFetch is unaffected.
- Databricks's exact context-limit wording is matched by regex against design-review notes, not yet confirmed live on the VPN.
- Reasoning/thinking output from non-Claude models is logged, never surfaced in the transcript.

## Licence

MIT. See `LICENSE`.
