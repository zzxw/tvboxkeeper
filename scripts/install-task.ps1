# Register/update Windows scheduled task: daily local fallback run at 06:10
# (backup for GitHub Actions). Run:
#   powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
# Uninstall:
#   schtasks /Delete /TN "TVBoxKeeper-daily" /F
# NOTE: keep this file pure ASCII; Windows PowerShell 5 misreads UTF-8 without BOM.

# Repo root = parent of this script's directory (avoids non-ASCII path literals)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $scriptDir
$bat = Join-Path $repo "scripts\daily-task.bat"

$action = New-ScheduledTaskAction -Execute $bat
$trigger = New-ScheduledTaskTrigger -Daily -At 06:10
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "TVBoxKeeper-daily" `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Registered task 'TVBoxKeeper-daily' (daily 06:10, catch-up on boot)."
Write-Host "Log: $repo\data\local-run.log"
Write-Host "Uninstall: schtasks /Delete /TN 'TVBoxKeeper-daily' /F"
