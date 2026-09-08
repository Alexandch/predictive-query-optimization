param(
    [string]$BackupRoot = "",
    [int]$RetentionDays = 30,
    [int]$MinimumCopies = 7
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($RetentionDays -lt 1) { throw "RetentionDays must be positive." }
if ($MinimumCopies -lt 1) { throw "MinimumCopies must be positive." }

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$applicationRoot = if ($env:PQO_APP_DATA_ROOT) {
    [Environment]::ExpandEnvironmentVariables($env:PQO_APP_DATA_ROOT)
} else {
    Join-Path $env:LOCALAPPDATA "PredictiveQueryOptimization"
}
$applicationArtifacts = Join-Path $applicationRoot "artifacts"
$projectArtifacts = Join-Path $projectRoot "artifacts"
$databaseUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "query_optimizer" }
$databaseName = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "query_optimizer" }

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
$partialCutoff = (Get-Date).ToUniversalTime().AddDays(-1)
$stalePartialSnapshots = @(
    Get-ChildItem -LiteralPath $resolvedBackupRoot -Directory |
        Where-Object {
            $_.Name -match '^\.pqo-backup-\d{8}T\d{6}Z\.partial-\d+$' -and
            $_.LastWriteTimeUtc -lt $partialCutoff
        }
)
foreach ($item in $stalePartialSnapshots) {
    $candidate = [IO.Path]::GetFullPath($item.FullName)
    if (-not $candidate.StartsWith(
        $resolvedBackupRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove a partial backup outside the configured root: $candidate"
    }
    Remove-Item -LiteralPath $candidate -Recurse -Force
}
$snapshotId = "pqo-backup-{0}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$partialPath = Join-Path $resolvedBackupRoot (".{0}.partial-{1}" -f $snapshotId, $PID)
$snapshotPath = Join-Path $resolvedBackupRoot $snapshotId
if ((Test-Path -LiteralPath $partialPath) -or (Test-Path -LiteralPath $snapshotPath)) {
    throw "Backup snapshot already exists: $snapshotId"
}

New-Item -ItemType Directory -Path $partialPath | Out-Null
$databaseDirectory = Join-Path $partialPath "database"
$artifactDirectory = Join-Path $partialPath "artifacts"
New-Item -ItemType Directory -Path $databaseDirectory, $artifactDirectory | Out-Null

function Assert-NativeSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

function Add-DirectoryArchive([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return $false }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $Source,
        $Destination,
        [IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    return $true
}

function Get-RelativeFilePath([string]$BasePath, [string]$FilePath) {
    $separator = [IO.Path]::DirectorySeparatorChar
    $baseUri = New-Object Uri ($BasePath.TrimEnd($separator) + $separator)
    $fileUri = New-Object Uri $FilePath
    return [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($fileUri).ToString())
}

$containerDump = "/tmp/$snapshotId.dump"
$databaseDump = Join-Path $databaseDirectory "$databaseName.dump"

Push-Location $projectRoot
try {
    docker compose up -d --wait
    Assert-NativeSuccess "Starting PostgreSQL"
    try {
        docker compose exec -T postgres pg_dump `
            -U $databaseUser -d $databaseName `
            --format=custom --compress=9 --file=$containerDump
        Assert-NativeSuccess "PostgreSQL backup"
        docker compose cp "postgres:$containerDump" $databaseDump
        Assert-NativeSuccess "Copying PostgreSQL backup"
    }
    finally {
        docker compose exec -T postgres rm -f $containerDump | Out-Null
    }

    $archivedSources = [ordered]@{}
    $applicationArchive = Join-Path $artifactDirectory "application-artifacts.zip"
    $archivedSources.application_artifacts = Add-DirectoryArchive `
        $applicationArtifacts $applicationArchive

    $projectArchive = Join-Path $artifactDirectory "project-artifacts.zip"
    $sameArtifactRoot = $false
    if ((Test-Path -LiteralPath $applicationArtifacts) -and
        (Test-Path -LiteralPath $projectArtifacts)) {
        $sameArtifactRoot = (Resolve-Path $applicationArtifacts).Path -eq `
            (Resolve-Path $projectArtifacts).Path
    }
    $archivedSources.project_artifacts = if ($sameArtifactRoot) {
        $false
    } else {
        Add-DirectoryArchive $projectArtifacts $projectArchive
    }

    $settingsKey = "HKCU\Software\PQO\Predictive Query Optimization"
    $settingsProviderPath = "HKCU:\Software\PQO\Predictive Query Optimization"
    $settingsFile = Join-Path $artifactDirectory "application-settings.reg"
    if (Test-Path -LiteralPath $settingsProviderPath) {
        reg.exe export $settingsKey $settingsFile /y | Out-Null
        Assert-NativeSuccess "Exporting application settings"
        $archivedSources.application_settings = $true
    } else {
        $archivedSources.application_settings = $false
    }

    $commit = git rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { $commit = $null }
    $files = @(
        Get-ChildItem -LiteralPath $partialPath -Recurse -File |
            Sort-Object FullName |
            ForEach-Object {
                [ordered]@{
                    path = Get-RelativeFilePath $partialPath $_.FullName
                    size_bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
                }
            }
    )
    $manifest = [ordered]@{
        format_version = 1
        snapshot_id = $snapshotId
        created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        computer_name = $env:COMPUTERNAME
        project_commit = $commit
        database = [ordered]@{
            service = "postgres"
            name = $databaseName
            user = $databaseUser
            dump_file = "database/$databaseName.dump"
            format = "PostgreSQL custom"
        }
        sources = [ordered]@{
            application_artifacts = $applicationArtifacts
            project_artifacts = $projectArtifacts
        }
        included = $archivedSources
        files = $files
    }
    $manifest | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath (Join-Path $partialPath "manifest.json") -Encoding utf8

    Move-Item -LiteralPath $partialPath -Destination $snapshotPath

    $cutoff = (Get-Date).ToUniversalTime().AddDays(-$RetentionDays)
    $snapshots = @(
        Get-ChildItem -LiteralPath $resolvedBackupRoot -Directory |
            Where-Object { $_.Name -match '^pqo-backup-\d{8}T\d{6}Z$' } |
            Sort-Object Name -Descending
    )
    for ($index = $MinimumCopies; $index -lt $snapshots.Count; $index++) {
        if ($snapshots[$index].LastWriteTimeUtc -lt $cutoff) {
            $candidate = [IO.Path]::GetFullPath($snapshots[$index].FullName)
            if (-not $candidate.StartsWith($resolvedBackupRoot + [IO.Path]::DirectorySeparatorChar)) {
                throw "Refusing to remove a backup outside the configured root: $candidate"
            }
            Remove-Item -LiteralPath $candidate -Recurse -Force
        }
    }

    [ordered]@{
        success = $true
        completed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        snapshot_path = $snapshotPath
        error = $null
    } | ConvertTo-Json | Set-Content `
        -LiteralPath (Join-Path $resolvedBackupRoot "last-backup-status.json") `
        -Encoding utf8
    Write-Output $snapshotPath
}
catch {
    $backupError = $_
    try {
        [ordered]@{
            success = $false
            completed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
            snapshot_path = $null
            partial_path = $partialPath
            error = $backupError.Exception.Message
        } | ConvertTo-Json | Set-Content `
            -LiteralPath (Join-Path $resolvedBackupRoot "last-backup-status.json") `
            -Encoding utf8
    }
    catch {
        # Preserve the original backup error if even status reporting fails.
    }
    Write-Error "Backup failed. Incomplete data was left at '$partialPath'. $($backupError.Exception.Message)"
    exit 1
}
finally {
    Pop-Location
}
