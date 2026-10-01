# Family baselines -- H13 Part D (2026-09-29)

Live acceptance set on the real OpenRouter account, one short battery per family with no real
volume yet (RECOMMENDATIONS.md §4): `pong`; Read a 200-line scratch file and answer a specific
line; a Write -> Edit -> Bash chain on a scratch file, `--permission-mode auto`; one steer
(`--input-format stream-json`, a second line written while the first turn is still in flight).
Kimi K3 kept to the brief's own cheap subset (pong + Read only). Numbers below are from
`halo stats --models --since 1d --all-projects --json` (the session logs these live calls
actually wrote), not the driver script's own stdout capture, which is the authoritative source
per the brief -- and turned out necessary: the driver's own capture of the steer turn's final text
raced against process exit on 3 of the 4 attempts (see "Driver bugs found" below), while the real
session log's own `steers` counter is populated synchronously by the harness itself and is never
affected by that race.

## Results

| Family | Model id | Provider that answered | Sessions | Calls | Cost | Tool calls | Tool error | Edit failures | Avg latency | Steer |
|---|---|---|---|---|---|---|---|---|---|---|
| GLM-5.3 | `z-ai/glm-5.3` | Z.AI (21/22 calls) | 9 | 22 | $0.1327 | 13 | 0% | 0% | ~8.4s | confirmed (1) |
| Qwen | `qwen/qwen3.8-27b` (see below) | Wafer (9/10 calls) | 5 | 10 | $0.0157 | 5 | 0% | 0% | ~5.0s | confirmed (1) |
| MiniMax M3 | `minimax/minimax-m3` | Together (17), CoreWeave (9), openrouter (1) | 13 | 27 | $0.0282 | 14 | 0% | 0% | ~2.0s | confirmed (1) |
| Kimi K2.7-code | `moonshotai/kimi-k2.7-code` | Moonshot AI (29/30 calls) | 13 | 30 | $0.0895 | 19 | 0% | 0% | ~2.4s | confirmed (1) |
| Kimi K3 | `moonshotai/kimi-k3` | Moonshot AI | 2 | 3 | $0.0391 | 1 | 0% | n/a | ~8.1s | not attempted (brief: cheap subset) |

"Confirmed" = the real session log's own `steers` counter (via `stats --models`) shows 1 for that
model, i.e. the second stdin line genuinely landed as a steer inside the still-running first turn,
never a separate second turn -- proven independently of the driver script's own (buggy, see below)
stdout capture. Edit failures are `n/a` for Kimi K3 since its cheap subset never calls Edit at all.
Tool error / edit failure percentages are all 0% across every family -- **no change to
`model_table.json` is justified by this pass** (the brief's own bar: adjust a row only where a
live failure proves the current value wrong; every row here stays "no change, n=1").

## Qwen: `qwen/qwen3.8-flash` is unavailable under this account's privacy settings

The brief's own "current `qwen/qwen3.8-*` coding-capable id" is `qwen/qwen3.8-flash` (its OpenRouter
listing: "suited for coding assistance, agentic workflows, visual understanding..."). Every one of
11 live calls against it failed IDENTICALLY and instantly (no tokens billed):

```
0 endpoints out of 1 requested are available matching your guardrail restrictions and data policy.
We removed them for the following reasons (an endpoint may have matched multiple reasons):
ZDR violation (account settings): 1 endpoint excluded; configurable at
https://openrouter.ai/settings/privacy
```

`qwen/qwen3.8-flash` has exactly ONE upstream provider on OpenRouter today, and that provider does
not meet this account's own Zero-Data-Retention privacy setting -- a real, 100%-reproducible,
account-configuration fact, not a halo code defect (no tool id / reasoning replay / sampling
param / edit format is at fault, so there is nothing in `model_table.json` to fix; the brief's own
"fix each bug found with a pinning test" categories all assume a code-level defect, which this
is not). The four live checks above ran against `qwen/qwen3.8-27b` instead (the OTHER `qwen3.8-*`
id OpenRouter's own listing describes as "suited for coding... professional workflows"), which has
multiple providers and answered cleanly on every check. Actionable for a user who specifically
wants `qwen3.8-flash`: relax the account's ZDR setting at the URL above, or accept `qwen3.8-27b`
(or the existing, already-tabulated `qwen/qwen3-coder` line) instead.

## Driver bugs found (in the live-run driver script, never in `halo_harness` itself)

The one-off Python driver used for these live calls (not part of the repo's own test suites) hit
two real bugs while exercising the steer check, both fixed in the driver before the affected
families were re-run -- recorded here because they explain the "confirmed via telemetry, not the
driver's own JSON" methodology note above, and because the SECOND one is a genuinely easy trap for
any future scripted `--input-format stream-json` client:

1. **stderr pipe deadlock**: the driver piped the child's stderr but never drained it until a
   final `communicate()` -- once a child wrote enough to stderr to fill the OS pipe buffer while
   the driver was separately blocked reading stdout, the child deadlocked (blocked writing stderr)
   and the driver hung past its own timeout (a single blocking `readline()` call never re-checks a
   deadline). Fixed by writing stderr straight to a file instead of a pipe.
2. **waiting for a stdout signal that may not exist**: the driver waited to see a `"stream_event"`
   line before writing the steer, to make sure the first turn was still in flight. For a FAST first
   turn (its whole reply -- "system" then "result" -- arriving in under a second, with no
   intermediate `stream_event` line ever emitted at all), that wait blocks forever: the driver is
   stuck in `readline()` waiting for stdout that will never come, while the CLI process is
   correctly, separately waiting for the driver's NEXT stdin line -- a mutual deadlock neither side
   breaks on its own. Fixed by draining stdout on a background thread (never blocks the main one)
   and sending the steer after a short fixed delay instead of waiting for a signal that isn't
   always there. Even with this fix, the driver's own end-of-run read of the accumulated stdout
   lines raced against the child process's exit on 3 of 4 attempts (an empty `result_count` in the
   driver's own JSON) -- `stats --models`'s `steers` counter, read straight from the session log
   the real harness wrote, is unaffected by either race and is what the "confirmed" column above
   is based on.

## Cost

Total spend for this whole pass (5 families, `stats --models --since 1d`): **$0.305** (GLM-5.3
$0.1327 + Qwen $0.0157 + MiniMax M3 $0.0282 + Kimi K2.7-code $0.0895 + Kimi K3 $0.0391), plus the
`qwen/qwen3.8-flash` attempts (11 calls, $0 -- every one failed before any token was billed).
