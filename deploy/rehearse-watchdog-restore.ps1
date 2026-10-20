<#
Rehearsal for ticket #155: prove the restore half of swap-package.ps1's abort
path on a disposable scheduled task, without touching the real watchdog.

What it does
  1. Finds the real watchdog by the same action query swap-package.ps1 uses and
     records its Settings.Enabled. It is NEVER disabled, enabled, unregistered
     or started here; only its Enabled bit is compared before/after.
  2. Creates one task named AIAssistantSwapRehearsal-<guid> at the task root
     (action: cmd.exe /c exit 0). The task is unregistered in finally, and any
     stale task with the same prefix and the same benign action is cleaned first.
  3. Extracts Get-WatchdogTaskState, Restore-WatchdogTask and Stop-AfterFailure
     FROM swap-package.ps1 with the PowerShell AST - the real helpers, not a
     copy - and replays:
       - never-disabled      : flag false, abort token watchdog=never-disabled
       - already-enabled     : flag true while the switch is still UP
                               (disable-did-not-take shape) -> already-enabled
       - re-enabled          : task really disabled -> Enable + read-back
       - read-back-failure   : Enable-ScheduledTask shadowed by a silent no-op;
                               the post-enable read-back must yield
                               re-enable-FAILED and leave the task disabled
       - recovery-after-failure: remove the shadow, restore really enables it
  4. Every abort goes through the real Stop-AfterFailure; Get-LiveProcess is
     stubbed to return no process and $LiveExe points at a path that does not
     exist, so the old-package restart branch cannot fire. Show-Rollback is a
     stub because backupDone stays false.
  5. Fails loudly on any unexpected token or if the real watchdog's Enabled bit
     changed. Writes the same lines to -EvidencePath when given.

The static locks for the read-back / already-enabled tokens live in
backend/tests/test_swap_package_disable_verify.py; this script is the runnable
behaviour evidence those locks cannot provide.

This script mutates exactly one disposable task plus its own stubs. It never
touches data/, chroma_db/, venv/, version.txt, dist/ or .github/workflows.
#>
[CmdletBinding()]
param(
    [string] $SwapScript = '',
    [string] $EvidencePath = ''
)

$ErrorActionPreference = 'Stop'
if (-not $SwapScript) {
    $SwapScript = Join-Path $PSScriptRoot 'swap-package.ps1'
}

$evidenceLines = New-Object System.Collections.Generic.List[string]
function Write-Evidence {
    param([string] $Line)
    [void] $evidenceLines.Add($Line)
    Write-Output $Line
}
function Fail-Rehearsal {
    param([string] $Line)
    Write-Evidence ('REHEARSAL-FAIL ' + $Line)
    throw $Line
}
function Assert-Equal {
    param($Actual, $Expected, [string] $Label)
    if ([string] $Actual -ne [string] $Expected) {
        Fail-Rehearsal ('case=' + $Label + ' expected=' + $Expected + ' actual=' + $Actual)
    }
}
function Assert-DisposableTask {
    if ($script:task.TaskPath -ne '\') {
        Fail-Rehearsal 'disposable task path is not the task root'
    }
    if ($script:task.TaskName -notlike 'AIAssistantSwapRehearsal-*') {
        Fail-Rehearsal ('refusing to touch task name=' + $script:task.TaskName)
    }
    if ($realTask.TaskPath -eq $script:task.TaskPath -and $realTask.TaskName -eq $script:task.TaskName) {
        Fail-Rehearsal 'disposable identity equals the real watchdog'
    }
    $live = Get-ScheduledTask -TaskPath $script:task.TaskPath -TaskName $script:task.TaskName
    if (-not $live -or $live.Actions.Execute -ne 'cmd.exe') {
        Fail-Rehearsal 'disposable task action is not cmd.exe; refusing to touch it'
    }
}
function Invoke-Abort {
    param([string] $Stage)
    $script:abortLines = New-Object System.Collections.Generic.List[string]
    $script:abortThrown = ''
    try {
        Stop-AfterFailure -FailedAt $Stage -Message ('rehearsal abort ' + $Stage) |
            ForEach-Object { [void] $script:abortLines.Add([string] $_) }
    } catch {
        $script:abortThrown = $_.Exception.Message
    }
    return @($script:abortLines)
}

# ---------------------------------------------------------------- preconditions
if (-not (Test-Path -LiteralPath $SwapScript)) {
    Fail-Rehearsal ('swap script missing path=' + $SwapScript)
}
$swapHash = (Get-FileHash -LiteralPath $SwapScript -Algorithm SHA256).Hash
Write-Evidence ('REHEARSAL psversion=' + $PSVersionTable.PSVersion.ToString())
Write-Evidence ('REHEARSAL swap-sha256=' + $swapHash)

$real = @(Get-ScheduledTask | Where-Object {
    $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
})
if ($real.Count -ne 1) {
    Fail-Rehearsal ('real watchdog not uniquely found count=' + $real.Count)
}
$realTask = $real[0]
$realEnabledBefore = [bool] $realTask.Settings.Enabled
Write-Evidence ('REHEARSAL real-watchdog name-len=' + $realTask.TaskName.Length +
                ' enabled-before=' + $realEnabledBefore)

# ---------------------------------------------------------------- extract helpers
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SwapScript, [ref] $tokens, [ref] $parseErrors)
if ($parseErrors.Count -gt 0) {
    Fail-Rehearsal ('swap script has parse errors count=' + $parseErrors.Count)
}
$extracted = @{}
foreach ($name in @('Get-WatchdogTaskState', 'Restore-WatchdogTask', 'Stop-AfterFailure')) {
    $found = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true))
    if ($found.Count -ne 1) {
        Fail-Rehearsal ('function extraction count name=' + $name + ' count=' + $found.Count)
    }
    $extracted[$name] = $found[0].Extent.Text
    Invoke-Expression $extracted[$name]
    Write-Evidence ('EXTRACT name=' + $name + ' end-line=' + $found[0].Extent.EndLineNumber)
}
foreach ($needle in @('Get-WatchdogTaskState', 're-enable-FAILED', 'already-enabled')) {
    if ($extracted['Restore-WatchdogTask'] -notlike ('*' + $needle + '*')) {
        Fail-Rehearsal ('extracted Restore-WatchdogTask lacks ' + $needle + ' - old script?')
    }
}

# Stubs: no backend process, no old package to restart, no rollback recipe path.
function Get-LiveProcess { return @() }
function Show-Rollback {
    Write-Evidence 'REHEARSAL-FAIL show-rollback called before backupDone'
}
$LiveExe = 'X:\qoder-155-rehearsal-no-such-package\run_backend.exe'
$script:backupDone = $false

# ---------------------------------------------------------------- disposable task
$taskPath = '\'
$stale = @(Get-ScheduledTask | Where-Object {
    $_.TaskPath -eq '\' -and $_.TaskName -like 'AIAssistantSwapRehearsal-*' -and
    $_.Actions.Execute -eq 'cmd.exe'
})
foreach ($s in $stale) {
    Unregister-ScheduledTask -TaskPath $s.TaskPath -TaskName $s.TaskName -Confirm:$false
    Write-Evidence ('CLEAN stale-name=' + $s.TaskName)
}

$taskName = 'AIAssistantSwapRehearsal-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c exit 0'
Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Action $action -Description 'disposable rehearsal task for swap-package Restore-WatchdogTask (#155); safe to delete' | Out-Null
$script:task = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName
Assert-DisposableTask
Write-Evidence ('TASK disposable name=' + $taskName + ' path=' + $taskPath + ' action=cmd.exe')

$exitCode = 0
try {
    # 1) flag false: nothing to give back
    $script:watchdogDisabled = $false
    $script:backupDone = $false
    $out = @(Invoke-Abort -Stage 'rehearsal-never-disabled')
    $abort = ($out | Where-Object { $_ -like 'ABORT stage=*' } | Select-Object -Last 1)
    Write-Evidence ('CASE never-disabled ' + $abort)
    Assert-Equal $script:abortThrown 'rehearsal abort rehearsal-never-disabled' 'never-disabled-thrown'
    Assert-Equal $abort ('ABORT stage=rehearsal-never-disabled moved=no watchdog=never-disabled package=intact-no-restart-needed') 'never-disabled-token'

    # 2) flag true but the switch was never down: disable-did-not-take shape
    $script:watchdogDisabled = $true
    $probe = Get-WatchdogTaskState -TaskToRead $script:task
    Assert-Equal $probe.Enabled $true 'already-enabled-precondition'
    $out = @(Invoke-Abort -Stage 'rehearsal-already-enabled')
    $abort = ($out | Where-Object { $_ -like 'ABORT stage=*' } | Select-Object -Last 1)
    $after = Get-WatchdogTaskState -TaskToRead $script:task
    Write-Evidence ('CASE already-enabled ' + $abort + ' readback=' + $after.Enabled + '/' + $after.EnabledSource + '/' + $after.State)
    Assert-Equal $abort ('ABORT stage=rehearsal-already-enabled moved=no watchdog=already-enabled package=intact-no-restart-needed') 'already-enabled-token'
    Assert-Equal $after.Enabled $true 'already-enabled-readback'

    # 3) real disable -> abort -> restore through the real Enable + read-back
    Assert-DisposableTask
    Disable-ScheduledTask -TaskPath $script:task.TaskPath -TaskName $script:task.TaskName | Out-Null
    $down = Get-WatchdogTaskState -TaskToRead $script:task
    Write-Evidence ('CASE disabled enabled=' + $down.Enabled + ' via=' + $down.EnabledSource + ' state=' + $down.State)
    Assert-Equal $down.Enabled $false 'disabled-precondition'
    $script:watchdogDisabled = $true
    $out = @(Invoke-Abort -Stage 'rehearsal-re-enabled')
    $abort = ($out | Where-Object { $_ -like 'ABORT stage=*' } | Select-Object -Last 1)
    $up = Get-WatchdogTaskState -TaskToRead $script:task
    Write-Evidence ('CASE re-enabled ' + $abort + ' readback=' + $up.Enabled + '/' + $up.EnabledSource + '/' + $up.State)
    Assert-Equal $abort ('ABORT stage=rehearsal-re-enabled moved=no watchdog=re-enabled package=intact-no-restart-needed') 're-enabled-token'
    Assert-Equal $up.Enabled $true 're-enabled-readback'
    Assert-Equal $up.EnabledSource 'settings-enabled' 're-enabled-readback-source'

    # 4) read-back failure: Enable-ScheduledTask returns but does not flip the switch
    Assert-DisposableTask
    Disable-ScheduledTask -TaskPath $script:task.TaskPath -TaskName $script:task.TaskName | Out-Null
    $script:watchdogDisabled = $true
    function Enable-ScheduledTask {
        param([string] $TaskPath, [string] $TaskName)
        # silent no-op: restores nothing and raises nothing - the shape the old
        # body took for success because it only looked for an exception
    }
    $out = @(Invoke-Abort -Stage 'rehearsal-readback-failure')
    $recover = ($out | Where-Object { $_ -like 'RECOVER-FAIL stage=*' } | Select-Object -Last 1)
    $abort = ($out | Where-Object { $_ -like 'ABORT stage=*' } | Select-Object -Last 1)
    $stillDown = Get-WatchdogTaskState -TaskToRead $script:task
    Write-Evidence ('CASE readback-failure ' + $recover)
    Write-Evidence ('CASE readback-failure ' + $abort + ' readback=' + $stillDown.Enabled + '/' + $stillDown.EnabledSource + '/' + $stillDown.State)
    Assert-Equal $script:abortThrown 'rehearsal abort rehearsal-readback-failure' 'readback-failure-thrown'
    Assert-Equal $abort ('ABORT stage=rehearsal-readback-failure moved=no watchdog=re-enable-FAILED package=intact-no-restart-needed') 'readback-failure-token'
    Assert-Equal $stillDown.Enabled $false 'readback-failure-still-down'
    if ($recover -notlike '*enabled=False*') {
        Fail-Rehearsal ('read-back failure token does not carry the raw read: ' + $recover)
    }
    if ($recover -notlike '*via=settings-enabled*') {
        Fail-Rehearsal ('read-back failure token does not carry via=: ' + $recover)
    }
    Remove-Item -LiteralPath 'Function:\Enable-ScheduledTask' -Force
    Write-Evidence 'CASE readback-failure shadow-removed=yes'

    # 5) the same task, real Enable after the failed attempt: recovery still works
    Assert-DisposableTask
    $script:watchdogDisabled = $true
    $out = @(Invoke-Abort -Stage 'rehearsal-recovery-after-failure')
    $abort = ($out | Where-Object { $_ -like 'ABORT stage=*' } | Select-Object -Last 1)
    $recovered = Get-WatchdogTaskState -TaskToRead $script:task
    Write-Evidence ('CASE recovery-after-failure ' + $abort + ' readback=' + $recovered.Enabled + '/' + $recovered.EnabledSource + '/' + $recovered.State)
    Assert-Equal $abort ('ABORT stage=rehearsal-recovery-after-failure moved=no watchdog=re-enabled package=intact-no-restart-needed') 'recovery-token'
    Assert-Equal $recovered.Enabled $true 'recovery-readback'

    Write-Evidence 'REHEARSAL ok=yes'
}
finally {
    $left = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
    if ($left) {
        Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false
        Write-Evidence ('CLEAN removed=' + $taskName)
    }
    $realAfter = [bool] (Get-ScheduledTask -TaskPath $realTask.TaskPath -TaskName $realTask.TaskName).Settings.Enabled
    Write-Evidence ('REHEARSAL real-watchdog enabled-after=' + $realAfter)
    if ($realAfter -ne $realEnabledBefore) {
        Write-Evidence 'REHEARSAL-FAIL real watchdog Enabled changed during rehearsal'
        $exitCode = 1
    }
    if ($EvidencePath) {
        [System.IO.File]::WriteAllLines($EvidencePath, $evidenceLines)
    }
}
if ($exitCode -ne 0) {
    throw 'rehearsal changed the real watchdog Enabled state'
}
