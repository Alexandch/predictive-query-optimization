$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$predict = Join-Path $PSScriptRoot "..\.venv\Scripts\pqo-predict.exe"
$recommend = Join-Path $PSScriptRoot "..\.venv\Scripts\pqo-recommend.exe"
$xgboostModel = Join-Path $PSScriptRoot "..\models\xgboost\xgboost_query_time.joblib"
$dqnModel = Join-Path $PSScriptRoot "..\models\dqn\dqn_index_advisor.pt"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment is missing. Follow docs/training-guide.md first."
}

docker compose up -d --wait
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose could not start PostgreSQL. Start Docker Desktop and retry."
}
$env:PQO_INTEGRATION_TESTS = "1"
& $python -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$query = "SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure"
& $predict $xgboostModel $query
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $recommend $dqnModel $query
exit $LASTEXITCODE
