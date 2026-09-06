"""Static request schemas for conversion endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.contracts.conversion_runtime import merge_runtime_options
from modules.contracts.conversion_runtime_options import ConversionRuntimeOptions
from modules.contracts.limits import MAX_BOOK_TITLE_LENGTH


class _InfNanSafeModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class ConversionRuntimeOptionsRequest(_InfNanSafeModel):
    voice: str = "am_adam"
    speed: float = Field(default=1.0, ge=0.5, le=3.0)
    tts_engine: str | None = None
    tts_quantization: str | None = None
    tts_provider: str | None = None
    tts_device: str | None = None
    cleaner_model: str | None = None
    cleaner_backend: str | None = None
    cleaner_cuda_device: str | None = None
    cleaner_cuda_quantization: str | None = None
    cleaner_api_model: str | None = None
    cleaner_api_key: str | None = None
    tts_options: Mapping[str, object] | None = None
    parallel_worker_mode: str | None = None
    enable_parallel: bool | None = None
    max_parallel_chapters: int | None = Field(default=None, ge=1)

    def to_app_options(self) -> ConversionRuntimeOptions:
        return ConversionRuntimeOptions(
            voice=self.voice,
            speed=self.speed,
            tts_engine=self.tts_engine,
            tts_quantization=self.tts_quantization,
            tts_provider=self.tts_provider,
            tts_device=self.tts_device,
            cleaner_model=self.cleaner_model,
            cleaner_backend=self.cleaner_backend,
            cleaner_cuda_device=self.cleaner_cuda_device,
            cleaner_cuda_quantization=self.cleaner_cuda_quantization,
            cleaner_api_model=self.cleaner_api_model,
            cleaner_api_key=self.cleaner_api_key,
            tts_options=self.tts_options,
            parallel_worker_mode=self.parallel_worker_mode,
            enable_parallel=self.enable_parallel,
            max_parallel_chapters=self.max_parallel_chapters,
        )


class _RuntimeOptionsRequest(ConversionRuntimeOptionsRequest):
    runtime_options: ConversionRuntimeOptionsRequest | None = None

    def to_runtime_options(self) -> ConversionRuntimeOptions:
        if self.runtime_options is None:
            return self.to_app_options()

        return merge_runtime_options(
            self.to_app_options(),
            self.runtime_options.to_app_options(),
            nested_fields_set=set(self.runtime_options.model_fields_set),
        )


class StartConversionRequest(_RuntimeOptionsRequest):
    source_path: str = Field(..., min_length=1)
    conversion_mode: Literal["audiobook", "document_audio"] = "audiobook"
    title: str | None = Field(default=None, max_length=MAX_BOOK_TITLE_LENGTH)
    author: str | None = None


class RepairConversionRequest(_RuntimeOptionsRequest):
    pass
