# Register the daily Paper Collector scheduled task.
# Triggers: every day at 06:00 + on user logon.
# Wakes the PC from sleep if needed; runs ASAP if the scheduled time was missed.

$ErrorActionPreference = "Stop"

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyExe  = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $pyExe) {
    Write-Host "ERROR: python.exe not found in PATH." -ForegroundColor Red
    exit 1
}
$autoPy = Join-Path $here "auto_update.py"
if (-not (Test-Path $autoPy)) {
    Write-Host "ERROR: auto_update.py not found at $autoPy" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument ('"{0}"' -f $autoPy) `
    -WorkingDirectory $here

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At 6:00AM
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger @($dailyTrigger, $logonTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "Paper Collector: daily 6:00 AM update + at-logon server check. Wakes PC from sleep; catches up if missed."

Register-ScheduledTask -TaskName "PaperCollectorDaily" -InputObject $task -Force | Out-Null

Write-Host "OK: Registered scheduled task 'PaperCollectorDaily'" -ForegroundColor Green
Write-Host "    - Daily at 06:00 + at logon" -ForegroundColor Gray
Write-Host "    - Wake from sleep / catch up after boot" -ForegroundColor Gray
