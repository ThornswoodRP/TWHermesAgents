#Requires -Version 7
<#
.SYNOPSIS
Puts the connector for Patrick's chat from the phone into his Hermes on One
in place, and starts it at every sign-in.

.DESCRIPTION
Runs on One, as Patrick, from a clone of Thornswood-Hermes at main. Patrick
chose a small connector on One that reaches out to the decision core on the
VM (1 Nov, answer A): nothing listens on One, and the VM never reaches in.

  1. It finds Hermes on One: the hermes command, and the Python its launcher
     says it runs (hermes --print-runtime-command).
  2. The first time (or with -NewToken) it has the VM make the connector's
     token, over ssh to thornswood-bots (One's alias for the VM), straight into %APPDATA%\Thornswood\own-one.token; the
     token never shows on screen or in a log. Only Patrick's user can read
     that file.
  3. It copies own_connector.py and room_send.py from this clone into
     %LOCALAPPDATA%\Thornswood-Hermes\own-connector, with the commit they came from.
     Never inside %LOCALAPPDATA%\Thornswood: that is the launcher's Velopack
     folder, and Velopack's Setup.exe, run over an install, stops what runs
     from it, renames it away and deletes it (velopack install.rs). On 2 Nov
     a launcher install did that to the connector there, and it stopped at
     2:58 PM with no file left to start. The script refuses any folder inside
     a Velopack install.
  4. It runs one round (--once) to check the token, the address and Hermes.
  5. It adds the task "Thornswood Own Hermes Connector" to Task Scheduler,
     which starts the connector at every sign-in, and starts it now. The task
     runs it under --supervise, which starts it again a minute after any stop.

The clone signs in to GitHub with gh, set in its own git config only, so
neither this script nor the connector's updates ever wait on a sign-in
prompt (One's git otherwise opens its credential helper selector).

After that the connector keeps itself current: the core names the release
the VM runs (a commit of main that passed the whole suite on the VM), and
the connector fetches main in this clone, takes its two files from that
commit with git show, finishes the turns it is running and starts again on
the new copy. Code comes from GitHub through this clone, never from the VM.
The update does not touch this clone's working tree, so leave the clone
where it is.

While own_chat_write is off the connector asks the core every half minute,
does nothing and copies nothing. It prints a PASS or FAIL line for each step
and exits 1 when any fails. It is run once; a copy installed before the
self-update (2 Nov) needs this one run to get it.

.EXAMPLE
pwsh -NoProfile -File scripts\one\Install-OwnConnector.ps1 -Core https://hermes.<tailnet>.ts.net/api/v1
#>
[CmdletBinding()]
param(
    # The decision core's API through Tailscale Serve: the VM's ts.net address and /api/v1.
    [Parameter(Mandatory)][string]$Core,
    # The ssh name One uses to run sudo on the VM, the first time or with -NewToken. One's ssh
    # config knows the VM as thornswood-bots (the VM's address and key, as install-on-oracle.sh
    # and send-to-oracle.sh use it); the tailnet name hermes is the HTTPS address only.
    [string]$SshHost = 'thornswood-bots',
    [string]$Hermes = '',
    [string]$HermesPython = '',
    # Default: %LOCALAPPDATA%\Thornswood-Hermes\own-connector, outside the launcher's Velopack folder.
    [string]$Folder = '',
    # Default: %APPDATA%\Thornswood\own-one.token.
    [string]$TokenFile = '',
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

# The default folders, worked out here rather than in param(): GetFolderPath answers an
# empty string for a folder that does not exist yet unless told DoNotVerify (on Linux
# ~/.config in a fresh home, as the VM's test run has), and Join-Path then stops the
# script before it can say anything. This script makes both folders itself.
function AppFolder([string]$which, [string]$sub, [string]$leaf) {
    $base = [Environment]::GetFolderPath($which, [Environment+SpecialFolderOption]::DoNotVerify)
    if (-not $base) { Fail "this user has no $which folder; pass -Folder and -TokenFile" }
    Join-Path (Join-Path $base $sub) $leaf
}
if (-not $Folder) { $Folder = AppFolder 'LocalApplicationData' 'Thornswood-Hermes' 'own-connector' }

# Never inside a Velopack install: its Setup.exe deletes the whole folder when the app is installed
# again (velopack src/bins/src/commands/install.rs: force_stop_package, rename, remove_dir_all), and
# its root holds Update.exe and current\. The launcher's is %LOCALAPPDATA%\Thornswood, even before
# it is installed, so that one is refused by name as well.
$launcherRoot = AppFolder 'LocalApplicationData' 'Thornswood' ''
function Under([string]$path, [string]$root) {
    $p = [IO.Path]::GetFullPath($path).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $r = [IO.Path]::GetFullPath($root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $p.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)
}
$velopack = $null
if (Under $Folder $launcherRoot) { $velopack = $launcherRoot.TrimEnd('\', '/') }
$up = [IO.Path]::GetFullPath($Folder)
while (-not $velopack -and $up) {
    if ((Test-Path (Join-Path $up 'Update.exe') -PathType Leaf) -and (Test-Path (Join-Path $up 'current') -PathType Container)) { $velopack = $up }
    $up = Split-Path $up
}
if ($velopack) {
    Fail "$Folder is inside $velopack, a Velopack install, whose Setup.exe deletes it when the app is installed again; leave -Folder out or pick a folder outside it"
}
if (-not $TokenFile) { $TokenFile = AppFolder 'ApplicationData' 'Thornswood' 'own-one.token' }

# 1. Hermes on One.
if (-not $Hermes) {
    $cmd = Get-Command hermes -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { Fail 'the hermes command is not on PATH; pass -Hermes with its full path' }
    $Hermes = $cmd.Source
}
if (-not (Test-Path $Hermes -PathType Leaf)) { Fail "no hermes command at $Hermes" }
# The connector itself needs only the standard library; it runs with the Python Hermes's own
# launcher says it starts (hermes --print-runtime-command: on One a Python in Hermes's store,
# with nothing beside hermes.exe), and asks the launcher again for the readiness check.
$given = [bool]$HermesPython
if (-not $HermesPython) {
    $answer = & $Hermes --print-runtime-command 2>$null
    if ($LASTEXITCODE -eq 0) {
        try { $HermesPython = @($answer | Out-String | ConvertFrom-Json)[0] } catch { $HermesPython = '' }
        if (-not $HermesPython -or -not (Test-Path $HermesPython -PathType Leaf)) {
            Fail "$Hermes --print-runtime-command did not name a Python that is there; pass -HermesPython"
        }
    } else {
        # A launcher too old to answer: a venv's hermes.exe, with its python.exe beside it.
        $HermesPython = Join-Path (Split-Path $Hermes) 'python.exe'
        if (-not (Test-Path $HermesPython)) { Fail "$Hermes does not say its Python and has none beside it; pass -HermesPython" }
    }
}
$quiet = Join-Path (Split-Path $HermesPython) 'pythonw.exe'
$runner = if (Test-Path $quiet) { $quiet } else { $HermesPython }
Pass "Hermes on One is $Hermes; the connector runs with $runner"

# 2. The token, made on the VM and written straight to its file.
$tokenDir = Split-Path $TokenFile
New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
if ($NewToken -or -not (Test-Path $TokenFile) -or (Get-Item $TokenFile).Length -eq 0) {
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
# Nobody is there to answer a sign-in prompt, here or when the connector updates itself from this
# clone under Task Scheduler. One's git asks its credential helper selector by default, a window that
# waits for a click, so a fetch would hang (2 Nov). This clone signs in to GitHub with gh instead, set
# in this clone's own config only: the empty entry drops every helper set before it for github.com,
# then gh answers with the account it is signed in as. Git never prompts.
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
$origin = "$(& git -C $here config --get remote.origin.url)".Trim()
if ($origin -match '^https://github\.com/') {
    $gh = (Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $gh) { Fail 'gh is not on PATH; this clone signs in to GitHub with it, so git never prompts' }
    & $gh.Source auth status --hostname github.com *> $null
    if ($LASTEXITCODE -ne 0) { Fail 'gh is not signed in to github.com; run gh auth login once, then this again' }
    $helper = "!'" + ($gh.Source -replace '\\', '/') + "' auth git-credential"
    & git -C $here config --local --replace-all credential.https://github.com.helper ''
    & git -C $here config --local --add credential.https://github.com.helper $helper
    if ($LASTEXITCODE -ne 0) { Fail "could not set this clone's GitHub sign-in to gh" }
    Pass "this clone signs in to GitHub with gh ($($gh.Source)), so git never prompts"
}
& git -C $here fetch --quiet origin main
if ($LASTEXITCODE -ne 0) { Fail "git fetch origin main failed in $here (exit $LASTEXITCODE)" }
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
$connArgs = @('-I', $script, '--gateway', 'one', '--hermes', $Hermes, '--core', $Core, '--token', $TokenFile)
if ($given) { $connArgs += @('--hermes-python', $HermesPython) }
$out = & $HermesPython @connArgs --once 2>&1
$code = $LASTEXITCODE
$line = ($out | Select-Object -Last 1)
if ($code -ne 0) { Fail "one round did not work (exit $code): $line" }
Pass "one round: $line"

# 5. The task: at every sign-in, never two at once. --supervise starts the connector again a minute
# after any stop and at once after it updates itself from this clone (--clone, with this git).
if ($NoTask) { Pass 'no task made (-NoTask)'; exit 0 }
$git = (Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $git) { Fail 'git is not on PATH; the connector updates itself with it' }
$taskArgs = @('-I', $script, '--supervise', '--clone', $here, '--git', $git.Source) + $connArgs[2..($connArgs.Count - 1)]
$argLine = ($taskArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
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
Pass "the task '$taskName' runs the connector now and at every sign-in, and it keeps itself current from $here"
