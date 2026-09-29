"""
Application Configuration
=========================
Configuration management for the audiobook creator application.
"""

from dataclasses import dataclass, field
import os
import threading
from pathlib import Path
from typing import Optional

from modules.tts.cleaner_config import resolve_cleaner_model_name

_DEFAULT_DATA_DIR = Path("data")
_DEFAULT_OUTPUT_DIR = Path("output")
_DEFAULT_TEMP_DIR = Path("temp")


def _load_env_defaults(env_file: Path) -> None:
    """Load KEY=VALUE lines from a local env file without overriding existing env."""
    if not env_file.exists() or not env_file.is_file():
        return

    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
            value = value[1:-1]

        os.environ[key] = value


def _read_path_env(env_key: str) -> Optional[Path]:
    """Return a normalized path from env or None when unset/empty."""
    raw_value = os.getenv(env_key)
    if raw_value is None:
        return None

    normalized = raw_value.strip()
    if not normalized:
        return None

    if normalized == "~" or normalized.startswith("~/") or normalized.startswith("~\\"):
        home = os.getenv("HOME")
        if home:
            suffix = normalized[2:] if len(normalized) > 1 else ""
            return Path(home, suffix)

    return Path(normalized).expanduser()


def _read_bool_env(env_key: str) -> Optional[bool]:
    """Return a boolean env override or None when unset/empty."""
    raw_value = os.getenv(env_key)
    if raw_value is None:
        return None

    normalized = raw_value.strip().lower()
    if not normalized:
        return None

    return normalized not in {"0", "false", "no", "off"}


def _resolve_path_override(current: Path, *, env_key: str, default: Path) -> Path:
    """Apply env override only when the caller did not pass a custom path."""
    if current != default:
        return current

    env_path = _read_path_env(env_key)
    if env_path is None:
        return current
    return env_path


@dataclass(frozen=True)
class SetupPreferences:
    cleaner_backend: str
    cleaner_model_name: str
    cleaner_cuda_quantization: str


@dataclass
class AppConfig:
    """
    Application configuration.
    
    Attributes:
        data_dir: Directory for application data (database, etc.)
        output_dir: Directory for output audiobooks
        temp_dir: Directory for temporary files
        default_voice: Default voice ID
        default_speed: Default speech speed
        cleaner_model_name: Default text-cleaner model ID
        cleaner_backend: Text cleaner backend (mlx, cuda, or api)
        cleaner_cuda_device: CUDA device for cleaner when backend is cuda
        cleaner_cuda_quantization: CUDA quant mode (4bit, 8bit, fp16) for cuda backend
        max_parallel_chapters: Max chapters to process in parallel
        enable_parallel: Enable parallel processing
        db_path: Path to SQLite database
    """
    
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
    _preferences_lock: object = field(default_factory=threading.RLock, init=False, repr=False, compare=False)
    _setup_preferences: Optional[SetupPreferences] = field(default=None, init=False, repr=False, compare=False)

    def setup_preferences(self) -> SetupPreferences:
        with self._preferences_lock:
            return self._setup_preferences or SetupPreferences(
                self.cleaner_backend, self.cleaner_model_name, self.cleaner_cuda_quantization,
            )
    
    def apply_setup_preferences(self, *, state: Optional[dict] = None) -> None:
        """Use the saved local model and precision as defaults for new conversions."""
        from scripts import setup_runtime
        from scripts.download_models import MODEL_GROUPS

        with self._preferences_lock:
            if state is None:
                try:
                    state, _ = setup_runtime._read_setup_state(setup_runtime.default_state_path())
                except setup_runtime.SetupExecutionError:
                    return
            if state is None:
                return
            cleaner = next((group for group in state["model_groups"] if group.startswith("cleaner-")), None)
            preferences = SetupPreferences(
                state["profile"] if cleaner else "basic",
                MODEL_GROUPS[cleaner][0] if cleaner else self.cleaner_model_name,
                state["cleaner_cuda_quantization"],
            )
            self.cleaner_backend = preferences.cleaner_backend
            self.cleaner_model_name = preferences.cleaner_model_name
            self.cleaner_cuda_quantization = preferences.cleaner_cuda_quantization
            self._setup_preferences = preferences

    def __post_init__(self):
        """Ensure directories exist and set defaults."""
        # Load local env defaults so API keys can be tracked in .env.local.
        _load_env_defaults(Path(".env.local"))

        self.data_dir = _resolve_path_override(
            self.data_dir,
            env_key="AUDIOBOOK_DATA_DIR",
            default=_DEFAULT_DATA_DIR,
        )
        self.output_dir = _resolve_path_override(
            self.output_dir,
            env_key="AUDIOBOOK_OUTPUT_DIR",
            default=_DEFAULT_OUTPUT_DIR,
        )
        self.temp_dir = _resolve_path_override(
            self.temp_dir,
            env_key="AUDIOBOOK_TEMP_DIR",
            default=_DEFAULT_TEMP_DIR,
        )

        # Optional env overrides for runtime backend selection.
        self.tts_provider = os.getenv("TTS_PROVIDER", self.tts_provider or "").strip() or None
        self.tts_device = os.getenv("TTS_DEVICE", self.tts_device or "").strip() or None
        env_tts_model = os.getenv("TTS_MODEL_NAME")
        if env_tts_model is not None:
            self.tts_model_name = env_tts_model.strip() or None
        self.cleaner_backend = os.getenv("CLEANER_BACKEND", self.cleaner_backend).strip() or self.cleaner_backend
        env_cleaner_model = os.getenv("CLEANER_MODEL_NAME")
        cleaner_model_explicitly_set = False
        if env_cleaner_model is not None:
            normalized_cleaner_model = env_cleaner_model.strip()
            if normalized_cleaner_model:
                self.cleaner_model_name = normalized_cleaner_model
                cleaner_model_explicitly_set = True
        self.cleaner_model_name = resolve_cleaner_model_name(
            backend=self.cleaner_backend,
            configured_model=self.cleaner_model_name,
            explicit_model=cleaner_model_explicitly_set,
        )
        self.cleaner_cuda_device = (
            os.getenv("CLEANER_CUDA_DEVICE", self.cleaner_cuda_device).strip()
            or self.cleaner_cuda_device
        )
        self.cleaner_cuda_quantization = (
            os.getenv("CLEANER_CUDA_QUANTIZATION", self.cleaner_cuda_quantization).strip()
            or self.cleaner_cuda_quantization
        )
        self.cleaner_api_base_url = os.getenv("CLEANER_API_BASE_URL", self.cleaner_api_base_url).strip() or self.cleaner_api_base_url
        self.cleaner_api_key_env = os.getenv("CLEANER_API_KEY_ENV", self.cleaner_api_key_env).strip() or self.cleaner_api_key_env
        env_api_model = os.getenv("CLEANER_API_MODEL")
        if env_api_model is not None:
            self.cleaner_api_model = env_api_model.strip() or None
        env_global_concurrency = os.getenv("CLEANER_API_GLOBAL_CONCURRENCY")
        if env_global_concurrency:
            try:
                self.cleaner_api_global_concurrency = max(1, int(env_global_concurrency))
            except ValueError:
                pass
        env_pool_reuse = _read_bool_env("WORKER_POOL_REUSE_ENABLED")
        if env_pool_reuse is not None:
            self.worker_pool_reuse_enabled = env_pool_reuse
        env_debug = _read_bool_env("SAVE_CLEANING_DEBUG")
        if env_debug is not None:
            self.save_cleaning_debug = env_debug
        env_emit_chapter_mp3 = _read_bool_env("AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS")
        if env_emit_chapter_mp3 is not None:
            self.emit_chapter_mp3_artifacts = env_emit_chapter_mp3
        env_chapter_aac = _read_bool_env("AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING")
        if env_chapter_aac is not None:
            self.enable_chapter_aac_packaging = env_chapter_aac
        env_pdf_backend = os.getenv("PDF_EXTRACTION_BACKEND")
        if env_pdf_backend is not None:
            requested_backend = env_pdf_backend.strip().lower()
            if requested_backend in {"fitz", "pymupdf4llm", "auto"}:
                self.pdf_extraction_backend = requested_backend
        env_enable_parallel = _read_bool_env("AUDIOBOOK_ENABLE_PARALLEL")
        if env_enable_parallel is not None:
            self.enable_parallel = env_enable_parallel
        env_max_parallel = os.getenv("AUDIOBOOK_MAX_PARALLEL_CHAPTERS")
        if env_max_parallel:
            try:
                self.max_parallel_chapters = max(1, int(env_max_parallel))
            except ValueError:
                pass
        if self.max_parallel_chapters <= 1:
            self.enable_parallel = False

        # Create directories
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # Set default db_path if not provided
        if self.db_path is None:
            env_db_path = _read_path_env("AUDIOBOOK_DB_PATH")
            if env_db_path is not None:
                self.db_path = env_db_path
            else:
                self.db_path = self.data_dir / "audiobooks.db"

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def from_dict(cls, config_dict: dict) -> "AppConfig":
        """
        Create config from dictionary.
        
        Args:
            config_dict: Configuration dictionary
            
        Returns:
            AppConfig instance
        """
        # Convert path strings to Path objects
        path_fields = {"data_dir", "output_dir", "temp_dir", "db_path"}
        processed = {}
        
        for key, value in config_dict.items():
            if key in path_fields and value is not None:
                processed[key] = Path(value)
            else:
                processed[key] = value
        
        return cls(**processed)
    
    def to_dict(self) -> dict:
        """
        Convert config to dictionary.
        
        Returns:
            Configuration dictionary
        """
        return {
            "data_dir": str(self.data_dir),
            "output_dir": str(self.output_dir),
            "temp_dir": str(self.temp_dir),
            "default_voice": self.default_voice,
            "default_speed": self.default_speed,
            "tts_engine": self.tts_engine,
            "tts_provider": self.tts_provider,
            "tts_device": self.tts_device,
            "tts_model_name": self.tts_model_name,
            "tts_quantization": self.tts_quantization,
            "cleaner_model_name": self.cleaner_model_name,
            "cleaner_backend": self.cleaner_backend,
            "cleaner_cuda_device": self.cleaner_cuda_device,
            "cleaner_cuda_quantization": self.cleaner_cuda_quantization,
            "cleaner_api_base_url": self.cleaner_api_base_url,
            "cleaner_api_key_env": self.cleaner_api_key_env,
            "cleaner_api_model": self.cleaner_api_model,
            "cleaner_api_global_concurrency": self.cleaner_api_global_concurrency,
            "worker_pool_reuse_enabled": self.worker_pool_reuse_enabled,
            "save_cleaning_debug": self.save_cleaning_debug,
            "pdf_extraction_backend": self.pdf_extraction_backend,
            "enable_parallel": self.enable_parallel,
            "max_parallel_chapters": self.max_parallel_chapters,
            "chunk_size": self.chunk_size,
            "enable_tts_file_batch_results": self.enable_tts_file_batch_results,
            "keep_tts_engine_warm": self.keep_tts_engine_warm,
            "tts_idle_timeout_seconds": self.tts_idle_timeout_seconds,
            "mp3_bitrate": self.mp3_bitrate,
            "emit_chapter_mp3_artifacts": self.emit_chapter_mp3_artifacts,
            "enable_chapter_aac_packaging": self.enable_chapter_aac_packaging,
            "normalize_volume": self.normalize_volume,
            "target_dbfs": self.target_dbfs,
            "db_path": str(self.db_path) if self.db_path else None,
        }
