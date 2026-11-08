@echo off
rem TVBoxKeeper 本地兜底跑批（供 Windows 计划任务调用）
setlocal
set "REPO=F:\zCodeSpace\TVBox源"
set "TVBOX_ROOT=%REPO%"
set "PYTHONIOENCODING=utf-8"
set "LOG=%REPO%\data\local-run.log"
cd /d "%REPO%" || exit /b 1
if not exist "%REPO%\data" mkdir "%REPO%\data"
echo ===== %DATE% %TIME% 本地兜底跑批开始 ===== >> "%LOG%"
for %%c in (import discover hunt check publish) do (
    "%REPO%\.venv\Scripts\python.exe" -m tvboxkeeper %%c >> "%LOG%" 2>&1
)
echo ===== 跑批结束 ===== >> "%LOG%"
endlocal
