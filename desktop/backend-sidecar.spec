# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

spec_path = Path(SPECPATH).resolve()
spec_dir = spec_path if spec_path.is_dir() else spec_path.parent
project_root = spec_dir.parent
entry_script = project_root / "apps" / "launchers" / "desktop_backend.py"

hiddenimports = [
    "en_core_web_sm",
    "mlx._reprlib_fix",
    "numpy._core._exceptions",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
] + collect_submodules("mlx_audio.tts.models.kokoro")
hiddenimports += collect_submodules("mlx_lm.models")

datas = (
    collect_data_files("mlx", includes=["lib/*.metallib"])
    + collect_data_files("misaki")
    + collect_data_files("en_core_web_sm")
    + collect_data_files("espeakng_loader")
    + collect_data_files("language_tags")
    + copy_metadata("en_core_web_sm")
)

binaries = collect_dynamic_libs("espeakng_loader")

a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PyQt5",
        "PyQt6",
        "PySide6",
        "bokeh",
        "cv2",
        "datasets",
        "dask",
        "distributed",
        "django",
        "h5py",
        "langchain",
        "jupyter",
        "matplotlib",
        "nltk",
        "notebook",
        "panel",
        "plotly",
        "numba",
        "pandas",
        "pyarrow",
        "skimage",
        "sklearn",
        "statsmodels",
        "tables",
        "torchaudio",
        "tensorflow",
        "jax",
        "onnxruntime",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name="orpheus-desktop-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="orpheus-desktop-backend",
)
