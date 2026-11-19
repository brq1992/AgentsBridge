# Code and site configuration

This repository holds code, tests and generic documentation only. Everything that describes one
deployment lives in a private **site directory** outside the repository and is never committed:

```text
<site>/
  site.json            hub_url, hub_fingerprint, deny (words that must never be published)
  package/             files added to the internal deployment package as-is
    site.json          hub_url and hub_fingerprint only (read by join-windows.ps1)
    hub-public-cert.pem
    ...                internal notes, acceptance records, site-specific helper scripts
  docs/, history/      internal documents and retired one-off scripts (not packaged)
```

`site.example.json` shows the fields with documentation addresses.

## Building

```powershell
python tools/build_release.py --runtime <runtime> --out <empty dir>                 # public release
python tools/build_release.py --runtime <runtime> --out <empty dir> --site <site>   # internal package
```

Both layouts contain byte-identical code. `--site` may only add files; it refuses to replace a file
from git and refuses a site directory inside the repository. Added files appear in
`SOURCE_SHA256.json` like every other file, so `upgrade-recovery.ps1` verifies them too.

`join-windows.ps1` takes `-Hub` and `-Fingerprint` from `site.json` next to it when they are not
passed. `refresh-agent.ps1 -Alias <alias>` restarts the local Agent and refreshes one Claude endpoint.

## Before pushing

```powershell
git config core.hooksPath .githooks
$env:AGENTBRIDGE_SITE = '<site>\site.json'
python agentbridge/tools/check_public.py
```

The pre-push hook runs the same check. It rejects private IPv4 addresses, e-mail addresses other than
noreply/example, Windows account SIDs, real user-profile paths and every word in the site deny list.
It reports file and line only, never the matched value. Use `192.0.2.x`, `node-a`, `<UUID>` style
placeholders in code, tests and documentation.
