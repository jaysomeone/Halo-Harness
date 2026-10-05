# Multi-subscription build plan

1. [x] Create secure managed Codex profiles and guided browser login.
2. [x] Make Codex app-server launch account-aware and report each managed account's status.
3. [x] Detect confirmed subscription exhaustion and switch to another Codex account without retrying unsafe tool side effects.
4. [ ] Add equivalent isolated profiles and guided login for Claude subscriptions.
5. [ ] Add cross-provider fallback after all accounts on the active provider are unavailable.
6. [ ] Finish status UI, documentation, regression checks, and release preparation.
   - [x] Keep the last successful `cc:`/`cx:` usage reading visible on subscription routes and hide it on every other route.
