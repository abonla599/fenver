#Requires -Version 5.1
<#
==============================================================================
swap-package.ps1 - land a freshly built onedir package over the live backend
==============================================================================
Turns the deployment guide's "swap into the running service: three steps, the
order is not negotiable" section into an executable, fail-fast form. Every
ordering rule below exists because breaking it is a documented real incident;
the guide remains the prose that explains WHY, this file is the part that runs.

Usage (dry run first - it is safe to run while production serves users):
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\swap-package.ps1 -ExpectedVersion v0.29.1 -Marker msg-trace -DryRun
  # once the dry run prints DRYRUN ok, rerun the same line without -DryRun.

Exit shape: 0 on SWAP-DONE; non-zero either with production UNTOUCHED (the
abort happens before anything was moved and this script puts things back) or
with an explicit ROLLBACK recipe printing the real backup path (any failure
after the live directory was moved aside - an auto-rollback there can turn one
half-swap into two).

Progress lines are one-token machine-parsable (PRECHECK / DISABLED / STOPPED /
BACKUP / SWAPPED / HEALTH / ACCEPT / ENABLED / SWAP-DONE) so a future
automation can watch a swap without reading prose.

Keep this file 100% ASCII. PowerShell 5.1 mis-decodes a UTF-8-no-BOM .ps1 with
Chinese comments and dies on a syntax error - production then silently keeps
serving the OLD build for nine days because every later step never ran. Also
why the watchdog task is found by its ACTION instead of by name: the task name
is Chinese and cannot be hardcoded in an ASCII file.
#>
[CmdletBinding()]
param(
    # repo root; defaults to the parent of deploy\ so the script travels with
    # the repo and pins no machine-specific absolute path
    [string] $ProjectRoot = '',

    # the tag this build must report, e.g. v0.29.1 (same string as
    # git describe --tags --abbrev=0 that was baked into version.txt at build)
    [string] $ExpectedVersion = '',

    # a string only the NEW release's app.js contains (e.g. msg-trace);
    # acceptance is "the new code is what is served", not "something answers"
    [string] $Marker = '',

    # staged build from pyinstaller --distpath dist_new (NEVER build straight
    # into dist\run_backend: PyInstaller deletes the destination first, and a
    # failed build leaves production as an empty directory)
    [string] $StageDir = '',

    # backend listen port, same number the watchdog and start-ai-stack use
    [int] $Port = 8000,

    # run every precheck and print the plan; touches no process and no file
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------- path resolve
if (-not $ProjectRoot) {
    if (-not $PSScriptRoot) {
        throw 'cannot derive project root: invoke via -File or pass -ProjectRoot explicitly'
    }
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
if (-not $StageDir) { $StageDir = Join-Path $ProjectRoot 'dist_new\run_backend' }
if (-not $ExpectedVersion) { throw '-ExpectedVersion required, e.g. v0.29.1' }
if (-not $Marker) { throw '-Marker required: a string only the NEW app.js contains, e.g. msg-trace' }

$Live       = Join-Path $ProjectRoot 'dist\run_backend'
$LiveExe    = Join-Path $Live 'run_backend.exe'
$StageExe   = Join-Path $StageDir 'run_backend.exe'
$StageAppJs = Join-Path $StageDir '_internal\app\web\static\app.js'
$StageStamp = Join-Path $StageDir '_internal\version.txt'
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$Backup = Join-Path $ProjectRoot ('dist\run_backend_old-' + $stamp)

$script:backupDone = $false
$script:failures   = New-Object System.Collections.ArrayList

function Add-Fail {
    param([string] $Line)
    # direct ArrayList mutation, never "return an array through the pipeline":
    # watchdog.ps1 documents a scalar-array flattening bug of the same shape.
    [void] $script:failures.Add($Line)
}

function Get-LiveProcess {
    # match by FULL exe path (same rule as watchdog.ps1 Get-BackendProcess):
    # a same-named run_backend.exe elsewhere must never be mistaken for the one
    # we are about to stop, and vice versa. .ProcessId is correct HERE because
    # this is a Win32_Process CIM object; on a Get-Process object the property
    # is .Id and writing .ProcessId there silently stops nothing (guide).
    @(Get-CimInstance Win32_Process -Filter "Name = 'run_backend.exe'" |
        Where-Object { $_.ExecutablePath -and
            ($_.ExecutablePath.TrimEnd('\') -ieq $LiveExe.TrimEnd('\')) })
}

function Show-Rollback {
    # printed, never auto-run: after the live dir has been moved aside, a wrong
    # automatic un-rename under a file lock converts one half-swap into two.
    Write-Output ('ROLLBACK backup-package=' + $Backup)
    Write-Output 'ROLLBACK-HOWTO 1) keep the watchdog DISABLED until the old package is proven healthy again'
    $stopCmd = "Get-CimInstance Win32_Process -Filter ""Name = 'run_backend.exe'"" | Where-Object { `$_.ExecutablePath -eq '$LiveExe' } | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }"
    Write-Output 'ROLLBACK-HOWTO 2) stop the NEW backend by full exe path:'
    Write-Output ('    ' + $stopCmd)
    Write-Output 'ROLLBACK-HOWTO 3) move the half-live package aside (keep it as evidence, never delete it):'
    Write-Output ("    Move-Item -LiteralPath '$Live' -Destination '$Live-failed-$stamp'")
    Write-Output 'ROLLBACK-HOWTO 4) move the backup package back into place:'
    Write-Output ("    Move-Item -LiteralPath '$Backup' -Destination '$Live'")
    Write-Output 'ROLLBACK-HOWTO 5) start the old package and wait until /health answers with the OLD build:'
    Write-Output ("    Start-Process -FilePath '$LiveExe' -WorkingDirectory '$ProjectRoot' -WindowStyle Hidden")
    Write-Output 'ROLLBACK-HOWTO 6) only now re-enable the watchdog task (find it by its action, as this script did)'
    Write-Output 'ROLLBACK-HOWTO ref: deployment guide, swap section - the same steps, in reverse order.'
}

function Stop-AfterFailure {
    param([string] $FailedAt, [string] $Message)
    if (-not $script:backupDone) {
        # before the backup rename nothing on disk was touched: restarting the
        # intact package and re-enabling the watchdog is a complete recovery.
        Write-Output ('ABORT stage=' + $FailedAt + ' moved=no (package never disturbed; restoring watchdog)')
        if ((Get-LiveProcess).Count -eq 0 -and (Test-Path -LiteralPath $LiveExe)) {
            # do not double-start: two backends would write the same data\ files
            Start-Process -FilePath $LiveExe -WorkingDirectory $ProjectRoot -WindowStyle Hidden
            Write-Output 'RECOVERED old-package-restarted'
        }
        Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
        Write-Output 'RECOVERED watchdog-re-enabled'
        throw $Message
    }
    Write-Output ('FAIL stage=' + $FailedAt)
    Show-Rollback
    throw $Message
}

# ------------------------------------------- step 0: preflight, ALL before any stop
# Every check below exists because its failure was once discovered AFTER
# production had already been stopped. Being unable to swap in a bad package is
# survivable; being unable to swap in the good one is the incident.
if (-not (Test-Path -LiteralPath $StageExe)) {
    Add-Fail ('PRECHECK fail stage-exe=missing path=' + $StageExe)
}

$stageMarkerLines = 0
if (-not (Test-Path -LiteralPath $StageAppJs)) {
    Add-Fail ('PRECHECK fail stage-appjs=missing path=' + $StageAppJs)
} else {
    $stageMarkerLines = @(Select-String -LiteralPath $StageAppJs -Pattern $Marker -SimpleMatch).Count
    if ($stageMarkerLines -lt 1) {
        Add-Fail ('PRECHECK fail stage-appjs-marker=absent expected=' + $Marker)
    }
}

$stageVersion = ''
if (-not (Test-Path -LiteralPath $StageStamp)) {
    Add-Fail ('PRECHECK fail stage-version-file=missing path=' + $StageStamp)
} else {
    # the frozen exe reports this stamp on /health; a stamp that is not the
    # target tag means the swap would "succeed" while serving a different build
    # (v0.24.1-as-v0.24.0, fixed in commit 4d39daa9).
    $stageVersion = (Get-Content -LiteralPath $StageStamp -Raw).Trim()
    if ($stageVersion -ne $ExpectedVersion) {
        Add-Fail ('PRECHECK fail stage-version=' + $stageVersion + ' expected=' + $ExpectedVersion)
    }
}

if (-not (Test-Path -LiteralPath $LiveExe)) {
    Add-Fail ('PRECHECK fail live-exe=missing path=' + $LiveExe + ' (no rollback target exists, this is not a swap)')
}

foreach ($poison in @('.env', 'data', 'chroma_db')) {
    if (Test-Path -LiteralPath (Join-Path $StageDir $poison)) {
        # 2026-09-16 data loss: dist\run_backend is deleted on every rebuild,
        # runtime data baked into the package dies with the next swap.
        Add-Fail ('PRECHECK fail stage-contaminant=' + $poison)
    }
}

if ($script:failures.Count -gt 0) {
    foreach ($f in $script:failures) { Write-Output $f }
    if ($DryRun) {
        Write-Output ('DRYRUN precheck-failures=' + $script:failures.Count)
        exit 1
    }
    throw ('pre-flight failed (' + $script:failures.Count +
           ' problems) - nothing was stopped, production is untouched')
}
Write-Output ('PRECHECK ok stage-version=' + $stageVersion + ' marker-lines=' + $stageMarkerLines)

# --------------------------------- step 1: find the watchdog task and its state
# Located by ACTION (wscript.exe + watchdog-hidden.vbs), NOT by name: the task
# name is Chinese, and an ASCII-only script cannot hardcode it. A later rename
# of the task or a re-encoding of this file then silently disables the disable
# step - which is exactly how the watchdog re-launched an exe mid-swap once.
$task = @(Get-ScheduledTask | Where-Object {
    $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
})
if ($task.Count -ne 1) {
    throw ('watchdog task not uniquely found by its action (count=' + $task.Count +
           '); if the task is gone on purpose, fix this script - do not skip this check')
}
$task = $task[0]
Write-Output ('TASK name-len=' + $task.TaskName.Length + ' state=' + $task.State)

if ($DryRun) {
    Write-Output ('DRYRUN would-disable task-state=' + $task.State)
    foreach ($p in (Get-LiveProcess)) { Write-Output ('DRYRUN would-stop pid=' + $p.ProcessId) }
    Write-Output ('DRYRUN would-check-port port=' + $Port)
    Write-Output ('DRYRUN would-backup ' + $Live + ' -> ' + $Backup)
    Write-Output ('DRYRUN would-swap-in ' + $StageDir + ' -> ' + $Live)
    Write-Output ('DRYRUN would-start ' + $LiveExe)
    Write-Output ('DRYRUN would-accept health-build=' + $ExpectedVersion + ' appjs-marker=' + $Marker)
    Write-Output 'DRYRUN ok (no process was stopped, no file was moved)'
    exit 0
}

# DISABLE comes here, after the preflight and BEFORE the first stop/rename:
# the task only ever STARTS the exe and never kills it, so a wake between the
# stop and the rename relaunches the old package, its file handle makes the
# rename die with PermissionError, and the box ends up "swapped halfway".
Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
$afterState = (Get-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName).State
Write-Output ('DISABLED state=' + $afterState)
if ($afterState -ne 'Disabled') {
    throw 'watchdog did not actually disable - aborting BEFORE touching the running package'
}

# ------------------------------------------------------- step 2: stop the backend
$procs = Get-LiveProcess
$stoppedPids = @($procs | ForEach-Object { $_.ProcessId })
foreach ($p in $procs) {
    Write-Output ('STOP pid=' + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force
}
Write-Output ('STOPPED pid=' + $(if ($stoppedPids.Count -gt 0) { $stoppedPids -join ',' } else { 'none' }))
Start-Sleep -Seconds 3

# --------------------------------------------- step 3: confirm the port let go
# Warn, never silently continue: if something still listens on the port it is a
# live handle on the directory we are about to move, and we would rather learn
# it HERE than as a half-completed rename.
$stillListening = @(netstat -ano | Select-String -Pattern (':{0}\s' -f $Port) | Select-String -Pattern 'LISTENING')
if ($stillListening.Count -gt 0) {
    Write-Output ('WARN port-' + $Port + '-still-listening count=' + $stillListening.Count)
    $stillListening | ForEach-Object { Write-Output ('  ' + $_.Line.Trim()) }
} else {
    Write-Output ('PORT state=clear port=' + $Port)
}
Start-Sleep -Seconds 1

# ------------------------------------- step 4/5: rename the old aside, then swap
# Move-Item throughout, never Rename-Item: Rename-Item's -NewName takes only a
# LEAF name; given a full target path it does not raise, it does nothing, and
# the script cheerfully prints success (documented 2026-09-20 morning incident
# - two packages still in their original places, operator told the swap worked).
# The old package is renamed, NEVER deleted: rollback depends on it staying on
# disk, which is exactly why the backup gets a timestamped name (no overwrite).
try {
    Move-Item -LiteralPath $Live -Destination $Backup
} catch {
    Stop-AfterFailure 'backup-rename' $_.Exception.Message
}
$script:backupDone = $true   # from here on, failures print the recipe, no auto-undo
Write-Output ('BACKUP ' + $Backup)

try {
    Move-Item -LiteralPath $StageDir -Destination $Live
} catch {
    Stop-AfterFailure 'stage-move' $_.Exception.Message
}
if (-not (Test-Path -LiteralPath $LiveExe)) {
    Stop-AfterFailure 'stage-move' ('no exe at ' + $LiveExe + ' after the swap')
}
Write-Output 'SWAPPED'

# ------------------------------------------------------ step 6: start the new one
# WorkingDirectory = project root, same shape as watchdog.ps1 and
# start-ai-stack.bat: run_backend resolves data\ against the working directory,
# and launching it from elsewhere strands fresh runtime data inside the package
# (the same 2026-09-16 data-loss shape, one swap later).
try {
    Start-Process -FilePath $LiveExe -WorkingDirectory $ProjectRoot -WindowStyle Hidden
} catch {
    Stop-AfterFailure 'start' $_.Exception.Message
}
Start-Sleep -Seconds 12
$running = Get-LiveProcess
if ($running.Count -lt 1) {
    Stop-AfterFailure 'started-check' ('no process at ' + $LiveExe + ' twelve seconds after start')
}
Write-Output ('STARTED pid=' + (@($running)[0]).ProcessId)

# ------------------------------------- step 7: acceptance - the NEW code answers
# Not "is the port up": the 2026-09-19 first failed swap kept /health answering
# 200 the whole time - with the OLD package. Acceptance is two positive checks:
# the served build stamp equals the target tag, and the served app.js carries
# this release's marker string.
$h = $null
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    try {
        $h = Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/health' -f $Port) -TimeoutSec 5
        if ($h -and $h.build) { break }
    } catch { }
    Start-Sleep -Milliseconds 800
}
if (-not $h -or -not $h.build) {
    Stop-AfterFailure 'health' ('/health gave no build field within 30s on port ' + $Port)
}
if ($h.build -ne $ExpectedVersion) {
    Stop-AfterFailure 'health-build' ('served build=' + $h.build + ' expected=' + $ExpectedVersion)
}
Write-Output ('HEALTH build=' + $h.build)

$js = $null
try {
    $js = Invoke-WebRequest -Uri ('http://127.0.0.1:{0}/app/app.js' -f $Port) -TimeoutSec 10 -UseBasicParsing
} catch {
    Stop-AfterFailure 'accept-fetch' ('could not fetch /app/app.js: ' + $_.Exception.Message)
}
$markerLines = @(($js.Content -split "`n") | Where-Object { $_ -like ('*' + $Marker + '*') }).Count
if ($markerLines -lt 1) {
    Stop-AfterFailure 'accept-marker' ('served app.js contains no ' + $Marker + ' - still running the old package')
}
Write-Output ('ACCEPT marker-lines=' + $markerLines)

# --------------------------------- step 8: watchdog back ON - only now, AFTER the
# health and marker assertions above. Enabling it earlier would let it "protect"
# a half-baked or wrong-build package; the task staying disabled through the
# whole acceptance window is the point of ordering it last.
try {
    Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
} catch {
    Stop-AfterFailure 'enable' ('swap is verified but re-enabling the task failed: ' + $_.Exception.Message)
}
$finalState = (Get-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName).State
Write-Output ('ENABLED state=' + $finalState)
Write-Output ('REMINDER if this swap window logged failed launches, clear the "backend" restart counter in ' +
              (Join-Path $ProjectRoot 'data\watchdog-state.json') + ' so the watchdog does not inherit a spent budget')
Write-Output 'SWAP-DONE'
