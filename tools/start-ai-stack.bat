@echo off
rem AI Assistant - login autostart: backend + Cloudflare named tunnel.
rem Uninstall: delete the shortcut in  shell:startup  and delete this file.
rem
rem Comments here are ASCII on purpose. cmd.exe reads .bat with the OEM code page
rem (936 on this machine), so UTF-8 Chinese text in this file gets mis-decoded and
rem cmd ends up executing fragments of a rem line. Verified on 2026-09-19: a Chinese
rem comment made the netstat guard below never run. Put prose in docs/ instead.
rem
rem Both processes start Hidden, not with "start /MIN": /MIN gives each one a console
rem window with a close button, and clicking that X silently kills the process. Two
rem "backend vanished" incidents today (about 2.5h and about 37min of downtime) had
rem this exact shape - no traceback in data\backend.log (the exe writes it itself),
rem no Windows crash report, no reboot. deploy\watchdog.ps1 starts them the same way,
rem so the two start paths behave identically.
setlocal
rem Project root = parent of this script's folder, so the script follows wherever
rem the repo is checked out. Keep it machine-path-free on purpose: this file ships
rem in the public snapshot.
cd /d "%~dp0.."
set "ROOT=%CD%"

rem PROXY-SCRUB-BEGIN
rem Ticket #295 defense-in-depth: clear env proxy vars before the backend child is
rem launched, so a poisoned NO_PROXY (bracketed [::1] construction-crash family,
rem #294) never reaches run_backend. The token prints even when nothing matched
rem (names=none), so the log proves this step ran. Per-line if-defined below: each
rem line re-expands %_PSCRUB% at its own parse time, so accumulation works without
rem delayed expansion. Names only - never echo the values.
rem
rem 2026-11-09: list BOTH cases of all four proxy names. A host can hand this process a
rem block holding http_proxy and HTTP_PROXY as two separate entries; `if defined`
rem is case-insensitive (it sees either), but `set "NAME="` removes only ONE entry,
rem so the lowercase twin survived into run_backend with a dead proxy. Same fixed
rem roster as deploy/swap-package.ps1 and deploy/watchdog.ps1; never enumerate.
set "_PSCRUB="
if defined HTTP_PROXY (set "_PSCRUB=%_PSCRUB%HTTP_PROXY," & set "HTTP_PROXY=")
if defined http_proxy (set "_PSCRUB=%_PSCRUB%http_proxy," & set "http_proxy=")
if defined HTTPS_PROXY (set "_PSCRUB=%_PSCRUB%HTTPS_PROXY," & set "HTTPS_PROXY=")
if defined https_proxy (set "_PSCRUB=%_PSCRUB%https_proxy," & set "https_proxy=")
if defined ALL_PROXY (set "_PSCRUB=%_PSCRUB%ALL_PROXY," & set "ALL_PROXY=")
if defined all_proxy (set "_PSCRUB=%_PSCRUB%all_proxy," & set "all_proxy=")
if defined NO_PROXY (set "_PSCRUB=%_PSCRUB%NO_PROXY," & set "NO_PROXY=")
if defined no_proxy (set "_PSCRUB=%_PSCRUB%no_proxy," & set "no_proxy=")
rem SSL_CERT_FILE / SSL_CERT_DIR are the two env names OpenSSL itself reads for its
rem verify locations, so a parent session can add trust anchors the backend child
rem would inherit. Both cases, same fixed-roster rule as the proxy names above.
rem Range: child-start inheritance only - an .env read inside the process and a
rem manual start are out of range, see backend/tests/test_launch_env_hygiene.py.
if defined SSL_CERT_FILE (set "_PSCRUB=%_PSCRUB%SSL_CERT_FILE," & set "SSL_CERT_FILE=")
if defined ssl_cert_file (set "_PSCRUB=%_PSCRUB%ssl_cert_file," & set "ssl_cert_file=")
if defined SSL_CERT_DIR (set "_PSCRUB=%_PSCRUB%SSL_CERT_DIR," & set "SSL_CERT_DIR=")
if defined ssl_cert_dir (set "_PSCRUB=%_PSCRUB%ssl_cert_dir," & set "ssl_cert_dir=")
if not defined _PSCRUB (echo PROXY-ENV-SCRUBBED names=none) else (echo PROXY-ENV-SCRUBBED names=%_PSCRUB%)
set "_PSCRUB="
rem PROXY-SCRUB-END

rem Skip the backend if something already listens on 8000: a second instance loses the
rem race for the port, and in the window before it dies it writes the same session and
rem memory files as the first one.
netstat -ano | findstr /R /C:":8000 " | findstr LISTENING >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%ROOT%\dist\run_backend\run_backend.exe' -WorkingDirectory '%ROOT%' -WindowStyle Hidden"
)

rem Named tunnel for ai.fenever.xyz. Guard is "is there a cloudflared process", not a
rem port check - the tunnel dials out, so local :8000 says nothing about whether it runs.
tasklist /FI "IMAGENAME eq cloudflared.exe" | findstr /I cloudflared.exe >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%ROOT%\tools\cloudflared.exe' -ArgumentList 'tunnel','run','ai-assistant' -WindowStyle Hidden"
)
endlocal
