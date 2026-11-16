param(
    [Parameter(Mandatory=$true)][string]$State,
    [string]$Python = (Get-Command python.exe -ErrorAction Stop).Source
)
$ErrorActionPreference = 'Stop'
$bridgeState = [IO.Path]::GetFullPath($State)
New-Item -ItemType Directory -Path $bridgeState -Force | Out-Null
$bridgeSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $bridgeState /inheritance:r /grant:r "*${bridgeSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot restrict state directory ACL' }
$bridgeLauncher = Join-Path $PSScriptRoot 'agentbridge.py'
$bridgeArgs = '"' + $bridgeLauncher + '" --state "' + $bridgeState + '" agent'
$bridgeProcess = Start-Process -FilePath $Python -ArgumentList $bridgeArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $bridgeState 'agent.stdout.log') -RedirectStandardError (Join-Path $bridgeState 'agent.stderr.log')
Write-Output "AgentBridge coordinator PID: $($bridgeProcess.Id)"
