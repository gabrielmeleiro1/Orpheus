"""Pure application defaults and precedence, independent of process state."""

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping, Optional

from modules.tts.cleaner_config import resolve_cleaner_model_name

_DEFAULT_DATA_DIR = Path("data")
_DEFAULT_OUTPUT_DIR = Path("output")
_DEFAULT_TEMP_DIR = Path("temp")


@dataclass
class ConfigValues:
    """Constructor defaults shared by configuration construction and resolution."""

    # Directories
    data_dir: Path = field(default_factory=lambda: _DEFAULT_DATA_DIR)
    output_dir: Path = field(default_factory=lambda: _DEFAULT_OUTPUT_DIR)
    temp_dir: Path = field(default_factory=lambda: _DEFAULT_TEMP_DIR)

    # TTS defaults
    default_voice: str = "am_adam"
    default_speed: float = 1.0
    tts_engine: str = "kokoro"
    tts_provider: Optional[str] = None
    tts_device: Optional[str] = None
    tts_model_name: Optional[str] = None
    tts_quantization: str = "bf16"
    cleaner_model_name: str = "mlx-community/Llama-3.2-3B-Instruct-4bit"
    cleaner_backend: str = "mlx"
    cleaner_cuda_device: str = "cuda"
    cleaner_cuda_quantization: str = "4bit"
    cleaner_api_base_url: str = "https://api.openai.com/v1"
    cleaner_api_key_env: str = "OPENAI_API_KEY"
    cleaner_api_model: Optional[str] = "gpt-5-nano"
    cleaner_api_global_concurrency: int = 6
    worker_pool_reuse_enabled: bool = True
    save_cleaning_debug: bool = False
    pdf_extraction_backend: str = "fitz"

    # Pipeline settings
    enable_parallel: bool = False
    max_parallel_chapters: int = 4
    chunk_size: int = 700
    enable_tts_file_batch_results: bool = False
    keep_tts_engine_warm: bool = True
    tts_idle_timeout_seconds: float = 300.0

    # Audio settings
    mp3_bitrate: str = "128k"
    emit_chapter_mp3_artifacts: bool = False
    enable_chapter_aac_packaging: bool = True
    normalize_volume: bool = True
    target_dbfs: float = -16.0

    # Database
    db_path: Optional[Path] = None


@dataclass(frozen=True)
class ConfigResolution:
    """Effective values and source labels; repr never includes values.

    AppConfig stores the name of the API-key environment variable, not the key.
    Actual API keys and Hub tokens in environ are neither copied nor resolved.
    Keep values out of repr as paths and URLs can also contain credentials.
    """

    values: dict[str, object] = field(repr=False)
    provenance: dict[str, str]


def _path_value(raw: str | None, environ: Mapping[str, str], expanded_paths: Mapping[str, Path]) -> Path | None:
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip()
    if normalized == "~" or normalized.startswith(("~/", "~\\")):
        home = environ.get("HOME")
        if home:
            return Path(home, normalized[2:] if len(normalized) > 1 else "")
    if normalized in expanded_paths:
        return expanded_paths[normalized]
    path = Path(normalized)
    if str(path).startswith("~"):
        raise RuntimeError("Home expansion must be supplied as a read-only input")
    return path


def resolve_setup_values(
    values: Mapping[str, object], *, state: Mapping[str, object],
    model_groups: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    """Resolve only setup-owned fields, without normalizing live edits again."""
    cleaner = next((group for group in state["model_groups"] if group.startswith("cleaner-")), None)
    return {
        "cleaner_backend": state["profile"] if cleaner else "basic",
        "cleaner_model_name": model_groups[cleaner][0] if cleaner else values["cleaner_model_name"],
        "cleaner_cuda_quantization": state["cleaner_cuda_quantization"],
    }


def resolve_database_path(
    environ: Mapping[str, str], *, data_dir: Path, db_path: Path | None,
    expanded_paths: Mapping[str, Path] | None = None,
) -> tuple[Path, str]:
    """Resolve the database separately so construction can preserve I/O order."""
    if db_path is not None:
        return db_path, "constructor"
    path = _path_value(environ.get("AUDIOBOOK_DB_PATH"), environ, expanded_paths or {})
    if path is not None:
        return path, "env"
    return data_dir / "audiobooks.db", "default"


def resolve_config(
    environ: Mapping[str, str],
    overrides: Mapping[str, object] | None = None,
    *,
    setup_state: Mapping[str, object] | None = None,
    model_groups: Mapping[str, tuple[str, ...]] | None = None,
    expanded_paths: Mapping[str, Path] | None = None,
    defer_database: bool = False,
) -> ConfigResolution:
    """Resolve values without reading files, process environment, or host accounts.

    Overrides use AppConfig constructor types (in particular, Path for paths).
    Supply host-expanded tilde paths when HOME cannot resolve them. Setup is
    optional and requires its read-only model catalog. Sources describe winning
    inputs; normalization and derived defaults retain their input source.
    A chapter limit that disables parallelism supplies that flag's source too.
    AppConfig defers the database stage until after creating its directories;
    in that mode db_path retains its constructor/default value and source.
    """
    defaults = ConfigValues()
    values = {item.name: getattr(defaults, item.name) for item in fields(ConfigValues)}
    provenance = dict.fromkeys(values, "default")
    for name, value in (overrides or {}).items():
        if name not in values:
            raise TypeError(f"Unknown configuration field: {name}")
        values[name] = value
        provenance[name] = "constructor"

    def assign(name: str, value: object, source: str = "env") -> None:
        values[name] = value
        provenance[name] = source

    for name in ("data_dir", "output_dir", "temp_dir"):
        if values[name] == getattr(defaults, name):
            path = _path_value(environ.get("AUDIOBOOK_" + name.upper()), environ, expanded_paths or {})
            if path is not None:
                assign(name, path)

    for name in ("tts_provider", "tts_device"):
        key = name.upper()
        value = environ.get(key, values[name] or "").strip() or None
        assign(name, value, "env" if key in environ else provenance[name])
    for name in ("tts_model_name", "cleaner_api_model"):
        raw = environ.get(name.upper())
        if raw is not None:
            assign(name, raw.strip() or None)
    for name in (
        "cleaner_backend", "cleaner_cuda_device", "cleaner_cuda_quantization",
        "cleaner_api_base_url", "cleaner_api_key_env",
    ):
        key = name.upper()
        normalized = environ.get(key, values[name]).strip()
        if normalized:
            assign(name, normalized, "env" if key in environ else provenance[name])

    model = environ.get("CLEANER_MODEL_NAME")
    explicit_model = model is not None and bool(model.strip())
    if explicit_model:
        assign("cleaner_model_name", model.strip())
    values["cleaner_model_name"] = resolve_cleaner_model_name(
        backend=values["cleaner_backend"], configured_model=values["cleaner_model_name"],
        explicit_model=explicit_model,
    )
    for name, key in (
        ("cleaner_api_global_concurrency", "CLEANER_API_GLOBAL_CONCURRENCY"),
        ("max_parallel_chapters", "AUDIOBOOK_MAX_PARALLEL_CHAPTERS"),
    ):
        raw = environ.get(key)
        if raw:
            try:
                assign(name, max(1, int(raw)))
            except ValueError:
                pass
    for name, key in (
        ("worker_pool_reuse_enabled", "WORKER_POOL_REUSE_ENABLED"),
        ("save_cleaning_debug", "SAVE_CLEANING_DEBUG"),
        ("emit_chapter_mp3_artifacts", "AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS"),
        ("enable_chapter_aac_packaging", "AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING"),
        ("enable_parallel", "AUDIOBOOK_ENABLE_PARALLEL"),
    ):
        raw = environ.get(key)
        if raw is not None and raw.strip():
            assign(name, raw.strip().lower() not in {"0", "false", "no", "off"})
    raw = environ.get("PDF_EXTRACTION_BACKEND")
    if raw is not None and raw.strip().lower() in {"fitz", "pymupdf4llm", "auto"}:
        assign("pdf_extraction_backend", raw.strip().lower())
    if values["max_parallel_chapters"] <= 1:
        assign("enable_parallel", False, provenance["max_parallel_chapters"])

    if not defer_database:
        path, source = resolve_database_path(
            environ, data_dir=values["data_dir"], db_path=values["db_path"],
            expanded_paths=expanded_paths,
        )
        assign("db_path", path, source)

    if setup_state is not None:
        if model_groups is None:
            raise ValueError("Setup resolution requires the model catalog")
        selected = resolve_setup_values(values, state=setup_state, model_groups=model_groups)
        has_cleaner = any(group.startswith("cleaner-") for group in setup_state["model_groups"])
        for name, value in selected.items():
            if name != "cleaner_model_name" or has_cleaner:
                assign(name, value, "setup")
    return ConfigResolution(values, provenance)
