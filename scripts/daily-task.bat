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
echo ===== run done ===== >> "%LOG%"
endlocal
