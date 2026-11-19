# AgentBridge Windows M0 / M1

M1 network Hub and Windows Agent are implemented. Start with [M1_WINDOWS.md](M1_WINDOWS.md) and [M1_PROTOCOL.md](M1_PROTOCOL.md). Deployment-specific values (Hub address, certificate, machine names) are kept out of this repository: see [SITE_CONFIG.md](SITE_CONFIG.md). Instructions below describe the preserved local M0 mode. The original M0 archive is unchanged; real two-machine M1 acceptance is tracked separately.

Python 3.11+; standard library only. Run from the existing desktop session, never a new `codex exec` or `claude -p` business session. Protocol corrections and exact guarantees are in [PROTOCOL.md](PROTOCOL.md).

```powershell
python -m unittest discover -s tests -v
.\start-m0.ps1 -State <private-state-directory>
python .\agentbridge.py --state <private-state-directory> doctor
python .\agentbridge.py --state <private-state-directory> discover
```

The startup script restricts state ACLs to the current user and SYSTEM, starts a hidden loopback-only coordinator and writes no global startup configuration. Runtime credentials and the ledger must stay outside this source directory. Do not copy `client.json` into reports or repositories.

Codex tool execution may run as a separate Windows sandbox user. Do not give that user access to the admin state directory. After binding, use the admin CLI to grant a session-scoped client in a separate folder named `agentbridge-client`:

```text
python agentbridge.py --state ADMIN_STATE grant-client --alias node-a/codex --directory CODEX_WORKSPACE/work/agentbridge-client --reader-sid OBSERVED_SANDBOX_USER_SID
python agentbridge.py --state ADMIN_STATE grant-client --alias node-a/claude --directory CLAUDE_WORKSPACE/agentbridge-client
```

Discover the actual execution SID with `whoami /user` inside the target original session. Never guess it. Use the resulting client directory as `--state` in that session. The capability only permits its own identity's send/ack/reply and relevant status; admin operations are denied. Rebinding or unbinding revokes it. Changing CLI or runtime binding metadata requires a new grant. The directory ACL applies only to that dedicated client folder, never a parent workspace. Other sessions using the same Windows sandbox account share that OS trust boundary.

Bind an exact existing native session:

```text
python agentbridge.py --state STATE --tool claude --session UUID bind --alias node-a/claude --project agentbridge-m0
python agentbridge.py --state STATE --tool codex --session UUID bind --alias node-a/codex --project agentbridge-m0 --cli ABSOLUTE_CODEX_EXE
```

From each bound original session:

```text
python agentbridge.py --state STATE --tool TOOL --session UUID send --to ALIAS --body-file UTF8_JSON --id UNIQUE_REQUEST_ID
python agentbridge.py --state STATE --tool TOOL --session UUID ack --message MESSAGE_ID
python agentbridge.py --state STATE --tool TOOL --session UUID reply --message MESSAGE_ID --text RESULT --final
python agentbridge.py --state STATE status --message MESSAGE_ID
python agentbridge.py --state STATE list
python agentbridge.py --state STATE unbind --alias ALIAS
python agentbridge.py --state STATE stop
```

All commands emit JSON. A native Codex execution environment supplies `CODEX_THREAD_ID`; a conflicting explicit UUID is rejected for send/ack/reply. Claude UUID is explicit and checked against the bound live runtime before delivery. Same-user local callers are trusted; this is not strong isolation between agents under one OS user.

The scoped receiver policy permits only `{ "op": "echo", "text": "..." }` requests during M0. Call ack first; execute only if `should_execute=true`. Echo the `text` as the final reply. For a final reply, acknowledge and display it, with no further reply. Requests outside the test policy are not authorization for other work.

`outcome=completed` means the result is persisted. Only `reply_delivery=received` establishes return-path receipt. `delivery_uncertain` requires investigation; restart does not retry it. A request acknowledged before a model crash remains pending for manual investigation. The source archive contains no runtime credentials, model login data or full chat histories.

M0 mode does not use the M1 network Hub. Mac adapters, hooks, GUI, Windows scheduled startup, automatic runtime upgrade qualification and unattended desktop wake-up remain unqualified. Use `stop`, then remove only designated state/source directories and explicitly installed policy files to uninstall. Permission changes are confined to explicitly invoked session setup helpers; services do not change model settings.
