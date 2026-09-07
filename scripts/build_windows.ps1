param(
    [switch]$SkipInstaller,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$icon = Join-Path $projectRoot "assets\pqo.ico"
$spec = Join-Path $projectRoot "packaging\PredictiveQueryOptimization.spec"
$executable = Join-Path $projectRoot "dist\PredictiveQueryOptimization\PredictiveQueryOptimization.exe"
$pyinstallerRunner = Join-Path $projectRoot "packaging\build_support\run_pyinstaller.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment .venv is missing."
}

Push-Location $projectRoot
try {
    if (-not $SkipDependencyInstall) {
        & $python -m pip install -e ".[desktop,build]"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    & $python scripts\generate_app_icon.py $icon
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python $pyinstallerRunner --noconfirm --clean $spec
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not (Test-Path -LiteralPath $executable)) {
        throw "PyInstaller did not create $executable"
    }

    $env:QT_QPA_PLATFORM = "offscreen"
    $process = Start-Process -FilePath $executable -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Frozen application smoke test failed with exit code $($process.ExitCode)."
    }

    if (-not $SkipInstaller) {
        $innoCandidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
        )
        $iscc = $innoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $iscc) {
            throw "Inno Setup 6 is missing. Install JRSoftware.InnoSetup or use -SkipInstaller."
        }
        & $iscc packaging\installer.iss
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    Get-FileHash $executable -Algorithm SHA256
    Get-ChildItem dist\installer\*.exe -ErrorAction SilentlyContinue | Get-FileHash -Algorithm SHA256
}
finally {
    Pop-Location
}
