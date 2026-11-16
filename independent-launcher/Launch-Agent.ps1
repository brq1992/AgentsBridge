param(
    [string]$Profile = 'profile',
    [switch]$StatusOnly
)
$ErrorActionPreference = 'Stop'
if ($Profile -notmatch '^[A-Za-z0-9_-]{1,64}$') { throw 'Profile name may contain only letters, digits, underscore and hyphen.' }
$bridgeConfig = Join-Path $PSScriptRoot ($Profile + '.json')
if (-not (Test-Path -LiteralPath $bridgeConfig)) { throw "Copy profiles.example.json to $bridgeConfig and fill in this machine's paths." }
$bridgeSettings = Get-Content -Raw -LiteralPath $bridgeConfig | ConvertFrom-Json
$bridgePython = [Environment]::ExpandEnvironmentVariables($bridgeSettings.python)
if (-not (Test-Path -LiteralPath $bridgePython -PathType Leaf)) { throw "Edit $bridgeConfig to select your installed Python runtime." }
$bridgeArguments = @((Join-Path $PSScriptRoot 'launcher.py'), '--config', $bridgeConfig)
if ($StatusOnly) { $bridgeArguments += '--status' }
& $bridgePython @bridgeArguments
if ($LASTEXITCODE -ne 0) { throw 'Agent launcher failed; see the diagnostic above.' }
