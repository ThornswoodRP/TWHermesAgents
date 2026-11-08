#Requires -Version 7
<#
.SYNOPSIS
Pulls the Oracle VM's nightly encrypted Hermes backups to One, keeps copies,
uploads them to Google Drive, and once a month proves one opens.

.DESCRIPTION
Runs on One (02 section 7). The VM writes one archive a night at 3:30 AM
Mountain into /srv/tw/backups, encrypted to Patrick's age public key; only
One holds the private key (%LOCALAPPDATA%\hermes\backup-key\). This script:

  1. pulls every archive One does not have yet over SFTP as tw-backup, the
     VM user that can read that folder and nothing else;
  2. checks each one is an age file, then keeps the newest -Keep on One;
  3. copies them to thornswood-gdrive:Thornswood-Hermes-Backups with rclone
     and deletes Drive copies older than -DriveKeepDays;
  4. every 28 days (or with -TestRestore) decrypts the newest to a temp
     folder, checks it lists the store and no sign-in or key, and deletes it.

It prints a PASS or FAIL line for each step and exits 1 when any fails. It
never prints a key. -MakeKey makes One's SFTP key the first time;
scripts/install-on-oracle.sh puts its public half on the VM. -Register adds
the nightly task (4:30 AM) to Task Scheduler.

.EXAMPLE
pwsh -NoProfile -File scripts\one\Pull-HermesBackup.ps1
#>
[CmdletBinding()]
param(
    [string]$HostName = 'thornswood-bots',
    [string]$Folder = (Join-Path $env:LOCALAPPDATA 'hermes\backups'),
    [string]$KeyFolder = (Join-Path $env:LOCALAPPDATA 'hermes\backup-key'),
    [string]$SftpKey = (Join-Path $HOME '.ssh\tw-backup-pull'),
    [string]$Remote = 'thornswood-gdrive:Thornswood-Hermes-Backups',
    [int]$Keep = 30,
    [int]$DriveKeepDays = 90,
    # Windows' own tar (bsdtar). On One, Git's GNU tar comes first on PATH and
    # reads C:\... as a host named C (31 Oct: "Cannot connect to C: resolve
    # failed"), so the restore test names Windows' tar by its full path.
    [string]$Tar = $(if ($IsWindows) { Join-Path $env:SystemRoot 'System32\tar.exe' } else { 'tar' }),
    [switch]$TestRestore,
    [switch]$MakeKey,
    [switch]$Register
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:failed = $false
function Pass([string]$m) { Write-Output "PASS $m" }
function Fail([string]$m) { Write-Output "FAIL $m"; $script:failed = $true }
$namePattern = '^hermes-\d{8}-\d{6}\.tar\.gz\.age$'

if ($MakeKey) {
    if (Test-Path $SftpKey) { Pass "One's SFTP key is already at $SftpKey"; exit 0 }
    New-Item -ItemType Directory -Force -Path (Split-Path $SftpKey) | Out-Null
    & ssh-keygen -q -t ed25519 -N '' -C 'one-pulls-hermes-backups' -f $SftpKey
    if ($LASTEXITCODE -ne 0) { Fail 'ssh-keygen did not make the key'; exit 1 }
    Pass "made $SftpKey; scripts/install-on-oracle.sh puts $SftpKey.pub on the VM"
    exit 0
}

if ($Register) {
    # The task runs this script where it is now, every night: never from a
    # folder that gets cleaned up (31 Oct: a clone in Hermes's scratch folder,
    # which goes after a day without use).
    $here = [System.IO.Path]::GetFullPath($PSCommandPath)
    $passing = @([System.IO.Path]::GetTempPath()) + @(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'hermes' })
    foreach ($root in $passing) {
        $full = [System.IO.Path]::GetFullPath($root).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if ($here.StartsWith($full, [System.StringComparison]::OrdinalIgnoreCase)) {
            Fail "this copy is under $root, which gets cleaned up; clone TWHermesAgents to $(Join-Path $HOME 'TWHermesAgents') and run -Register from there"
            exit 1
        }
    }
    # Task names ignore case on Windows, so a name is only ours when the task
    # runs this script. One that runs anything else is never replaced (31 Oct:
    # -Force replaced the task for Patrick's own Hermes backup).
    $name = 'Thornswood Bots Hermes Backup Pull'
    $ours = { param($t) @($t.Actions | Where-Object { "$($_.Arguments)" -match 'Pull-HermesBackup\.ps1' }).Count -gt 0 }
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing -and -not (& $ours $existing)) {
        Fail "a task named '$($existing.TaskName)' already exists and runs something else; it is not replaced"
        exit 1
    }
    # The name this script used before 31 Oct, when it is this script's.
    $old = Get-ScheduledTask -TaskName 'Thornswood Hermes backup pull' -ErrorAction SilentlyContinue
    if ($old -and $old.TaskName -ne $name -and (& $ours $old)) {
        Unregister-ScheduledTask -TaskName $old.TaskName -Confirm:$false
        Pass "removed this script's task under its old name, '$($old.TaskName)'"
    }
    $pwsh = (Get-Command pwsh).Source
    $action = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoProfile -File `"$here`""
    $trigger = New-ScheduledTaskTrigger -Daily -At '4:30 AM'
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $task = @{ TaskName = $name; Action = $action; Trigger = $trigger; Settings = $settings
        Description = 'Pulls the Thornswood Bots'' encrypted Hermes backups from the VM and copies them to Google Drive' }
    if ($existing) { Set-ScheduledTask -TaskName $existing.TaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null }
    else { Register-ScheduledTask @task | Out-Null }
    Pass "the nightly pull runs at 4:30 AM from Task Scheduler ($name), from $here"
    exit 0
}

foreach ($tool in 'sftp', 'age', 'rclone', $Tar) {
    if (-not (Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue)) { Fail "$tool is not installed on One"; exit 1 }
}
if (-not (Test-Path $SftpKey)) { Fail "no SFTP key at $SftpKey; run this with -MakeKey, then scripts/install-on-oracle.sh"; exit 1 }
New-Item -ItemType Directory -Force -Path $Folder | Out-Null

function Invoke-Sftp([string[]]$commands) {
    $batch = New-TemporaryFile
    try {
        Set-Content -Path $batch -Value $commands -Encoding ascii
        $out = & sftp -q -b $batch -i $SftpKey -o IdentitiesOnly=yes -o BatchMode=yes "tw-backup@$HostName" 2>&1
        return @{ Code = $LASTEXITCODE; Out = @($out | ForEach-Object { "$_" }) }
    } finally {
        Remove-Item $batch -Force
    }
}

# 1. Pull what One does not have yet.
$list = Invoke-Sftp @('ls -1')
if ($list.Code -ne 0) {
    Fail "SFTP to tw-backup@$HostName did not list the backups: $($list.Out -join ' ')"
    exit 1
}
$onVm = @($list.Out | ForEach-Object { ($_ -split '/')[-1].Trim() } | Where-Object { $_ -match $namePattern } | Sort-Object)
if ($onVm.Count -eq 0) { Fail 'the VM holds no backup; look at journalctl -u tw-backup on the VM' }
$newest = $onVm | Select-Object -Last 1
if ($newest) {
    $made = [datetime]::ParseExact($newest.Substring(7, 15), 'yyyyMMdd-HHmmss', $null)
    if (((Get-Date).ToUniversalTime() - $made).TotalHours -gt 36) {
        Fail "the newest backup on the VM is from $made UTC; last night's did not run"
    }
}
# pulled.txt names every archive One already took, so one pruned below is
# not pulled again the next night.
$ledger = Join-Path $Folder 'pulled.txt'
$had = @(if (Test-Path $ledger) { Get-Content $ledger })
$pulled = 0
foreach ($name in $onVm) {
    $dest = Join-Path $Folder $name
    if ($had -contains $name -or (Test-Path $dest)) { continue }
    $part = "$dest.part"
    $get = Invoke-Sftp @("get $name `"$($part -replace '\\', '/')`"")
    if ($get.Code -ne 0 -or -not (Test-Path $part)) { Fail "could not pull ${name}: $($get.Out -join ' ')"; continue }
    $bytes = [System.IO.File]::ReadAllBytes($part)
    $head = [System.Text.Encoding]::ASCII.GetString($bytes, 0, [Math]::Min(21, $bytes.Length))
    if (-not $head.StartsWith('age-encryption.org/v1')) {
        Remove-Item $part -Force
        Fail "$name is not an age file; it is not kept"
        continue
    }
    Move-Item $part $dest -Force
    Add-Content -Path $ledger -Value $name
    $pulled++
}
Pass "pulled $pulled new backup(s); the VM holds $($onVm.Count)"

# 2. Keep the newest -Keep on One.
$local = @(Get-ChildItem $Folder -File | Where-Object { $_.Name -match $namePattern } | Sort-Object Name)
if ($local.Count -gt $Keep) {
    $local | Select-Object -First ($local.Count - $Keep) | Remove-Item -Force
}
Pass "One keeps the newest $Keep in $Folder"

# 3. Google Drive.
& rclone copy $Folder $Remote --include 'hermes-*.tar.gz.age' --no-traverse --quiet
if ($LASTEXITCODE -ne 0) { Fail "rclone copy to $Remote failed (exit $LASTEXITCODE)" }
else {
    & rclone delete $Remote --include 'hermes-*.tar.gz.age' --min-age "$($DriveKeepDays)d" --quiet
    if ($LASTEXITCODE -ne 0) { Fail "rclone could not delete Drive copies older than $DriveKeepDays days" }
    else { Pass "copied to $Remote; Drive keeps $DriveKeepDays days" }
}

# 4. Prove one opens, every 28 days.
$marker = Join-Path $Folder 'last-restore-test.txt'
$due = $TestRestore -or -not (Test-Path $marker) -or ((Get-Date) - (Get-Item $marker).LastWriteTime).TotalDays -ge 28
$latest = Get-ChildItem $Folder -File | Where-Object { $_.Name -match $namePattern } | Sort-Object Name | Select-Object -Last 1
if ($due -and $latest) {
    $keys = @(Get-ChildItem $KeyFolder -File -ErrorAction SilentlyContinue |
        Where-Object { (Select-String -Path $_.FullName -Pattern '^AGE-SECRET-KEY-1' -Quiet) })
    if ($keys.Count -ne 1) {
        Fail "found $($keys.Count) age private keys in $KeyFolder; the test needs exactly one"
    } else {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("twh-restore-test-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            $plain = Join-Path $tmp 'backup.tar.gz'
            & age --decrypt -i $keys[0].FullName -o $plain $latest.FullName
            if ($LASTEXITCODE -ne 0) { throw "age could not decrypt $($latest.Name)" }
            $names = @(& $Tar -tzf $plain)
            if ($LASTEXITCODE -ne 0) { throw "$($latest.Name) decrypts but tar cannot read it" }
            foreach ($want in 'MANIFEST.json', 'srv/tw/policy/policy.db') {
                if ($names -notcontains $want) { throw "$($latest.Name) does not hold $want" }
            }
            $bad = @($names | Where-Object { $_ -match '(^|/)(auth\.json|\.env|[^/]*\.pem|[^/]*\.key|credentials[^/]*|hosts\.yml|\.git-credentials)$' })
            if ($bad.Count) { throw "$($latest.Name) holds what a backup never should: $($bad -join ', ')" }
            Set-Content -Path $marker -Value "$($latest.Name) opened on $(Get-Date -Format o) with $($names.Count) files"
            Pass "$($latest.Name) decrypts and lists $($names.Count) files, the store among them, and no sign-in or key"
        } catch {
            Fail "the restore test: $_"
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($script:failed) { exit 1 }
exit 0
