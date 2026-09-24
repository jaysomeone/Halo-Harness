# Review findings — M4–M6 integration (Opus 5.5 advisor pass, 2026-09-23)

Bounded review of the M4–M6 diff against `docs/m4-6-brief.md` + `docs/design-review-2026-09-23.md`.
Findings marked *(confirmed)* were reproduced by importing `bridge.py` and calling the functions.
Line numbers refer to the 17:42 snapshot of `bridge.py` and drift after later edits — locate by
function name. **Triage (Fable): all 12 accepted.** Finding 8 corrects the brief itself: the brief's
"`dbx:databricks-X` → `/serving-endpoints/X/invocations`" was wrong — the invocations path takes the
FULL endpoint name (`/serving-endpoints/databricks-kimi-k3/invocations`), as the work MCP README and
the passthrough route (`databricks-claude-…`) already do.

## 1. critical — up-front clamp counts base64 image bytes as tokens, floors at 1 *(confirmed)*
`estimate_tokens` = JSON length ÷ 4 including `image_url` data; clamp result floored at `max(1, …)`;
the A ≤ L overflow retry floors at 1 too. One ~450 KB screenshot in history → every later request
goes upstream with `max_tokens: 1` (compaction included) → 1-token answers every turn.
**Fix:** count each image part at a flat ~1600 tokens in the estimate; if the headroom is below a sane
floor (`min(requested, 4096)`), skip the headroom clamp and let the upstream overflow path decide;
treat `L − A − 256` below that floor as unfixable (rewrite to prompt-too-long instead of retrying).

## 2. major — Databricks error shape not read; cached max_tokens limit never used *(confirmed)*
Databricks serving endpoints reply `{"error_code": …, "message": …}` (no `error.message`). A real
`/invocations` 400 like "max_tokens must be <= 8192" or an overflow never triggers clamp-retry or the
prompt-too-long rewrite; Claude Code receives a 400 whose message is the Python repr of the dict.
`dbx_cache_get_max_tokens_limit` has no caller, so every later request pays the 400 + retry again.
**Fix:** one `upstream_error_text(body)` helper (OpenAI `error.message` → top-level `message` →
`error` string → str(body)) used everywhere; consume the cached per-model limit in the up-front clamp.

## 3. major — Databricks overflow regex takes A as L *(confirmed)*
Standard wording (which Claude Code 2.1.280 itself parses as `exceed context limit: (\d+) \+ (\d+) > (\d+)`):
"…exceed context limit: 6000 + 16384 > 8192". The code takes the first number (6000 = A) as the
limit, caches it as a max_tokens cap, the retry fails, and Claude Code gets
"prompt is too long: 6001 tokens > 6000 maximum" → compacts a prompt that fits.
**Fix:** parse `(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)` as A, B, L; retry silently when A ≤ L; keep this
wording out of `parse_databricks_max_tokens_limit`.

## 4. major — OpenRouter overflow never retried *(confirmed)*
Real OpenRouter wording: "This endpoint's maximum context length is 163840 tokens. However, you
requested about 170000 tokens (150000 of text input, 20000 in the output). …". A is only read from
"(N in the messages", so A is never known → always rewritten as prompt-too-long → needless compaction
(and the compaction call can fail the same way — the loop design item 7 warns about).
**Fix:** parse `\((\d+) of text input`; fall back to `A = T − oai_body["max_tokens"]`.

## 5. major — passthrough keeps `tool_reference` blocks *(confirmed)*
`build_passthrough_body` drops the `tool-search-tool-*` beta but leaves `tool_reference` blocks in
place → first ToolSearch result makes every later passthrough request 400 on an unknown block type.
**Fix:** replace each `tool_reference` block with a text block naming the tool (same as the
openai-chat path), and apply the same `defer_loading` strip / 128 cap.

## 6. major — malformed upstream data crashes the handler thread *(all four confirmed)*
`{"error":"boom"}` body → AttributeError; dict in `error.metadata.raw` → TypeError; mid-stream
`{"error":"boom"}` chunk or a `{"type":"text","text":null}` part raises inside the stream loops,
which only catch OSError. Before headers: socket dropped with no status (SDK retries with backoff,
looks like a hang). Mid-stream: the `finally` writes `0\r\n\r\n` → stream ends with neither `error`
nor `message_stop`.
**Fix:** coerce these fields in one helper (isinstance / str()); wrap both stream loops in
`except Exception` that emits `error_event` before the terminator; before headers map to a 502
`api_error`. The server must never die because one request misbehaved.

## 7. major — `ANTHROPIC_CUSTOM_HEADERS` from the shell env is lost; mirrored headers not logged
The launcher merges only the settings-chain value; a shell-exported
`x-databricks-use-coding-agent-mode: true` is overwritten by the launcher's settings env (a settings
`env` value REPLACES the process var) and never mirrored. Nothing logs what was mirrored, so the work
acceptance line "bridge.log shows the mirrored header" cannot pass.
**Fix:** merge `os.environ["ANTHROPIC_CUSTOM_HEADERS"]` too (settings chain wins on conflicts, bridge
`x-bridge-*` lines appended last); log mirrored header NAMES (values redacted if they look secret) at
INFO in the Databricks chat call and the passthrough relay.

## 8. major — `databricks-` prefix stripped from the invocations path (brief was wrong)
`_dbx_bare_name` turns `dbx:databricks-kimi-k3` into `/serving-endpoints/kimi-k3/invocations`.
Pay-per-token endpoint names keep the prefix (documented form
`/serving-endpoints/databricks-meta-llama-3-3-70b-instruct/invocations`); the passthrough already sends
`databricks-claude-…` in full. Every such model 404s on its primary route. Tests pin the stripped path.
**Fix:** use the full name in the invocations path; fix the tests to pin the full name; confirm
against the `--probe` endpoint list at work.

## 9. minor — launcher port checks
`check_server_status` treats every OSError as "absent" (a silent listener that times out included) and
lets `http.client.BadStatusLine` escape; the stale-server path spawns a new server even if `/shutdown`
was refused and the port is still busy; `cmd_probe` never reports a busy port.
**Fix:** after any non-fresh result check `port_is_free()` and raise `BridgeStartError` naming the
port; catch `http.client.HTTPException`; have `--probe` print the port status.

## 10. minor — passthrough detection and model-name parsing in the launcher *(confirmed)*
`is_passthrough_ref` requires a `databricks-`/`system.ai.` prefix but `route_model` sends ANY `dbx:`
name containing "claude" to passthrough → `dbx:claude-sonnet-4-5` gets `MAX_THINKING_TOKENS=0` and a
forced `ENABLE_TOOL_SEARCH` on a raw Claude relay. `bare_model` splits on the first ':' →
`qwen/qwen3-coder:free` looks up models.json key "free". Passthrough refs always get the
128000/16384 caps → a 200k Claude session compacts at ~100k unlike plain `claude`.
**Fix:** reuse `_dbx_dialect` in the launcher; strip only a leading `dbx:`/`or:`; omit the
`CLAUDE_CODE_MAX_*` variables for passthrough refs that have no models.json entry.

## 11. minor — passthrough `event: error` frame can land mid-event
The synthetic error frame is written straight after the last `read1` chunk, which may end mid-event
→ the SDK joins `event: error` onto a partial `data:` line and throws a JSON parse error.
**Fix:** prefix the frame with `b"\n\n"` (harmless on an event boundary).

## 12. minor — tests use invented error wordings; missing cases
Overflow/clamp fixtures were made up to fit the code ("context limit of 8192: got 6000 input tokens +
3000 max_tokens", OpenAI-style `error.message` for Databricks, "You requested about 71000 tokens
total."); the foreign-port test has no time limit and its port-in-stderr check also passes on the
10 s timeout path. Findings 1–4 and 9 all pass today's green suite.
**Fix:** use the real shapes (Databricks `{"error_code","message"}` + "exceed context limit: A + B > L";
OpenRouter "maximum context length is L tokens. However, you requested about T tokens (A of text
input, B in the output)"; vLLM/OpenAI "maximum context length is L tokens. However, you requested T
tokens (A in the messages, B in the completion)"); assert elapsed < 3 s on the foreign-port test; add
tests for: requests carrying images (clamp stays sane), passthrough non-2xx relay, passthrough
`tool_reference`/`defer_loading`, the passthrough launcher env, `routes-cache.json` on disk, reuse of
the cached max_tokens limit, env-merged `ANTHROPIC_CUSTOM_HEADERS`, full-name invocations path.

## 13. major — `--stop` returns before the server is gone (found by Worker C, confirmed by log timestamps)
`POST /shutdown` answers 200 and the real `server.shutdown()` fires ~200 ms later from a
`threading.Timer`. A `launch` issued right after `--stop` sees the dying server still answering
`/healthz`, reuses it, and the child `claude` gets `ECONNREFUSED` mid-request (1 of 3 fresh-spawn runs).
**Fix:** `cmd_stop` polls until the port refuses connections (≤ 5 s) before returning and prints
`stopped` only then; `launch` treats a healthz answer from a server whose `server.json` pid is dead,
or a connection reset during the check, as absent. Test: stop then immediate launch, three times, no sleep.

## 14. major — the `bin/` wrappers make every bridge flag unreachable from PATH (found by Worker R)
`bin/claude-bridge.cmd` / `bin/claude-bridge` run `launch -- %*`, and `main()` splits bridge flags from
claude args on the FIRST literal `--`, so `claude-bridge --model X -p hi` from PATH forwards `--model X`
to Claude Code (only `python bridge.py launch --model X -- …` works). Blocks the M7 acceptance.
**Fix:** wrappers call `launch %*` / `launch "$@"` (no `--`); `main()` parses the `launch` argv by
scanning from the front: consume `--model V`/`--model=V`, `--small-model V`, `--port N` while they
lead; an optional `--` ends the scan; everything else is forwarded to `claude` untouched (a user
`--model` after the bridge flags or after `--` still goes to claude). Tests with the fake claude:
`launch --model X -p hi` (no `--`) → claude sees `-p hi`, settings model X; `launch -p hi` → default
ref; `launch --model X -- --model Y` → claude sees `--model Y`. Update the README usage section
accordingly (Worker R documented the broken behaviour; replace that paragraph).

## 15. minor — `routes.json` `default`/`small` are never consumed (found by Worker R)
Only `profiles` is read. **Fix:** launcher default main ref = `--model` → `BRIDGE_MODEL` env →
`routes.json` `default` → built-in `or:deepseek/deepseek-v3.2`; small ref = `--small-model` →
`BRIDGE_MODEL_SMALL` → `routes.json` `small` → main ref. Remove the unused `providers` block from
`routes.example.json` (or mark it reserved) so the example does not lie. Test via `BRIDGE_TEST_HOME`.

## Areas reviewed and found clean
Discovery chain order; loopback/domain-suffix host filter; workspace-root derivation; invocations
(no `model`) and mlflow (with `model`) body shapes and allowlist; 404 fallback with memory + disk route
cache; connect/DNS failure → 502 `api_error` + `x-should-retry: false` + VPN hint; passthrough `read1`
relay, `?beta=true`, chunked framing and Content-Length consistency, header swap, `dbx:` rewrite,
`bridge1.` thinking strip, beta filter; `count_tokens` fallback; upstream socket closed on client
abort; exact prompt-too-long format with T > L; `--config` root/routes; probe 401/403 wording;
models.json shape; settings.json `model` snapshot/restore; stale-hash restart happy path; the four
`BRIDGE_DUMP` kinds; no secrets in `--probe` output or logs.
