export type ConversionMode = "audiobook" | "document_audio";

type RuntimeProbeState = "fresh" | "stale" | "pending";

export type ConversionJobStatus =
  | "idle"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown";

export type ConversionTerminalJobStatus = "completed" | "failed" | "cancelled" | "unknown";

export interface ConversionJobTerminalEvent {
  jobId: string;
  status: ConversionTerminalJobStatus;
  message: string;
}

export interface TtsVoiceOption {
  id: string;
  name: string;
  language: string;
  locale?: string | null;
  gender?: string | null;
  description?: string | null;
  style_tags?: string[];
  sample_text?: string | null;
  is_multilingual?: boolean;
}

interface TtsEngineCapabilities {
  supports_batching?: boolean;
  supports_native_batching?: boolean;
  batching_mode?: string;
  supports_streaming?: boolean;
  supported_languages?: string[];
  supports_engine_options?: boolean;
  supported_quantizations?: string[];
  [key: string]: unknown;
}

export interface TtsEngineOption {
  id: string;
  name: string;
  display_name?: string;
  available_for_conversion?: boolean;
  voices: TtsVoiceOption[];
  capabilities?: TtsEngineCapabilities;
  supported_quantizations?: string[];
  options_schema?: Record<string, unknown>;
}

/** Raw cleaner backend option shape from `/api/options/models`. */
export interface CleanerBackendOptionApi {
  name: string;
  display_name: string;
  is_local: boolean;
  requires_model?: boolean;
  requires_api_key: boolean;
  models: string[];
  supported_quantizations?: string[];
  default_device?: string;
}

/** Normalized cleaner backend option for desktop UI and runtime decisions. */
export interface CleanerBackendOption {
  id: string;
  display_name: string;
  is_local: boolean;
  requires_model: boolean;
  requires_api_key: boolean;
  models: string[];
  supported_quantizations?: string[];
  default_device?: string;
}

export interface ModelOptionsResponse {
  tts_engines: TtsEngineOption[];
  cleaner_models: string[];
  api_cleaner_models: string[];
  cuda_cleaner_models?: string[];
  cleaner_backend_options: CleanerBackendOptionApi[];
}

export interface VoiceOptionsResponse {
  engine: string;
  voices: TtsVoiceOption[];
}

interface RuntimeProbeEntry {
  available: boolean;
  latency_ms: number | null;
  error: string | null;
  [key: string]: unknown;
}

interface RuntimeProbe {
  state: RuntimeProbeState;
  refresh_in_progress: boolean;
  mode?: string;
  cache_ttl_seconds?: number;
  cache_hit?: boolean;
  tts?: RuntimeProbeEntry;
  cleaner?: RuntimeProbeEntry;
  [key: string]: unknown;
}

export interface SystemCapabilitiesResponse {
  default_tts_engine: string;
  default_tts_quantization: string;
  cleaner_runtime_availability: Record<string, boolean>;
  runtime_probe: RuntimeProbe;
  primary_runtime?: string;
  mlx_runtime_available?: boolean;
  available_tts_engines?: string[];
  cleaner_backends?: string[];
  parallel_processing_enabled?: boolean;
  runtime_validation_cache_size?: number;
  global_api_limiter?: Record<string, unknown>;
  worker_pool?: Record<string, unknown>;
  parallel_worker_recommendation?: ParallelWorkerRecommendation;
  metrics?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ParallelWorkerRecommendation {
  memory_measurement_available: boolean;
  total_memory_bytes: number | null;
  available_memory_bytes: number | null;
  reserved_memory_bytes: number | null;
  headroom_memory_bytes: number | null;
  static_estimated_worker_bytes: number;
  warmup_memory_delta_bytes: number | null;
  estimated_per_worker_memory_bytes: number;
  requested_parallel_chapters: number;
  max_safe_parallel_chapters: number;
  recommended_parallel_chapters: number;
  reason: string;
  calculation: string;
}

export interface ParallelWarmupRequest {
  voice: string;
  speed: number;
  tts_engine?: string | null;
  tts_quantization?: string | null;
  cleaner_model?: string | null;
  cleaner_backend?: string | null;
  cleaner_cuda_device?: string | null;
  cleaner_cuda_quantization?: string | null;
  cleaner_api_model?: string | null;
  cleaner_api_key?: string | null;
  max_parallel_chapters: number;
}

export interface TtsRuntimeOptions {
  model_name?: string | null;
  tts_model_name?: string | null;
  quantization?: string | null;
  provider?: string | null;
  device?: string | null;
  dtype?: string | null;
  compile_mode?: string | null;
  batch_policy?: string | null;
  model_path?: string | null;
  provider_options?: Record<string, string | number | boolean | null> | null;
}

export interface ConversionRuntimeOptions {
  voice: string;
  speed: number;
  tts_engine?: string | null;
  tts_quantization?: string | null;
  tts_provider?: string | null;
  tts_device?: string | null;
  cleaner_model?: string | null;
  cleaner_backend?: string | null;
  cleaner_cuda_device?: string | null;
  cleaner_cuda_quantization?: string | null;
  cleaner_api_model?: string | null;
  cleaner_api_key?: string | null;
  tts_options?: TtsRuntimeOptions | null;
  parallel_worker_mode?: "auto" | "manual";
  enable_parallel?: boolean | null;
  max_parallel_chapters?: number | null;
}

export interface StartConversionRequest extends Partial<ConversionRuntimeOptions> {
  source_path: string;
  conversion_mode: ConversionMode;
  title?: string | null;
  author?: string | null;
  runtime_options?: ConversionRuntimeOptions | null;
}

export interface StartConversionResponse {
  job_id: string;
  status: ConversionJobStatus;
  book_id: number | null;
  error: string | null;
}

export interface RepairConversionRequest extends Partial<ConversionRuntimeOptions> {
  runtime_options?: ConversionRuntimeOptions | null;
}

export interface OpenAIModelListResponse {
  provider: "openai";
  base_url: string;
  models: string[];
}

export interface SourcePreflight {
  title: string | null;
  author: string | null;
  chapter_count: number;
  source_type: string;
  sample: string;
  warnings: string[];
  size_bytes: number;
  requires_ocr: boolean;
  pdf_text_layer: {
    classification: "native_text" | "low_text_layer" | "ocr_required" | "unknown";
    confidence: number;
    requires_ocr: boolean;
    total_pages: number;
    sampled_pages: number;
    non_empty_pages: number;
    text_chars: number;
    word_count: number;
    image_pages: number;
    image_count: number;
  } | null;
}

export interface SourceUploadResponse {
  source_handle: string;
  original_name: string;
  size_bytes: number;
  preflight: SourcePreflight;
}

export interface SourceTextUploadRequest {
  text: string;
  title?: string | null;
}

export interface VoiceSuggestionResponse {
  engine: string;
  source_path: string;
  available_voice_count: number;
  book_sample: {
    unit?: string;
    units_analyzed?: number;
    total_units?: number;
    excerpt?: string;
  };
  book_profile: {
    book_type?: string;
    tone?: string;
  };
  voice_suggestion: {
    recommended_voice_id: string;
    recommended_voice_style?: string;
    reasoning?: string;
  };
  api_used: boolean;
}

interface ActiveConversionJob {
  id: string;
  book_id: number | null;
  status: ConversionJobStatus;
  error: string | null;
}

export interface ActiveConversionResponse {
  active: boolean;
  job: ActiveConversionJob | null;
}

export interface CancelConversionResponse {
  job_id: string;
  cancelled: boolean;
}
