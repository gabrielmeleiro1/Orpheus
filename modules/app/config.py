"""
Application Configuration
=========================
Configuration management for the audiobook creator application.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional

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
    tts_quantization: str = "bf16"
    cleaner_model_name: str = "mlx-community/Llama-3.2-3B-Instruct-4bit"
    cleaner_backend: str = "mlx"
    cleaner_api_base_url: str = "https://api.openai.com/v1"
    cleaner_api_key_env: str = "OPENAI_API_KEY"
    cleaner_api_model: Optional[str] = "gpt-5-nano"
    cleaner_api_global_concurrency: int = 6
    worker_pool_reuse_enabled: bool = True
    save_cleaning_debug: bool = True
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
    normalize_volume: bool = True
    target_dbfs: float = -16.0
    
    # Database
    db_path: Optional[Path] = None
    
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
        self.cleaner_backend = os.getenv("CLEANER_BACKEND", self.cleaner_backend).strip() or self.cleaner_backend
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
        env_pool_reuse = os.getenv("WORKER_POOL_REUSE_ENABLED")
        if env_pool_reuse is not None:
            self.worker_pool_reuse_enabled = env_pool_reuse.strip().lower() not in {"0", "false", "no", "off"}
        env_debug = os.getenv("SAVE_CLEANING_DEBUG")
        if env_debug is not None:
            self.save_cleaning_debug = env_debug.strip().lower() not in {"0", "false", "no", "off"}
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
            "tts_quantization": self.tts_quantization,
            "cleaner_model_name": self.cleaner_model_name,
            "cleaner_backend": self.cleaner_backend,
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
            "normalize_volume": self.normalize_volume,
            "target_dbfs": self.target_dbfs,
            "db_path": str(self.db_path) if self.db_path else None,
        }
