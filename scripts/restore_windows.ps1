param(
    [string]$Snapshot = "latest",
    [string]$BackupRoot = "",
    [string]$DatabaseName = "query_optimizer_restored",
    [switch]$ReplaceDatabase,
    [switch]$RestoreArtifacts,
    [switch]$RestoreSettings
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($DatabaseName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw "DatabaseName must be a safe PostgreSQL identifier."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$databaseUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "query_optimizer" }
if (-not $BackupRoot) {
    $BackupRoot = if ($env:PQO_BACKUP_ROOT) {
        [Environment]::ExpandEnvironmentVariables($env:PQO_BACKUP_ROOT)
    } elseif ($env:OneDrive) {
        Join-Path $env:OneDrive "PQO Backups"
    } else {
        Join-Path (Split-Path -Parent $projectRoot) "PQO Backups"
    }
}

if ($Snapshot -eq "latest") {
    $snapshotItem = Get-ChildItem -LiteralPath $BackupRoot -Directory -ErrorAction Stop |
        Where-Object { $_.Name -match '^pqo-backup-\d{8}T\d{6}Z$' } |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $snapshotItem) { throw "No backup snapshots found in '$BackupRoot'." }
    $snapshotPath = $snapshotItem.FullName
} else {
    $snapshotPath = (Resolve-Path -LiteralPath $Snapshot).Path
}

$manifestPath = Join-Path $snapshotPath "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Backup manifest not found: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifest.format_version -ne 1) {
    throw "Unsupported backup format version: $($manifest.format_version)"
}

foreach ($entry in $manifest.files) {
    $filePath = [IO.Path]::GetFullPath(
        (Join-Path $snapshotPath ([string]$entry.path).Replace("/", "\"))
    )
    if (-not $filePath.StartsWith(
        $snapshotPath + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Backup manifest contains an unsafe path: $($entry.path)"
    }
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        throw "Backup file is missing: $($entry.path)"
    }
    $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash
    if ($actualHash -ne $entry.sha256) {
        throw "Backup checksum mismatch: $($entry.path)"
    }
}

function Assert-NativeSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

$dumpPath = [IO.Path]::GetFullPath(
    (Join-Path $snapshotPath ([string]$manifest.database.dump_file).Replace("/", "\"))
)
$containerDump = "/tmp/pqo-restore-$PID.dump"

Push-Location $projectRoot
try {
    docker compose up -d --wait
    Assert-NativeSuccess "Starting PostgreSQL"
    $exists = docker compose exec -T postgres psql `
        -U $databaseUser -d postgres -Atqc `
        "SELECT 1 FROM pg_database WHERE datname = '$DatabaseName'"
    Assert-NativeSuccess "Checking restore database"
    if ($exists -eq "1" -and -not $ReplaceDatabase) {
        throw "Database '$DatabaseName' already exists. Use -ReplaceDatabase to overwrite it."
    }

    if ($exists -eq "1") {
        docker compose exec -T postgres psql -U $databaseUser -d postgres `
            -v ON_ERROR_STOP=1 -c `
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DatabaseName' AND pid <> pg_backend_pid();"
        Assert-NativeSuccess "Terminating restore database connections"
        docker compose exec -T postgres dropdb -U $databaseUser $DatabaseName
        Assert-NativeSuccess "Dropping restore database"
    }

    docker compose exec -T postgres createdb -U $databaseUser $DatabaseName
    Assert-NativeSuccess "Creating restore database"
    try {
        docker compose cp $dumpPath "postgres:$containerDump"
        Assert-NativeSuccess "Copying database backup to PostgreSQL"
        docker compose exec -T postgres pg_restore `
            -U $databaseUser -d $DatabaseName --exit-on-error `
            --no-owner --no-privileges $containerDump
        Assert-NativeSuccess "Restoring PostgreSQL backup"
    }
    finally {
        docker compose exec -T postgres rm -f $containerDump | Out-Null
    }

    if ($RestoreArtifacts) {
        $applicationRoot = if ($env:PQO_APP_DATA_ROOT) {
            [Environment]::ExpandEnvironmentVariables($env:PQO_APP_DATA_ROOT)
        } else {
            Join-Path $env:LOCALAPPDATA "PredictiveQueryOptimization"
        }
        $applicationArchive = Join-Path $snapshotPath "artifacts\application-artifacts.zip"
        if (Test-Path -LiteralPath $applicationArchive) {
            $destination = Join-Path $applicationRoot "artifacts"
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
            Expand-Archive -LiteralPath $applicationArchive -DestinationPath $destination -Force
        }
        $projectArchive = Join-Path $snapshotPath "artifacts\project-artifacts.zip"
        if (Test-Path -LiteralPath $projectArchive) {
            $destination = Join-Path $projectRoot "artifacts"
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
            Expand-Archive -LiteralPath $projectArchive -DestinationPath $destination -Force
        }
    }

    if ($RestoreSettings) {
        $settingsFile = Join-Path $snapshotPath "artifacts\application-settings.reg"
        if (-not (Test-Path -LiteralPath $settingsFile)) {
            throw "Application settings are not present in this snapshot."
        }
        reg.exe import $settingsFile | Out-Null
        Assert-NativeSuccess "Restoring application settings"
    }

    Write-Output "Restored database '$DatabaseName' from '$snapshotPath'."
}
finally {
    Pop-Location
}
