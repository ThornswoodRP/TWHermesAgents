#Requires -Version 7
<#
.SYNOPSIS
Puts the connector for Patrick's chat from the phone into his Hermes on One
in place, and starts it at every sign-in.

.DESCRIPTION
Runs on One, as Patrick, from a clone of Thornswood-Hermes at main. Patrick
chose a small connector on One that reaches out to the decision core on the
VM (1 Nov, answer A): nothing listens on One, and the VM never reaches in.

  1. It finds Hermes on One (the hermes command and the Python beside it).
  2. The first time (or with -NewToken) it has the VM make the connector's
     token, over ssh, straight into %APPDATA%\Thornswood\own-one.token; the
     token never shows on screen or in a log. Only Patrick's user can read
     that file.
  3. It copies own_connector.py and room_send.py from this clone into
     %LOCALAPPDATA%\Thornswood\own-connector, with the commit they came from.
  4. It runs one round (--once) to check the token, the address and Hermes.
  5. It adds the task "Thornswood Own Hermes Connector" to Task Scheduler,
     which starts the connector at every sign-in and again if it stops, and
     starts it now.

While own_chat_write is off the connector asks the core every half minute,
does nothing and copies nothing. It prints a PASS or FAIL line for each step
and exits 1 when any fails. Run it again after an update to main.

.EXAMPLE
pwsh -NoProfile -File scripts\one\Install-OwnConnector.ps1 -Core https://hermes.<tailnet>.ts.net/api/v1 -SshHost hermes
#>
[CmdletBinding()]
param(
    # The decision core's API through Tailscale Serve: the VM's ts.net address and /api/v1.
    [Parameter(Mandatory)][string]$Core,
    # The ssh name One uses to run sudo on the VM; needed the first time, or with -NewToken.
    [string]$SshHost = '',
    [string]$Hermes = '',
    [string]$HermesPython = '',
    [string]$Folder = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Thornswood\own-connector'),
    [string]$TokenFile = (Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Thornswood\own-one.token'),
    [switch]$NewToken,
    [switch]$NoTask
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Pass([string]$m) { Write-Output "PASS $m" }
function Fail([string]$m) { Write-Output "FAIL $m"; exit 1 }
$taskName = 'Thornswood Own Hermes Connector'
$here = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if ($Core -notmatch '^https://[a-z0-9-]+\.[a-z0-9-]+\.ts\.net/api/v1$') {
    Fail "-Core is the VM's Tailscale address and /api/v1, like https://hermes.tail1234.ts.net/api/v1"
}

# 1. Hermes on One.
if (-not $Hermes) {
    $cmd = Get-Command hermes -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { Fail 'the hermes command is not on PATH; pass -Hermes with its full path' }
    $Hermes = $cmd.Source
}
if (-not (Test-Path $Hermes -PathType Leaf)) { Fail "no hermes command at $Hermes" }
if (-not $HermesPython) {
    $HermesPython = Join-Path (Split-Path $Hermes) 'python.exe'
    if (-not (Test-Path $HermesPython)) { Fail "no python.exe beside $Hermes; pass -HermesPython" }
}
$quiet = Join-Path (Split-Path $HermesPython) 'pythonw.exe'
$runner = if (Test-Path $quiet) { $quiet } else { $HermesPython }
Pass "Hermes on One is $Hermes, with $HermesPython"

# 2. The token, made on the VM and written straight to its file.
$tokenDir = Split-Path $TokenFile
New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
if ($NewToken -or -not (Test-Path $TokenFile) -or (Get-Item $TokenFile).Length -eq 0) {
    if (-not $SshHost) { Fail 'the token is made on the VM the first time; pass -SshHost, the ssh name One uses to run sudo there' }
    $mint = 'sudo -n runuser -u tw-decide -- /opt/thornswood-hermes/bin/tw-decide --db /srv/tw/decide/core.db ' +
            'feed-token --workspace thornswood --name own-one'
    $token = & ssh -o BatchMode=yes $SshHost $mint 2>$null
    if ($LASTEXITCODE -ne 0 -or "$token" -notmatch '^[A-Za-z0-9_-]{40,}$') {
        Fail "the VM did not make the token (ssh $SshHost, exit $LASTEXITCODE); check that ssh $SshHost sudo -n true works"
    }
    $part = "$TokenFile.part"
    Set-Content -Path $part -Value "$token" -NoNewline -Encoding ascii
    Remove-Variable token
    Move-Item $part $TokenFile -Force
    Pass "the VM made the connector's token, into $TokenFile"
} else {
    Pass "the connector's token is already in $TokenFile"
}
# Only Patrick's user reads it: no inherited access, full control for him alone.
if ($IsWindows) {
    & icacls $TokenFile /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "icacls could not make $TokenFile his alone" }
} else {
    & chmod 0600 $TokenFile
}
Pass "$TokenFile is readable by its owner only"

# 3. The connector's two files, from this clone.
$commit = (& git -C $here rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { Fail "$here is not a clone of Thornswood-Hermes" }
& git -C $here fetch --quiet origin main
$main = (& git -C $here rev-parse origin/main).Trim()
if ($commit -ne $main) { Fail "this clone is at $commit, not main's $main; git -C $here checkout --detach origin/main first" }
New-Item -ItemType Directory -Force -Path $Folder | Out-Null
foreach ($f in 'own_connector.py', 'room_send.py') {
    Copy-Item (Join-Path $here "thornswood_decisions\$f") (Join-Path $Folder $f) -Force
}
Set-Content -Path (Join-Path $Folder 'COMMIT') -Value $commit -Encoding ascii
Pass "own_connector.py and room_send.py from $commit are in $Folder"

# 4. One round, to check the token, the address and Hermes.
$script = Join-Path $Folder 'own_connector.py'
$connArgs = @('-I', $script, '--gateway', 'one', '--hermes', $Hermes, '--hermes-python', $HermesPython,
              '--core', $Core, '--token', $TokenFile)
$out = & $HermesPython @connArgs --once 2>&1
$code = $LASTEXITCODE
$line = ($out | Select-Object -Last 1)
if ($code -ne 0) { Fail "one round did not work (exit $code): $line" }
Pass "one round: $line"

# 5. The task: at every sign-in, again a minute after it stops, never two at once.
if ($NoTask) { Pass 'no task made (-NoTask)'; exit 0 }
$argLine = ($connArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
$action = New-ScheduledTaskAction -Execute $runner -Argument $argLine -WorkingDirectory $HOME
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and -not (@($existing.Actions | Where-Object { "$($_.Arguments)" -match 'own_connector\.py' }).Count)) {
    Fail "a task named '$taskName' already exists and runs something else; it is not replaced"
}
if ($existing) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
} else {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'Carries Patrick''s chat from the phone into his Hermes on One' | Out-Null
}
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $taskName).State
if ("$state" -ne 'Running') { Fail "the task '$taskName' is $state, not running; Task Scheduler's history for it says why" }
Pass "the task '$taskName' runs the connector now and at every sign-in"
