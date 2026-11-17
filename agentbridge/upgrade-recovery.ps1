param(
    [Parameter(Mandatory=$true)][string]$InstallDirectory,
    [Parameter(Mandatory=$true)][string]$State,
    [string]$Python,
    [switch]$CheckOnly,
    [switch]$AllowAttached
)
# Agent upgrade: installs every release file except the embedded runtime, verified against
# this release's SOURCE_SHA256.json. Files are staged and checked before the Agent stops;
# any failure after the stop restores the previous files and restarts the previous Agent.
# The Hub process is not touched (see USAGE for the Hub machine).
$ErrorActionPreference = 'Stop'
function Get-BridgeSHA256([string]$Path) {
    $bridgeHasher = [Security.Cryptography.SHA256]::Create()
    $bridgeStream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($bridgeHasher.ComputeHash($bridgeStream))).Replace('-', '').ToLowerInvariant() }
    finally { $bridgeStream.Dispose(); $bridgeHasher.Dispose() }
}
function Wait-BridgeHealthy($Python, $Launcher, $StateDir) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $out = & $Python $Launcher --state $StateDir doctor 2>$null
        try { $result = $out | ConvertFrom-Json } catch { $result = $null }
        if ($result -and $result.ok -and $result.result.hub_connected) { return $result }
    } while ((Get-Date) -lt $deadline)
    return $null
}
function Get-BridgeDoctor($Python, $Launcher, $StateDir) {
    $out = & $Python $Launcher --state $StateDir doctor 2>$null
    try { $result = $out | ConvertFrom-Json } catch { return $null }
    if ($result -and $result.ok) { return $result.result }
    return $null
}
function Stop-BridgeAgent($Python, $Launcher, $StateDir, [int]$AgentPid) {
    # Request a normal stop and wait for the process to be gone; never act on a live Agent.
    & $Python $Launcher --state $StateDir stop 2>$null | Out-Null
    $deadline = (Get-Date).AddSeconds(60)
    while ($AgentPid -and (Get-Process -Id $AgentPid -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -ge $deadline) { return $false }
        Start-Sleep -Milliseconds 250
    }
    return $true
}
function Wait-BridgeLockFree($Python, $StateDir) {
    # Self-contained probe of the Agent's exclusive state lock (imports no bridge code).
    $probe = @'
import os, sys
path = os.path.join(sys.argv[1], 'agent.lock')
if not os.path.exists(path):
    print('free'); sys.exit()
try:
    stream = open(path, 'a+b')
except OSError:
    print('held'); sys.exit()
try:
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(stream, fcntl.LOCK_UN)
    print('free')
except OSError:
    print('held')
'@
    $deadline = (Get-Date).AddSeconds(60)
    do {
        if ((& $Python -c $probe $StateDir | Select-Object -Last 1) -eq 'free') { return $true }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}
function Start-BridgeAgent($Target, $StateDir, $Python, [switch]$Attached) {
    $script = Join-Path $Target 'start-m1.ps1'
    $supports = (Get-Content -Raw -LiteralPath $script) -match 'AllowAttached'
    if ($Attached -and $supports) { & $script -Mode Agent -State $StateDir -Python $Python -AllowAttached }
    else { & $script -Mode Agent -State $StateDir -Python $Python }
}
$bridgeTarget = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
$bridgeState = [IO.Path]::GetFullPath($State)
if (-not $Python) { $Python = Join-Path $bridgeTarget 'runtime\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Specify the existing Python executable with -Python.' }
& $Python -c 'import websockets.sync.client, cryptography'
if ($LASTEXITCODE -ne 0) { throw 'Python lacks the existing network dependencies. Use the enrolled Agent runtime; nothing has been stopped.' }
$bridgeLauncher = Join-Path $bridgeTarget 'agentbridge.py'
if (-not (Test-Path -LiteralPath $bridgeLauncher -PathType Leaf)) { throw 'An existing AgentBridge installation is required.' }
if (-not (Test-Path -LiteralPath (Join-Path $bridgeState 'network.json'))) { throw 'Existing enrolled M1 state is required.' }
if ($bridgeTarget -eq [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')) { throw 'Extract the upgrade beside the existing installation, not over it.' }

$bridgeManifest = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'SOURCE_SHA256.json') | ConvertFrom-Json
$bridgeFiles = @()
foreach ($bridgeEntry in $bridgeManifest.PSObject.Properties) {
    if (-not $bridgeEntry.Name.StartsWith('agentbridge/')) { continue }
    $bridgeRelative = $bridgeEntry.Name.Substring(12)
    if ($bridgeRelative.StartsWith('runtime/') -or $bridgeRelative -eq 'SOURCE_SHA256.json') { continue }
    $bridgeActual = Get-BridgeSHA256 (Join-Path $PSScriptRoot $bridgeRelative)
    if ($bridgeActual -ne $bridgeEntry.Value) { throw "Source checksum mismatch: $bridgeRelative" }
    $bridgeFiles += [pscustomobject]@{ Path = $bridgeRelative; Hash = $bridgeEntry.Value }
}
if ($bridgeFiles.Count -lt 10) { throw 'Release manifest lists too few files; refusing to upgrade.' }
$bridgeStoreCheck = @'
import ast, pathlib, sys
required = {'connect', 'sessions', '_session', 'refresh_endpoint', 'client_directory'}
def methods(root):
    tree = ast.parse((pathlib.Path(root) / 'bridge' / 'store.py').read_text(encoding='utf-8-sig'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Store')
    return {node.name for node in cls.body if isinstance(node, ast.FunctionDef)}
missing = required - methods(sys.argv[1])
if missing:
    raise SystemExit('Upgrade package Store API incomplete: ' + ', '.join(sorted(missing)))
old_missing = required - methods(sys.argv[2])
print('Installed Store methods to repair: ' + (', '.join(sorted(old_missing)) or 'none'))
'@
& $Python -c $bridgeStoreCheck $PSScriptRoot $bridgeTarget
if ($LASTEXITCODE -ne 0) { throw 'Store compatibility precheck failed; no installed code or processes changed.' }
foreach ($bridgeFile in $bridgeFiles) {
    $existing = Join-Path $bridgeTarget $bridgeFile.Path
    if (Test-Path -LiteralPath $existing -PathType Container) {
        throw "Install path $existing is a folder where the release has a file. Nothing was changed."
    }
}
$bridgeChanged = @($bridgeFiles | Where-Object {
    $existing = Join-Path $bridgeTarget $_.Path
    -not (Test-Path -LiteralPath $existing -PathType Leaf) -or (Get-BridgeSHA256 $existing) -ne $_.Hash })
$bridgeHealth = & $Python $bridgeLauncher --state $bridgeState doctor | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $bridgeHealth.ok -or $bridgeHealth.result.mode -ne 'windows-m1-network') { throw 'Existing M1 Agent is not reachable.' }
Write-Output "Release files: $($bridgeFiles.Count); to change: $($bridgeChanged.Count)"
# Self-contained probe (imports nothing from either release: the installed runtime would
# resolve 'bridge' to the OLD package). Starts only a no-op child with the breakaway flag.
$bridgeProbeCode = @'
import ctypes, os, subprocess, sys
if os.name != 'nt':
    print('true'); sys.exit()
k = ctypes.WinDLL('kernel32')
k.GetCurrentProcess.restype = ctypes.c_void_p
k.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
member = ctypes.c_int(0)
if not k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(member)) or not member.value:
    print('true'); sys.exit()
try:
    subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x01000000).wait(timeout=20)
    print('true')
except OSError as e:
    print('false' if getattr(e, 'winerror', None) == 5 else 'error')
'@
$bridgeProbe = (& $Python -c $bridgeProbeCode | Select-Object -Last 1)
if ($bridgeProbe -notin @('true', 'false')) { throw 'Could not check whether an independent Agent start is possible here. Nothing was changed.' }
if ($bridgeProbe -eq 'false') {
    if (-not $AllowAttached) { throw 'Windows refuses an independent Agent start from this shell (its job forbids breakaway). Run the upgrade from a Start-menu PowerShell, or pass -AllowAttached to accept a dependent Agent. Nothing was changed.' }
    Write-Warning 'Independent start is not possible here; -AllowAttached given, so the Agent depends on the calling program.'
}
if ($CheckOnly) { Write-Output 'Checks passed; no files or processes changed.'; return }

$bridgeStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$bridgeStaging = Join-Path $bridgeTarget ('.upgrade-staging-' + $bridgeStamp)
$bridgeBackup = Join-Path $bridgeTarget ('upgrade-backup-' + $bridgeStamp)
# 1. Stage and verify while the Agent still runs.
foreach ($bridgeFile in $bridgeChanged) {
    $staged = Join-Path $bridgeStaging $bridgeFile.Path
    New-Item -ItemType Directory -Force -Path (Split-Path $staged) | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $bridgeFile.Path) -Destination $staged
    if ((Get-BridgeSHA256 $staged) -ne $bridgeFile.Hash) { throw "Staged copy mismatch: $($bridgeFile.Path); nothing installed." }
}
# 2. Back up what will be replaced; remember files that are new.
$bridgeAdded = @()
$bridgeNewPid = 0
foreach ($bridgeFile in $bridgeChanged) {
    $existing = Join-Path $bridgeTarget $bridgeFile.Path
    if (Test-Path -LiteralPath $existing) {
        $saved = Join-Path $bridgeBackup $bridgeFile.Path
        New-Item -ItemType Directory -Force -Path (Split-Path $saved) | Out-Null
        Copy-Item -LiteralPath $existing -Destination $saved
    } else { $bridgeAdded += $bridgeFile.Path }
}
# 3. Stop the Agent and wait until its process is gone.
if (-not (Stop-BridgeAgent $Python $bridgeLauncher $bridgeState $bridgeHealth.result.pid)) {
    throw "Agent PID $($bridgeHealth.result.pid) did not exit; no installed code was changed. Staged files: $bridgeStaging"
}
# 4. Install, start, verify; roll back on any failure.
try {
    foreach ($bridgeFile in $bridgeChanged) {
        $destination = Join-Path $bridgeTarget $bridgeFile.Path
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        Copy-Item -LiteralPath (Join-Path $bridgeStaging $bridgeFile.Path) -Destination $destination -Force
        if ((Get-BridgeSHA256 $destination) -ne $bridgeFile.Hash) { throw "Installed copy mismatch: $($bridgeFile.Path)" }
    }
    # Record the started process from the start script's own output, independent of HTTP health.
    $bridgeStartText = (Start-BridgeAgent $bridgeTarget $bridgeState $Python -Attached:$AllowAttached | Out-String)
    Write-Output $bridgeStartText.Trim()
    if ($bridgeStartText -match 'Agent PID: (\d+)') { $bridgeNewPid = [int]$Matches[1] }
    $bridgeResult = Wait-BridgeHealthy $Python $bridgeLauncher $bridgeState
    if (-not $bridgeResult -or -not $bridgeResult.result.recovery_interval_seconds) { throw 'Upgraded Agent did not become healthy.' }
} catch {
    $bridgeFailure = $_.Exception.Message
    # Never restore files under a live Agent. Candidates: whatever doctor reports, plus the
    # PID the start script printed; then the state lock itself must be free, which also covers
    # an Agent that is alive but whose health endpoint does not answer.
    $bridgeRunning = Get-BridgeDoctor $Python $bridgeLauncher $bridgeState
    $bridgeCandidates = @(@($(if ($bridgeRunning) { [int]$bridgeRunning.pid }), $bridgeNewPid) | Where-Object { $_ } | Select-Object -Unique)
    foreach ($bridgeCandidate in $bridgeCandidates) {
        if (-not (Stop-BridgeAgent $Python $bridgeLauncher $bridgeState $bridgeCandidate)) {
            throw "Upgrade failed ($bridgeFailure) and ROLLBACK IS INCOMPLETE: Agent PID $bridgeCandidate did not exit, so files were NOT restored. Previous files: $bridgeBackup"
        }
    }
    if (-not (Wait-BridgeLockFree $Python $bridgeState)) {
        throw "Upgrade failed ($bridgeFailure) and ROLLBACK IS INCOMPLETE: a process still holds the Agent state lock (agent.lock) and could not be identified as stopped, so files were NOT restored. Previous files: $bridgeBackup"
    }
    $bridgeFailedPid = if ($bridgeCandidates.Count) { [int]$bridgeCandidates[0] } else { 0 }
    $bridgeRestoreAt = Get-Date
    $bridgeBad = @()
    foreach ($bridgeFile in $bridgeChanged) {
        $destination = Join-Path $bridgeTarget $bridgeFile.Path
        $saved = Join-Path $bridgeBackup $bridgeFile.Path
        try {
            if (Test-Path -LiteralPath $saved) {
                Copy-Item -LiteralPath $saved -Destination $destination -Force
                if ((Get-BridgeSHA256 $destination) -ne (Get-BridgeSHA256 $saved)) { $bridgeBad += $bridgeFile.Path }
            } elseif ($bridgeAdded -contains $bridgeFile.Path -and (Test-Path -LiteralPath $destination -PathType Leaf)) {
                Remove-Item -LiteralPath $destination -Force
            }
        } catch { $bridgeBad += $bridgeFile.Path }
    }
    if ($bridgeBad.Count) {
        throw "Upgrade failed ($bridgeFailure) and ROLLBACK IS INCOMPLETE: could not restore $($bridgeBad -join ', '). Agent left stopped. Previous files: $bridgeBackup"
    }
    try { Start-BridgeAgent $bridgeTarget $bridgeState $Python -Attached:$AllowAttached } catch { }
    $bridgeDeadline = (Get-Date).AddSeconds(30)
    $bridgeOld = $null
    do {
        Start-Sleep -Milliseconds 500
        $bridgeOld = Get-BridgeDoctor $Python $bridgeLauncher $bridgeState
    } while (-not $bridgeOld -and (Get-Date) -lt $bridgeDeadline)
    $bridgeOldProcess = if ($bridgeOld) { Get-Process -Id $bridgeOld.pid -ErrorAction SilentlyContinue } else { $null }
    if (-not $bridgeOldProcess -or $bridgeOld.pid -eq $bridgeFailedPid -or $bridgeOldProcess.StartTime -lt $bridgeRestoreAt) {
        throw "Upgrade failed ($bridgeFailure). Previous files restored (hash-verified) from $bridgeBackup, but the previous Agent did NOT start; start it manually."
    }
    throw "Upgrade rolled back ($bridgeFailure). Previous files restored (hash-verified) from $bridgeBackup; previous Agent running as PID $($bridgeOld.pid) (hub_connected=$($bridgeOld.hub_connected))."
}
Remove-Item -LiteralPath $bridgeStaging -Recurse -Force -ErrorAction SilentlyContinue
$bridgeInstalled = [ordered]@{}
foreach ($bridgeFile in $bridgeFiles) { $bridgeInstalled[$bridgeFile.Path] = $bridgeFile.Hash }
$bridgeInstalled | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $bridgeTarget 'INSTALLED_SHA256.json')
Write-Output "Upgrade installed ($($bridgeChanged.Count) files). Previous files: $bridgeBackup"
$bridgeResult | ConvertTo-Json -Depth 6
