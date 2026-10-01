# H1 review findings (commit 78c72bd; checked against the adapter-rules report, plan revisions 1–16 + Primary use case + D2–D4, H0 findings, H1 brief A–J)

Format: severity — file:line — defect — failure scenario — fix. "Verified" means reproduced in a scratch script, or in-process against `tests/helpers/mock_openai.py`, with `BRIDGE_STATE_DIR` and `BRIDGE_TEST_HOME` pointed at a temp dir.

Request bodies checked field by field, built from a one-step synthetic log exactly as the loop builds them:
- **OpenRouter `deepseek/deepseek-v4.1-flash --effort high`:** `{model, messages[system, user(str), assistant{content, tool_calls[id "toolu_…"], reasoning_details: [last fragment] | absent}, tool{tool_call_id "toolu_…"}], stream: true, max_tokens: 65536, tools[Read], tool_choice: "auto", provider: {require_parameters: true}, reasoning: {effort: "high"}, usage: {include: true}}`.
- **OpenRouter `moonshotai/kimi-k3`:** the same body minus `reasoning`, with no sampling params, no pin and `max_tokens: 131072`.
- **Databricks `databricks-kimi-k3` / `databricks-deepseek-v4-1-flash`:** `{messages[… assistant{…, reasoning_content: "…" | ""}], stream: true, max_tokens: 40000 | 10000, tools, tool_choice: "auto"}`. There is no `model` on the invocations route, no sampling params and no `stream_options`, and `reasoning_effort` is sent only when `--effort` is given.

1. **critical** — `halo_harness/providers/oai_stream.py:165,259` — Upstream tool-call ids are never captured (the per-index `tool_buf` has no id slot), so every call is re-issued under a freshly minted `toolu_…` id, and that minted id is what the log stores and every later request replays. — Verified: an upstream `functions.Read:0` comes back in the next request as `tool_calls[0].id`/`tool_call_id` `toolu_82a4…`, so Kimi K2.x/K3 lose their native ids (the report's tool-loop pass rate falls from 100 % to 20 %), MiniMax rejects the synthesized id with error 2013, and `prompt.py:83` tells Kimi its ids are "preserved exactly as issued". — In `harness_mode`, keep the first non-empty `tc["id"]` per index and emit it as the tool_use id (mint only when the upstream sent none; the proxy keeps minting), then add the row-driven `tool_id_normalize` hook (Kimi rename-on-import, Mistral alnum9) on top.

2. **critical** — `halo_harness/providers/oai_stream.py:159` + `profiles.py:188` + `request.py:71-74` — OpenRouter reasoning replay loses data in two places: each streamed `reasoning_details` delta overwrites the previous one, even though OpenRouter streams that array as per-chunk fragments (Roo and the AI-SDK provider merge them by type+index), and the OpenRouter profile turns every echo family into details-only replay, so reasoning captured as text only, or empty, is replayed as nothing (no `reasoning_content`, no `""`). — Verified: a stream carrying "I need " + "to read." is logged and replayed as `[{"type":"reasoning.text","text":"to read."}]`, and on the home default `or:deepseek/deepseek-v4.1-flash` any turn with empty reasoning goes back with no reasoning field at all, which DeepSeek rejects (OpenRouter relays the error) with the 400 "reasoning_content … must be passed back", while Kimi K3 gets truncated or no thinking history. — Merge `reasoning_details` deltas by (type, index), concatenating `text`/`summary`/`data` and keeping `id`/`format`/`signature`, and for `echo_required_400` rows (DeepSeek V4) also send `reasoning_content` = the accumulated `reasoning` text (`""` when absent) beside the verbatim details (research `deepseek_kimi_adapters.md:129`).

3. **major** — `halo_harness/agent/loop.py:356-362` — The loop never reads `harness_meta.tool_call_flags` or `length_with_minimal_output`, so a `length`-truncated tool call is logged as a `tool_use` with `input: {}` and no `tool_result`, the turn ends as `turn_done("max_turns")`, and a `length` reply with ≤ 1 token is treated as an ordinary stop. — Verified: a `finish_reason: length` tool call leaves an unpaired `toolu_…` in the log with zero tool_results (the next turn survives only because `_flatten_messages` pads "(no result)"), the model is never told to split the write, and the report's 287k-prompt "length with 1 token" case ends the `-p` run silently instead of re-routing. — On `max_tokens` + tool_use, write one `is_error` result per call ("cut off at max_tokens mid-call; split the operation") and continue; turn `malformed_json` flags into an `invalid` result that quotes the JSON error; on `length_with_minimal_output`, raise a ProviderFailure that re-pins (never retries in place); and report `max_tokens`, not `max_turns` (left over from H0 #12).

4. **major** — `halo_harness/agent/loop.py:104-119,340-345` + `agent/derive.py:43-45` — The log is not yet a checked source of truth: `request_hash` comes from a second `derive_request` run after the call rather than from the body actually sent, tools come from the live registry instead of the logged catalog, and `Session.__init__` appends meta + system + snapshots even to an existing log. — The runtime assertion can never fire (`tests/test_log_derive.py:120-138` re-derives the same nodes and passes by construction), a tool-description change between runs replays bytes that were never logged, and the first resume (`SessionLog.latest_for_cwd` → `Session(session_log=…)`) dies with `LogAssemblyError: more than one 'system' node`. — Hash the exact `prebuilt_oai_body` in `_step`, leaving out the policy keys (max_tokens, sampling, provider, usage), and test that rebuilding it from `derive_request(upto=seq)` plus the meta node's tools reproduces it; derive tools from the logged frozen catalog; and on a non-empty log, skip the meta/system/snapshot writes, check the logged system text, and run `synthesize_missing_results` before the first new user node.

5. **major** — `halo_harness/providers/request.py:179-180` — `profile.openrouter_pin` is never used: OpenRouter requests carry only `provider: {"require_parameters": true}`, and only when tools are present. — Every seeded pin is dead data: `or:deepseek/deepseek-v4.1-flash` is not held to the `deepseek` first-party endpoint, `deepseek/deepseek-v3.2` can land on deepinfra fp4 (16 384-token output cap) while asking for `max_tokens: 65536`, and the H1 acceptance line "(pinned providers)" ran unpinned. — Merge the row's pin (`order`, `allow_fallbacks`, `only`, `ignore`, `quantizations`) into `provider` on every OpenRouter request, and add `require_parameters: true` whenever tools are sent.

6. **major** — `halo_harness/providers/model_table.json:68,70` + `request.py:133-146` — Databricks `max_tokens` is set to the whole OTPM budget (V4 Pro 0813: 4 000; Kimi K3: 40 000) with no accounting for output already spent in the trailing minute, no `stream_options: {"include_usage": true}` to account with, and no default `reasoning_effort`, so Databricks' `max` default applies (plan point 12 says to lower it). — Under the documented pre-admission rule, the second step of a tool loop within 60 s gets an immediate 429, after which the single capped retry stalls about 60 s per step, and max-effort reasoning readily spends V4 Pro's 4 000 tokens, leaving an empty `length` reply. — Send `stream_options.include_usage`, keep a per-model rolling 60 s output-token window and budget `min(cap, OTPM − used − margin)` (waiting when below a floor instead of taking the 429), and send the row's default effort on Databricks when `--effort` is absent.

7. **major** — `halo_harness/agent/loop.py:243-279` — Retries fall short of brief D/D4 on four counts:
   - at most one retry, instead of the 1-2-4-8-16 s ladder with max 5;
   - no empty-completion detection (`EMPTY_RESPONSE` is defined but never used);
   - the Databricks 429 body's `retry_after` is never read (`parse_databricks_rate_limit` is only called from tests);
   - `is_reasoning_replay_bug` runs only on phase-2 wire errors, but a direct Databricks/DeepSeek 400 arrives in phase 1.

   — DeepSeek V4 Pro's documented empty completion after tool results (22 of 46 turns) ends a `-p` run with empty stdout and exit 0, and a replay-bug 400 on Databricks surfaces as a generic `invalid_request_error`. — Implement the ladder with an abort-aware capped wait fed by the header or body `retry_after`, retry an empty completion once (then lower effort or re-pin, then error), and name the replay bug on both paths.

8. **major** — `halo_harness/providers/request.py:38-46,59-65` — Reasoning is re-attached by assistant-message position, but `_flatten_messages` emits no proto for an empty or whitespace-only assistant node, so each later assistant message receives its predecessor's reasoning. — Verified: an assistant node with `content: []` and reasoning R1 (a reply that spent its whole budget thinking), followed by an assistant tool_use with R2, is sent as `reasoning_content: "R1…"` and R2 is lost. This stays latent inside a single `-p` turn, but every multi-turn session hits it after an empty V4 Pro reply, and signed OpenRouter details get a signature 400. — Attach reasoning while building each assistant proto, emitting `content: ""` for empty nodes instead of dropping them, and never match by index.

9. **major** — `halo_harness/agent/loop.py:306` — `--max-turns` counts `turn()` calls (one per `-p` run), so only the identical-call breaker bounds the model calls inside a turn. — `tests/test_loop_tools.py:48,69` runs with `--max-turns 3` and asserts at least 8 upstream calls, and a model that pages through a large file by `offset`, or alternates between two different calls, runs and bills indefinitely in `-p`. — Count model calls per turn against `max_turns` in `_turn_body` (Claude Code semantics), end with `turn_done("max_turns")`, and keep the breaker as a separate guard.

10. **major** — `halo_harness/tools/read.py:56-65` — Read calls `read_text` on the whole target with no regular-file check, no size cap and no per-line cap. — On Kali, `Read /dev/zero`, `/dev/urandom` or a FIFO hangs the process or runs it out of memory, and a minified bundle or binary becomes one multi-megabyte "line" in a logged tool_result that overflows every later request of the session (there is no compaction before H5). — Refuse non-`S_ISREG` files, read only up to `offset + limit` lines, truncate lines at 2 000 chars and the result at about 25k tokens with an offset/limit hint, and flag binary content.

11. **major** — `halo_harness/providers/oai_stream.py:80-97,170-173` — Stream aggregation is not null-safe and mis-keys index-less deltas: `"name": null` overwrites the captured name, `"arguments": null` raises TypeError, and an index-less delta whose id changed opens a new call instead of continuing the current one. — Verified: explicit-null continuation deltas either turn the call into tool "unknown" or kill the step with "upstream sent malformed data: can only concatenate str (not "NoneType") to str" (again on the retry), and a GLM-style id change (`chatcmpl-tool-…` → `toolcall0`) splits one call into two malformed ones. — Set id/name only from a non-empty str when unset, treat `arguments: null` as `""`, and key an index-less delta to the current call regardless of id (the report's `stream_aggregate` rules).

12. **major** — `halo_harness/providers/stream.py:374-375,392-410` + `agent/loop.py:164` — H0 #4 is only half fixed. The malformed-data `except` path and the mid-stream `error` chunk both `break` into `terminal_reached = True`, so the upstream socket is never shut, and the loop still passes no `abort` to `stream_completion`. — Verified: after the consumer received its error event, the mock kept streaming all 30 remaining chunks over about 3 s, with `sse_reader_thread` still alive and no disconnect, and the loop's retry then opens a second billed stream while the first is still running. — Set `terminal_reached` only for `done`/`eof`/`exc`, shut the socket on the error/malformed break, and give `Session` an abort Event that `_step` passes and the retry wait observes.

13. **major** — `halo_harness/providers/request.py:91` — `simplify_schema_for_databricks` deletes every dict key named `pattern`, `anyOf`, `oneOf`, `allOf` or `$defs` at any depth, including property names inside `properties`. — Verified: a Grep-shaped schema loses its `pattern` property while `required: ["pattern"]` stays, so H2's Grep and Glob (both require `pattern`) break on every Databricks route. — Strip these keywords only where they appear as schema keywords (never the keys of `properties`/`$defs`), and collapse `anyOf: [T, {"type": "null"}]` to T instead of deleting the type.

14. **minor** — `halo_harness/agent/prompt.py:61,74,83,101` — The self-description promises four things H1 cannot deliver:
   - saving memory "ONLY inside that memory directory" (there is no Write tool and the path is never given);
   - "running a test" (there is no Bash tool);
   - Kimi ids "preserved exactly as issued" (false, see finding 1);
   - "No MCP servers are configured" (false for rolo's 15 user MCP servers).

   — Models claim to have saved memory or run tests, and tell the user MCP servers don't exist. — Build the capability sentences from the registry, say "MCP tools are not available in this build", put the memory directory path in the memory snapshot, and drop the id sentence until finding 1 lands.

15. **minor** — `halo_harness/output.py:104,130` — Print-mode output has three gaps:
   - text mode prints every assistant message of the tool loop back to back, without `strip_display_artifacts` (which only tests call);
   - captured OpenAI-dialect reasoning never becomes a `thinking_delta`, so `--verbose` shows no thinking (brief acceptance 3);
   - JSON `usage`/`total_cost_usd` come from the last model call only.

   — `-p "read X and reply with only the number of lines"` prints "Let me read the file.42" whenever the model narrates first, and a three-call turn reports a third of its cost. — Print only the final message in text mode (intermediate messages and dimmed reasoning under `--verbose`), apply `strip_display_artifacts` to displayed text, and report cumulative usage and `CostMeter.total_usd`.

16. **minor** — `tests/test_databricks_mock.py:134`, `tests/test_loop_tools.py:135-136`, `tests/test_request_builder.py:71` — Several tests pass for the wrong reason, pin defects, or are missing:
   - the 429 assertion (`in ("2", 2, None) or … is not None`) is always true;
   - the interrupt test closes the turn at the first `message_start`, before any tool_use exists;
   - the OpenRouter test asserts finding 2's missing `reasoning_content`;
   - capture → log → replay is never exercised, because the mock has no reasoning scenario;
   - `SessionLog` tests write into the real `~/.halo/sessions` (39 `derive-test-*` dirs on this host);
   - env vars leak between tests, and `fake_home.py:129` still copies the real `settings.json`;
   - H0 #14 cases 3–6 and 8 are still missing.

   — The suite stays green through every finding above. — Set `BRIDGE_STATE_DIR` per test and restore env, then add these eight tests:
   1. an upstream `functions.Read:0` id round-trips into the next request;
   2. streamed `reasoning_details` fragments replay merged;
   3. OpenRouter DeepSeek with empty reasoning still sends `reasoning_content: ""`;
   4. an interrupt after `tool_use_ready` leaves a synthetic result;
   5. a `length`-truncated call yields an error result and a paired log;
   6. `length` + 1 token re-routes;
   7. explicit-null deltas are handled;
   8. the loop honours a Databricks 429 body's `retry_after`.

## No findings in
- **Databricks body:** the top-level allowlist with per-route `model` add/drop, explicit `stream: true`, and the 32-tool cap check.
- **Host-specific fields:** no `parallel_tool_calls` on any host (Qwen's DashScope default `false` holds), and OpenRouter-only fields stay off every non-OpenRouter host.
- **System message:** exactly one, leading (derive never emits `system` and `_flatten_messages` hoists), with no `developer` role, so the Qwen3.5 hard rule holds.
- **DeepSeek on Databricks:** `reasoning_content` is sent, including `""`, exactly when tools are present, and is absent otherwise.
- **tool_choice:** `required` → `auto` for the DeepSeek-V4/GLM/Qwen/K2.x rows. A named `tool_choice` is not downgraded, but nothing sends one yet.
- **Sampling:** no sampling params go to Kimi K2.6+/K3 on either host. GLM temperatures are all in [0, 1] with no override path (enforce `temperature_range` once the table is ingested).
- **GLM-5.3:** never disabled. Its fallback maps to `max` rather than the report's `low`, but no CLI value reaches that fallback.
- **System prompt:** no refusal/safety/cyber wording in any family, and byte-stable within a session (logged once and reused; date, git and model live in snapshots; tools name-sorted).
- **Loop mechanics:** the 3/5/8 per-turn loop breaker on canonical args, GeneratorExit/BaseException synthesis for the last assistant node, and surrogate repair.
- **Linux:** git calls (UTF-8, DEVNULL, timeout), path handling, and the POSIX fake-claude chmod.
- **H0 findings:**
  - 1–3, 6–10 and 13 are verified fixed.
  - #11 is fixed except that an inline `--settings '[]'` still raises AttributeError.
  - #5 is fixed except that a legacy backslash `projects` key still counts as trusted (no D-CFG re-decision is recorded).
  - #4, #12 and #14 are only partly fixed: see findings 12, 3/7 and 16.

## H2 must-do
1. **Fix findings 1–2 first.** H2's Write→Edit→Bash acceptance on `or:deepseek/deepseek-v4.1-flash` hits both.
2. **Confirm brief item E.** Ingest the canonical block (hosts + families + models, row = family ⊕ model ⊕ host, `unverified` kept) in place of `_thinking_defaults_for_family`/`tc_required_default`.
3. **Make the eight branches real.** Turn them into named hooks that request/stream/loop actually call, with one test each; today `run_code_branch` is an empty registry that nothing invokes. Findings 1–3, 5–8, 11 and 13 belong in these hooks, and `leak_parser`/`args_repair` are H2's repair layer anyway.
4. **Catch `ToolCatalogTooLarge` in `_step`** before tools + MCP can pass 32; today it escapes as a traceback.
5. **Phase-1 abort.** It is low risk while `-p` has no interrupt source, but it must ship with the first interrupt (Bash kill/Esc): hand `conn.sock` to the abort watcher right after `open_upstream()` connects, so an abort during a 300 s time-to-first-byte closes the socket.
6. **Open H0 compatibility items.** Route `_resolve_creds` through `Settings.effective_env`, and send `x-databricks-use-coding-agent-mode: true`.
7. **Live checks on the work VPN.** Confirm that Databricks accepts and forwards message-level `reasoning_content` (plan point 15), and that a three-step V4 Pro/K3 loop does not 429.
