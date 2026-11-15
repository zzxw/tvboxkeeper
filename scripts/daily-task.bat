@echo off
rem TVBoxKeeper local fallback batch. NOTE: keep this file pure ASCII --
rem cmd.exe parses .bat in the OEM codepage, UTF-8 Chinese here breaks paths.
setlocal
rem Repo root = parent of this script's directory (runtime path, no literals)
set "REPO=%~dp0.."
set "TVBOX_ROOT=%REPO%"
set "PYTHONIOENCODING=utf-8"
set "LOG=%REPO%\data\local-run.log"
cd /d "%REPO%" || exit /b 1
if not exist "%REPO%\data" mkdir "%REPO%\data"
echo ===== %DATE% %TIME% local fallback run ===== >> "%LOG%"
rem Lightweight chain only: discover/hunt are left to GitHub Actions
for %%c in (import check publish) do (
    "%REPO%\.venv\Scripts\python.exe" -m tvboxkeeper %%c >> "%LOG%" 2>&1
)
echo ===== run done, pushing results ===== >> "%LOG%"

rem ---- Best-effort push: never fight GitHub Actions, never break offline ----
git add -A >> "%LOG%" 2>&1
git -c user.name="tvboxkeeper-local" ^
    -c user.email="tvboxkeeper-local@users.noreply.github.com" ^
    commit -m "chore: local fallback run [skip ci]" >> "%LOG%" 2>&1
if errorlevel 1 echo nothing new to commit >> "%LOG%"

git pull --rebase >> "%LOG%" 2>&1
if errorlevel 1 (
    git rebase --abort >> "%LOG%" 2>&1
    echo skip push: pull/rebase failed - offline or conflict, local kept >> "%LOG%"
    goto :done
)
git push >> "%LOG%" 2>&1
if errorlevel 1 (
    rem push rejected (bot pushed meanwhile): one rebase retry, then give up
    git pull --rebase >> "%LOG%" 2>&1
    git push >> "%LOG%" 2>&1
    if errorlevel 1 echo skip push: still rejected, local kept >> "%LOG%"
)
:done
echo ===== local run finished ===== >> "%LOG%"
endlocal
