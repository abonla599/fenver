#Requires -Version 5.1
<#
==============================================================================
register-guardian.ps1 - register the guard's guardian as a second task
==============================================================================

WHY A SECOND TASK: the crash guard is a scheduled task. When swap-package.ps1
disables it (step 3) and the swap then hangs or is killed, step 8 never runs and
the guard stays Disabled - and a disabled task cannot notice that it is disabled.
2026-11-13 (16 hours down) and 2026-11-27 (manual re-enable) were both this shape.

So the recovery has to live outside the guard. This registers
deploy\watchdog-guardian.ps1 as its own task, repeating every N minutes.

It COPIES the real guard task's principal (same user, same LogonType) instead of
inventing one: the guardian must run in the same session the guard runs in, or it
would report "guard down" for a guard that is fine in another session.

The action is wscript.exe + guardian-hidden.vbs (that wrapper exists for the same
reason the guard has one: a PowerShell task started from Task Scheduler flashes a
console window otherwise; see watchdog-hidden.vbs).

USAGE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\register-guardian.ps1
  ... -Unregister
  ... -Show
#>
param(
    [string] $GuardianTaskName = 'AIAssistantGuardian',
    [int]    $EveryMinutes = 5,
    [switch] $Unregister,
    [switch] $Show
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$guardianScript = Join-Path $ProjectRoot 'deploy\watchdog-guardian.ps1'
$logPath = Join-Path $ProjectRoot 'logs\register-guardian.log'

function Write-Line([string] $m) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
    Write-Output $line
    try {
        $dir = Split-Path -Parent $logPath
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    } catch { }
}

# The guard is located by ACTION, never by name - same rule as swap-package.ps1 and
# watchdog-guardian.ps1, so all three agree on what "the guard" is.
$guard = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
})

if (-not $guard -or $guard.Count -ne 1) {
    Write-Line ('REFUSED: expected exactly one guard task, found ' + $guard.Count + '. Run deploy\register-watchdog.ps1 first.')
    exit 1
}
$guardTask = $guard[0]

if ($Show) {
    $t = Get-ScheduledTask -TaskName $GuardianTaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Line ('guardian task not registered: ' + $GuardianTaskName); exit 0 }
    $info = $t | Get-ScheduledTaskInfo
    Write-Line ('task      : ' + $t.TaskPath + $t.TaskName)
    Write-Line ('state     : ' + $t.State + ' / Settings.Enabled=' + $t.Settings.Enabled)
    Write-Line ('last run  : ' + $info.LastRunTime + ' result=' + $info.LastTaskResult)
    Write-Line ('next run  : ' + $info.NextRunTime)
    Write-Line ('guard     : ' + $guardTask.TaskName + ' enabled=' + $guardTask.Settings.Enabled)
    exit 0
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $GuardianTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Line ('unregistered: ' + $GuardianTaskName)
    exit 0
}

if (-not (Test-Path -LiteralPath $guardianScript)) {
    Write-Line ('REFUSED: missing ' + $guardianScript)
    exit 1
}

$vbs = Join-Path $ProjectRoot 'deploy\guardian-hidden.vbs'
if (-not (Test-Path -LiteralPath $vbs)) {
    Write-Line ('REFUSED: missing wrapper ' + $vbs + ' - without it Task Scheduler flashes a console window')
    exit 1
}

$arg = [char]34 + $vbs + [char]34
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg -WorkingDirectory $ProjectRoot

# Hidden + short execution window: this job does one cmdlet call either way.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -Priority 6
$settings.Hidden = $true

$once = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)

# Same identity as the guard, deliberately: a guardian in another session would read
# the guard's Enabled bit correctly but could not report meaningfully next to it, and
# at Boot mode the guard runs S4U while an Interactive guardian would not run at all.
$principal = New-ScheduledTaskPrincipal -UserId $guardTask.Principal.UserId -LogonType $guardTask.Principal.LogonType -RunLevel Limited

Unregister-ScheduledTask -TaskName $GuardianTaskName -Confirm:$false -ErrorAction SilentlyContinue
try {
    Register-ScheduledTask -TaskName $GuardianTaskName -Action $action -Trigger $once -Settings $settings -Principal $principal | Out-Null
} catch {
    Write-Line ('REGISTER-FAILED: ' + $_.Exception.Message)
    exit 1
}

Write-Line ('registered: ' + $GuardianTaskName + ' every ' + $EveryMinutes + ' min, principal=' + $guardTask.Principal.LogonType)

# Read it back: a returned Register call is not evidence the task exists, the same rule
# the two runtime scripts follow for their own cmdlets.
$check = Get-ScheduledTask -TaskName $GuardianTaskName -ErrorAction SilentlyContinue
if (-not $check) { Write-Line 'READBACK-FAILED: registered call returned but the task does not exist'; exit 1 }
Write-Line ('readback: state=' + $check.State + ' enabled=' + $check.Settings.Enabled)

Start-ScheduledTask -TaskName $GuardianTaskName
Start-Sleep -Seconds 12
$info = Get-ScheduledTask -TaskName $GuardianTaskName | Get-ScheduledTaskInfo
Write-Line ('first run: result=' + $info.LastTaskResult)
exit 0
