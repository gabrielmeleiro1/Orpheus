"""Configuration and trace models for text cleaning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from modules.tts.runtime_options import validate_device
from modules.tts.runtime_spec import (
    CLEANER_CUDA_DEFAULT_MODEL,
    CLEANER_MLX_DEFAULT_MODEL,
)

# Curated MLX model options for text cleaning (backend="mlx").
# The default remains first.
CLEANER_MODEL_CHOICES = [
    CLEANER_MLX_DEFAULT_MODEL,
    "mlx-community/Llama-3.2-1B-Instruct-4bit",
]
# Curated Hugging Face model options for CUDA text cleaning (backend="cuda").
CUDA_CLEANER_MODEL_CHOICES = [
    CLEANER_CUDA_DEFAULT_MODEL,
    "Qwen/Qwen2.5-3B-Instruct",
]
DEFAULT_CUDA_MODEL = CLEANER_CUDA_DEFAULT_MODEL
CUDA_CLEANER_QUANTIZATIONS = ("4bit", "8bit", "fp16")

DEFAULT_API_MODEL = "gpt-5-nano"
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
API_MODEL_CHOICES = [
    DEFAULT_API_MODEL,
    "gpt-5-mini",
    "gpt-4o-mini",
    "gpt-4o",
    "moonshot-v1-8k",
]
@dataclass
class CleanerConfig:
    """Configuration for text cleaner.

    ``backend`` selects the registered strategy: ``mlx`` (Apple Silicon),
    ``cuda`` (NVIDIA GPU), or ``api`` (remote). Local backends use
    ``model_name`` with their respective catalogs; CUDA also uses
    ``cuda_device`` and ``cuda_quantization``.
    """

    model_name: str = CLEANER_MLX_DEFAULT_MODEL
    max_tokens: int = 2048
    temperature: float = 0.0
    batch_size: int = 1
    cache_model: bool = True
    skip_llm_when_not_needed: bool = True
    backend: str = "mlx"
    cuda_device: str = "cuda"
    cuda_quantization: str = "4bit"
    api_base_url: str = DEFAULT_API_BASE_URL
    api_key_env: str = "OPENAI_API_KEY"
    api_key: Optional[str] = None
    api_timeout_seconds: int = 120
    api_model: Optional[str] = DEFAULT_API_MODEL
    api_batch_concurrency: int = 6
    api_global_limiter: Optional[Any] = None
    api_ca_bundle: Optional[str] = None
    api_allow_insecure_ssl: bool = False
    artifact_gate_light_threshold: float = 0.20
    artifact_gate_heavy_threshold: float = 0.55
    source_type: Optional[str] = None


@dataclass
class CleanTrace:
    """Structured cleaner trace for before/after inspection."""

    original_text: str
    rule_based_text: str
    cleaned_text: str
    mode: Literal["rule_only", "llm_success", "llm_fallback", "hallucination_fallback"]
    llm_output: Optional[str] = None
    llm_error: Optional[str] = None
    artifact_score: Optional[dict[str, Any]] = None


def list_cleaner_models() -> list[str]:
    """Return MLX cleaner model identifiers (backend=\"mlx\")."""
    return CLEANER_MODEL_CHOICES.copy()


def list_cuda_cleaner_models() -> list[str]:
    """Return CUDA cleaner model identifiers (backend=\"cuda\")."""
    return CUDA_CLEANER_MODEL_CHOICES.copy()


def normalize_cuda_device(value: str) -> str:
    """Normalize and validate a CUDA device identifier."""
    normalized = str(value or "").strip()
    validate_device("cuda", normalized)
    return normalized


def normalize_cuda_quantization(value: str) -> str:
    """Normalize and validate a CUDA cleaner quantization mode."""
    normalized = str(value or "").strip().lower()
    if normalized not in CUDA_CLEANER_QUANTIZATIONS:
        available = ", ".join(CUDA_CLEANER_QUANTIZATIONS)
        raise ValueError(
            f"Unsupported CUDA cleaner quantization '{value}'. "
            f"Supported values: {available}."
        )
    return normalized


def list_api_cleaner_models() -> list[str]:
    """Return API model identifiers offered to users."""
    return API_MODEL_CHOICES.copy()


def resolve_cleaner_model_name(
    *,
    backend: str,
    requested_model: Optional[str] = None,
    configured_model: Optional[str] = None,
    explicit_model: bool = False,
) -> str:
    """Resolve the effective cleaner model for a backend and request context."""
    normalized_backend = str(backend or "mlx").strip().lower()
    if requested_model:
        return requested_model

    config_model = configured_model or CLEANER_MLX_DEFAULT_MODEL
    if normalized_backend == "cuda" and not explicit_model:
        if config_model in CLEANER_MODEL_CHOICES:
            return DEFAULT_CUDA_MODEL
        return config_model
    if normalized_backend != "cuda" and config_model in CUDA_CLEANER_MODEL_CHOICES:
        return CLEANER_MLX_DEFAULT_MODEL
    return config_model


def build_cleaner_config_from_pipeline(config: Any) -> CleanerConfig:
    """Build CleanerConfig from a PipelineConfig-like object."""
    return CleanerConfig(
        model_name=config.cleaner_model_name,
        backend=config.cleaner_backend,
        cuda_device=config.cleaner_cuda_device,
        cuda_quantization=config.cleaner_cuda_quantization,
        api_base_url=config.cleaner_api_base_url,
        api_key_env=config.cleaner_api_key_env,
        api_key=config.cleaner_api_key,
        api_model=config.cleaner_api_model,
        api_batch_concurrency=config.cleaner_api_global_concurrency,
        api_global_limiter=config.cleaner_api_global_limiter,
        skip_llm_when_not_needed=config.cleaner_skip_llm_when_not_needed,
        source_type=getattr(config, "source_type", None),
        cache_model=True,
    )


def build_cleaner_config_from_worker_init(worker_init_config: dict[str, Any]) -> CleanerConfig:
    """Build CleanerConfig from serialized worker initialization settings."""
    return CleanerConfig(
        model_name=worker_init_config["cleaner_model_name"],
        backend=worker_init_config["cleaner_backend"],
        cuda_device=worker_init_config.get("cleaner_cuda_device", "cuda"),
        cuda_quantization=worker_init_config.get("cleaner_cuda_quantization", "4bit"),
        api_base_url=worker_init_config["cleaner_api_base_url"],
        api_key_env=worker_init_config["cleaner_api_key_env"],
        api_key=worker_init_config.get("cleaner_api_key"),
        api_model=worker_init_config["cleaner_api_model"],
        api_batch_concurrency=worker_init_config.get(
            "cleaner_api_global_concurrency",
            6,
        ),
        api_global_limiter=worker_init_config.get("cleaner_api_global_limiter"),
        skip_llm_when_not_needed=worker_init_config.get(
            "cleaner_skip_llm_when_not_needed",
            True,
        ),
        source_type=worker_init_config.get("source_type"),
        cache_model=True,
    )


def list_cleaner_backends() -> list[str]:
    """Return registered cleaner backend identifiers (mlx, cuda, api)."""
    from modules.tts.cleaner_backends.registry import available_cleaner_backends

    return list(available_cleaner_backends())


def normalize_cleaner_backend(value: str) -> str:
    """Normalize and validate a text cleaner backend identifier."""
    from modules.tts.cleaner_backends.registry import validate_cleaner_backend

    return validate_cleaner_backend(value)


CLEANER_PROMPT_TEMPLATE = """You are a minimal text-correction assistant for a text-to-speech system.

Your ONLY job is to fix extraction artifacts so the text reads naturally when spoken aloud.
You must NOT rewrite, paraphrase, or add any words that were not already present or clearly intended.
Assume each input should be read as a coherent paragraph by a TTS narrator.
Likely cleanup reason: {reason}

What you MUST fix:
1. Remove stray formatting characters left over from PDF/OCR extraction (e.g., lone "#", "|", "*", "_", "`")
2. Rejoin words that were broken by page/column breaks (e.g., "rom the moment" → "From the moment")
3. Remove orphaned single letters that are clearly extraction errors (e.g., "ten # F years" → "ten years")
4. Fix obviously garbled punctuation or spacing from extraction
5. Remove leftover HTML tags (e.g., "<br>"), markdown table syntax, and inline numeric reference markers like "[5]"
6. If a token/number looks obviously corrupted by OCR (split letters, dropped leading letters, malformed casing), repair it conservatively

What you must NOT do:
- If you are not certain a change fixes an extraction artifact, return the text unchanged
- preserve literary repetition, emphasis, dialect, unusual phrasing, and intentional fragments
- preserve epigraphs, poems, titles, foreign-language text, roman numerals, and all-caps emphasis unless they are clearly OCR damage
- Do NOT expand abbreviations (e.g., keep "Dr." as "Dr.", keep "etc." as "etc.")
- Do NOT convert numbers to words (e.g., keep "1987" as "1987")
- Do NOT rephrase or reorder sentences
- Do NOT add words, commentary, or explanations
- Do NOT change the meaning of the text in any way

Return ONLY the corrected text, nothing else.

Text to correct:
---
{text}
---

Corrected text:"""


def build_cleaner_prompt(text: str, *, reason: str = "general conservative cleanup") -> str:
    """Build the cleaner prompt with the gate reason scoped into the task."""
    return CLEANER_PROMPT_TEMPLATE.format(reason=reason, text=text)
