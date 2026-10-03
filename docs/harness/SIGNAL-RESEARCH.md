# Signal remote control research (signal-cli) — 2026-10-02

Research pass for [plans/2.0.8-signal-brief.md](../../plans/2.0.8-signal-brief.md) (Halo 2.0.8: remote control of a running session from Signal). Every fact is cited to its URL. `support.signal.org` returned HTTP 403 to automated fetches this session (Zendesk bot wall) and the Wayback Machine is unreachable from this environment, so the first-party Signal support facts in §2-3 are cited to their canonical article URLs on the strength of stable, widely-corroborated public documentation rather than a live re-fetch; anything with a genuinely uncertain current value is pushed into the Open Questions (§7) instead of asserted. Everything under `github.com/AsamK/signal-cli` — README, CHANGELOG, man pages, wiki pages, and four JSON-serialization source files — was fetched live on 2026-10-02.

## 1. signal-cli today

signal-cli is "an unofficial commandline, JSON-RPC and dbus interface for the Signal messenger" — AsamK's own framing on the [repo home page](https://github.com/AsamK/signal-cli), GPLv3. Latest tagged release as of this research is **v0.14.8** (2026-09-10); the five most recent tags are 0.14.8, 0.14.7 (2026-08-01), 0.14.6 (2026-07-13), 0.14.5 (2026-06-11), 0.14.4.1 (2026-05-23) — [releases](https://github.com/AsamK/signal-cli/releases). The project warns its own releases go stale: "signal-cli needs to be kept up-to-date to keep up with Signal-Server changes... signal-cli releases older than three months may not work correctly" — [README](https://github.com/AsamK/signal-cli/blob/master/README.md). Halo should pin a version and have `halo doctor` check staleness against that 3-month window.

**Install options**, per the [README](https://github.com/AsamK/signal-cli/blob/master/README.md):
- Requires "at least Java Runtime Environment (JRE) 25" for the regular (JVM) build.
- **GraalVM native build**: Linux-only, the "experimental" native-image build (faster startup, lower memory); extract the native tar.gz to `/opt` — confirmed Linux-specific on the [Binary distributions](https://github.com/AsamK/signal-cli/wiki/Binary-distributions) wiki page.
- **Windows / macOS**: no native-image build; run the JVM tar.gz under a JRE 25. The README says provided binaries "should work on Linux, macOS and Windows." Don't confuse this with the *libsignal-client* crypto library, which is bundled natively for "x86_64 Linux (with recent enough glibc), Windows and MacOS" inside the JVM build — that bundling is what makes the JVM build cross-platform at all; it is not the GraalVM native-image build.
- **Docker**: not published by AsamK; the README points to "docker image and some Linux packages" as community contributions, with the actual image living in a third-party group's container registry — same wiki page.
- **Linux packages** (all third-party, same wiki page): Flathub (`org.asamk.SignalCli`), AUR, Debian/Ubuntu amd64+arm64 via a Zig-based standalone packaging project (glibc ≥ 2.17), FreeBSD ports, Alpine, and an RPM project for Fedora/EL (`pbiering/signal-cli-rpm`, not yet in Fedora/EPEL proper).

**Daemon modes.** Two distinct entry points, documented in [man/signal-cli-jsonrpc.5.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli-jsonrpc.5.adoc) and [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc):
- `signal-cli -a ACCOUNT jsonRpc` — reads JSON-RPC on stdin, writes on stdout; one embedded client only, no socket.
- `signal-cli [-a ACCOUNT] daemon [--socket[=PATH]] [--tcp[=HOST:PORT]] [--http[=HOST:PORT]] [--dbus|--dbus-system]` — multi-client. `--socket` exports a JSON-RPC Unix socket, default `$XDG_RUNTIME_DIR/signal-cli/socket`; `--tcp` defaults to `localhost:7583`; `--http` defaults to `localhost:8080` and exposes three endpoints: `POST /api/v1/rpc` (single or batch JSON-RPC requests), `GET /api/v1/events` (Server-Sent Events stream of incoming-message notifications), `GET /api/v1/check` (liveness probe). `-a` is optional on `daemon` — omit it to serve every local account from one process.
- D-Bus: `--dbus` (session bus) or `--dbus-system`, service name `org.asamk.Signal`, object path `/org/asamk/Signal` (or `/org/asamk/Signal/_<phone-number>` for a multi-account daemon); incoming messages arrive as a `MessageReceived` signal (timestamp, source, groupID, message, attachments) — [DBus service](https://github.com/AsamK/signal-cli/wiki/DBus-service) and [man/signal-cli-dbus.5.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli-dbus.5.adoc). The brief already rules this out ("never over D-Bus"); confirmed here only for completeness.

**JSON-RPC method surface Halo needs**, names per [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc) (every signal-cli subcommand is callable as a same-named JSON-RPC method against a running `daemon`): `send`, `sendTyping`, `sendReceipt`, `receive` (daemon subscribes clients automatically; HTTP mode uses `/api/v1/events` SSE instead of a `receive` call), `listIdentities`, `trust`, `listDevices`, `removeDevice`, `link`/`addDevice` (linking, §2), `register`, `verify`, `updateProfile`, `updateAccount`, `listContacts`, `sendContacts`, `sendSyncRequest`, `setPin`, `removePin`, `submitRateLimitChallenge`, `block`/`unblock`, `remoteDelete`.

**Notification shape of an incoming message.** The daemon's `receive` push (socket/TCP) and the HTTP `/api/v1/events` stream both serialize a `MessageEnvelope` via `JsonMessageEnvelope`, whose fields are, per source ([JsonMessageEnvelope.java](https://github.com/AsamK/signal-cli/blob/master/src/main/java/org/asamk/signal/json/JsonMessageEnvelope.java)): `source` (deprecated), `sourceNumber`, `sourceUuid`, `sourceName`, `sourceDevice`, `timestamp`, `serverReceivedTimestamp`, `serverDeliveredTimestamp`, and exactly one of `dataMessage`/`editMessage`/`storyMessage`/`syncMessage`/`callMessage`/`receiptMessage`/`typingMessage` (null fields omitted via `@JsonInclude(NON_NULL)`). A plain-text chat message arrives as `dataMessage`, whose own fields ([JsonDataMessage.java](https://github.com/AsamK/signal-cli/blob/master/src/main/java/org/asamk/signal/json/JsonDataMessage.java)) include `timestamp`, `message`, `expiresInSeconds`, `viewOnce`, `attachments`, `groupInfo`, `mentions`, `quote`, `reaction`, `remoteDelete`, `textStyles`, and more. A typing notification is `typingMessage`: `action` (`STARTED`/`STOPPED`), `timestamp`, optional `groupId` ([JsonTypingMessage.java](https://github.com/AsamK/signal-cli/blob/master/src/main/java/org/asamk/signal/json/JsonTypingMessage.java)). A message the user sent from another linked device (or Note to Self) arrives as `syncMessage` wrapping `JsonSyncDataMessage`: `destination`/`destinationNumber`/`destinationUuid`, optional `editMessage`, and an unwrapped `dataMessage` ([JsonSyncDataMessage.java](https://github.com/AsamK/signal-cli/blob/master/src/main/java/org/asamk/signal/json/JsonSyncDataMessage.java)) — this is the exact shape Halo's linked-device filter matches on (§7).

Exit codes, same man page: `1` user-fixable error, `2` unexpected error, `3` server/IO error, `4` sending failed due to an untrusted key, `5` server rate-limiting error, `6` CAPTCHA rejected.

## 2. Registration and linking

**Captcha registration flow** ([Registration with captcha](https://github.com/AsamK/signal-cli/wiki/Registration-with-captcha)): open `https://signalcaptchas.org/registration/generate.html` on the same device/IP as signal-cli, solve it, right-click the resulting "Open Signal" link and copy its URL — that `signalcaptcha://signal-recaptcha-v2...` string is the `--captcha` value. Then:
```
signal-cli -a +1555... register --captcha "signalcaptcha://signal-recaptcha-v2..."
signal-cli -a +1555... verify 123456
```
A second captcha endpoint, `https://signalcaptchas.org/challenge/generate.html`, feeds `submitRateLimitChallenge` instead of registration — same wiki page. Registering disables the number on any phone that held it; re-enabling phone use requires signal-cli's device-linking flow run the other way (phone links to the signal-cli-registered account) — same page.

**Number types / voice vs SMS.** `register` takes `--voice` to request a voice call instead of SMS; the [Quickstart](https://github.com/AsamK/signal-cli/wiki/Quickstart) wiki page notes voice is for numbers that cannot receive SMS (landlines), and that signal-cli enforces a 60-second wait after an SMS attempt before `--voice` can be retried. Neither this page nor the man page states whether VoIP numbers are accepted or disfavored by Signal's servers — open question (§7); community reports (not independently re-verified this session) describe VoIP numbers as workable but more likely to need a voice-call fallback or trip rate limits.

**Rate limits.** Exit code `5` is literally "server rate limiting error"; the `trust`/registration/sending paths can all hit it. `submitRateLimitChallenge` exists specifically to "lift" a rate limit "by solving a CAPTCHA" against the `challenge/generate.html` endpoint above — [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc). Signal does not publish numeric thresholds; treat them as opaque and handle exit code 5 by backing off and surfacing the challenge flow to the TUI, not by retrying blindly.

**Registration lock PIN.** `setPin` — "Set a registration lock pin, to prevent others from registering your account's phone number"; `removePin` is the inverse; `verify` takes an optional PIN, "only required if a PIN was set" — [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc). Signal's own article on this mechanism is [support.signal.org — Signal PIN](https://support.signal.org/hc/en-us/articles/360007459591-Signal-PIN) (blocked to automated fetch this session, see header note); publicly documented behavior is that re-registering a PIN-locked number without the PIN is refused for a lockout window before the new registrant can force it through — exact current lockout length is an open question (§7), commonly reported as 7 days.

**What happens if the dedicated number is later reused by a phone carrier.** Follows directly from the mechanics above: registering any number on a new device always unregisters whatever Signal account currently holds it server-side (that is what `register`/`verify` do). If Halo's dedicated VoIP number is later recycled to a new phone owner, that install will attempt to register the same number; with `setPin` set on the Halo account, it is refused until the PIN is supplied or the lockout window elapses — exactly why the brief should set a registration-lock PIN on Halo's dedicated number and store it in `~/.halo/remote/` with the rest of the secrets.

**Linking as a secondary device** ([Linking other devices (Provisioning)](https://github.com/AsamK/signal-cli/wiki/Linking-other-devices-\(Provisioning\))):
```
signal-cli link -n "halo"                         # prints a sgnl://linkdevice?... URI
signal-cli -u +1555... addDevice --uri "sgnl://linkdevice?..."   # run on the primary device
```
Render the printed URI as a QR code (the wiki suggests `qrencode`) for the phone's "Link a device" scanner, or run `addDevice` from an already-linked signal-cli. "Signal allows up to *five* linked devices per primary" — same page; exceeding it surfaces as an authentication failure. Only the primary (directly registered) account can add devices; `link` must not be given `-a`. After linking, the new device must run `receive` once to pull "the list of contacts and groups from the main device" — the wiki does not separately document full message-history sync, so Halo should not assume backfill of old messages, only forward sync from link time. `listDevices`/`removeDevice -d ID` manage the device list. **Sync messages / Note to Self**: a message the account sends from any device (including to itself) reaches every other linked device as a `syncMessage` (`JsonSyncDataMessage`, §1); "Note to Self" is simply a conversation whose destination is the account's own number/UUID, not structurally different from any other sync message — Halo's linked-device filter (§7) keys off exactly this field.

## 3. Limits

**Message size.** signal-cli does not document a hard character limit; instead it mirrors the official clients' behavior of converting long text to an attachment. Three corroborating [CHANGELOG.md](https://github.com/AsamK/signal-cli/blob/master/CHANGELOG.md) entries: "[0.11.5] Send long text messages as attachment instead. This matches the behavior of the official clients," "[0.13.4] For long text messages the text attachment is used instead of the truncated body," "[0.13.8] Fix sending large text messages." No exact byte/character threshold was found in any fetched source — open question (§7); Halo's bridge should chunk long replies well before any such cliff rather than rely on signal-cli's fallback.

**Attachment limits.** Not documented anywhere fetched (man pages, wiki, CHANGELOG) — genuinely an open question (§7), to be determined empirically (send an oversized file and read the resulting exit code / JSON-RPC error) before relying on attachment relay for large tool output.

**Sending rate / throttling.** Covered in §2: opaque server-side limiting surfaced as exit code `5`, recoverable via `submitRateLimitChallenge`. No documented per-minute number exists to design around; the bridge's own per-peer rate limit (brief §2.6) should stay well under whatever Signal's threshold is so Halo never triggers a captcha challenge on the dedicated number mid-session.

**Typing indicators.** `sendTyping` — "Send typing message to trigger a typing indicator for the recipient. Indicator will be shown for 15 seconds unless a typing STOP message is sent first" — [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc). Received typing events arrive with the `typingMessage` shape from §1 — a good fit for "a turn is running" feedback, re-sent every &lt;15s while streaming.

**Read receipts.** `sendReceipt` sends "a read or viewed receipt to a previously received message"; `receive --send-read-receipts` auto-sends read receipts "for all incoming data messages" — same man page. Halo should call `sendReceipt` on each command message once accepted, doubling as a lightweight "received" ack to the phone.

**Disappearing messages interaction.** `JsonDataMessage` carries `expiresInSeconds` per message (§1 citation) — signal-cli surfaces, and can presumably set, the per-conversation disappearing-timer value on each message rather than hiding it. Design implication: the bridge should read and propagate whatever `expiresInSeconds` it receives on outgoing replies rather than hard-coding its own value, so a user with a disappearing-message timer set on their side keeps it instead of the bridge silently overriding it.

## 4. Identity and verification

`listIdentities` "List[s] all known identity keys and their trust status, fingerprint and safety number" for all contacts, or `-n NUMBER` for one — [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc). This is the single call that gives Halo a peer's current safety number to pin.

**Trust model is TOFU** (trust-on-first-use), stated directly in the `trust` command's own description: "The first time a key for a recipient is seen, it is trusted by default (TOFU). If the key changes, the new key must be trusted manually" — same man page. The global `--trust-new-identities` flag controls policy: `on-first-use` (default, as above), `always` (trust any new key, no verification — unsafe for a bridge), `never` (every key, including the first, needs manual trust). **Halo should run signal-cli with `--trust-new-identities=never`** so that it, not signal-cli, decides when a peer's identity is first accepted (at pairing) and on any later change (refused per the brief's design, not silently re-trusted).

Trusting a key manually is `trust -v SAFETY_NUMBER NUMBER` (verified: "having verified it") or the explicitly discouraged `trust -a NUMBER` ("only use this if you don't care about security") — same page. Attempting to `send` to a recipient whose key is untrusted fails with **exit code 4** — the concrete signal Halo's bridge can use to detect "safety number changed, blocked" without parsing prose.

**Mapping to the brief's pairing/pinning design** (brief §2.2): at pairing time, call `listIdentities -n <peer>` (or read the identity fields off the first `receive` envelope) and store the ACI/UUID plus the returned safety number in `peers.json`. Before relaying each subsequent message, either re-check `listIdentities` or simply attempt `send` with `--trust-new-identities=never` already set daemon-wide, and treat exit code 4 as "identity changed, refuse and notify" — this gives the brief's "any later safety-number change is refused... until re-verified" behavior directly from signal-cli's own exit-code semantics, no extra polling loop required. Re-verification should still require a fresh `trust -v` with the newly confirmed safety number plus the phone-side pairing-code check, not just an operator `trust -a`.

## 5. Operational concerns (Kali VM, work VM behind a proxy)

**Network.** signal-cli needs outbound HTTPS/WebSocket access to Signal's own service domains; it needs no inbound port from the internet — the daemon's socket/TCP/HTTP listeners are for local clients (Halo) only, not for Signal. Exact current hostnames were not independently re-verified this session (no fetchable, stable list was found in the man pages or wiki); treat "allow outbound 443 to Signal's service infrastructure" as the working rule and confirm specific domains against the work VM's egress logs during setup (open question, §7).

**Proxy.** The CHANGELOG shows signal-cli has *some* proxy support: "[0.13.13 — 2025-02-28] Fix check for registered users with a proxy" and "[0.13.13] Fix contact sync for networks requiring proxy" — [CHANGELOG.md](https://github.com/AsamK/signal-cli/blob/master/CHANGELOG.md). No `--proxy`/`-x` flag turned up in the fetched `signal-cli.1.adoc` text, nor does the FAQ wiki page mention proxies, so this is most likely Signal's own anti-censorship TLS-proxy feature (the one the official apps expose as "use proxy"), not a generic corporate HTTP/SOCKS passthrough — unconfirmed, open question (§7). For the work VM, don't assume a signal-cli flag routes through a corporate proxy: verify against `signal-cli --help`/`daemon --help` at setup time; as a generic JVM fallback, the regular (non-GraalVM) build should honor standard Java networking system properties (`-Dhttps.proxyHost=`, `-Dhttps.proxyPort=`, settable via `JAVA_TOOL_OPTIONS`) if signal-cli's HTTP stack respects them, which needs a hands-on check; OS-level SOCKS/VPN routing is the proxy-agnostic fallback.

**Data directory.** Default `$XDG_DATA_HOME/signal-cli` (`$HOME/.local/share/signal-cli`), overridable with `-d`/`--data-dir` or `-c`/`--config`; the man page's only stated requirement is "full read/write access to the given directory" — [man/signal-cli.1.adoc](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc). signal-cli stores account state in SQLite: the [Quickstart](https://github.com/AsamK/signal-cli/wiki/Quickstart) wiki page links "backup procedures using SQLite before version upgrades," confirming the data directory holds live SQLite databases, not just key files. signal-cli itself does not mandate restrictive permissions; **0700 on the directory / 0600 on its files is Halo's own hardening choice** (consistent with the brief's `~/.halo/remote/` posture), on top of keeping it under `~/.halo/remote/signal-cli/` as the brief already specifies.

**Backups.** Per the Quickstart page above, back up the SQLite files before any signal-cli upgrade. Treat the whole data directory as the backup unit — it must also survive a VM rebuild without re-registering the dedicated number.

**systemd.** Nothing is shipped upstream: neither the [signal-cli repo](https://github.com/AsamK/signal-cli) nor its Debian/Ubuntu packaging project ([gitlab.com/packaging/signal-cli](https://gitlab.com/packaging/signal-cli)) provides a unit file. An illustrative `--user` unit built only from documented flags, for `halo remote signal setup --daemon` to write:
```ini
[Unit]
Description=signal-cli JSON-RPC daemon for Halo remote control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/signal-cli/bin/signal-cli -a +15555550123 --config %h/.halo/remote/signal-cli daemon --socket %h/.halo/remote/signal-cli.sock
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```
installed at `~/.config/systemd/user/halo-signal-cli.service`, enabled with `systemctl --user enable --now halo-signal-cli`. On the Kali VM this is the natural fit; a native-Windows work VM has no user-systemd equivalent and no XDG runtime dir, so prefer `--tcp 127.0.0.1:PORT` (loopback-only) over `--socket` there and run the daemon under a Scheduled Task instead — Halo's own recommendation, not a signal-cli-documented Windows service story (none was found).

## 6. Alternatives considered and why not

**signald.** Self-described on its own project page as "an API for interacting with Signal Private Messenger. **Does not work with the current production Signal servers**" — [gitlab.com/signald/signald](https://gitlab.com/signald/signald). That first-party statement alone disqualifies it regardless of its historical feature set.

**Matrix bridges (e.g. mautrix-signal).** [mautrix/signal](https://github.com/mautrix/signal) describes itself simply as "A Matrix-Signal puppeting bridge" (Go, AGPL-3.0). Wrong shape even setting its backend aside: a puppeting bridge exists to mirror many Signal conversations into many Matrix rooms for many Matrix users, pulling in a Matrix homeserver as a new dependency and solving a federation problem Halo doesn't have. Halo needs one authenticated phone talking directly to one daemon's JSON-RPC surface — a bridge like this is strictly more moving parts for less control over the pairing/signing/pinning layers the brief specifies in §2.2-2.3.

**No public bot API.** Signal provides no official automation/bot API; the entire ecosystem signal-cli sits in exists *because* of that gap. signal-cli's own README calls itself "unofficial" (§1 citation), and the [project wiki's home page](https://github.com/AsamK/signal-cli/wiki) separately catalogs a long tail of third-party workarounds built on top of it (REST gateways, MQTT bridges, mailing-list and archiving bots) — evidence every "Signal bot" in the wild is reverse-engineered client traffic, not a sanctioned integration surface. Practical consequence: Signal grants signal-cli no special trust boundary beyond "it's a device like any other," so the brief's own pairing code, per-peer HMAC signing, and safety-number pinning (§2.2-2.3) are doing all of the "only my owner can drive this" work — none of it comes from Signal or signal-cli for free.

## 7. Mapping the brief onto signal-cli

**Pairing message flow.**
1. Bridge subscribes to `receive` (socket/TCP daemon) or polls `/api/v1/events` (HTTP daemon).
2. A `dataMessage` envelope arrives from an unknown `sourceNumber`/`sourceUuid` (not in `peers.json`).
3. Bridge calls `listIdentities -n <sourceNumber>` to capture that sender's current safety number, generates a one-time pairing code, and calls `send` back to `sourceNumber` with it (also shown in the Halo TUI per the brief).
4. The phone replies; bridge matches the code against the pending pairing entry, then writes `{aci, number, safetyNumber}` into `~/.halo/remote/peers.json`.
5. Every later message from that `sourceUuid` is accepted only while a fresh `listIdentities` (or a `send` attempt) doesn't come back exit-code-4 (§4); on change, refuse and surface it in both the phone reply and the TUI, matching the brief's "refused... until re-verified."

**Linked-device self-filtering** (brief's alternative identity model): in this mode the `receive`/`/api/v1/events` stream carries the account's entire traffic, not just messages addressed to Halo. The bridge must drop everything except messages whose envelope is a `syncMessage` (`JsonSyncDataMessage`, §1) with `destinationNumber`/`destinationUuid` equal to the account's own number/ACI — i.e. exactly "Note to Self" traffic — and must never log, persist, or otherwise relay any other conversation it incidentally receives, per the brief's explicit requirement.

**Chat commands → JSON-RPC calls** (method names per §1; `account` scoping omitted):

| Phone command | signal-cli call(s) |
|---|---|
| plain text (prompt/steer) | relayed to `Controller.submit`/`steer`; reply via `send` |
| `/sessions` | `send` with the rendered `list_sessions` output |
| `/attach <n>` | local state only, then `send` confirming; no signal-cli call |
| `/status` | `send` with current phase/status line |
| `/stop` | local `Controller.interrupt`; `send` confirming |
| `/mode <name>` | local `set_permission_mode`; `send` confirming |
| `/model <ref>` | local `set_model`; `send` confirming |
| `/effort <level>` | local controller call; `send` confirming |
| turn running | repeated `sendTyping` every &lt;15s; coalesced `send` for streamed output |
| incoming command accepted | `sendReceipt` (read/viewed ack) |
| `/remote pair` / `/remote unpair` | pairing state machine above; `listIdentities`/`trust` |
| any Halo slash command | passed to `Controller.run_slash`; `send` with its output |

**Open questions** (none resolved by sources fetched this session):
1. Exact `--proxy`/equivalent flag name and semantics (Signal's anti-censorship proxy vs. a generic corporate proxy) — confirm via `signal-cli --help` on the installed version before the work-VM rollout.
2. Exact message-length threshold before the long-text-as-attachment fallback kicks in (mechanism confirmed, no number found).
3. Exact attachment size cap (nothing found in any fetched source).
4. Exact registration-lock lockout duration for a contested number (commonly reported as 7 days; not independently confirmed this session).
5. Whether VoIP numbers are reliably accepted for registration/voice verification, or disfavored/rate-limited more than mobile numbers.
6. The current outbound hostname/port allowlist Signal's servers actually require, for the work VM's proxy/firewall rules.
7. Whether `daemon --socket` is usable at all on native Windows, or `--tcp` on loopback is required there (no Windows-specific daemon-mode guidance was found).
8. What exactly the [Feature-Matrix](https://github.com/AsamK/signal-cli/wiki/Feature-Matrix) wiki page's plaintext-CLI/json-CLI/dbus support grid says feature-by-feature — the page exists and covers this, but its table content wasn't captured in this pass.
9. Whether HTTP-mode's `/api/v1/events` stream, in a multi-account daemon (no `-a`), tags each event with its receiving account — needed if Halo ever runs the dedicated number and a linked-device account from one daemon.
