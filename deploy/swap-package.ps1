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

-Marker must DISCRIMINATE, and since v0.34.2 the precheck proves that on BOTH
sides: the staged app.js must contain it AND the installed app.js must not.
Counting only the staging side was not a check, it was a ritual - twice in
production a backend-only swap passed with a string that lived on both sides
(/changelog: 2 hits staging, 2 hits live), so "the marker answers" proved only
that nothing had changed. A vacuous green is worse than no gate: it tells the
operator the acceptance ran.
For a backend-only release NO discriminating marker exists unless that release
itself introduced one in app.js. That is a fact about the release, not about
this script, and the precheck now says so out loud instead of letting it slide.

The one legitimate case with no forward marker is moving BACKWARD to an older
package: -Rollback. It is not reachable by accident - it requires
-RollbackEvidence (a non-empty one-line reason, printed verbatim so the swap
record carries the human claim) and it inverts both gates: the marker must then
name the build being RETIRED, so it must be present in the CURRENTLY INSTALLED
app.js and absent from the package coming in, and after the swap the served
app.js must stop answering it.

Exit shape: 0 on SWAP-DONE; non-zero either with production UNTOUCHED (the
abort happens before anything was moved and this script puts things back) or
with an explicit ROLLBACK recipe printing the real backup path (any failure
after the live directory was moved aside - an auto-rollback there can turn one
half-swap into two).

Progress lines are one-token machine-parsable (PRECHECK / DISABLED / STOPPED /
BACKUP / SWAPPED / HEALTH / ACCEPT / ENABLED / BUDGET / SWAP-DONE, plus
ROLLBACK-MODE when -Rollback was used) so a future
automation can watch a swap without reading prose.
Since the v0.34.2 abort fixes three more tokens carry the failure side, because
an abort that prints nothing is how the crash guard got lost: DISABLED now
reports enabled= / via= / state= / instances= (the verdict is enabled=, which is
read from the task's Settings.Enabled because the task object has no Enabled
member on this machine; via= names which read answered or failed, so an
enabled=True from a switch that never replied is distinguishable from a guard
that is genuinely still up; state=Running is
a mid-flight instance, visible rather than fatal), WAIT marks the bounded wait
for that instance to finish, READFAIL marks a read that could not answer and stage= names
which one: task-state (fails CLOSED - it waits and then aborts instead of assuming the guard
is down) or boot-poll (the process-list read inside the boot window - it asks again on that
loop's own cadence, bounded by the window the loop already had, and gives up through
ABORT stage=boot-poll-threw) or final-enabled-read (the confirmation read right after the
re-enable - it does not abort, it degrades into the ENABLED line below),
DISABLE-THREW marks the disable call itself throwing: it prints those same four
fields plus error=, and the stage token right after it says which of the three
answers the switch actually gave - disable-threw-task-down (the guard really did
go off, so this run owns it and hands it back), disable-threw-task-up (nothing
was flipped, so no disable is owned), disable-threw-guard-state-unproven (the
switch did not answer at all - the fail-safe, which attempts the restore anyway
because a guard left down is worse than one re-enabled for nothing),
and every abort ends with ABORT stage=... moved=... watchdog=... stating in one
token whether the guard was handed back. An abort past the backup rename is a
different animal and the tokens say so: moved=yes with
watchdog=left-disabled-by-design plus the printed ROLLBACK recipe, because a
watchdog re-armed over a package nobody has accepted yet is not protection - it
is a guardian for the thing under suspicion.

THE POST-ACCEPTANCE TAIL DEGRADES INTO TOKENS INSTEAD OF DYING, and that is a rule, not a
preference: past ACCEPT there is no failure allowed to leave the run token-less, because
the swap has already been accepted and re-rolling it is not on the table. Two sites carry
it. BUDGET state=read-threw / BUDGET state=write-threw is the restart-budget ledger at the
last step - data\watchdog-state.json, which deploy\watchdog.ps1 rewrites on every patrol
(once a minute), so colliding with a mid-write is a normal outcome, not an exotic one. The
line prints budget-returned=false, meaning this run did NOT hand the credits back: a crash
inside the next hour can meet the already-spent budget and watchdog.ps1's own fuse
(Get-LaunchCount reaching -MaxRestartsPerHour) returns WITHOUT launching - unsupervised.
backend-entries= on the same line tells the operator whether the failed step owed anything
(0 = the ledger held no backend key to clear, unproven = the read never answered at all).
The fix is that one key in that file, by hand, or letting the hour roll; nothing here
retries the write forever and nothing here deletes or truncates live state.
ENABLED state=Unreadable is the same rule on the re-enable's confirmation read, with the
READFAIL stage=final-enabled-read line above it naming which read went silent. Neither
site calls Stop-AfterFailure, and that is on the record because the next person will want
to "unify" it with the aborts above: Stop-AfterFailure's post-backupDone branch prints the
literal watchdog=left-disabled-by-design plus a full ROLLBACK recipe, and by then both
would be FALSE - the guard is back up and the package has passed HEALTH build= and ACCEPT
marker-lines=. Aborting there would tell an operator to tear down a healthy, supervised
service over a bookkeeping file. So the tail says what it knows and finishes at SWAP-DONE.
backend/tests/test_swap_package_post_acceptance_tokens.py locks both routes by making this
machine answer, not by matching text.

THE ROLLBACK PATH CANNOT BE ALLOWED TO START BLIND - THE RUNTIME DATA IS SNAPSHOT FIRST (#42).
v0.35 parks a task by writing status "waiting_user" plus a resume_cursor into data\tasks.json.
The ALREADY SHIPPED v0.34.2 package restores that file through TaskStatus(data.get("status",
...)): in its own blob (git show v0.34.2:backend/app/agents/task_store.py) that call sits at
:101, and TaskStatus there has exactly five members (pending / running / completed / failed /
cancelled), so the literal raises ValueError. The per-record handler at :206-207 prints
"skipping one unreadable task record" and drops that record from MEMORY, and the flush at
:216-217 - or any later write, since :156-160 dumps the whole dict - rewrites the file FROM
MEMORY. A v0.35 -> v0.34.2 rollback therefore does not "hide the parked tasks in the old UI",
it ERASES them together with their breakpoints, and the shipped package's code is exactly the
thing that cannot be edited. Hence DATA-SAVED: before this run can let any package start, it
copies data\tasks.json and data\approvals.json into data\swap-snapshots\<stamp>\ and prints
where. Three asymmetries carry the design and each is argued at its own site: (1) the snapshot
runs in BOTH modes, not only rollback - a forward swap to v0.35 is what CREATES the records the
next rollback would eat, and copying a 19-byte file costs nothing; (2) a failed COPY aborts
(DATA-SAVE-FAIL, then a pre-flight-style throw while nothing has been stopped and no guard is
held down) while a failed COUNT only degrades to waiting_user=unproven - the copy is the only
thing that makes recovery possible, a missing number is not. That is the mirror image of the
degrade rule above: back there the swap had already succeeded, here the run is still free to
refuse; (3) the copies go under data\ and never dist\ - dist\run_backend_old-* is precisely
what a "delete the build garbage" pass eats, while data\ is declared a runtime asset that no
cleanup may touch, so a copy meant to outlive the rollback package must sit where the cleaner
does not go. The copies are copies: this step never moves or deletes an original, and a file it
cannot read is reported unreadable rather than as zero records ("cannot read" and "read zero"
are different verdicts, same rule as backend-entries=unproven above).
backend/tests/test_swap_package_data_snapshot.py runs the WHOLE script against a fake
project root with Start-Process / Stop-Process / the scheduled-task cmdlets stubbed to a log,
and requires DATA-SAVED on the observed output BEFORE the first would-start entry - the
ordering is proven by what this machine did, not by where the line sits.

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
    # acceptance is "the new code is what is served", not "something answers".
    # The precheck now rejects a string the INSTALLED package already contains,
    # because then the acceptance below cannot tell the two packages apart.
    [string] $Marker = '',

    # staged build from pyinstaller --distpath dist_new (NEVER build straight
    # into dist\run_backend: PyInstaller deletes the destination first, and a
    # failed build leaves production as an empty directory)
    [string] $StageDir = '',

    # backend listen port, same number the watchdog and start-ai-stack use
    [int] $Port = 8000,

    # how long to wait for the frozen backend to appear AND to answer /health;
    # a cold start with chromadb plus the provider probe is not a two-second job
    [int] $StartTimeoutSeconds = 90,

    # keep the watchdog restart counter exactly as this swap window left it.
    # Default is OFF (i.e. the budget IS handed back), because the whole point of
    # clearing it is that the next real crash must not meet a spent allowance.
    # Set this only when the spent budget is itself evidence you want to read.
    [switch] $KeepBackendBudget,

    # run every precheck and print the plan; touches no process and no file
    [switch] $DryRun,

    # backward swap: put an OLDER package back into the live slot. With this
    # switch -Marker names the build being RETIRED instead of the build coming
    # in, so both marker gates flip (see the header). It does nothing on its
    # own without -RollbackEvidence, and it never relaxes the stage-version or
    # contamination gates - those are about the package, not about direction.
    [switch] $Rollback,

    # one line, non-empty, required by -Rollback: why a backward swap is
    # intended here (incident id, spent budget, bad build). Printed verbatim so
    # the swap record carries the human claim next to the machine verdict.
    [string] $RollbackEvidence = ''
)

$ErrorActionPreference = 'Stop'

# Relative inputs are made absolute HERE, before anything derives from them.
# Why this exists: the same-volume precheck compares [IO.Path]::GetPathRoot() of
# the stage path with the live path, and GetPathRoot returns an EMPTY string for a
# relative path. So a perfectly normal "dist_new\run_backend" typed from the repo
# root produced a FAKE 'stage-volume stage= live=C:\' failure. A fake failure on a
# swap day is worse than no check at all - it trains the operator to distrust
# prechecks and to reach for --force-style workarounds.
# [Environment]::CurrentDirectory is deliberately NOT used: in PowerShell it can
# disagree with the session location, and the location the operator is standing in
# is the one the typed relative path refers to.
function ConvertTo-AbsolutePath {
    param([string] $Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Value))
}

# --------------------------------------------------------------- path resolve
if (-not $ProjectRoot) {
    if (-not $PSScriptRoot) {
        throw 'cannot derive project root: invoke via -File or pass -ProjectRoot explicitly'
    }
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = ConvertTo-AbsolutePath -Value $ProjectRoot
if (-not $StageDir) { $StageDir = Join-Path $ProjectRoot 'dist_new\run_backend' }
# normalise AFTER the default is built (the default is already absolute; an
# operator-typed value may not be) and BEFORE $StageExe/$StageAppJs/$StageStamp
# are joined off it, so the version precheck and the volume precheck read the
# same directory the rename step will move.
$StageDir = ConvertTo-AbsolutePath -Value $StageDir
if (-not $ExpectedVersion) { throw '-ExpectedVersion required, e.g. v0.29.1' }
if (-not $Marker) { throw '-Marker required: a string only the NEW app.js contains, e.g. msg-trace' }
if ($Rollback -and -not $RollbackEvidence.Trim()) {
    throw '-Rollback requires -RollbackEvidence "<one line: why an older package goes live now>" - backward swaps are rare enough that the reason must be stated, not implied'
}
if ($Rollback) {
    Write-Output ('ROLLBACK-MODE marker-identifies=build-being-retired stage-must-NOT-contain=' + $Marker + ' live-must-contain=' + $Marker)
    Write-Output ('ROLLBACK-MODE evidence=' + $RollbackEvidence.Trim())
}

$Live       = Join-Path $ProjectRoot 'dist\run_backend'
$LiveExe    = Join-Path $Live 'run_backend.exe'
$LiveAppJs  = Join-Path $Live '_internal\app\web\static\app.js'
$StageExe   = Join-Path $StageDir 'run_backend.exe'
$StageAppJs = Join-Path $StageDir '_internal\app\web\static\app.js'
$StageStamp = Join-Path $StageDir '_internal\version.txt'
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$Backup = Join-Path $ProjectRoot ('dist\run_backend_old-' + $stamp)
# The two runtime data files an older package can eat (#42), and where this run keeps its
# copies. Under data\ and NOT under dist\: dist\run_backend_old-* is exactly the shape a
# "delete the build garbage" pass removes, while data\ is the directory the machine's own
# AGENTS.md declares a runtime asset that no cleanup may touch. A copy whose whole job is to
# outlive a rollback has to sit where the cleaner does not go; and no code path globs data\ -
# both files are opened by their exact names (task_store.py _default_path, approval.py
# _default_path), so a snapshot subdirectory is inert to the running service.
$TasksJson       = Join-Path $ProjectRoot 'data\tasks.json'
$ApprovalsJson   = Join-Path $ProjectRoot 'data\approvals.json'
$DataSnapshotDir = Join-Path $ProjectRoot ('data\swap-snapshots\' + $stamp)

$script:backupDone = $false
# Whether THIS run currently holds the watchdog down. The restore machinery keys
# off it so an abort can say in one token whether it actually gave the guard
# back, and so a failure raised before any disable never claims to have re-enabled
# something it never touched.
$script:watchdogDisabled = $false
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

function Invoke-PackageMove {
    # Move a WHOLE package directory: one rename, never a child-by-child copy.
    #
    # Why not Move-Item (measured 2026-10-03 22:42, first v0.29.2 swap attempt):
    # the backend had been stopped and the port was free, yet the rename still
    # failed on a handle the OS had not finished tearing down. Move-Item does not
    # simply raise in that case - the FileSystem provider creates the destination
    # and starts moving children, then dies partway. That run left an empty
    # dist\run_backend_old-20261003-224259 sitting next to an intact source, so
    # "which directory is the rollback package?" briefly had two answers.
    # [System.IO.Directory]::Move is a single rename: it moves everything or
    # nothing, and it refuses to move ONTO an existing directory - which is also
    # why the printed rollback recipe below uses it: Move-Item given a destination
    # that exists does not raise, it nests the backup INSIDE the live package
    # (hit for real on 2026-10-03 while hand-following this script's own recipe).
    # The short retry loop exists because a lingering handle is a two-second
    # condition, not a decision to abort a production swap.
    param([string] $From, [string] $To, [int] $TrySeconds = 20)
    $giveUpAt = (Get-Date).AddSeconds($TrySeconds)
    while ($true) {
        try {
            [System.IO.Directory]::Move($From, $To)
            return
        } catch {
            if ((Get-Date) -ge $giveUpAt) { throw }
            Start-Sleep -Milliseconds 1000
        }
    }
}

function Get-HealthOnce {
    # One non-blocking /health probe. Anything that can go wrong (no listener
    # yet, half-up service, TLS/proxy noise) comes back as $null: the caller
    # decides what silence means, this function never throws.
    try {
        return Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/health' -f $Port) -TimeoutSec 5
    } catch {
        return $null
    }
}

function Clear-BackendBudget {
    # Give the BACKEND its restart allowance back at the end of a swap window.
    #
    # Why this is a step and not a printed reminder: the watchdog keeps a
    # rolling budget (MaxRestartsPerHour, default 3) in data\watchdog-state.json.
    # The swap window itself is a period where launches legitimately fail - the
    # old exe is gone while the new one is still booting, and until v0.33.1 the
    # tunnel probe could not even recognise the tunnel it launched, so it fired
    # its own relaunches. If those consume the counter, the NEXT real crash -
    # the one someone actually needs rescued - meets a spent budget and the
    # watchdog logs "budget exhausted" instead of starting the service. The
    # operator then re-enables a guardian that is already out of ammo.
    #
    # Scope is deliberately the smallest thing that is safe: remove ONLY the
    # 'backend' key. Every other entry (tunnel records, the seen:* throttle
    # markers) is preserved by key and value. Touching them would hide a
    # pre-existing condition, and the seen:* markers are what keep the log from
    # repeating the same warning every minute. "Preserved" is NOT "byte-for-byte":
    # the whole document is re-serialized below, so ConvertTo-Json may normalize
    # formatting and escapes (non-ASCII comes back as \uXXXX - see the note at the
    # write) while no key or value changes meaning. Do not compare the ledger
    # byte-for-byte after a swap.
    #
    # Shape constraints, each learned the hard way in this repo:
    #   - BOM preserved: watchdog.ps1 writes this file with Set-Content -Encoding
    #     UTF8, which under PowerShell 5.1 produces a UTF-8 BOM, and it reads it
    #     back the same way. Writing UTF-8-without-BOM from here would make the
    #     next Read-State see a leading U+FEFF inside the first key name.
    #   - never Remove-Item: the whole no-delete rule of this script applies to
    #     every file it touches, and the watchdog may be mid-write.
    #   - writes with [System.IO.File]::WriteAllText, one shot, so an open that
    #     fails never gets as far as truncating (measured below, not assumed).
    param([string] $StatePath)
    if (-not (Test-Path -LiteralPath $StatePath)) {
        Write-Output 'BUDGET state=absent (nothing to clear)'
        return
    }
    # WHY THE TWO FILE CALLS BELOW ARE WRAPPED, AND WHY NEITHER ONE ABORTS (#47).
    #
    # This function is called BARE, at the very tail of the run: after ACCEPT, after
    # the re-enable, one line before SWAP-DONE. Before this wrap a throw here killed
    # the script with NO BUDGET token, NO SWAP-DONE and the watchdog's restart
    # budget still spent - the loudest possible silence in the worst possible place,
    # because the swap itself had already succeeded and been guarded. The file's own
    # comment above says the watchdog may be mid-write to this exact path, and that
    # is a measurement, not a hedge: deploy/watchdog.ps1 calls Save-State at :585 on
    # EVERY patrol, the patrol runs once a minute (register-watchdog.ps1:145
    # RepetitionInterval=1min), and the live ledger's mtime here was sampled at
    # 09:12:05 -> 09:13:05 -> 09:14:05 (delta exactly 60 s, 799 bytes unchanged) while
    # this ticket was being written. So a sharing violation here is a NORMAL outcome,
    # not an exotic one, and the odds of hitting it rise with every second the swap
    # takes. Measured here 2026-10-17 on PS 5.1.26100.9549, against hand-built fake
    # paths (never the live ledger) - the exception CHAINS are what justify two wraps
    # instead of one, since the two halves fail as different types:
    #   ReadAllBytes, file held by another handle (FileShare=None) ->
    #     MethodInvocationException / IOException  "... being used by another process";
    #   ReadAllBytes on a directory -> MethodInvocationException / UnauthorizedAccessException;
    #   ReadAllBytes on a read-only-attribute file -> ANSWERS, no throw (15 bytes read);
    #   WriteAllText on that same read-only file -> MethodInvocationException /
    #     UnauthorizedAccessException, and the fake file came back byte-for-byte
    #     (measured unchanged=True) because the throw was at the open, not after it;
    #   WriteAllText into a directory that does not exist -> DirectoryNotFoundException.
    # Note the third and fourth lines together: the read half and the write half fail in
    # DIFFERENT ways, so one state= value covering both would describe an event that did
    # not happen. That is the whole reason for two wraps.
    #
    # WHY DEGRADE AND NOT ABORT, and why this is NOT the same animal as a pre-swap
    # failure: at this point the package is accepted (HEALTH build= and ACCEPT
    # marker-lines= both passed) and the crash guard is already back up. Aborting
    # would go through Stop-AfterFailure, whose post-backupDone branch prints a full
    # ROLLBACK recipe over a service that is healthy and supervised - the wrong thing
    # to tell an operator at 3 a.m., and precisely the judgment #45's reviewer made
    # about the phase after backupDone. So the token says the bookkeeping failed, and
    # the run goes on to SWAP-DONE. Someone will want to "unify" this with the aborts
    # above; this paragraph is the argument against, kept here rather than in a doc.
    #
    # WHY THE TOKEN CLAIMS ONLY budget-returned=false: a throw at the open leaves the
    # previous file byte-for-byte (measured above), but a throw AFTER the open would
    # have truncated first - and nothing here can tell the two apart from the
    # exception alone. So the line refuses to claim the ledger is intact. It states
    # the one thing this run knows about itself: the budget was NOT handed back. What
    # that costs is spelled out at :250-258 - the next real crash can meet a spent
    # budget and the watchdog logs "budget exhausted" instead of starting the service
    # (deploy/watchdog.ps1:442 returns without launching once Get-LaunchCount reaches
    # -MaxRestartsPerHour), i.e. a crash right after the swap can go unsupervised.
    # backend-entries= is what makes the line actionable: unproven on the read side
    # (nothing was parsed yet), and on the write side the count this run found in the
    # ledger and could not take out - 0 means the failed write owed nothing, because
    # the budget was already unspent (the live file today carries no backend key at
    # all, measured 2026-10-17). The fix is a manual edit of that one key, or letting
    # the hour roll - re-running the swap with -KeepBackendBudget would NOT help, that
    # switch keeps a spent ledger on purpose, it does not hand credits back.
    # The three early exits (state=absent just above; state=empty-file /
    # state=unparsable below) deliberately omit budget-returned=: none of them ever
    # owed a return. absent has no ledger to clear; empty-file has no keys in which
    # a backend entry could live; unparsable refuses to rewrite a file it could not
    # parse, so all three say "skipped" instead of borrowing the failure grammar.
    # Only the two catch handlers, which did attempt a read/write, carry
    # budget-returned=false together with backend-entries=.
    # Do not "fix" a throw on this path by retrying the write in a loop: this is
    # bookkeeping, and a bounded retry that also fails would still need this token.
    $bytes = $null
    try {
        $bytes = [System.IO.File]::ReadAllBytes($StatePath)
    } catch {
        # Captured into a named variable before the token is built: the same shape
        # the disable handler uses. The order is a cheap convenience, not a
        # guarantee - measured on PS 5.1, $_ survives a nested catch - so do not
        # cite this line as the reason the printed message is correct (#46).
        $budgetReadError = $_.Exception.Message
        Write-Output ('BUDGET state=read-threw budget-returned=false backend-entries=unproven error=' + $budgetReadError)
        return
    }
    $hadBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($hadBom) { $text = $text.TrimStart([char]0xFEFF) }
    if (-not $text.Trim()) {
        # An empty or whitespace-only file is what a killed mid-write leaves
        # behind. Do NOT invent a "{}" and overwrite it: that would erase real
        # budget history for a file we cannot prove is ours to rewrite.
        Write-Output 'BUDGET state=empty-file skipped (not overwritten)'
        return
    }
    try {
        $state = $text | ConvertFrom-Json
    } catch {
        Write-Output ('BUDGET state=unparsable skipped: ' + $_.Exception.Message)
        return
    }
    # ConvertFrom-Json gives a PSCustomObject; remove the one property by name.
    $had = $null
    if ($state.PSObject.Properties['backend']) {
        $had = @($state.backend).Count
        $state.PSObject.Properties.Remove('backend')
    }
    $json = $state | ConvertTo-Json -Depth 10
    # PS 5.1 escapes non-ASCII in ConvertTo-Json output as \uXXXX in some
    # positions; that is still valid JSON for the watchdog's reader, so it is
    # left alone rather than "fixed" with a regex that could corrupt values.
    $enc = New-Object System.Text.UTF8Encoding($hadBom)
    # ITS OWN try, separate from the read above (#47): the two halves fail for
    # different measured reasons - the read throws on a sharing violation or a
    # directory, the write throws on an attribute or an ACL - and one state= value
    # cannot name both without lying about which half ran. The write is the half that
    # COULD truncate, so this line claims nothing about the file's state either; see
    # the (#47) block at the top of this function for why.
    try {
        [System.IO.File]::WriteAllText($StatePath, $json, $enc)
    } catch {
        $budgetWriteError = $_.Exception.Message
        Write-Output ('BUDGET state=write-threw budget-returned=false backend-entries=' + $(if ($null -eq $had) { '0' } else { $had }) + ' error=' + $budgetWriteError)
        return
    }
    Write-Output ('BUDGET backend-entries-cleared=' + $(if ($null -eq $had) { '0' } else { $had }) +
                  ' bom=' + $hadBom + ' other-keys-kept=true')
}

function Get-TaskFileVerdict {
    # HOW MUCH IS AT RISK, read from the JSON (#42). This function exists so the operator can
    # see, before choosing to roll back, how many parked tasks and breakpoints the package
    # coming in is about to erase.
    #
    # WHY THE COUNT IS READ FROM THE FILE AND NOT BY RE-USING THE RESTORE CODE: the restore
    # logic that does the damage lives inside the frozen onedir package (its own TaskStatus
    # enum has no waiting_user member, which is the whole incident), so there is nothing here
    # to import, and re-implementing it in PowerShell would be a second ruler that drifts the
    # moment the state machine changes. Reading the JSON needs no semantics: "waiting_user" is
    # the persisted literal, written as .value by to_dict precisely so the file stays readable
    # across versions (task_store.py documents that str() would print "TaskStatus.PENDING").
    #
    # WHY -ceq AND NOT -eq: PowerShell's -eq is case-insensitive, and this count is a claim
    # about the exact literal the old enum rejects. A status stored as "WAITING_USER" is not a
    # parked task, it is a different verdict about a different file.
    #
    # "CANNOT READ" IS NOT "READ ZERO", the standing rule of this file (see
    # backend-entries=unproven in Clear-BackendBudget). Every non-ok state reports
    # unproven, never 0, because 0 is what tells the operator the rollback is safe.
    #
    # Returns ONE object and prints nothing, for the pipeline-capture reason documented in
    # Get-WatchdogTaskState: the caller assigns this result, and an assignment takes the WHOLE
    # output stream.
    param([string] $TasksPath)
    if (-not (Test-Path -LiteralPath $TasksPath)) {
        # no file at all is a read verdict, not a failure: nothing is parked, nothing to copy
        return [pscustomobject]@{ State = 'absent'; Records = 'absent'; WaitingUser = 'absent'; Detail = '' }
    }
    $text = $null
    try {
        $text = [System.IO.File]::ReadAllText($TasksPath)
    } catch {
        $verdictReadError = $_.Exception.Message
        return [pscustomobject]@{ State = 'unreadable'; Records = 'unproven'; WaitingUser = 'unproven'; Detail = $verdictReadError }
    }
    if ($null -eq $text -or -not $text.Trim()) {
        return [pscustomobject]@{ State = 'empty-file'; Records = 'unproven'; WaitingUser = 'unproven'; Detail = 'read answered no content' }
    }
    $data = $null
    try {
        $data = $text | ConvertFrom-Json
    } catch {
        $verdictParseError = $_.Exception.Message
        return [pscustomobject]@{ State = 'unparsable'; Records = 'unproven'; WaitingUser = 'unproven'; Detail = $verdictParseError }
    }
    if ($null -eq $data.tasks) {
        # Guarded BEFORE any counting, and that order is a measurement rather than a style
        # choice: @($null).Count answers 1 on this PowerShell, so counting through the wrap
        # would report one record that does not exist - the same flattening trap documented at
        # the boot poll. A file that parses but has no tasks object is a shape this script has
        # never seen, so it gets unproven, not a number.
        return [pscustomobject]@{ State = 'no-tasks-key'; Records = 'unproven'; WaitingUser = 'unproven'; Detail = 'json answers no tasks object' }
    }
    if ($data.tasks -is [array]) {
        return [pscustomobject]@{ State = 'tasks-not-object'; Records = 'unproven'; WaitingUser = 'unproven'; Detail = 'tasks key is an array, not the id-keyed object write_unlocked emits' }
    }
    $props = @($data.tasks.PSObject.Properties)
    $waiting = @($props | Where-Object { $_.Value.status -ceq 'waiting_user' })
    return [pscustomobject]@{ State = 'ok'; Records = [string] $props.Count; WaitingUser = [string] $waiting.Count; Detail = '' }
}

function Save-RuntimeDataSnapshot {
    # The copies that make a rollback reversible (#42). COPY ONLY - never a move, never a
    # rename, never a delete, of anything under data\: the originals are the files the service
    # that is still answering requests is reading and writing, and this step runs while it is
    # up. [System.IO.File]::Copy opens the source for read and writes a new path; the source
    # keeps its bytes, its mtime and its existence.
    #
    # Overwrite is $true and that is safe only because $DataSnapshotDir carries the swap stamp
    # (:202 Get-Date yyyyMMdd-HHmmss), so two runs never share a destination - the same
    # no-overwrite reasoning the backup directory uses for its name.
    #
    # WHY A FAILURE HERE ABORTS AND DOES NOT DEGRADE (the mirror image of #47's tail rule, and
    # the opposite ruling on purpose): Clear-BackendBudget degrades because by then the swap
    # has been ACCEPTED and aborting would print a rollback recipe over a healthy, guarded
    # service. Here nothing has succeeded yet, no package has moved, the crash guard has not
    # been touched - so the run is still free to refuse, and what it would be refusing to
    # permit is the irreversible half of this ticket: without a copy there is no recovery from
    # the erase documented in the header. The caller throws the pre-flight way; deliberately
    # NOT through Stop-AfterFailure, whose pre-backupDone branch calls Start-Process on
    # $LiveExe whenever the process list answers zero - i.e. the very thing being protected
    # against, started by the failure handler meant to prevent it.
    #
    # Returns one object, prints nothing (same pipeline-capture rule as the helper above).
    param([string] $Dir, [string] $TasksPath, [string] $ApprovalsPath)
    try {
        [void] [System.IO.Directory]::CreateDirectory($Dir)
    } catch {
        $snapshotDirError = $_.Exception.Message
        return [pscustomobject]@{ Ok = $false; Dir = $Dir; Tasks = 'failed'; Approvals = 'not-attempted'; Error = $snapshotDirError }
    }
    $savedTasks = 'absent'
    if (Test-Path -LiteralPath $TasksPath) {
        $destTasks = Join-Path $Dir 'tasks.json'
        try {
            [System.IO.File]::Copy($TasksPath, $destTasks, $true)
            $savedTasks = $destTasks
        } catch {
            $tasksCopyError = $_.Exception.Message
            return [pscustomobject]@{ Ok = $false; Dir = $Dir; Tasks = 'failed'; Approvals = 'not-attempted'; Error = $tasksCopyError }
        }
    }
    $savedApprovals = 'absent'
    if (Test-Path -LiteralPath $ApprovalsPath) {
        # v0.34.2 has no reader for this file at all (git grep approvals v0.34.2 -- backend/app
        # answers nothing), so a rollback leaves it as an orphan holding tool arguments and
        # execution results that only the new code sweeps (approval.py:193-212). It is copied
        # for the same reason as tasks.json: it is the only record of what was parked where.
        $destApprovals = Join-Path $Dir 'approvals.json'
        try {
            [System.IO.File]::Copy($ApprovalsPath, $destApprovals, $true)
            $savedApprovals = $destApprovals
        } catch {
            $approvalsCopyError = $_.Exception.Message
            return [pscustomobject]@{ Ok = $false; Dir = $Dir; Tasks = $savedTasks; Approvals = 'failed'; Error = $approvalsCopyError }
        }
    }
    return [pscustomobject]@{ Ok = $true; Dir = $Dir; Tasks = $savedTasks; Approvals = $savedApprovals; Error = '' }
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
    Write-Output ("    [System.IO.Directory]::Move('$Live', '$Live-failed-$stamp')")
    Write-Output 'ROLLBACK-HOWTO 4) move the backup package back into place (this REFUSES if the live dir still'
    Write-Output '    exists - that refusal is the point: a plain Move-Item would bury the backup one level'
    Write-Output '    deeper inside the half-live package instead of failing):'
    Write-Output ("    [System.IO.Directory]::Move('$Backup', '$Live')")
    Write-Output 'ROLLBACK-HOWTO 5) start the old package and wait until /health answers with the OLD build:'
    Write-Output ("    Start-Process -FilePath '$LiveExe' -WorkingDirectory '$ProjectRoot' -WindowStyle Hidden")
    Write-Output 'ROLLBACK-HOWTO 6) only now re-enable the watchdog task (find it by its action, as this script did)'
    Write-Output ('ROLLBACK-HOWTO 7) runtime data copies this run saved (DATA-SAVED dir=) live at ' + $DataSnapshotDir + ' - if the package going live is older than v0.35 it cannot parse a waiting_user status and erases those records from data\tasks.json on its first flush, so stop the backend, copy tasks.json back over data\tasks.json, then start it again (DATA-SAVE-FAIL instead of DATA-SAVED means no copy exists)')
    Write-Output 'ROLLBACK-HOWTO ref: deployment guide, swap section - the same steps, in reverse order.'
}

function Get-WatchdogTaskState {
    # One FRESH read of the two task properties that answer the only question the
    # disable step has to answer: "can the watchdog launch the exe again during my
    # swap window?" The switch that answers it is the task's ENABLED SETTING, and on
    # this box (PowerShell 5.1.26100.9549, measured read-only 2026-10-18) it lives at
    # (Get-ScheduledTask ...).Settings.Enabled - NOT on the task object itself.
    # Get-ScheduledTask hands back an MSFT_ScheduledTask CIM instance whose
    # top-level members are TaskName / TaskPath / State / Actions / Triggers /
    # Principal / Settings; Enabled is a member of that Settings association.
    #
    # Why this is spelled out: commit a5d37bde fixed a real misread (State, see the
    # disable step) and then read the bare $fresh.Enabled - a member that does not
    # exist here. That is worse than the State bug in one specific way: a missing CIM
    # member is not an error, it evaluates silently to $null, for all 202 tasks on
    # this machine including the 31 that are genuinely State=Disabled. So the
    # fail-closed default below became PERMANENT, the pass condition could never
    # hold, and every future swap would burn the bounded window and abort with
    # disable-did-not-take. Safely - nothing is touched, the guard is restored - but
    # a 100% blocker on releasing, invisible to a textual lock.
    # backend/tests/test_swap_package_watchdog_enabled_machine_truth.py is the lock
    # that asks the MACHINE about exactly this expression, read-only, and it goes red
    # if the read ever goes back to the bare form.
    #
    # State is carried as a REPORTED field, never as the verdict: Task Scheduler keeps
    # reporting Running for a run that was already in progress when the task was
    # disabled, because disabling blocks FUTURE triggers and does not stop the one
    # underway.
    #
    # The task object captured at step 1 is deliberately NOT reused: a CIM object is
    # a snapshot of the moment it was read, so the pre-disable copy would go on
    # saying Settings.Enabled=True forever and this check could never pass.
    #
    # A read that cannot answer returns Enabled=True (fail CLOSED) and leaves
    # EnabledSource naming WHICH read failed, so "I could not prove it" is visible to
    # the operator as a value in the DISABLED token instead of as a green line.
    # Treating "no answer" as "already disabled" would reopen, as a green line, the
    # exact hole this function exists to close.
    #
    # Deliberately ONE read, no cross-cmdlet fallback chain, and that choice is
    # measured rather than stylistic: on this box Get-ScheduledTaskInfo exposes no
    # Enabled member at all, and Get-CimInstance -Namespace
    # root/Microsoft/Windows/TaskScheduler -ClassName MSFT_ScheduledTask exposes the
    # same Settings.Enabled (top-level Enabled: absent). Two cmdlets would be two
    # ways of reading one truth while adding branches a 3 a.m. operator has to reason
    # about inside the fail-closed path.
    param([object] $TaskToRead)
    $enabled = $true
    $state = 'Unreadable'
    $readError = ''
    $enabledSource = 'unread'
    try {
        $fresh = Get-ScheduledTask -TaskPath $TaskToRead.TaskPath -TaskName $TaskToRead.TaskName
        if ($fresh) {
            $state = [string] $fresh.State
            if ($null -eq $fresh.Settings) {
                # no Settings association came back at all -> nothing proves the guard
                # is down, so the fail-closed default above stands
                $enabledSource = 'settings-missing'
            }
            elseif ($null -eq $fresh.Settings.Enabled) {
                # the a5d37bde shape of the bug: the switch is not answering. This is
                # the one failure mode that cannot be told apart from success by
                # looking at enabled= alone, which is why via= is in the token.
                $enabledSource = 'settings-enabled-missing'
            }
            else {
                $enabled = [bool] $fresh.Settings.Enabled
                $enabledSource = 'settings-enabled'
            }
        }
        else {
            # the task is gone: also not proof of a disabled guard - it is proof of no
            # guard, and this script's job here is only to hand a guard back later
            $enabledSource = 'task-missing'
        }
    } catch {
        # Carried out as a FIELD, never printed here: this function's result is
        # assigned ($verify = ...), and PowerShell assigns the WHOLE output stream -
        # a stray log line would make $verify an array and turn every property read
        # below into a lucky guess about the wrong object.
        $readError = $_.Exception.Message
        $enabledSource = 'read-threw'
    }
    # The instance count comes off the task's own live state: 1 means a watchdog
    # run is underway right now. It is never killed here - this script stops the
    # backend when it decides to and nothing else, the rule every other step runs
    # on - so an underway run is waited out (bounded, see the disable step) or the
    # swap aborts and someone retries a minute later.
    $instances = 0
    if ($state -eq 'Running') { $instances = 1 }
    return [pscustomobject]@{ Enabled = $enabled; State = $state; Instances = $instances; ReadError = $readError; EnabledSource = $enabledSource }
}

function Restore-WatchdogTask {
    # Give the crash guard back and report, as one token value, what actually
    # happened. The 2026-10-16 abort at the disable-verify check was a bare throw
    # that walked straight past this machinery and left the task DISABLED: the box
    # then ran with no guardian until the operator found the task by its action and
    # enabled it by hand.
    #
    # Its own function, and deliberately NOT nested behind the "do I also have to
    # restart the package" decision - a restore that only runs when the OTHER
    # restore was needed is exactly how a guard goes missing while the code that
    # re-enables it still reads as present.
    #
    # A returned Enable-ScheduledTask call is NOT evidence that the switch moved.
    # The cmdlet can come back without raising while the task stays disabled, and
    # the old body then printed re-enabled from the mere absence of an exception -
    # a green line about a guard that was still down. So the switch is READ BACK
    # through Get-WatchdogTaskState after the call, and re-enabled is emitted only
    # when that fresh read answers Enabled=true from Settings.Enabled. A read that
    # cannot answer, a source other than settings-enabled, or Enabled=false is
    # re-enable-FAILED with the raw read in Error, and the ownership flag stays
    # set: the guard is not proven up, so a later restore must try again instead
    # of believing this run is done.
    #
    # The pre-enable read answers the other half: the disable-did-not-take abort
    # arrives here with the ownership flag set but the switch never flipped down.
    # Enabling an already-enabled task would make the ABORT token claim a re-enable
    # that never happened, so when the switch itself answers Enabled=true the
    # status is already-enabled and this run does not touch the task at all.
    #
    # Returns one object and prints nothing, for the same pipeline-capture reason
    # as Get-WatchdogTaskState; the caller prints the tokens.
    $status = 'never-disabled'
    $failDetail = ''
    if ($script:watchdogDisabled) {
        $before = Get-WatchdogTaskState -TaskToRead $task
        if ($before.Enabled -and $before.EnabledSource -eq 'settings-enabled') {
            # The switch itself says UP: nothing is left down by this run. Calling
            # Enable here would only manufacture the false token re-enabled that
            # the disable-did-not-take abort used to print.
            $script:watchdogDisabled = $false
            $status = 'already-enabled'
        } else {
        # The try and the Enable line are indented one level LESS than this else on
        # purpose: test_swap_package_disable_verify.py locates that exact line by
        # this indentation to prove the re-enable was not deleted, so re-indenting
        # it would silently turn that existing tooth green. PowerShell does not
        # care; readers should not "fix" it.
        try {
            Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
            $after = Get-WatchdogTaskState -TaskToRead $task
            if ($after.ReadError -or $after.EnabledSource -ne 'settings-enabled' -or -not $after.Enabled) {
                # The call returned but the switch did not follow it: the exact
                # failure the old body could not see. Keep the ownership flag set
                # because the guard is NOT proven up.
                $status = 're-enable-FAILED'
                $failDetail = ('read-back after Enable-ScheduledTask did not prove the guard is up: enabled=' +
                               $after.Enabled + ' via=' + $after.EnabledSource + ' state=' + $after.State +
                               ' instances=' + $after.Instances +
                               $(if ($after.ReadError) { ' read-error=' + $after.ReadError } else { '' }))
            } else {
                $status = 're-enabled'
                $script:watchdogDisabled = $false
            }
        } catch {
            $status = 're-enable-FAILED'
            $failDetail = $_.Exception.Message
        }
        }
    }
    return [pscustomobject]@{ Status = $status; Error = $failDetail }
}

function Stop-AfterFailure {
    param([string] $FailedAt, [string] $Message)
    if ($script:backupDone) {
        # The live directory has been moved aside: nothing here is auto-undone (a
        # wrong automatic un-rename under a file lock turns one half-swap into two)
        # and the guard stays DISABLED on purpose - Show-Rollback step 1 says so,
        # an unverified package must not be "protected". Stated in the token,
        # because silence here is readable as "the machinery handled it".
        Write-Output ('ABORT stage=' + $FailedAt + ' moved=yes watchdog=left-disabled-by-design')
        Write-Output ('FAIL stage=' + $FailedAt)
        Show-Rollback
        throw $Message
    }
    # Before the backup rename nothing on disk was touched, so the complete
    # recovery is: intact package running, guard enabled again. Both halves are
    # attempted independently and the verdict is printed AFTER them, so the token
    # states what was done and not what was intended.
    $package = 'intact-no-restart-needed'
    try {
        # MEASURED, THEN MADE HONEST (#51). With exactly ONE process running, the
        # bare form `(Get-LiveProcess).Count` lands on a bare CimInstance (the shape
        # the boot poll documents below) whose .Count is $null, and $null -eq 0 is
        # False - so the branch stays shut while a backend is up. That is the wanted
        # verdict, but computed by accident. @(...) counts elements: on this box
        # (PS 5.1.26100.9549) 0/1/2 processes answer True/False/False, the same table
        # as the bare spelling, now for a reason the reader can see. Do NOT "tidy"
        # this into `-not (Get-LiveProcess).Count -eq 0`: measured FALSE/FALSE/TRUE -
        # zero processes would stop restarting a dead package (silently left down)
        # and two processes would start another backend over the same data\ files.
        # The truth table is pinned by the machine probe (backend/tests/
        # test_swap_package_boot_poll_single_process_machine_truth.py), which
        # evaluates THIS line's own condition for 0/1/2 processes and requires
        # True/False/False. Re-spelling the condition no longer moves another lock:
        # test_swap_package_disable_verify.py locates this if structurally, by the
        # Start-Process call in its body.
        if (@(Get-LiveProcess).Count -eq 0 -and (Test-Path -LiteralPath $LiveExe)) {
            # do not double-start: two backends would write the same data\ files
            Start-Process -FilePath $LiveExe -WorkingDirectory $ProjectRoot -WindowStyle Hidden
            $package = 'old-package-restarted'
        }
    } catch {
        # Get-LiveProcess is a CIM query and CIM can fail. "Cannot prove it is
        # down" is not a licence to start a second backend, so the package is left
        # alone - while the guard still comes back below, which is the half of the
        # recovery that must never depend on the other half.
        Write-Output ('RECOVER-FAIL stage=' + $FailedAt + ' package-state=unproven error=' + $_.Exception.Message)
        $package = 'restart-skipped-process-list-unreadable'
    }
    $restore = Restore-WatchdogTask
    if ($restore.Error) {
        Write-Output ('RECOVER-FAIL stage=' + $FailedAt + ' watchdog-task-path-len=' + $task.TaskPath.Length + ' error=' + $restore.Error)
    }
    Write-Output ('ABORT stage=' + $FailedAt + ' moved=no watchdog=' + $restore.Status + ' package=' + $package)
    throw $Message
}

# ------------------------------------------- step 0: preflight, ALL before any stop
# Every check below exists because its failure was once discovered AFTER
# production had already been stopped. Being unable to swap in a bad package is
# survivable; being unable to swap in the good one is the incident.
if (-not (Test-Path -LiteralPath $StageExe)) {
    Add-Fail ('PRECHECK fail stage-exe=missing path=' + $StageExe)
}

# THE MARKER MUST DISCRIMINATE, and "discriminate" is a claim about TWO
# packages, so both sides are counted here with one ruler: matching LINES via
# Select-String -SimpleMatch, the same shape the served-app.js acceptance uses
# after the swap. Counting only the staged side was the bug - for a
# backend-only release the frontend is byte-identical, so every candidate
# string sits on both sides and "marker found in staging" then proves only that
# the operator typed something that exists. Two production swaps were accepted
# that way on /changelog (2 hits staging, 2 hits live): a vacuous green, which
# is worse than no gate because it reports the acceptance as having run.
$stageMarkerLines = 0
$liveMarkerLines  = 0
$liveAppJsPresent = $false
if (-not (Test-Path -LiteralPath $StageAppJs)) {
    Add-Fail ('PRECHECK fail stage-appjs=missing path=' + $StageAppJs)
} else {
    $stageMarkerLines = @(Select-String -LiteralPath $StageAppJs -Pattern $Marker -SimpleMatch).Count
}
if (-not (Test-Path -LiteralPath $LiveAppJs)) {
    Add-Fail ('PRECHECK fail live-appjs=missing path=' + $LiveAppJs + ' marker=' + $Marker)
} else {
    $liveAppJsPresent = $true
    $liveMarkerLines = @(Select-String -LiteralPath $LiveAppJs -Pattern $Marker -SimpleMatch).Count
}
if ($Rollback) {
    # backward swap: -Marker names the build being RETIRED, so the proof flips.
    if ($liveAppJsPresent -and $liveMarkerLines -lt 1) {
        Add-Fail ('PRECHECK fail rollback-live-appjs-marker=absent count=' + $liveMarkerLines + ' expected>=1 marker=' + $Marker + ' - in -Rollback the marker must be a string the package you are LEAVING answers, i.e. the build being retired; name that one, or drop -Rollback')
    }
    if ($stageMarkerLines -ge 1) {
        Add-Fail ('PRECHECK fail rollback-stage-appjs-marker=present count=' + $stageMarkerLines + ' expected=0 marker=' + $Marker + ' - the incoming package already carries it, so even a backward swap could not tell the two packages apart')
    }
} else {
    if ($stageMarkerLines -lt 1) {
        Add-Fail ('PRECHECK fail stage-appjs-marker=absent expected=' + $Marker)
    }
    if ($liveAppJsPresent -and $liveMarkerLines -ge 1) {
        Add-Fail ('PRECHECK fail live-appjs-marker=present count=' + $liveMarkerLines + ' expected=0 marker=' + $Marker + ' - this marker does not discriminate, it is already in the installed package so the swap would prove nothing; pick a string only the new app.js contains (a backend-only release has no such string unless this release itself added a marker line in app.js), or -Rollback with -RollbackEvidence when moving backward is the intent')
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

# The swap is a RENAME, and a rename cannot cross volumes: staging must sit on
# the same drive as the live package. Discovering that at step 5 would leave
# production stopped, the old package safely backed up, and no way in - so it is
# checked before anything is stopped (same rule the all-or-nothing mover below
# depends on; PyInstaller's default --distpath dist_new is under the repo root).
$stageRoot = [System.IO.Path]::GetPathRoot($StageDir)
$liveRoot = [System.IO.Path]::GetPathRoot($Live)
if ($stageRoot -ne $liveRoot) {
    Add-Fail ('PRECHECK fail stage-volume stage=' + $stageRoot + ' live=' + $liveRoot)
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
Write-Output ('PRECHECK ok stage-version=' + $stageVersion + ' marker-lines=' + $stageMarkerLines + ' live-marker-lines=' + $liveMarkerLines + ' live-appjs=' + $(if ($liveAppJsPresent) { 'present' } else { 'missing' }) + ' marker-mode=' + $(if ($Rollback) { 'rollback' } else { 'forward' }))

# ---------------------------------- step 0b: snapshot the runtime data nobody can re-derive
# Everything above this line only READS. Everything below it can move a package and start a
# process, and the process that comes up is the one that rewrites data\tasks.json.
#
# WHY THE COPY COMES HERE AND NOT NEXT TO THE BACKUP STEP: the backup rename (:910) is not the
# first moment a package can start. Stop-AfterFailure's pre-backupDone branch calls
# Start-Process on $LiveExe whenever the process list answers zero, and the first such call
# site is the disable-verify abort (:855/:857) - i.e. before any rename, before any stop. A
# snapshot taken at the backup step would therefore already be reachable by a start of the
# package that eats the file. This site is the earliest one that has both resolved paths
# ($ProjectRoot is absolute from :179) and a preflight verdict, and it is before every one of
# those exits.
#
# WHY BOTH MODES (#42 ruling, and the reason is a date not an opinion): -Rollback is the mode
# that starts an old package now. A FORWARD swap to v0.35 is what puts waiting_user records on
# disk in the first place, so the rollback that matters is always the NEXT one, and by then
# the file has grown. One copy of a 19-byte file (measured on this box 2026-10-19:
# data\tasks.json = 19 bytes, content {"tasks": {}}, 0 records; data\approvals.json absent)
# costs nothing and makes the hazard moot from this hour forward, so the step does not branch
# on direction.
#
# WHY THE COUNT DEGRADES AND THE COPY ABORTS: see Save-RuntimeDataSnapshot - the short version
# is that a missing number costs a look at the file, while a missing copy costs the parked
# tasks. Neither one is allowed to pass silently: an unreadable file prints state=unreadable
# waiting_user=unproven, never 0, and a copy that threw prints DATA-SAVE-FAIL and stops the
# run while production is still untouched.
#
# The warning line is rollback-only because that is the mode where the erasing package comes
# up in THIS run. Forward swaps get the same sentence in ROLLBACK-HOWTO step 7, which is the
# only place a forward run can end up starting an old package (a hand-followed recipe).
# Spelled $DryRun.IsPresent, not $DryRun: three existing locks locate the -DryRun exit with a
# FIRST-MATCH on ^if \(\$DryRun\) (test_swap_package_marker_gate.py _marker_gate_ok and
# test_dryrun_path_reports_the_gate_it_just_passed, test_swap_package_disable_verify.py
# test_pre_move_window_is_located_at_all). A second column-0 match ahead of the real exit would
# not fail them today - they only compare "before the disable" - but it would silently move
# three other tickets' anchor, so the branch is written in the form that cannot.
$SwapMode = $(if ($Rollback) { 'rollback' } else { 'forward' })
if ($DryRun.IsPresent) {
    Write-Output ('DRYRUN would-save-data mode=' + $SwapMode + ' dir=' + $DataSnapshotDir + ' tasks=' + $(if (Test-Path -LiteralPath $TasksJson) { 'present' } else { 'absent' }) + ' approvals=' + $(if (Test-Path -LiteralPath $ApprovalsJson) { 'present' } else { 'absent' }))
} else {
    $taskVerdict = Get-TaskFileVerdict -TasksPath $TasksJson
    $snap = Save-RuntimeDataSnapshot -Dir $DataSnapshotDir -TasksPath $TasksJson -ApprovalsPath $ApprovalsJson
    if (-not $snap.Ok) {
        Write-Output ('DATA-SAVE-FAIL mode=' + $SwapMode + ' dir=' + $snap.Dir + ' state=' + $taskVerdict.State + ' records=' + $taskVerdict.Records + ' waiting_user=' + $taskVerdict.WaitingUser + ' error=' + $snap.Error)
        throw ('runtime data snapshot failed - refusing to continue, because a package older than v0.35 drops every waiting_user record it cannot parse and its next flush rewrites data\tasks.json from memory, so with no copy there is nothing to recover the parked tasks and their breakpoints from. Nothing was stopped and no package was moved: production is untouched. ' + $snap.Error)
    }
    Write-Output ('DATA-SAVED mode=' + $SwapMode + ' dir=' + $snap.Dir + ' tasks=' + $snap.Tasks + ' approvals=' + $snap.Approvals + ' state=' + $taskVerdict.State + ' records=' + $taskVerdict.Records + ' waiting_user=' + $taskVerdict.WaitingUser + ' moved=no')
    if ($Rollback) {
        Write-Output ('DATA-WARNING mode=rollback parked=' + $taskVerdict.WaitingUser + ' records=' + $taskVerdict.Records + ' state=' + $taskVerdict.State + ' hazard=old-package-restore-drops-waiting_user-and-its-next-flush-rewrites-the-file-from-memory copies=' + $snap.Dir + ' recovery=stop-the-backend-then-copy-' + $snap.Tasks + ' over-' + $TasksJson + '-then-start-again')
    }
}

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
    # the same two-sided gate the real run had to pass to get here, printed so a
    # dry run is a rehearsal of the verdict and not only of the file moves
    Write-Output ('DRYRUN marker-gate mode=' + $(if ($Rollback) { 'rollback' } else { 'forward' }) + ' stage-lines=' + $stageMarkerLines + ' live-lines=' + $liveMarkerLines + ' live-appjs=' + $(if ($liveAppJsPresent) { 'present' } else { 'missing' }))
    Write-Output ('DRYRUN would-accept health-build=' + $ExpectedVersion + ' appjs-marker=' + $Marker + ' expect=' + $(if ($Rollback) { 'absent' } else { 'present' }))
    Write-Output 'DRYRUN ok (no process was stopped, no file was moved)'
    exit 0
}

# DISABLE comes here, after the preflight and BEFORE the first stop/rename:
# the task only ever STARTS the exe and never kills it, so a wake between the
# stop and the rename relaunches the old package, its file handle makes the
# rename die with PermissionError, and the box ends up "swapped halfway".
#
# The disable is wrapped for one reason: this script runs under
# $ErrorActionPreference = 'Stop', so a disable that THREW used to kill the run
# with no stage token and no restore attempt, while leaving the real state of the
# crash guard UNKNOWN. The same CIM/Taskschd flakiness that made ticket #24 wrap
# the stop/port stage in try -> Stop-AfterFailure can hit this cmdlet too, and it
# can flip the switch and THEN fail on the output pipeline - so "it threw" does not
# mean "the guard was never pressed", and guessing either way is how the guard got
# lost once already. The handler below answers the only question that matters,
# "is the guard up right now?", using the read helper this file already trusts
# (Get-WatchdogTaskState: read-only, fail-closed, names which read answered). It
# never writes the task itself: the re-enable belongs to Restore-WatchdogTask and
# nowhere else, because two places owning one switch is how the second bug hides.
# Do not "simplify" this back to the bare two lines - the bare form IS the hole,
# and backend/tests/test_swap_package_disable_threw_handler.py is the lock that
# bites when it comes back.
#
# The two statements inside the try deliberately stay at COLUMN 0. PowerShell is not
# indentation-sensitive, so this behaves identically on 5.1; and three ordering locks
# locate this step by a line starting at column 0 (disable-verify _window,
# marker_gate _marker_gate_ok, all_or_nothing_move's same-volume precheck), so
# re-indenting them is a lock-breaking edit rather than a cosmetic one.
try {
Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
$script:watchdogDisabled = $true
} catch {
    # Captured before the DISABLE-THREW token is built; the lock in
    # test_swap_package_disable_threw_handler.py pins that order. It is NOT
    # load-bearing that this is the first statement (measured 2026-10 for #46,
    # PS 5.1.26100.9549: $_ survives the nested/inline catch below, and moving this
    # assignment after the read turns no lock red). Kept first because it is a cheap
    # convenience - one line, and the handler reads top-down.
    $disableError = $_.Exception.Message
    # ONE read, the same helper the verify loop uses. It catches its own failures and
    # returns, so the outer try here is only there to keep "the read could not even
    # run" from becoming a second unaccounted exit; $disableProbe left at $null means
    # the switch gave no answer at all, which the fail-safe branch below owns.
    $disableProbe = $null
    try { $disableProbe = Get-WatchdogTaskState -TaskToRead $task } catch { $disableProbe = $null }
    # Printed once, before the branches, and it claims nothing about the outcome: it
    # carries the raw read so the line above ABORT says what the machine answered.
    # Which branch was taken is told by that branch's own stage token, never by a
    # field this handler computes about itself.
    Write-Output ('DISABLE-THREW enabled=' + $disableProbe.Enabled + ' via=' + $disableProbe.EnabledSource + ' state=' + $disableProbe.State + ' instances=' + $disableProbe.Instances + ' error=' + $disableError)
    if ($disableProbe -and -not $disableProbe.Enabled) {
        # (1) The switch says DOWN: the disable DID land even though the cmdlet threw,
        # so this run owns the disable. The flag goes on BEFORE the abort because it is
        # the only thing that makes Stop-AfterFailure's restore path hand the guard back.
        $script:watchdogDisabled = $true
        Stop-AfterFailure 'disable-threw-task-down' ('the failed disable did land - the watchdog switch reads OFF, so this run owns the disable and hands the guard back, aborting BEFORE touching the running package, error=' + $disableError)
    }
    if ($disableProbe -and $disableProbe.Enabled -and $disableProbe.EnabledSource -eq 'settings-enabled') {
        # (2) The switch itself says UP: nothing was flipped, so this run owns NO
        # disable and the flag is left exactly as it stands - the ABORT token then
        # honestly reads watchdog=never-disabled instead of claiming a re-enable that
        # never happened. Only the switch answering can take this branch: an
        # Enabled=True coming from a failed read is branch (3), not this one.
        Stop-AfterFailure 'disable-threw-task-up' ('the failed disable flipped nothing - the watchdog switch still reads ON from the switch itself, so the guard was never down, aborting BEFORE touching the running package, error=' + $disableError)
    }
    # (3) FAIL-SAFE, and deliberately the fallthrough instead of a third if: a read
    # that cannot answer (ReadError / task-missing / settings-enabled-missing /
    # settings-missing / the re-read returning nothing) lands here, and so does any
    # branch added later that forgets to abort. Enabled=True from a switch that never
    # replied means "no answer", NOT "still up", so the guard is treated as possibly
    # down and the restore is attempted anyway: an unnecessary re-enable costs one
    # log line, a crash guard left down costs the box until someone goes looking.
    $script:watchdogDisabled = $true
    Stop-AfterFailure 'disable-threw-guard-state-unproven' ('the disable call threw and the task switch could not answer whether the guard is up, so the restore is attempted anyway, aborting BEFORE touching the running package, error=' + $disableError)
}
# The verdict below reads the task's Settings.Enabled, not State, because the question
# this step has to answer is "can the watchdog start the exe again during my window?" -
# that is the Enabled switch, which the disable flips at once. It is spelled
# Settings.Enabled because that is where the switch actually lives on this machine: the
# task object returned by Get-ScheduledTask has no Enabled member of its own, so the bare
# $task.Enabled that commit a5d37bde used answers $null for all 202 tasks here (31 of them
# genuinely disabled) without raising anything - and a $null under a fail-closed default is
# a swap that can never pass this check. See Get-WatchdogTaskState for the measurement and
# backend/tests/test_swap_package_watchdog_enabled_machine_truth.py for the lock that asks
# the machine instead of the text.
# State cannot answer it either: while an instance of the task is in flight Task Scheduler
# reports State=Running even after a fully successful disable, since disabling blocks
# FUTURE triggers and leaves the run already underway alone. The previous check required
# State to be literally Disabled and threw otherwise, so it aborted a healthy swap whenever
# one was attempted in the same second as a wake: the 2026-10-16 release
# (DISABLED state=Running -> throw -> PSEXIT=1, package left untouched - the
# fail-safe worked, the reading did not), while a retry minutes later, with no
# instance running, printed state=Disabled and completed. That throw was also
# bare, so it walked past Stop-AfterFailure and left the guard DISABLED: one
# abort, two defects, and the second one silent until someone went looking.
# Both halves are locked by backend/tests/test_swap_package_disable_verify.py.
# The via= field is part of the DISABLED token because enabled=True has two very
# different meanings - "the guard is still up" and "the switch did not answer me" - and
# an operator must be able to tell them apart from the one line the swap printed.
# An underway run is still a hazard - it can launch the old exe inside my window -
# so it is waited out rather than waved through. The wait is bounded, because
# every second here is a second the guard is already down and
# watchdog-hidden.vbs waits for watchdog.ps1 to return (waitOnReturn=True), which
# is normally seconds. Running the window out aborts through the restore machinery
# with its own stage token; nothing is killed, and retrying a minute later is
# cheap while a package swapped halfway is not.
$DisableVerifyWindowSeconds = 15
$verifyDeadline = (Get-Date).AddSeconds($DisableVerifyWindowSeconds)
$verifyTries = 0
while ($true) {
    $verify = Get-WatchdogTaskState -TaskToRead $task
    if ($verify.ReadError) { Write-Output ('READFAIL stage=task-state error=' + $verify.ReadError) }
    Write-Output ('DISABLED enabled=' + $verify.Enabled + ' via=' + $verify.EnabledSource + ' state=' + $verify.State + ' instances=' + $verify.Instances)
    if (-not $verify.Enabled -and $verify.Instances -eq 0) { break }
    if ((Get-Date) -ge $verifyDeadline) {
        if ($verify.Enabled) {
            Stop-AfterFailure 'disable-did-not-take' ('the watchdog disable did not take: Enabled is still true after ' + $DisableVerifyWindowSeconds + 's, so it can still start the exe - aborting BEFORE touching the running package')
        }
        Stop-AfterFailure 'disable-instance-running' ('the watchdog is disabled but an instance is still underway after ' + $DisableVerifyWindowSeconds + 's, state=' + $verify.State + ' - that run may still start the old exe, so aborting BEFORE touching the running package; retry once the wake has finished')
    }
    $verifyTries = $verifyTries + 1
    Write-Output ('WAIT stage=disable-verify enabled=' + $verify.Enabled + ' state=' + $verify.State + ' instances=' + $verify.Instances + ' tries=' + $verifyTries)
    Start-Sleep -Seconds 1
}

# ------------------------------------------------------- step 2: stop the backend
# The whole stop/port stage sits in a try that hands over to Stop-AfterFailure.
# Why: from the disable above to the re-enable at the end, ANY way this script
# leaves is a way the guard is left down, and the two CIM/process queries below
# (Get-LiveProcess, Stop-Process) can raise on their own. Left bare, one of them
# failing was a silent loss of the crash guard with no token at all - the same
# second defect as the bare throw this ticket replaced, just arriving from a
# cmdlet instead of an if. Before the backup rename the restore is complete
# (package still intact on disk), so routing here is also cheap.
try {
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
} catch {
    Stop-AfterFailure 'stop' ('aborted in the stop/port stage, before any package was moved: ' + $_.Exception.Message)
}

# ------------------------------------- step 4/5: rename the old aside, then swap
# Never Rename-Item: its -NewName takes only a LEAF name; given a full target
# path it does not raise, it does nothing, and the script cheerfully prints
# success (documented 2026-09-20 morning incident - two packages still in their
# original places, operator told the swap worked). And never a cmdlet that
# copies child by child when the rename fails: see Invoke-PackageMove for what
# that shape cost on 2026-10-03 (an empty backup directory next to an intact
# source, both claiming to be the rollback package).
# The old package is renamed, NEVER deleted: rollback depends on it staying on
# disk, which is exactly why the backup gets a timestamped name (no overwrite).
try {
    Invoke-PackageMove -From $Live -To $Backup
} catch {
    Stop-AfterFailure 'backup-rename' $_.Exception.Message
}
$script:backupDone = $true   # from here on, failures print the recipe, no auto-undo
Write-Output ('BACKUP ' + $Backup)

try {
    Invoke-PackageMove -From $StageDir -To $Live
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
# A frozen onedir backend needs more than a few seconds before it owns the
# port: chromadb imports, then the provider probe. Measured on 2026-10-03 the
# process was alive but /health still silent at +12s, so a fixed short wait
# fails a GOOD package and prints a rollback recipe for no reason. Poll
# instead, and keep the window configurable for slower machines.
try {
    Start-Process -FilePath $LiveExe -WorkingDirectory $ProjectRoot -WindowStyle Hidden
} catch {
    Stop-AfterFailure 'start' $_.Exception.Message
}
$livePid = 0
$bootDeadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
# WHY THIS READ IS WRAPPED, AND WHY IT LEAVES THROUGH Stop-AfterFailure (#45).
# Get-LiveProcess answers over CIM/WMI, and this file already documents twice
# that those queries can RAISE (#24 wrapped the stop/port stage for exactly
# this reason, #32 wrapped the disable for it). The file runs under
# $ErrorActionPreference = 'Stop' with no trap and no finally, so an unwrapped
# read here was a third bare exit - and the worst of the three, because by now
# step 4 has renamed the live package aside ($script:backupDone is set), step 5
# has renamed the NEW one into place and step 6 has started it: this run is
# holding the crash guard down over a live slot that is sitting on a package
# nobody has accepted yet. A bare death at that moment printed no ABORT stage=,
# no rollback recipe and no word about the guard. Each requirement below is
# argued here because #24 and #32 were both reopened by someone "simplifying"
# a wrapped read back into a bare one:
#
#  (1) NOT a bare exit, and NOT swallowed either. The give-up aborts with its
#      own stage, 'boot-poll-threw', in the disable-threw-* idiom. It is
#      deliberately NOT folded into the existing 'started-check' stage below:
#      that one means "the poll ANSWERED and saw no process", and reporting
#      that when the poll could not answer is the same lie #32 refused to tell
#      about the disable.
#  (2) THE GUARD IS ACCOUNTED, NOT ASSUMED - and it is NOT raised here. The
#      ownership flag $script:watchdogDisabled is already $true (the disable
#      step set it), and this handler writes the scheduled task not at all:
#      Restore-WatchdogTask is the one owner of that switch, so a second
#      "helpful" Enable-ScheduledTask here is #32's bug wearing a new coat.
#      Because $script:backupDone is true, what Stop-AfterFailure actually
#      prints on this path is its post-backup branch:
#        ABORT stage=boot-poll-threw moved=yes watchdog=left-disabled-by-design
#        FAIL stage=boot-poll-threw   ... then the ROLLBACK recipe, then throw.
#      It does NOT print re-enabled and does NOT call the restore, on purpose:
#      ROLLBACK-HOWTO step 1 tells the operator to keep the guard down until
#      the kept package is proven healthy, because re-arming a watchdog over an
#      unverified package "protects" precisely the thing under suspicion, and a
#      wrong automatic un-rename under a file lock turns one half-swap into two.
#      So this fix cannot honestly raise the guard from here; what it fixes is
#      that the guard is left down LOUDLY, with a recipe naming the backup,
#      instead of silently.
#  (3) ONE RAISE ASKS AGAIN, BOUNDED BY THE WINDOW THIS LOOP ALREADY OWNS. A
#      transient CIM hiccup must not doom a swap, so the catch remembers the
#      error and the loop re-reads on its next pass. The bound is not new
#      machinery: it is the existing $bootDeadline, and the retry cadence is
#      this loop's own Start-Sleep of 2 seconds - so the worst case is
#      floor($StartTimeoutSeconds / 2) reads, 45 at the 90 s default, which is
#      precisely the rate the loop already put on CIM when CIM was answering.
#      That is why it can neither run past the window silently nor add load;
#      there is no new timer, no backoff and no unbounded retry. Every raise
#      prints its own READFAIL line (the token #24 introduced; stage= says which
#      read failed) so 90 seconds are never spent with nothing on the screen.
#      The give-up carries the LAST real message, and a read that ANSWERS
#      clears it: the abort below fires only if the window's final attempt still
#      could not look. If anything answered, the verdict stays with the existing
#      path below, which asks /health before declaring failure (the 2026-10-03
#      lesson) - this branch never steals it.
#  (4) On the give-up the abort reads /health ONCE and reports it as a FIELD
#      only. Get-HealthOnce never throws, and the verdict does not depend on it.
#      Reason: this abort prints a ROLLBACK recipe, and a recipe is how an
#      operator ends up tearing down a backend that was healthy all along.
#      "The process list cannot answer" is a statement about the process list,
#      so the line states both facts and lets the human weigh them.
#      backend/tests/test_swap_package_boot_poll_threw_handler.py is the lock
#      that bites when the try comes off, and it runs these same lines against a
#      Get-LiveProcess that really throws - not a text match alone.
$bootPollError = ''
$bootPollFails = 0
while ((Get-Date) -lt $bootDeadline) {
    Start-Sleep -Seconds 2
    try {
        $running = Get-LiveProcess
        # Cleared only on a read that ANSWERED - see (3): this is what makes
        # "the poll could not look" and "the poll saw nothing" two different
        # verdicts instead of one guess.
        $bootPollError = ''
    } catch {
        # $_ is turned into a named variable in the first statement, before
        # anything else can take it over, so the line below prints the real
        # failure and not somebody else's.
        $bootPollFails = $bootPollFails + 1
        $bootPollError = $_.Exception.Message
        Write-Output ('READFAIL stage=boot-poll fails=' + $bootPollFails + ' error=' + $bootPollError)
        continue
    }
    # ONE RUNNING PROCESS IS THE NORMAL CASE, AND IT ARRIVES HERE UNWRAPPED.
    # Get-LiveProcess (:195-204) wraps its own pipeline in @(), but PowerShell
    # enumerates function output element by element, so a one-element result
    # lands on $running as a BARE CimInstance - and a bare CimInstance answers
    # .Count as $null, which makes -ge 1 False. Measured on this box 2026-10-17
    # (PS 5.1.26100.9549) with exactly one run_backend.exe serving, pid 126256:
    # is_array=False, .Count=$null, ($running.Count -ge 1)=False, @($running) has
    # 1 element. Do not eyeball this from scalar behaviour: an int answers .Count
    # as 1 and $null answers it as 0 - the CimInstance is the exception, which is
    # exactly why this survived two readers. Unbroken, the loop never breaks on
    # the normal path: it burns the whole $StartTimeoutSeconds window on every
    # swap, and the only pid it can then report is the /health sentinel below
    # (-1) rather than the pid this loop exists to find.
    # So re-wrap it, AT THE PREDICATE - the idiom this file already uses where it
    # assigns $task, $stoppedPids and $stillListening, and the one watchdog.ps1
    # uses against this same flattening bug (its :315 -is [array], :377, :480).
    # The wrap is safe for the no-process case (measured: @(Get-LiveProcess)
    # counts 0, and an $running freshly assigned from a command that emitted
    # nothing also counts 0), but it is not a general identity - @($null) over a
    # literal $null value counts 1 - so a future "$running = $null" initialiser
    # plus a path that reaches this line without a fresh read would quietly
    # change what the count means. The lock that asks the machine instead of the
    # reader is backend/tests/
    # test_swap_package_boot_poll_single_process_machine_truth.py and it goes red
    # against an unwrapped predicate - do not "simplify" this back.
    if (@($running).Count -ge 1) {
        $livePid = (@($running)[0]).ProcessId
        break
    }
}
if ($livePid -eq 0 -and $bootPollError) {
    # Report-only witness (see (4) above): never a verdict, never a throw of its
    # own - Get-HealthOnce swallows its own failures by design.
    $pollWitness = Get-HealthOnce
    $pollWhy = 'the boot poll could not read the process list on its final attempt (' + $bootPollFails + ' failed reads in ' + $StartTimeoutSeconds + ' seconds), last error=' + $bootPollError
    $pollWhy = $pollWhy + ' - this is "cannot look", NOT "no process found" (that verdict is the started-check stage, which asks /health first). The live slot already holds the package that was swapped in and this run is holding the crash guard down, so nothing here is undone automatically: follow the ROLLBACK recipe and re-enable the task only once the package you decide to keep is proven healthy. health=' + $(if ($pollWitness -and $pollWitness.build) { [string] $pollWitness.build } else { 'silent' })
    Stop-AfterFailure 'boot-poll-threw' $pollWhy
}
if ($livePid -eq 0) {
    # 2026-10-03 22:44 (v0.29.2 swap): this loop ran its full window without ever
    # matching a process, and the script printed the complete rollback recipe - for
    # a package that had been up since +7s and was serving the target build the
    # whole time (/health answered build=v0.29.2 within seconds of the abort).
    # "The process list disagrees with me" is a statement about the process list,
    # not about production. So the verdict gets a second witness before it fires:
    # ask the service itself. If /health already answers with THE BUILD WE ARE
    # TRYING TO SHIP, there is nothing to roll back, and printing a recipe only
    # invites an operator to tear down a healthy backend (this is the same failure
    # shape as the old hardcoded 12-second sleep, one signal riper).
    $asked = Get-HealthOnce
    if ($asked -and $asked.build -eq $ExpectedVersion) {
        # -1 = up per /health, invisible to the process list. Deliberately not 0:
        # 0 is the value that means "declare failure and print the recipe".
        $livePid = -1
    } else {
        # One statement per line on purpose: PowerShell 5.1 ends a statement at a
        # newline even inside parens unless the line ends on an open operator, and
        # a wrapped message here would take the whole script down with it.
        $why = 'no process at ' + $LiveExe + ' within ' + $StartTimeoutSeconds
        $why = $why + ' seconds after start, and /health stayed silent on build=' + $ExpectedVersion
        Stop-AfterFailure 'started-check' $why
    }
}
Write-Output ('STARTED pid=' + $livePid)

# ------------------------------------- step 7: acceptance - the NEW code answers
# Not "is the port up": the 2026-09-19 first failed swap kept /health answering
# 200 the whole time - with the OLD package. Acceptance is two positive checks:
# the served build stamp equals the target tag, and the served app.js carries
# this release's marker string.
$h = $null
$deadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    $h = Get-HealthOnce
    if ($h -and $h.build) { break }
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
if ($Rollback) {
    # backward swap: -Marker named the build being RETIRED, so after a rollback
    # that string must be GONE from what is served. The precheck proved the two
    # packages differ on it, so "absent now" really does mean "the older
    # package answers" - the same positive proof, read from the other side.
    if ($markerLines -ge 1) {
        Stop-AfterFailure 'accept-marker-rollback' ('served app.js still carries ' + $Marker + ' lines=' + $markerLines + ' - the build being retired is still the build being served')
    }
    Write-Output ('ACCEPT marker-lines=' + $markerLines + ' mode=rollback expect=absent')
} else {
    if ($markerLines -lt 1) {
        Stop-AfterFailure 'accept-marker' ('served app.js contains no ' + $Marker + ' - still running the old package')
    }
    Write-Output ('ACCEPT marker-lines=' + $markerLines + ' mode=forward expect=present')
}

# --------------------------------- step 8: watchdog back ON - only now, AFTER the
# health and marker assertions above. Enabling it earlier would let it "protect"
# a half-baked or wrong-build package; the task staying disabled through the
# whole acceptance window is the point of ordering it last.
try {
    Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
    # The flag means "this run is holding the guard down", so it comes off with
    # the planned re-enable too - otherwise a restore reached from anywhere later
    # would report re-enabling a task this script had already given back.
    $script:watchdogDisabled = $false
} catch {
    Stop-AfterFailure 'enable' ('swap is verified but re-enabling the task failed: ' + $_.Exception.Message)
}
# WHY THIS CONFIRMATION READ GOES THROUGH THE HELPER, AND WHY IT NEVER EXITS (#47).
#
# Up to two lines ago this was `$finalState = (Get-ScheduledTask ...).State`, a BARE
# read sitting immediately after a SUCCESSFUL re-enable. The re-enable is already wrapped
# (the `Stop-AfterFailure 'enable'` catch right above this block); the read that confirms
# it was the one unwrapped statement left in the post-acceptance region, and
# $ErrorActionPreference = 'Stop' with no trap and no finally means one throw there was
# a third bare exit in the same tail (#32, #45 and #50 fixed the ones before it). Measured 2026-10-17 on this box (PS
# 5.1.26100.9549), read-only, against a hand-built fake identity
# (\t47-fake-path\ / t47-fake-task) so the live guard was never aimed at:
#   Get-ScheduledTask -TaskPath ... -TaskName ... for an identity that does not answer
#     -> THROWS Microsoft.PowerShell.Cmdletization.Cim.CimJobException ("no matching
#        MSFT_ScheduledTask objects"), so under Stop the script dies printing NO
#        ENABLED, NO BUDGET and NO SWAP-DONE;
#   the same call with -ErrorAction SilentlyContinue -> returns nothing, and the .State
#     of nothing is $null, so the "helpful" fix would print `ENABLED state=` with an
#     EMPTY value - a token that says neither yes nor no.
# Both shapes are avoided by routing the read through Get-WatchdogTaskState, which
# catches its own failures and hands the outcome back as FIELDS. That reuse is the
# point, not the wrap: this file already paid for one way of reading this switch (the
# helper's own comment records the a5d37bde misread, and its "deliberately ONE read,
# no cross-cmdlet fallback chain" records why a second spelling is not a convenience).
# Wrapping the raw cmdlet here would have installed a SECOND owner of that property
# path, and a5d37bde's silent-$null bug is exactly what duplicates like that keep.
#
# THE SHAPE WAS MEASURED, NOT READ OFF THE INTENT, because #50 is about this exact
# trap: what the call returns to $finalProbe is ONE object (is_array=False), and on
# this PowerShell a bare PSCustomObject answers .Count as $null - so any future
# judgement written as a count on this variable would be silently False forever. The
# site reads NoteProperties only (State, ReadError), which is what the helper returns:
# Enabled / State / Instances / ReadError / EnabledSource. ReadError is non-empty
# exactly when the read could not answer, and State carries 'Unreadable' then; the
# helper itself prints nothing (test_swap_package_disable_verify.py
# _single_value_helpers_ok is the lock that keeps that true, because $finalProbe is
# assigned and an assignment takes the WHOLE output stream).
#
# WHY DEGRADE AND NOT STOP-AFTER-FAILURE, and this one is not a judgment call, it is
# an arithmetic fact about the tokens: by the time this line runs, $script:watchdogDisabled
# has just been set to $false by the re-enable above, and $script:backupDone is $true -
# so Stop-AfterFailure's post-backup branch would print the literal string
# 'watchdog=left-disabled-by-design' about a guard this run re-enabled two lines ago,
# followed by a ROLLBACK recipe for a package that has already passed HEALTH build= and
# ACCEPT marker-lines=. Same conclusion #45's reviewer reached for the phase after
# backupDone, for a different reason: there the guard really is down and the recipe is
# the honest output; here both would be false. So: say what the read answered, say
# plainly when it could not answer, and walk on to BUDGET and SWAP-DONE.
#
# What is deliberately NOT added here: enabled= / via= / instances= fields. Those would
# be token cosmetics for whoever owns the token shapes, and enabled= in particular is
# fail-CLOSED inside the helper (a read that cannot answer reports Enabled=True, which
# is the right default for "may I assume the guard is down?" at the disable step and is
# NOT the question being asked here) - printing it at step 8 would need its own
# argument. The state= value alone keeps this ticket's change to one line of grammar.
$finalProbe = Get-WatchdogTaskState -TaskToRead $task
if ($finalProbe.ReadError) {
    # The third member of the READFAIL stage= family (task-state, boot-poll, and now
    # this one), named by the header registry: a read that could not answer, saying
    # which. It is a report line, not a verdict - ENABLED below still prints.
    Write-Output ('READFAIL stage=final-enabled-read error=' + $finalProbe.ReadError)
}
Write-Output ('ENABLED state=' + $finalProbe.State)

# ------------------------------------------------- step 9: hand the budget back
# Comes AFTER re-enable and after both acceptance assertions: the counter is
# only meaningless once we know the package we are guarding is the right one.
# Opt-out exists for the case where the operator wants the spent budget kept as
# evidence of a bad swap window.
if ($KeepBackendBudget) {
    Write-Output ('BUDGET kept -operator-choice state=' +
                  (Join-Path $ProjectRoot 'data\watchdog-state.json'))
} else {
    Clear-BackendBudget -StatePath (Join-Path $ProjectRoot 'data\watchdog-state.json')
}
Write-Output 'SWAP-DONE'
