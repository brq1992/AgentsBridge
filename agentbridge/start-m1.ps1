param(
    [Parameter(Mandatory=$true)][ValidateSet('Hub','Agent')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$State,
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$HubConfig
)
$ErrorActionPreference = 'Stop'
$bridgeState = [IO.Path]::GetFullPath($State)
$bridgeLauncher = Join-Path $PSScriptRoot 'agentbridge.py'
if ($Mode -eq 'Hub') {
    if (-not (Test-Path -LiteralPath (Join-Path $bridgeState 'hub.json'))) { throw 'Initialize Hub first' }
    $bridgeArgs = '"' + $bridgeLauncher + '" --state "' + $bridgeState + '" hub'
    $bridgeProcess = Start-Process -FilePath $Python -ArgumentList $bridgeArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $bridgeState 'Hub.stdout.log') -RedirectStandardError (Join-Path $bridgeState 'Hub.stderr.log')
    Start-Sleep -Milliseconds 600
    $bridgeProcess.Refresh()
    if ($bridgeProcess.HasExited) { throw 'AgentBridge Hub exited; inspect the private stderr log' }
    Write-Output "AgentBridge Hub PID: $($bridgeProcess.Id)"
    return
}
if (-not $HubConfig) { $HubConfig = Join-Path $bridgeState 'network.json' }
if (-not (Test-Path -LiteralPath $HubConfig)) { throw 'Enroll the Agent first' }
# agent-start: detached, breaks away from the caller's Windows job when allowed, never
# starts a duplicate, and reports windows_job_member / a warning instead of claiming independence.
$bridgeOutput = & $Python $bridgeLauncher --state $bridgeState agent-start --hub-config ([IO.Path]::GetFullPath($HubConfig))
$bridgeResult = $bridgeOutput | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $bridgeResult.ok) { throw "AgentBridge Agent did not start: $($bridgeResult.error)" }
Write-Output "AgentBridge Agent PID: $($bridgeResult.result.pid) ($($bridgeResult.result.action))"
if ($bridgeResult.result.warning) { Write-Warning $bridgeResult.result.warning }
