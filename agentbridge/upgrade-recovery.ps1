param(
    [Parameter(Mandatory=$true)][string]$InstallDirectory,
    [Parameter(Mandatory=$true)][string]$State,
    [string]$Python,
    [switch]$CheckOnly
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
$bridgeChanged = @($bridgeFiles | Where-Object {
    $existing = Join-Path $bridgeTarget $_.Path
    -not (Test-Path -LiteralPath $existing) -or (Get-BridgeSHA256 $existing) -ne $_.Hash })
$bridgeHealth = & $Python $bridgeLauncher --state $bridgeState doctor | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $bridgeHealth.ok -or $bridgeHealth.result.mode -ne 'windows-m1-network') { throw 'Existing M1 Agent is not reachable.' }
Write-Output "Release files: $($bridgeFiles.Count); to change: $($bridgeChanged.Count)"
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
foreach ($bridgeFile in $bridgeChanged) {
    $existing = Join-Path $bridgeTarget $bridgeFile.Path
    if (Test-Path -LiteralPath $existing) {
        $saved = Join-Path $bridgeBackup $bridgeFile.Path
        New-Item -ItemType Directory -Force -Path (Split-Path $saved) | Out-Null
        Copy-Item -LiteralPath $existing -Destination $saved
    } else { $bridgeAdded += $bridgeFile.Path }
}
# 3. Stop the Agent.
& $Python $bridgeLauncher --state $bridgeState stop | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Agent stop failed; no installed code was changed. Staged files: $bridgeStaging" }
$bridgeDeadline = (Get-Date).AddSeconds(40)
while (Get-Process -Id $bridgeHealth.result.pid -ErrorAction SilentlyContinue) {
    if ((Get-Date) -ge $bridgeDeadline) { throw 'Agent did not exit; refusing to replace code.' }
    Start-Sleep -Milliseconds 250
}
# 4. Install, start, verify; roll back on any failure.
try {
    foreach ($bridgeFile in $bridgeChanged) {
        $destination = Join-Path $bridgeTarget $bridgeFile.Path
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        Copy-Item -LiteralPath (Join-Path $bridgeStaging $bridgeFile.Path) -Destination $destination -Force
        if ((Get-BridgeSHA256 $destination) -ne $bridgeFile.Hash) { throw "Installed copy mismatch: $($bridgeFile.Path)" }
    }
    & (Join-Path $bridgeTarget 'start-m1.ps1') -Mode Agent -State $bridgeState -Python $Python
    $bridgeResult = Wait-BridgeHealthy $Python $bridgeLauncher $bridgeState
    if (-not $bridgeResult -or -not $bridgeResult.result.recovery_interval_seconds) { throw 'Upgraded Agent did not become healthy.' }
} catch {
    $bridgeFailure = $_.Exception.Message
    & $Python $bridgeLauncher --state $bridgeState stop 2>$null | Out-Null
    Start-Sleep -Seconds 2
    foreach ($bridgeFile in $bridgeChanged) {
        $destination = Join-Path $bridgeTarget $bridgeFile.Path
        $saved = Join-Path $bridgeBackup $bridgeFile.Path
        if (Test-Path -LiteralPath $saved) { Copy-Item -LiteralPath $saved -Destination $destination -Force }
        elseif ($bridgeAdded -contains $bridgeFile.Path) { Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue }
    }
    & (Join-Path $bridgeTarget 'start-m1.ps1') -Mode Agent -State $bridgeState -Python $Python
    throw "Upgrade rolled back ($bridgeFailure). Previous files restored from $bridgeBackup and the previous Agent restarted."
}
Remove-Item -LiteralPath $bridgeStaging -Recurse -Force -ErrorAction SilentlyContinue
$bridgeInstalled = [ordered]@{}
foreach ($bridgeFile in $bridgeFiles) { $bridgeInstalled[$bridgeFile.Path] = $bridgeFile.Hash }
$bridgeInstalled | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $bridgeTarget 'INSTALLED_SHA256.json')
Write-Output "Upgrade installed ($($bridgeChanged.Count) files). Previous files: $bridgeBackup"
$bridgeResult | ConvertTo-Json -Depth 6
