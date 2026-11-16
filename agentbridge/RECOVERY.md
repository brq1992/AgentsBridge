# AgentBridge M1 recovery update R3 — 2026-11-16

## Scope

Keep Hub and Agents running. After manually reopening the **same original** Claude
conversation, its Agent discovers and validates the new endpoint within approximately
5 seconds. UUID, alias, project, generation and client capability remain unchanged.
New conversations require explicit binding; this update does not retarget them.

Claude discovery uses `hostSessionId` only when it parses as a UUID; otherwise it
falls back to a valid `sessionId`. In particular, desktop IDs such as `local_<uuid>`
are not binding UUIDs. Both fields are normalized before matching. Registries with
no valid UUID are rejected. Discovery, refresh and preflight use the same rule.
Versions 2.1.321 and 2.1.325 are accepted. Version 2.1.325 was exercised in the
2026-11-16 original-session round trip; other versions remain unqualified.
Discovery requires a single exact live UUID, matching process start, allowed pipe
namespace, and one matching peer key. No credentials are logged or packaged.

Codex keeps its durable `queue --thread UUID --resume` route. Opening Codex manually
lets the native queue continue; AgentBridge does not launch the app or inject a
second copy of an already submitted message. Actual app reopen behavior still
requires the two-machine acceptance below. `doctor` readiness means endpoint/CLI
validation, not confirmation that an application has consumed a message.

Only unexpired preflight-blocked jobs with **no submission attempt anywhere in their
recorded history** are automatically retried. Hub verifies the sequence and binding
again. Submitted, acknowledged, expired, revoked and uncertain jobs are not replayed.
A crash during submission remains uncertain and requires investigation.
An app closing between preflight and pipe write can also remain uncertain.

## Upgrade an existing Agent

Verify the archive SHA-256 against its supplied `.sha256`, extract into a separate
directory, then run its `upgrade-recovery.ps1` in PowerShell as the Agent's current
OS user. Use the existing installation and **private Agent state**, not a session
client directory. This script calls the existing administrative CLI without printing
its credentials. It preserves enrollment, bindings, policies, ACLs and runtime.

```powershell
.\upgrade-recovery.ps1 -InstallDirectory 'C:\path\to\existing\agentbridge' -State 'C:\path\to\existing\state' -CheckOnly
.\upgrade-recovery.ps1 -InstallDirectory 'C:\path\to\existing\agentbridge' -State 'C:\path\to\existing\state'
```

If the installation uses system Python, add `-Python 'C:\path\to\python.exe'`.
The script validates source hashes, backs up the changed files, stops the Agent,
copies the update and starts it again. Hub needs no update. An overlay checksum file
records the installed changes separately from the original release manifest.
On startup failure it stops with the backup path; it does not spawn duplicate Agents.
Rollback: stop the updated Agent, restore the backed-up files, then start the Agent
using the existing `start-m1.ps1`. Newly added documentation/tests can remain.

This update was built from the local v3 archive SHA-256
`4821d3dcf463bf84b5937380ec82f4acc12d4c8cbd4c511d99e3bd8ede0ac3c6`.
It is **not** the separately mentioned archive with SHA-256 `489617c4…`.
It has its own manifest and archive hash and must be treated as a new release.

## Acceptance after installation on both machines

1. Confirm `doctor`: Hub connected and `recovery_interval_seconds: 5`.
2. Close only the bound Claude app/session on the receiving machine; keep its Agent running.
3. Send a new authorized echo from the other machine with a fresh ID and sufficient TTL.
   Observe preflight blocking; do not manually ack or forge a reply.
4. Manually reopen the same Claude conversation (same UUID). Within roughly
   5 seconds the Agent should refresh the endpoint and retry without `refresh-claude`.
   The conversation itself must ack and echo; source must ack the final reply.
5. Repeat in the other direction with Codex closed and manually reopened. Native
   queue submission alone does not count as success; require the original conversation
   to ack, echo and receive the final ack. No second injected copy is allowed.
6. Verify request `completed`, reply `received`, matching text and identities.

Automated tests exercise real loopback TLS and isolated ledgers, with native delivery
and process/registry transitions simulated. They do not replace this manual native
application test or prove unattended computer startup.

## R3 installation correction

R3 includes bridge/store.py in the verified and backed-up overlay because network.py
requires Store.refresh_endpoint. CheckOnly inspects both Store APIs and reports
missing installed methods that the update will replace. An incomplete package is
rejected before stopping the existing Agent. The bundled Store also explicitly deletes
revoked client capabilities on unbind; a rebind does not restore them.

R2's overlay omission could pass CheckOnly yet leave Claude unavailable with
AttributeError after reopen. R3 fixes that package dependency; it does not claim
an identified cause for the separately reported unexplained process exit.

The Agent now logs startup, a 30-second main-loop heartbeat, explicit stop requests,
worker/main failures (exception type only), and clean shutdown to stderr. Forced
termination or power loss cannot log a final event; use the last heartbeat and OS
records. This is diagnostics, not an automatic process supervisor.

Validation includes running the actual PowerShell updater against an isolated
legacy-layout installation lacking refresh_endpoint, followed by a refresh using
the installed modules, a real Windows process identity, and synthetic registry/key.
The native pipe is not written. Production reinstall/restart tests remain paused.
