# AgentBridge protocol 1.3 — Windows network extension

This extends PROTOCOL.md (1.2). M0 remains available without `agent --hub-config`. The Hub is authoritative for network messages; a network Agent does not move historical M0 messages into a different ledger. Complete or investigate the M0 queue before switching modes.

## Trust and addressing

Each per-user Windows Agent makes one outbound `wss://` connection. TLS 1.2+ verifies both trust and hostname/IP SAN. Bootstrap checks the SHA-256 certificate fingerprint. The private certificate lasts 90 days; renewal and redistributing trust are explicit administrator operations. This build targets a trusted LAN, not public Internet exposure.

The Hub stores hashes of node bearer tokens and explicit project grants. A one-time, one-hour invitation can be bound to an expected source IP. Redemption atomically consumes it and replaces the pre-enrollment token. A lost enrollment response requires investigation; it does not silently create another identity. Invitations stay out of source archives and shared code directories. Runtime credentials and keys stay in user/SYSTEM-protected directories.

First authentication binds a persistent random machine-install UUID and OS user identity. These self-reported fields prevent accidental reuse; they are not hardware attestation. A copied token plus copied identity can impersonate that node. The OS user, explicitly granted sandbox identities and Hub administrator remain trusted boundaries.

Global binding ID: `node_uuid:tool:native_uuid`; alias: `node_label/local_alias`. Identical native UUIDs and names on different nodes do not collide. Binding generations are immutable; pending messages never follow a new generation. Every RPC checks node revocation, actor ownership, generation and project. Revocation affects existing connections. Agents publish identity, alias, project, generation and enabled status only; native endpoints, peer tokens, CLI paths and model credentials remain local.

Local loopback capabilities remain session-scoped. Sandbox clients cannot bind, grant, stop, inspect other projects, impersonate another binding or read admin credentials. The legacy project label `agentbridge-m0` is reused for the explicitly enrolled M1 test nodes; it does not make network transport local.

## Durability and claims

Agent `net_ops` is a durable SQLite outbox. Hub mutation and RPC replay result commit atomically. Same RPC ID/payload returns its original result; a changed payload is rejected. Fresh CLI calls use fresh RPC IDs. Thus a lost response to one claim can be recovered, while a fresh duplicate claim returns `should_execute=false`.

Offline send returns `delivery=local_pending`, never Hub acceptance. Absolute expiration is preserved, so offline time does not grant a new TTL. Unknown target aliases are rejected, never guessed. Execution requires a confirmed Hub claim; offline/unconfirmed ack means do not execute. A crash after claim but before the model sees the result may leave `received/pending`; investigation is required. Exactly-once arbitrary model side effects are not claimed.

Routing adds `dispatched` between `accepted` and native submission. The Agent persists unique message-ID jobs to deduplicate reoffers. Before native write it commits a `submitting` intent and reporting outbox record together. Interrupted intents become `delivery_uncertain` and are never blindly reinjected. Reports use increasing sequence numbers and cannot overwrite receipt. Hub restart preserves messages, claims and final results; Agent restart preserves outboxes and intent.

Request completion and reply receipt are distinct. A final reply is another durable message; only `reply_delivery=received` proves the return path. Reply/notice/progress never trigger another business reply. Claimed work can finish after request TTL; expired unclaimed work cannot start.

## Explicit original-session endpoint refresh (2026-11-13 amendment)

Reopening Claude may replace its PID, pipe and peer key while preserving the original native session UUID. `refresh-claude --alias ALIAS` is a local administrator operation that discovers that exact UUID, verifies the live process start time, qualified runtime and matching peer key, then updates only endpoint metadata. The logical binding ID, native UUID, project, alias, generation and session capability remain unchanged. This is distinct from `bind`/replacement, which retains the original generation invalidation rules. Disabled bindings and concurrent generation changes cannot be refreshed. No alias guessing, new native session, unqualified runtime or credential fallback is allowed.

Refresh may recover an unexpired `blocked` delivery only if the complete local report journal proves every prior phase was a preflight block, with no native submission intent. The Hub independently checks receiver ownership, current binding generations, expiry, latest report sequence, no receipt and no historical submitting/submitted/uncertain event. The local retry stays pending until the Hub confirms it atomically; duplicate RPC replies cannot schedule duplicate native writes. Received, submitted, uncertain, expired or replaced-binding work is never automatically replayed. A session-scoped model client cannot invoke refresh or retry administration.

## Limits and acceptance

Body limit 32 KiB UTF-8; 50 unconfirmed messages per target; four envelopes per pull; WebSocket frame limit 1 MiB. Native escaped Windows command-line limits still apply. Claude qualification remains restricted to versions listed in adapters.py.

Automated tests use independent local Agent state directories, real TLS/WebSockets and simulated native queues. They do not count as two physical Windows machines or original model sessions.

Physical acceptance requires two enrolled Windows machines, user-authorized original Claude/Codex sessions, requests originated by those sessions, receiver ack/final reply, and source ack of that final reply. Preserve message IDs and matching native transcripts. Repeated IDs must not cause another native submission. Script-generated business replies do not substitute for model replies. macOS begins after this gate passes.
