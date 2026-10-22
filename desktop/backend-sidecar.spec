# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from importlib.util import find_spec
import os
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


def _runtime_profile():
    configured = os.environ.get("ORPHEUS_RUNTIME_PROFILE", "").strip().lower()
    if not configured:
        configured = (
            "mlx"
            if platform.system() == "Darwin" and platform.machine().lower() == "arm64"
            else "base"
        )
    if configured not in {"base", "mlx", "cuda"}:
        raise ValueError(f"Unknown ORPHEUS_RUNTIME_PROFILE: {configured!r}")
    if configured == "mlx" and not (
        platform.system() == "Darwin" and platform.machine().lower() == "arm64"
    ):
        raise ValueError("The MLX runtime profile requires arm64 macOS.")
    if configured == "mlx":
        version = platform.mac_ver()[0]
        try:
            major_version = int(version.split(".", 1)[0])
        except (AttributeError, ValueError):
            major_version = 0
        if major_version < 14:
            raise ValueError("The MLX runtime profile requires macOS 14 or newer.")
    if configured == "cuda" and platform.system() not in {"Linux", "Windows"}:
        raise ValueError("The CUDA runtime profile requires Linux or Windows.")
    return configured

spec_path = Path(SPECPATH).resolve()
spec_dir = spec_path if spec_path.is_dir() else spec_path.parent
project_root = spec_dir.parent
entry_script = project_root / "apps" / "launchers" / "desktop_backend.py"
runtime_profile = _runtime_profile()
MLX_PHONEMIZER_HIDDEN_IMPORTS = ("misaki.en", "misaki.espeak")

hiddenimports = [
    "numpy._core._exceptions",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]
hiddenimports += _collect_submodules("modules")

if runtime_profile == "mlx":
    hiddenimports += ["en_core_web_sm", "spacy", "thinc"]
    hiddenimports += ["mlx._reprlib_fix"]
    hiddenimports += MLX_PHONEMIZER_HIDDEN_IMPORTS
    hiddenimports += _collect_submodules("mlx_audio.tts.models.kokoro")
    hiddenimports += _collect_submodules("mlx_lm.models")
elif runtime_profile == "cuda":
    hiddenimports += ["en_core_web_sm", "spacy", "thinc"]
    hiddenimports += _collect_submodules("kokoro")
    hiddenimports += _collect_submodules("bitsandbytes")

datas = []

# The setup status endpoint validates installed packages against these fixed
# manifests at runtime; without them every capability probe fails.
datas += [
    (str(project_root / "requirements" / f"{name}.txt"), "requirements")
    for name in ("base", "mlx", "cuda")
]
# huggingface_hub looks for hf_xet's distribution metadata before using it;
# without it model downloads fall back to plain HTTP.
datas += _copy_metadata("hf_xet")

if runtime_profile in {"mlx", "cuda"}:
    datas += _collect_data_files("en_core_web_sm") + _copy_metadata("en_core_web_sm")
    datas += _collect_data_files("misaki")

datas += _collect_data_files("espeakng_loader")
datas += _collect_data_files("language_tags", includes=["data/json/*.json"])

if runtime_profile == "mlx":
    datas += _collect_data_files("mlx", includes=["lib/*.metallib"])
elif runtime_profile == "cuda":
    # PyTorch inspects Kokoro module source while loading its scripted layers.
    datas += _collect_data_files("kokoro", include_py_files=True) + _copy_metadata("kokoro")

binaries = []
if runtime_profile in {"mlx", "cuda"}:
    binaries += _collect_dynamic_libs("espeakng_loader")
if runtime_profile == "mlx":
    # libmlx.dylib references @rpath/libjaccl.dylib. PyInstaller cannot resolve
    # @rpath deps and only warns; without an explicit collect the frozen MLX
    # import fails even though mlx.metallib and libmlx.dylib are present.
    binaries += _collect_dynamic_libs("mlx")
if runtime_profile == "cuda":
    binaries += _collect_dynamic_libs("bitsandbytes")

profile_excludes = []
if runtime_profile in {"base", "mlx"}:
    profile_excludes += ["torch", "torchaudio", "kokoro", "bitsandbytes"]
if runtime_profile == "base":
    profile_excludes += [
        "transformers",
        "mlx",
        "mlx_lm",
        "mlx_audio",
        "spacy",
        "thinc",
        "en_core_web_sm",
    ]

a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=profile_excludes + [
        # fsspec's PyInstaller hook can discover its test conftest, which
        # otherwise pulls the complete pytest and Hypothesis stacks into PYZ.
        "fsspec.conftest",
        "hypothesis",
        "pytest",
        "_pytest",
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
