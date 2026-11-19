param(
    [Parameter(Mandatory=$true)][string]$Code,
    [string]$Hub,
    [string]$ClaudePeer,
    [string]$CodexPeer,
    [string]$Fingerprint,
    [string]$Install = (Join-Path $env:LOCALAPPDATA 'AgentBridge-M1'),
    [string]$Python,
    [switch]$CheckRuntime
)
$ErrorActionPreference = 'Stop'
# Site values (Hub URL, certificate fingerprint) come from site.json next to this script when the
# internal package carries one; explicit parameters win. The public repository ships only site.example.json.
$bridgeSiteFile = Join-Path $PSScriptRoot 'site.json'
if (Test-Path -LiteralPath $bridgeSiteFile) {
    $bridgeSite = Get-Content -LiteralPath $bridgeSiteFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $Hub) { $Hub = $bridgeSite.hub_url }
    if (-not $Fingerprint) { $Fingerprint = $bridgeSite.hub_fingerprint }
}
if (-not $Hub -or -not $Fingerprint) { throw 'Pass -Hub and -Fingerprint, or use an internal package that contains site.json' }
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$bridgeInstall = [IO.Path]::GetFullPath($Install)
$bridgeState = Join-Path $bridgeInstall 'state'
$bridgeRuntime = Join-Path $bridgeInstall 'runtime'
$bridgeBundled = Join-Path $PSScriptRoot 'runtime\python.exe'
if (-not $Python -and (Test-Path -LiteralPath $bridgeBundled)) {
    $bridgePython = $bridgeBundled
    $Python = $bridgePython
    Write-Host 'Using the bundled private Python runtime; no system Python or pip installation is needed.'
} else {
    if (-not $Python) {
        $bridgeCandidates = @(Get-Command python.exe,python3.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
        $bridgeCandidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe') -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        foreach ($bridgeCandidate in ($bridgeCandidates | Select-Object -Unique)) {
            if ($bridgeCandidate -like '*\Microsoft\WindowsApps\*') { continue }
            try {
                $bridgeVersion = & $bridgeCandidate -c 'import sys; print(int(sys.version_info >= (3,11)))' 2>$null
                if ($LASTEXITCODE -eq 0 -and $bridgeVersion -eq '1') { $Python = $bridgeCandidate; break }
            } catch { continue }
        }
    }
    if (-not $Python) { throw 'No Python 3.11+ found. Use the self-contained Windows x64 package or pass -Python with an installed interpreter path.' }
    $bridgeVersion = & $Python -c 'import sys; print(int(sys.version_info >= (3,11)))'
    if ($LASTEXITCODE -ne 0 -or $bridgeVersion -ne '1') { throw 'The selected interpreter is not Python 3.11+. Use the self-contained package.' }
    New-Item -ItemType Directory -Path $bridgeInstall -Force | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $bridgeRuntime 'Scripts\python.exe'))) {
        & $Python -m venv $bridgeRuntime
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python runtime' }
    }
    $bridgePython = Join-Path $bridgeRuntime 'Scripts\python.exe'
    & $bridgePython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-network.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
}
Write-Host 'Checking the private Python runtime:'
& $bridgePython -c 'import sys,ssl,sqlite3,ctypes,websockets,cryptography; print(sys.version.split()[0])'
if ($LASTEXITCODE -ne 0) { throw 'Private Python runtime failed its startup check' }
if ($CheckRuntime) { return }
$bridgeLauncher = Join-Path $PSScriptRoot 'agentbridge.py'
if (-not (Test-Path -LiteralPath (Join-Path $bridgeState 'network.json'))) {
    & $bridgePython $bridgeLauncher --state $bridgeState node-enroll --url $Hub --certificate (Join-Path $PSScriptRoot 'hub-public-cert.pem') --fingerprint $Fingerprint --code $Code
    if ($LASTEXITCODE -ne 0) { throw 'Enrollment failed; check Hub reachability, certificate fingerprint and invitation expiry' }
}
$bridgeAlreadyRunning = $false
if (Test-Path -LiteralPath (Join-Path $bridgeState 'client.json')) {
    $bridgeHealth = & $Python $bridgeLauncher --state $bridgeState doctor 2>$null
    if ($LASTEXITCODE -eq 0) { $bridgeAlreadyRunning = $true }
}
if (-not $bridgeAlreadyRunning) { & (Join-Path $PSScriptRoot 'start-m1.ps1') -Mode Agent -State $bridgeState -Python $bridgePython }
$bridgeDeadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $bridgeStatus = & $Python $bridgeLauncher --state $bridgeState doctor | ConvertFrom-Json
    if ($bridgeStatus.ok -and $bridgeStatus.result.hub_connected) { break }
} while ((Get-Date) -lt $bridgeDeadline)
if (-not $bridgeStatus.result.hub_connected) { throw 'Agent started but Hub is not connected; inspect private Agent.stderr.log and status' }
Write-Host 'Agent connected. Select existing sessions; blank input skips a tool.'
$bridgeDiscovered = & $Python $bridgeLauncher --state $bridgeState discover | ConvertFrom-Json
$bridgeDiscovered.result | Select-Object sessionId,name,cwd,version | Format-Table -AutoSize
$bridgeClaude = Read-Host 'Claude original session UUID (from the list above)'
if ($bridgeClaude) {
    if (-not $ClaudePeer) { $ClaudePeer = Read-Host 'Exact remote alias this Claude session may send to (e.g. node-a/codex)' }
    & $Python $bridgeLauncher --state $bridgeState --tool claude --session $bridgeClaude bind --alias claude --project agentbridge-m0
    if ($LASTEXITCODE -ne 0) { throw 'Claude binding failed' }
    $bridgeClaudeRecord = @($bridgeDiscovered.result | Where-Object { $_.sessionId -eq $bridgeClaude })
    if ($bridgeClaudeRecord.Count -ne 1) { throw 'Exact Claude workspace could not be resolved' }
    & $Python (Join-Path $PSScriptRoot 'configure-m1-session.py') --state $bridgeState --tool claude --session $bridgeClaude --alias claude --workspace $bridgeClaudeRecord[0].cwd --peer $ClaudePeer --install-claude-rules
    if ($LASTEXITCODE -ne 0) { throw 'Claude client/policy setup failed' }
}
Write-Host 'For Codex, in the existing test chat ask it to run: $env:CODEX_THREAD_ID; Get-Location; whoami /user'
$bridgeCodex = Read-Host 'Codex original session UUID (blank to skip for now)'
if ($bridgeCodex) {
    $bridgeCliCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
    $bridgeCli = if ($bridgeCliCommand) { $bridgeCliCommand.Source } else { Read-Host 'Full path to Codex codex.exe (not a .cmd wrapper)' }
    if (-not $CodexPeer) { $CodexPeer = Read-Host 'Exact remote alias this Codex session may send to (e.g. node-a/claude)' }
    & $Python $bridgeLauncher --state $bridgeState --tool codex --session $bridgeCodex bind --alias codex --project agentbridge-m0 --cli $bridgeCli
    if ($LASTEXITCODE -ne 0) { throw 'Codex binding failed' }
    $bridgeCodexWorkspace = Read-Host 'Codex original session working directory (Get-Location output)'
    $bridgeCodexSid = Read-Host 'Codex tool user SID (whoami /user output; S-1-...)'
    & $Python (Join-Path $PSScriptRoot 'configure-m1-session.py') --state $bridgeState --tool codex --session $bridgeCodex --alias codex --workspace $bridgeCodexWorkspace --peer $CodexPeer --reader-sid $bridgeCodexSid
    if ($LASTEXITCODE -ne 0) { throw 'Codex client/policy setup failed' }
}
& $Python $bridgeLauncher --state $bridgeState list
Write-Host "Ready. Agent state: $bridgeState"
Write-Host 'Paste the authorization text printed above into each selected original session. The sessions will send and auto-echo according to that policy.'
Write-Host 'Do not paste node credentials or state files into chat. Report only the connection result and selected UUIDs.'
