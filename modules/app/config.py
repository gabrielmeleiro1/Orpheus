"""
Application Configuration
=========================
Configuration management for the audiobook creator application.
"""

from dataclasses import dataclass, field, fields
import os
import threading
from pathlib import Path
from typing import Optional

from modules.app.config_resolution import (
    ConfigValues,
    resolve_config,
    resolve_database_path,
    resolve_setup_values,
)


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


def _expanded_path_input(env_key: str) -> dict[str, Path]:
    """Read host account paths only when supplied HOME cannot expand a tilde."""
    raw = os.environ.get(env_key, "").strip()
    if str(Path(raw)).startswith("~") and not (
        os.environ.get("HOME") and (raw == "~" or raw.startswith(("~/", "~\\")))
    ):
        return {raw: Path(raw).expanduser()}
    return {}


@dataclass(frozen=True)
class SetupPreferences:
    cleaner_backend: str
    cleaner_model_name: str
    cleaner_cuda_quantization: str


@dataclass
class AppConfig(ConfigValues):
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
    
    _preferences_lock: object = field(default_factory=threading.RLock, init=False, repr=False, compare=False)
    _setup_preferences: Optional[SetupPreferences] = field(default=None, init=False, repr=False, compare=False)
    _pre_setup_preferences: Optional[SetupPreferences] = field(default=None, init=False, repr=False, compare=False)

    def setup_preferences(self) -> SetupPreferences:
        with self._preferences_lock:
            return self._setup_preferences or SetupPreferences(
                self.cleaner_backend, self.cleaner_model_name, self.cleaner_cuda_quantization,
            )
    
    def apply_setup_preferences(self, *, state: Optional[dict] = None) -> None:
        """Use the saved local model and precision as defaults for new conversions."""
        from modules.runtime_setup import state as setup_runtime
        from modules.runtime_setup.catalog import MODEL_GROUPS

        with self._preferences_lock:
            if state is None:
                try:
                    state, _ = setup_runtime._read_setup_state(setup_runtime.default_state_path())
                except setup_runtime.SetupExecutionError:
                    return
            if state is None:
                return
            if self._pre_setup_preferences is None:
                self._pre_setup_preferences = SetupPreferences(
                    self.cleaner_backend, self.cleaner_model_name, self.cleaner_cuda_quantization,
                )
            values = resolve_setup_values(
                vars(self), state=state, model_groups=MODEL_GROUPS,
            )
            preferences = SetupPreferences(**values)
            self.cleaner_backend = preferences.cleaner_backend
            self.cleaner_model_name = preferences.cleaner_model_name
            self.cleaner_cuda_quantization = preferences.cleaner_cuda_quantization
            self._setup_preferences = preferences

    def reset_setup_preferences(self) -> None:
        """Restore defaults owned by setup after an explicit successful reset."""
        with self._preferences_lock:
            baseline = self._pre_setup_preferences
            applied = self._setup_preferences
            if baseline is not None and applied is not None:
                for name in ("cleaner_backend", "cleaner_model_name", "cleaner_cuda_quantization"):
                    if getattr(self, name) == getattr(applied, name):
                        setattr(self, name, getattr(baseline, name))
            self._setup_preferences = None
            self._pre_setup_preferences = None

    def __post_init__(self):
        """Ensure directories exist and set defaults."""
        # Load local env defaults so API keys can be tracked in .env.local.
        _load_env_defaults(Path(".env.local"))

        defaults = ConfigValues()
        overrides = {
            item.name: getattr(self, item.name)
            for item in fields(ConfigValues)
        }
        # Host account lookup stays outside the pure resolver. Ordinary ~/ paths
        # use the supplied HOME value, including the existing Windows spelling.
        expanded_paths = {}
        for name in ("data_dir", "output_dir", "temp_dir"):
            if getattr(self, name) != getattr(defaults, name):
                continue
            expanded_paths.update(_expanded_path_input("AUDIOBOOK_" + name.upper()))
        resolved = resolve_config(
            os.environ, overrides, expanded_paths=expanded_paths, defer_database=True,
        )
        for name, value in resolved.values.items():
            if name != "db_path":
                setattr(self, name, value)

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path, _ = resolve_database_path(
            os.environ, data_dir=self.data_dir, db_path=self.db_path,
            expanded_paths=_expanded_path_input("AUDIOBOOK_DB_PATH") if self.db_path is None else {},
        )
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
