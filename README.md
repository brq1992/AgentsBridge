# AgentBridge

Lets already-open **original** Claude Code and Codex sessions on different Windows machines exchange
messages through a small TLS Hub, with durable delivery, explicit claims (ack) and exactly-one final reply.
Peer messages are data, never a permission grant: each session follows a locally installed policy
(by default echo-only).

Status: Windows M1, release R4. macOS is not implemented or verified.

## Layout

| Path | Contents |
|---|---|
| `agentbridge/agentbridge.py` | CLI entry point |
| `agentbridge/bridge/store.py` | SQLite ledger, binding generations, session capabilities, ack/reply |
| `agentbridge/bridge/hub.py` | TLS WebSocket Hub: node identity, directory, routing, retry arbitration |
| `agentbridge/bridge/network.py` | Agent: outbound connection, durable outbox, native delivery, endpoint recovery |
| `agentbridge/bridge/server.py` | Agent loopback API, scoped client authorization, lifecycle logs |
| `agentbridge/bridge/adapters.py` | Claude registry/pipe and Codex CLI queue adapters |
| `agentbridge/bridge/spawn.py` | Detached Agent start that leaves the caller's Windows job when allowed |
| `agentbridge/bridge/provision.py`, `cli.py` | Enrollment, private files, administration commands |
| `agentbridge/upgrade-recovery.ps1` | Verified upgrade with staging and automatic rollback |
| `independent-launcher/` | Double-click Start/Status entry point for a user-owned Agent |

## Documentation

- `agentbridge/USAGE.md` – how to run it on each machine (Chinese)
- `agentbridge/M1_WINDOWS.md`, `agentbridge/M1_PROTOCOL.md`, `agentbridge/PROTOCOL.md` – deployment and protocol
- `agentbridge/RECOVERY.md`, `agentbridge/RELEASE_NOTES_R4.md` – recovery behaviour and R4 changes

## Requirements and tests

Python 3.11+ (tested on 3.14). The network Agent and Hub need `requirements-network.txt`; the session CLI uses
the standard library only. Release packages bundle a private embedded runtime under `agentbridge/runtime/`,
which is not kept in this repository.

```bash
cd agentbridge
python -m unittest discover -s tests
```

The upgrade and rollback tests run the real PowerShell script and need `agentbridge/runtime/` with the network
dependencies installed. The launcher tests must run where Windows allows breaking away from the job object
(File Explorer or a Start-menu shell), not inside an AI tool's shell.

## Never commit

State directories, `network.json`, `client.json`, `ledger.sqlite` / `hub.sqlite`, Hub keys or invitation codes.
