# Design review notes — 2026-09-23 (Opus 5.5 advisor pass on the foundation plan)

These notes correct and extend the plan in `~/.claude/plans/we-created-an-mcp-lively-valiant.md`.
**Where they conflict with the plan, these notes win.** Items marked *(verified in the installed
Claude Code 2.1.280 binary)* were checked on the home box.

## Verified in the binary

- Tool search is **off** when `ANTHROPIC_BASE_URL` is not an Anthropic host, unless
  `ENABLE_TOOL_SEARCH` is set. ToolSearch returns
  `tool_result.content=[{"type":"tool_reference","tool_name":…}]`.
- Compaction after a "prompt too long" error parses the message with
  `prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)`; it also recognises
  "input length and `max_tokens` exceed context limit".
- The SDK honours the `x-should-retry` header.
- `claude` on Windows is npm's `claude.cmd`, which runs
  `node_modules\@anthropic-ai\claude-code\bin\claude.exe`.

## 1. Defects and gaps in the plan

1. **Too many tools — blocks the home box.** With tool search off every request carries all of
   the owner's 250+ tools; OpenAI-style APIs cap at 128 functions → 400 on every turn.
   - Launcher sets `ENABLE_TOOL_SEARCH=true` for openai-chat refs.
   - Bridge sends the non-deferred tools plus every name found in any
     `{"type":"tool_reference"}` block in the history (key `tool_name` or `name`).
   - Strip `defer_loading`; turn each `tool_reference` block into text.
   - Passthrough does the same and drops the `tool-search-tool-*` beta.
   - Hard cap 128; log anything dropped.
2. **Request paths** arrive as `/v1/messages?beta=true` and `/v1/messages/count_tokens?beta=true`.
   Route on `urlsplit(path).path`.
3. **SSE framing.** Every event needs an `event: <type>` line (the SDK silently drops events
   without one — looks like a hang). `index` is ONE counter across all blocks, never
   `tool_calls[].index`. Open a block only on its first non-empty delta.
4. **Finalize on `[DONE]` or EOF, not on `finish_reason`.** Usage arrives afterwards in a chunk
   with `choices:[]`. Some streams send neither `[DONE]` nor a finish_reason.
5. **Buffer tool calls and emit them at finalize.** Deltas can interleave, repeat `id`, or omit
   `index` (treat a new id as a new call). Arguments `""` → `{}`; invalid JSON → `{}` + log line
   (Claude Code's validator error then lets the model correct itself). `length` → drop the
   unfinished call, `stop_reason=max_tokens`. If any tool_use was emitted, `stop_reason=tool_use`
   even when the provider says `stop`. Text still streams live.
6. **Send the HTTP 200 only after the upstream returns 2xx headers.** Upstream 4xx/5xx then reach
   Claude Code as real statuses (drives its retries/compaction). Until the first content block is
   sent, allow one silent retry (connect error, Databricks 404 route fallback, max_tokens clamp).
7. **The "prompt is too long" rewrite can loop compaction.** Parse limit L, prompt size A and
   requested output B (or total T) from: OpenAI/vLLM "maximum context length is L … (A in the
   messages, B in the completion)"; OpenRouter "requested about T"; `error.metadata.raw`.
   If A ≤ L, max_tokens is the cause → retry with `L−A−256`. Otherwise reply
   `prompt is too long: T tokens > L maximum` with T forced above L. Clamp up front:
   `max_tokens ≤ min(profile max, context − 1.1×estimate − 512)`.
8. **Tool call/result pairing.** For each assistant turn emit all its `tool` messages together in
   tool_use order, then one `user` message with hoisted images and remaining text. Missing result →
   `"(no result)"`; result with unknown id → user text. Drop empty/whitespace-only text and empty
   messages, then merge neighbouring same-role messages. Assistant turn with only tool calls →
   `content: null`. Always mint new `toolu_`/`msg_` + 24-hex ids (Kimi returns `functions.Read:0`
   and reuses it every turn).
9. **Model aliases must be per session** (one server may serve several launches). The launcher
   appends `x-bridge-main: <ref>` and `x-bridge-small: <ref>` lines to `ANTHROPIC_CUSTOM_HEADERS`
   (newline-separated `Name: Value`, merged with any existing value). The server resolves
   `claude-*` and tier names from those headers and strips them before calling upstream.
10. **The `--settings` env block also needs:** `NO_PROXY`/`no_proxy` = settings-chain value plus
    loopback (else a work `env.NO_PROXY` wins and loopback traffic goes to the corporate proxy);
    `WORK_MCP_BASE_URL/TOKEN` and `DATABRICKS_MCP_BASE_URL/TOKEN` set to the REAL gateway values
    (else those MCPs pick up the loopback URL inside bridge sessions); if the settings chain uses
    `apiKeyHelper`, run it during discovery and override it. Also snapshot the `model` key of
    `~/.claude/settings.json` before launch and restore it after exit (`/model` can persist a
    bridge ref there and break plain `claude`).
11. **WebSearch** sends its own `web_search_*` request; stripping that tool produces made-up
    results. Return 400 "web search unavailable via claude-bridge" instead.
12. **Databricks.** `databricks-X` → `/serving-endpoints/X/invocations` with NO `model` in the
    body; `system.ai.X` → `/ai-gateway/mlflow/v1/chat/completions` WITH `model`. Cache whichever
    route works. Build the body from an allowlist: messages, max_tokens, temperature, top_p, stop,
    stream, tools, tool_choice. Response `content` may be a list of `text` and
    `reasoning{summary[]}` parts. If a 400 names a max_tokens limit: parse, retry once, cache.
    Connect/DNS failure → 502 `api_error` with `x-should-retry: false` and a "VPN?" hint.
13. **Reasoning.** Read `reasoning` (OpenRouter), `reasoning_content` (DeepSeek/Kimi) and the
    Databricks parts. Foundation: LOG it only. If later shown as thinking, emit only before the
    first text/tool block with signature `bridge1.<base64 reasoning_details>`; passthrough must
    strip `bridge1.`-signed blocks (fake signatures 400 on real Claude); the openai-chat path
    replays them (Gemini 3 on OpenRouter needs this inside tool loops).
14. **Milestone 1 must be SSE** — Claude Code streams even the "pong" request. One state machine
    takes SSE chunks, or a JSON body as one chunk, and feeds either an SSE writer or a Message
    collector.
15. **Other.** Drop system blocks starting with `x-anthropic-billing-header`. Keep any note the
    bridge adds to the prompt byte-stable (provider prompt caching). OpenRouter with tools: send
    `provider:{"require_parameters":true}`. Ignore SSE comment lines starting with `:`. A chunk
    containing `{"error":…}` becomes an SSE `error` event. Serialize with
    `json.dumps(o, ensure_ascii=False).encode("utf-8","replace")` (a lone surrogate in tool output
    otherwise crashes every replay of that history).

## 2. Python implementation notes

**Server handler** — `protocol_version="HTTP/1.1"`, `disable_nagle_algorithm=True`,
`timeout=120`, `daemon_threads=True`. Read the whole request body BEFORE checking auth (an unread
body after a 401 breaks the next request on that keep-alive socket). JSON responses need an exact
byte Content-Length.

**SSE output** — `Transfer-Encoding: chunked` + `Connection: close`, `close_connection=True`; one
`wfile.write(b"%X\r\n%s\r\n" % (len(b), b))` per event, finish with `0\r\n\r\n`. Once headers are
sent, always end with `message_stop` or `error`. On write error (BrokenPipe/Reset/Aborted) call
`shutdown(SHUT_RDWR)` on the upstream socket.

**Pings** — reader thread feeding a `Queue`; `get(timeout=15)` empty → send ping. Do NOT poll with
socket timeouts (after one timeout `SocketIO` raises "cannot read from timed out object" forever).

**Windows port binding** — `allow_reuse_address=False` and `SO_EXCLUSIVEADDRUSE` (default
SO_REUSEADDR lets two servers share 8787 on Windows).

**Server lifecycle** — bind port → load or create token → write `server.json` (pid, version, sha1 of
bridge.py) → serve. Reuse the token across restarts (else live sessions start getting 401s).
`/healthz` returns service, version, hash; the launcher restarts a stale-hash server and fails fast
if something else owns the port. `--stop` = `POST /shutdown` requiring the token. Server
stdout/stderr → `server-stdio.log`; only the RotatingFileHandler writes `bridge.log`.

**Upstream client** — `http.client` with a small `open_upstream()` helper. Proxy from env plus the
settings chain; tunnel with `set_tunnel`; check NO_PROXY with
`urllib.request.proxy_bypass_environment`. Never `getproxies()` (on Windows it falls back to the
registry proxy). Connect timeout 10 s, then `sock.settimeout(300)` idle. Keep the socket for
aborts; non-2xx responses don't raise. Read SSE with `readline()`; passthrough uses `read1(65536)`
(`read(n)` blocks and freezes the TUI). Send `Accept-Encoding: identity`. TLS: default context plus
`load_verify_locations` for `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `BRIDGE_CA_BUNDLE`.

**Detached server on Windows** — `Popen([sys.executable, bridge, "--serve", …],
creationflags=CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW|0x01000000 (CREATE_BREAKAWAY_FROM_JOB),
stdin=DEVNULL, stdout=log, stderr=STDOUT, close_fds=True)`. On winerror 5 fall back to
`powershell -NoProfile -EncodedCommand` running `Invoke-CimMethod Win32_Process -MethodName Create`
with `pythonw.exe` (WMI-created processes are outside every job object). Don't use
DETACHED_PROCESS (console children get a visible console window). Milestone 0 acceptance: close
the launching terminal, launch again, the same server pid answers.

**Running claude on Windows** — executable lookup: `BRIDGE_CLAUDE_EXE` → `which("claude.exe")` →
the exe `claude.cmd` points to → `~\.local\bin\claude.exe`. Never pass JSON through a `.cmd`. Pass
settings as a file: `--settings ~/.claude-bridge/launch-<pid>.json`, merging any `--settings` the
user passed (also keeps tokens off the command line). `Popen([exe, …])` so it inherits the console;
no-op SIGINT handler and loop on `wait()` (not `subprocess.call`, which kills the child on
KeyboardInterrupt). Map exit code 0xC000013A → 130. POSIX: `execvp`; delete settings files whose
pid is dead. Launcher writes only to stderr. When launched from inside another Claude Code session
(tests), drop `CLAUDECODE` from the child env.

## 3. Shapes

**Request, Anthropic → OpenAI**
```
"system":[{"type":"text","text":"SYS","cache_control":{…}}]
{"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"a"}},{"type":"tool_use","id":"toolu_2",…}]}
{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","is_error":true,"content":"ENOENT"},
 {"type":"tool_result","tool_use_id":"toolu_2","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"iVB…"}}]},
 {"type":"text","text":"<system-reminder>…"}]}
→
{"role":"system","content":"SYS"}
{"role":"assistant","content":null,"tool_calls":[{"id":"toolu_1","type":"function","function":{"name":"Read","arguments":"{\"file_path\":\"a\"}"}},{"id":"toolu_2",…}]}
{"role":"tool","tool_call_id":"toolu_1","content":"[tool error] ENOENT"}
{"role":"tool","tool_call_id":"toolu_2","content":"(image in next message)"}
{"role":"user","content":[{"type":"text","text":"Image from toolu_2:"},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVB…"}},{"type":"text","text":"<system-reminder>…"}]}
tool → {"type":"function","function":{"name","description","parameters":input_schema}}
tool_choice auto→"auto", any→"required", {"type":"tool","name":N}→{"type":"function","function":{"name":N}}
```

**Upstream stream chunks** (data lines)
```
{"choices":[{"index":0,"delta":{"content":"On it."}}]}
{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_9","function":{"name":"Read","arguments":"{\"file_"}}]}}]}
{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"path\":\"a\"}"}}]}}]}
{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}
{"choices":[],"usage":{"prompt_tokens":5210,"completion_tokens":31,"prompt_tokens_details":{"cached_tokens":4096}}}
[DONE]
```

**Emitted events** (each preceded by `event: <type>`)
```
{"type":"message_start","message":{"id":"msg_…","type":"message","role":"assistant","model":"<as requested>","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":<est>,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"On it."}}
{"type":"content_block_stop","index":0}
{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_…","name":"Read","input":{}}}
{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"file_path\":\"a\"}"}}
{"type":"content_block_stop","index":1}
{"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"input_tokens":1114,"cache_read_input_tokens":4096,"output_tokens":31}}
{"type":"message_stop"}
```
Thinking (later): start `{"type":"thinking","thinking":"","signature":""}` → `thinking_delta` →
`signature_delta` → stop. Ping: `event: ping` with `{"type":"ping"}`.

**Errors before the 200** (real status, `application/json`, `x-should-retry`)
```
{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 171204 tokens > 163840 maximum"}}
```
| Upstream | Error type (status sent) |
|---|---|
| 401 | authentication_error |
| 402, 403 | permission_error |
| 404 | not_found_error |
| 429 | rate_limit_error (+ retry-after) |
| 500 | api_error |
| 503, 504, timeout | overloaded_error (529) |

Mid-stream errors: `event: error` with
`{"type":"error","error":{"type":"overloaded_error","message":"upstream: …"}}`, then close — no
`message_stop`.

**Databricks Claude passthrough** — rewrite body `model` from `dbx:X` to `X`; drop thinking blocks
signed `bridge1.`; drop `x-api-key`, the bridge `authorization`, `host`, `content-length`,
`accept-encoding` headers; add the gateway Bearer token and the custom headers.

## 4. Tests, most important first

1. A stream validator run on every streaming test: each event name equals `data.type`; every block
   start has a matching stop; indices contiguous; `output_tokens` is an int; `message_stop` last;
   final chunk within 2 s.
2. Usage chunk after the finish chunk; EOF with no `[DONE]` and no finish_reason.
3. Tool-call deltas: arguments split over 3 chunks, 2 calls, interleaved indices, repeated id,
   missing index, `stop` reported alongside tool calls.
4. `length` cutting off arguments; empty arguments; invalid JSON.
5. The request round-trip above, plus: tool_use with no result, result with no tool_use,
   whitespace-only text, consecutive user messages, a lone surrogate.
6. HTTP: `?beta=true` paths routed; two requests on one keep-alive socket; a 401 on a request with
   a body then a valid request on the same socket.
7. Context-overflow table: OpenAI/vLLM wording, OpenRouter "requested about", `metadata.raw`, the
   A ≤ L retry; output matches Claude Code's regex with T > L.
8. Status mapping before the 200; an OpenRouter error chunk; upstream socket dying mid-stream.
9. Pings during upstream silence (injectable 0.2 s interval); on client abort the mock upstream
   sees the connection close within 1 s.
10. 150 tools, 140 deferred, plus a `tool_reference`: correct set upstream, 128 cap holds.
11. Two concurrent sessions with different `x-bridge-*` headers resolve aliases independently.
12. Launcher against a fake claude (`BRIDGE_CLAUDE_EXE` dumps argv/env and exits 7): exactly one
    merged `--settings`; exit code 7 comes back; `model` key restored; live server reused;
    stale-hash server replaced.

## 5. Cut from / add to the foundation

**Cut:** trimming tool descriptions and stripping top-level `oneOf`/`anyOf` (removes usage rules
the model needs — only per model profile); serving the probe list from `/v1/models` (return
`{"data":[]}`); the hidden-window STARTUPINFO fallback; showing reasoning as thinking (log only).

**Add:** `BRIDGE_DUMP=1` writing each (Claude request, upstream body, raw upstream stream, emitted
events) to `state/dumps/`; milestone 0 answers with a fixed SSE "bridge alive" message so the event
format is tested from day one; in milestone 3 log the request shape of the auto-mode permission
check (rolo runs `defaultMode: auto` — if that check can't parse a weaker model's answer it denies
every tool call and the agent loops).
