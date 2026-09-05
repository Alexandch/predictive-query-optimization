# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path.cwd()
if not (ROOT / "src" / "pqo").is_dir():
    ROOT = Path(SPECPATH).resolve().parent.parent

datas = [
    (str(ROOT / "assets" / "pqo.ico"), "assets"),
    (str(ROOT / "models"), "models"),
    (str(ROOT / "dataset" / "postgresql" / "aviation_dataset.csv"), "dataset/postgresql"),
    (
        str(ROOT / "dataset" / "postgresql" / "multidomain_dqn_augmented.jsonl"),
        "dataset/postgresql",
    ),
]
datas += [
    entry
    for entry in collect_data_files("xgboost")
    if Path(entry[0]).name in {"VERSION", "py.typed"}
]

hiddenimports = [
    "sklearn.compose._column_transformer",
    "sklearn.pipeline",
    "sklearn.preprocessing._encoders",
    "sqlglot.dialects.postgres",
    "xgboost.sklearn",
]

analysis = Analysis(
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=collect_dynamic_libs("xgboost"),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PIL",
        "_pytest",
        "matplotlib",
        "notebook",
        "pytest",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PredictiveQueryOptimization",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ROOT / "assets" / "pqo.ico"),
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PredictiveQueryOptimization",
)
