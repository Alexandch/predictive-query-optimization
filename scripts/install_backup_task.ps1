param(
    [string]$BackupRoot = "",
    [string]$DailyAt = "20:00",
    [int]$RetentionDays = 30,
    [int]$MinimumCopies = 7,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$taskName = "PQO Automatic Backup"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Output "Scheduled task '$taskName' removed."
    exit 0
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backupScript = (Resolve-Path (Join-Path $PSScriptRoot "backup_windows.ps1")).Path
if (-not $BackupRoot) {
    $BackupRoot = if ($env:PQO_BACKUP_ROOT) {
        [Environment]::ExpandEnvironmentVariables($env:PQO_BACKUP_ROOT)
    } elseif ($env:OneDrive) {
        Join-Path $env:OneDrive "PQO Backups"
    } else {
        Join-Path (Split-Path -Parent $projectRoot) "PQO Backups"
    }
}
$resolvedBackupRoot = [IO.Path]::GetFullPath($BackupRoot)
New-Item -ItemType Directory -Path $resolvedBackupRoot -Force | Out-Null

try {
    $triggerTime = [DateTime]::Today.Add([TimeSpan]::ParseExact($DailyAt, "hh\:mm", $null))
}
catch {
    throw "DailyAt must use 24-hour HH:mm format."
}

$arguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy Bypass",
    "-File `"$backupScript`"",
    "-BackupRoot `"$resolvedBackupRoot`"",
    "-RetentionDays $RetentionDays",
    "-MinimumCopies $MinimumCopies"
) -join " "
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Daily PostgreSQL and PQO artifact backup" -Force | Out-Null

Write-Output "Scheduled task '$taskName' installed."
Write-Output "Daily time: $DailyAt"
Write-Output "Backup root: $resolvedBackupRoot"
