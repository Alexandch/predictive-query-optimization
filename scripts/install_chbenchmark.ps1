param(
    [switch]$SchemaOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$schemaFile = Join-Path $projectRoot "database\init\040_chbenchmark_schema.sql"
$seedFile = Join-Path $projectRoot "database\init\041_seed_chbenchmark.sql"
$databaseUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "query_optimizer" }
$databaseName = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "query_optimizer" }

Push-Location $projectRoot
try {
    docker compose up -d --wait
    Get-Content -Raw -Encoding UTF8 $schemaFile |
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 `
            -U $databaseUser -d $databaseName
    if (-not $SchemaOnly) {
        Get-Content -Raw -Encoding UTF8 $seedFile |
            docker compose exec -T postgres psql -v ON_ERROR_STOP=1 `
                -U $databaseUser -d $databaseName
    }
}
finally {
    Pop-Location
}
