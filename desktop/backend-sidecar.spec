# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from importlib.util import find_spec
import platform

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


def _collect_submodules(package):
    return collect_submodules(package) if find_spec(package) is not None else []


def _collect_data_files(package, **kwargs):
    return collect_data_files(package, **kwargs) if find_spec(package) is not None else []


def _collect_dynamic_libs(package):
    return collect_dynamic_libs(package) if find_spec(package) is not None else []


def _copy_metadata(package):
    return copy_metadata(package) if find_spec(package) is not None else []

spec_path = Path(SPECPATH).resolve()
spec_dir = spec_path if spec_path.is_dir() else spec_path.parent
project_root = spec_dir.parent
entry_script = project_root / "apps" / "launchers" / "desktop_backend.py"

hiddenimports = [
    "en_core_web_sm",
    "numpy._core._exceptions",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]
hiddenimports += _collect_submodules("modules")
hiddenimports += _collect_submodules("kokoro")

if platform.system() == "Darwin":
    hiddenimports += ["mlx._reprlib_fix"]
    hiddenimports += _collect_submodules("mlx_audio.tts.models.kokoro")
    hiddenimports += _collect_submodules("mlx_lm.models")

datas = (
    _collect_data_files("kokoro")
    + _collect_data_files("misaki")
    + _collect_data_files("en_core_web_sm")
    + _collect_data_files("espeakng_loader")
    + _collect_data_files("language_tags")
    + _copy_metadata("kokoro")
    + _copy_metadata("en_core_web_sm")
)

if platform.system() == "Darwin":
    datas += _collect_data_files("mlx", includes=["lib/*.metallib"])

binaries = _collect_dynamic_libs("espeakng_loader")

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
