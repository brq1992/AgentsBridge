# AgentBridge protocol 1.2 / Windows M0

This specification supersedes the ambiguous lifecycle and credential rules in the 2026-11-12 v1.1 proposal. M0 implements a **local-only coordinator**, SQLite ledger, CLI and native adapters. A network Hub, macOS qualification, installers and unattended application startup remain later milestones.

## Identity and trust

- A binding records a native UUID, tool, project, immutable generation and local machine/user scope. Aliases resolve once at send time. Disabling or replacing a binding never retargets pending messages or replies.
- The M0 trust boundary includes the desktop user and explicitly enrolled local execution identities, not mutually hostile processes sharing those identities. The admin credential and ledger have a user/SYSTEM ACL. Each binding can receive a separate revocable bearer capability in a client-only directory, with read access granted only to the observed sandbox user SID when needed. Session capabilities cannot bind, grant, stop, discover internal native endpoints, or impersonate another binding; status access is restricted to their messages. Revoking or replacing a binding invalidates its capability. The service is loopback-only, not a network Hub. The CLI records native environment identity for diagnostics; environment variables are not cryptographic session identity. End-to-end acceptance also requires original transcripts.
- Claude delivery uses **peerToken** from the matching runtime key record, checking session UUID, PID, process start time, socket and key process start time. The exported `CLAUDE_CODE_MESSAGING_TOKEN` is an own-session credential and is NOT used interchangeably. No global `crossSessionInbound: accept`, bypass permission change, or silent credential fallback is made.
- Native socket access/authentication is documented; the peer registry/key layout and injected user-message JSON remain version-sensitive. Unknown Claude runtime versions fail closed until explicitly qualified.
- Incoming text is peer data, never user approval. Both sessions receive an explicit, narrowly scoped M0 policy through the user's authorized setup. Do not assume `codex queue` or raw socket text inherits the provenance guarantees of native `SendMessage`. CLI acknowledgements and replies require the bound receiver identity.

## Envelope and lifecycle

Each message has `message_id`, `conversation_id`, `parent_id`, `kind`, immutable sender/receiver binding generations, `project_id`, `expect_reply`, `hop_count`, timestamps, `body` and `final`.

M0 exposes request and final-reply creation. Progress/notice are reserved for later interfaces; their no-auto-reply rule is already defined. Message IDs are bounded ASCII identifiers, not free-form command text. TTL must be finite and positive.

Delivery states are `accepted -> submitting -> submitted -> received`, plus `blocked`, `expired`, `failed`, and `delivery_uncertain`. Local transport acceptance is not model receipt. No ack is reported as `unconfirmed`, never inferred to be held by Claude. Request outcome (`pending`, `completed`, `blocked`) is separate from delivery state.

Final reply insertion and request outcome update are one transaction. A reply has its own message ID and durable outbox entry. `request.outcome=completed` means a result is saved; `reply_delivery=received` proves the original source session acknowledged it. The final reply has a separate 24-hour delivery lifetime. The database permits exactly one final result per request. Repeating the same final result returns its existing ID; conflicting results are rejected.

Only `request && expect_reply` triggers a business reply. Reply/progress/notice messages may be acknowledged but never automatically replied to. The coordinator derives conversation and hop count from parent messages; callers cannot reset hop count within a chain. At most four hops. Independent new requests are independent conversations. No arbitrary autonomous creation of new conversations is authorized by the M0 receiver policy.

## Expiry, claim and failures

TTL limits **starting execution**, not finishing already claimed work. Native queues cannot be assumed to support cancellation. Therefore the receiver MUST call `ack` before executing anything, and execute only when `should_execute=true`. This transaction checks target identity, binding generation, project, expiration and prior receipt. Repeated ack returns false. Late or revoked requests return false even if delivered by a native queue. Failure to contact the coordinator means do not execute.

A claimed request can complete after its expiry. A request that expires before claim cannot be revived by a late ack or reply. This is a cooperative receiver contract, not a claim that queued prompts can be erased or that arbitrary model behavior is prevented.

The coordinator commits submission intent before invoking the native transport. On restart, unfinished `submitting` intents become `delivery_uncertain` and are never automatically reinjected. Explicit native success becomes `submitted`; only receiver ack proves receipt. Fast ack/final reply cannot be overwritten by a late transport result. Missing ack is not proof of failure. A crash after ack but before reply remains visibly `received/pending`; M0 requires manual investigation, not automatic re-execution of side effects.

Send idempotency binds a caller-supplied ID to the full immutable payload. Same ID/different payload is rejected. Duplicate ack is harmless. M0 test requests are echo-only and have no external business effects.

## Limits and native adapters

- Maximum body: 32 KiB UTF-8; maximum 50 unconfirmed deliveries per receiver. Native adapter limits may be lower.
- Windows Codex preflight measures the final escaped command line in UTF-16 code units, including executable, UUID, envelope and NUL. Anything over 32,767 is rejected BEFORE submission. `--body-file` on AgentBridge does not imply that Codex accepts files. Arguments use arrays and no shell.
- Codex CLI path and CODEX_HOME are explicit. `queue --resume` exit 0 proves only queue submission, including when resume emits a warning. M0 stores this distinction. It does not read or modify Codex's queue database. No matching `task_complete` is used as an ack.
- Existing loaded desktop threads are the M0 target. Startup without opening a thread, desktop exit, sleep and macOS are separate unqualified capabilities.
- Claude process replacement invalidates the binding; rebind the same native session explicitly after validating the new runtime. No PID-only matching or newest-session guessing.

## M0 acceptance

Both original sessions originate an echo request using the CLI, the other original session claims and returns a final result, and the source original session acknowledges the final reply. Save message IDs, native IDs, delivery events, versions and matching transcript excerpts. A coordinator-generated synthetic ack or a standalone model process does not pass.

Also test state transitions, duplicate sends/acks/finals, conflicting final results, reply loop rejection, late receipt, revoked bindings, command length, recovery of a submission intent, and the fast-receiver race. Busy/same-name/live-restart scenarios must be explicitly marked passed or untested.
