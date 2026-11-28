#Requires -Version 5.1
<#
==============================================================================
watchdog-guardian.ps1 - re-enable the crash guard when a swap died holding it down
==============================================================================

WHY THIS FILE EXISTS (2026-11-28)

deploy\swap-package.ps1 disables the scheduled task for the crash guard before it
stops the backend, and re-enables it at step 8, AFTER the health and marker
acceptance. That ordering is correct and deliberate (a half-baked package must not
be "protected"), and step 8 DOES restore the guard on every path that reaches it.

The hole is the window between the disable (step 3) and step 8:

  - 2026-11-13: the equivalent path hung and the guard stayed Disabled for 16 hours.
    Nothing else was watching, because the thing that watches IS the guard.
  - 2026-11-27: the v0.40.1 swap hung at Start-Process (no output redirection). The
    guard stayed Disabled and the operator re-enabled it by hand.

A disabled task cannot notice that it is disabled, so the recovery has to live
OUTSIDE it. This script is the second, tiny task that does one thing:

  if the guard is disabled AND no swap is actually in progress -> enable it again.

It never touches the backend package, never starts or stops the service, and never
writes the guard task when that task is already up.

HOW IT TELLS THE TWO CASES APART

swap-package.ps1 writes data\swap-in-progress.marker right after disabling the guard
and removes it right after re-enabling. So:

  marker present and fresh   -> a swap is running: stay out, do nothing
  marker present but STALE   -> a swap died mid-flight: re-enable (the swap is gone)
  marker absent              -> an abort left it down (by design for an unverified
                                package) or someone disabled it by hand: re-enable

Staleness is measured against -StaleMinutes (default 15). A normal swap is 2-5
minutes, so 15 is comfortably past any healthy run while still short enough that a
dead swap does not leave production unprotected for long.

USAGE

  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\watchdog-guardian.ps1
  ...  -DryRun      # report what it would do, change nothing

Exit codes: 0 nothing to do or guard restored; 1 could not restore; 2 could not read.
#>
param(
    [string] $TaskName = "",
    [int]    $StaleMinutes = 15,
    [switch] $DryRun
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$MarkerPath  = Join-Path $ProjectRoot 'data\swap-in-progress.marker'
$LogPath     = Join-Path $ProjectRoot 'logs\watchdog-guardian.log'

function Write-Log([string] $Level, [string] $Message) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $Level.PadRight(7) + ' ' + $Message
    try {
        $dir = Split-Path -Parent $LogPath
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch { }
    Write-Output $line
}

# Located by ACTION (wscript.exe + watchdog-hidden.vbs), NOT by name: the name can be
# renamed or localized, while the action IS the identity. Same rule and same reason as
# swap-package.ps1 step 1 - two files, one lookup shape.
function Find-GuardTask {
    if ($TaskName) {
        return @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
    }
    return @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
    })
}

# Enabled lives on the Settings association, NOT on the task object: reading the bare
# $task.Enabled answers $null silently for every task on this box. That is why the
# fail-closed default here is "unreadable -> do NOT touch it", not "not enabled ->
# enable it": guessing in that direction would start a guard for an unknown identity.
function Get-GuardEnabled([object] $Task) {
    try {
        $fresh = Get-ScheduledTask -TaskPath $Task.TaskPath -TaskName $Task.TaskName -ErrorAction Stop
        $enabled = $fresh.Settings.Enabled
        if ($null -eq $enabled) { return @{ Ok = $false; Enabled = $null; Why = 'settings-enabled-absent' } }
        return @{ Ok = $true; Enabled = [bool] $enabled; Why = 'ok' }
    } catch {
        return @{ Ok = $false; Enabled = $null; Why = ('read-threw:' + $_.Exception.Message) }
    }
}

function Get-MarkerAgeMinutes {
    if (-not (Test-Path -LiteralPath $MarkerPath)) { return $null }
    try {
        $raw = (Get-Content -LiteralPath $MarkerPath -Raw -ErrorAction Stop).Trim()
        $epoch = 0L
        if (-not [long]::TryParse($raw, [ref] $epoch)) { return -1.0 }
        $written = [DateTimeOffset]::FromUnixTimeSeconds($epoch)
        return ([DateTimeOffset]::UtcNow - $written).TotalMinutes
    } catch {
        return -1.0
    }
}

$tasks = @(Find-GuardTask)
if ($tasks.Count -eq 0) {
    Write-Log 'ERROR' 'GUARDIAN-RESULT readable=no guard=not-found - no task matches the guard action; nothing to protect'
    exit 2
}
if ($tasks.Count -gt 1) {
    Write-Log 'WARN' ('GUARDIAN-RESULT readable=yes guard=ambiguous count=' + $tasks.Count + ' - more than one task matches the guard action; refusing to pick one')
    exit 2
}
$task = $tasks[0]

$state = Get-GuardEnabled $task
if (-not $state.Ok) {
    Write-Log 'ERROR' ('GUARDIAN-RESULT readable=no guard=unknown why=' + $state.Why + ' - cannot prove the switch, so it is not touched')
    exit 2
}

if ($state.Enabled) {
    Write-Log 'INFO' 'GUARDIAN-RESULT readable=yes guard=up action=none - already enabled'
    exit 0
}

$age = Get-MarkerAgeMinutes
if ($null -ne $age -and $age -ge 0 -and $age -lt $StaleMinutes) {
    Write-Log 'INFO' ('GUARDIAN-RESULT readable=yes guard=down action=wait marker-age-min=' + [math]::Round($age, 1) + ' - a swap is in progress, staying out')
    exit 0
}

$markerNote = 'absent'
if ($null -ne $age) {
    if ($age -lt 0) { $markerNote = 'unparsable' } else { $markerNote = 'stale:' + [math]::Round($age, 1) }
}

if ($DryRun) {
    Write-Log 'INFO' ('GUARDIAN-RESULT readable=yes guard=down action=would-enable marker=' + $markerNote + ' (dry run, nothing changed)')
    exit 0
}

try {
    Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName -ErrorAction Stop | Out-Null
} catch {
    Write-Log 'ERROR' ('GUARDIAN-RESULT readable=yes guard=down action=enable-failed marker=' + $markerNote + ' error=' + $_.Exception.Message)
    exit 1
}

# A returned Enable call is not evidence the switch moved - read it back, the same rule
# swap-package.ps1 states at length for this same cmdlet.
$after = Get-GuardEnabled $task
if (-not $after.Ok) {
    Write-Log 'ERROR' ('GUARDIAN-RESULT readable=no guard=down action=unproven marker=' + $markerNote + ' why=' + $after.Why)
    exit 1
}
if (-not $after.Enabled) {
    Write-Log 'ERROR' ('GUARDIAN-RESULT readable=yes guard=down action=enable-did-not-take marker=' + $markerNote)
    exit 1
}

Write-Log 'INFO' ('GUARDIAN-RESULT readable=yes guard=restored action=enabled marker=' + $markerNote + ' - the guard had been left down; it is back up now')

# Clear only a stale marker: a fresh one means a live swap and we never reach here then.
# Clearing stops the next run from re-reading the same stale value and reporting the
# same restoration twice.
if ($null -ne $age) {
    # [System.IO.File]::Delete, not Remove-Item: swap-package.ps1 is locked to
    # "Remove-Item may only target env: variables" (the backup package is the rollback
    # position, and a whitelist is the only shape that cannot be talked around). This
    # file is outside that lock's file scope, but keeping ONE deletion idiom across the
    # three scripts means the next reader does not have to work out which is safe where.
    try { [System.IO.File]::Delete($MarkerPath) } catch { }
}

exit 0
