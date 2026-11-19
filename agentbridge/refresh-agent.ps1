param([string]$State = (Join-Path $env:LOCALAPPDATA 'AgentBridge-M1\state'), [string]$Alias = 'claude')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$bridgePython = Join-Path $PSScriptRoot 'runtime\python.exe'
$bridgeLauncher = Join-Path $PSScriptRoot 'agentbridge.py'
$bridgeState = [IO.Path]::GetFullPath($State)
if (-not (Test-Path -LiteralPath (Join-Path $bridgeState 'network.json'))) { throw 'Existing enrolled Agent state is required' }
$bridgePrevious = & $bridgePython $bridgeLauncher --state $bridgeState doctor | ConvertFrom-Json
if (-not $bridgePrevious.ok) { throw 'Existing Agent is not reachable; stop and report its status' }
& $bridgePython $bridgeLauncher --state $bridgeState stop
if ($LASTEXITCODE -ne 0) { throw 'Could not stop the existing Agent cleanly' }
$bridgeDeadline = (Get-Date).AddSeconds(40)
while (Get-Process -Id $bridgePrevious.result.pid -ErrorAction SilentlyContinue) {
    if ((Get-Date) -ge $bridgeDeadline) { throw 'Agent did not stop; refusing to start a duplicate' }
    Start-Sleep -Milliseconds 250
}
& (Join-Path $PSScriptRoot 'start-m1.ps1') -Mode Agent -State $bridgeState -Python $bridgePython
$bridgeDeadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $bridgeHealth = & $bridgePython $bridgeLauncher --state $bridgeState doctor | ConvertFrom-Json
    if ($bridgeHealth.ok -and $bridgeHealth.result.hub_connected) { break }
} while ((Get-Date) -lt $bridgeDeadline)
if (-not $bridgeHealth.result.hub_connected) { throw 'Agent is not connected to Hub' }
& $bridgePython $bridgeLauncher --state $bridgeState refresh-claude --alias $Alias
if ($LASTEXITCODE -ne 0) { throw 'Exact original Claude session could not be refreshed; keep the original Claude conversation open and report the error' }
Write-Host 'Verified the same original Claude UUID and refreshed its live endpoint.'
Write-Host 'Only messages blocked before any native submission are queued for recovery; received work is not repeated.'
& $bridgePython $bridgeLauncher --state $bridgeState status
