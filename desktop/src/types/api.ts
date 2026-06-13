export type DashboardTab = "dashboard" | "convert" | "document" | "progress" | "library" | "logs" | "settings";

export type ThemeMode = "dark" | "light";

export type BackendConnectionState = "checking" | "connected" | "error";

export type DesktopBackendLifecycle = "stopped" | "starting" | "ready" | "failed";

export interface DesktopBackendEndpoint {
  host: string;
  port: number;
  token: string;
  base_url: string;
}

export interface DesktopBackendStatusEndpoint {
  host: string;
  port: number;
  base_url: string;
}

export interface DesktopBackendStatus {
  lifecycle: DesktopBackendLifecycle;
  endpoint: DesktopBackendStatusEndpoint | null;
  error: string | null;
}

export interface HealthResponse {
  status: string;
}

export type DesktopQueryTokenScope = "events" | "audio";

export interface DesktopQueryTokenRequest {
  scope: DesktopQueryTokenScope;
  book_id?: number;
}

export interface DesktopQueryTokenResponse {
  token: string;
  expires_at: number;
}

export interface TermSelectOption {
  value: string;
  label: string;
  search?: string;
}

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

export type ProgressStageName =
  | "ingestion"
  | "chunking"
  | "cleaning"
  | "synthesis"
  | "encoding"
  | "packaging";

type ProgressStageState = "pending" | "active" | "done";

export interface ProgressStageDetail {
  label: string;
  value: string;
  pct: number;
}

export interface ProgressStageView {
  name: ProgressStageName;
  state: ProgressStageState;
  pct: number;
  detail: ProgressStageDetail[];
}

export interface PerformanceSnapshot {
  pipeline_mode?: string;
  parallel_worker_mode?: string;
  requested_parallel_chapters?: number;
  effective_parallel_chapters?: number;
  active_workers?: number;
  max_workers?: number;
  queued_chapters?: number;
  running_chapters?: number;
  completed_chapters?: number;
  failed_chapters?: number;
  active_chapter_titles?: string[];
  cleaner_chunks_per_second?: number;
  tts_chars_per_second?: number;
  encoder_chapters_per_second?: number;
  elapsed_seconds?: number;
  eta_seconds?: number;
  fallback_reason?: string;
}

export interface EventStreamLogEntry {
  id: number;
  level: string;
  ts: string;
  msg: string;
}

export type EventStreamConnectionState = "idle" | "connecting" | "open" | "error";

export interface DesktopEventPayload {
  event_id?: string;
  timestamp?: string;
  event_type?: string;
  state?: ConversionJobStatus | string;
  stage?: string;
  progress?: number;
  overall_progress?: number;
  stage_progress?: number;
  chapter_current?: number;
  chapter_total?: number;
  chunk_current?: number;
  chunk_total?: number;
  message?: string;
  level?: string;
  job_id?: string;
  eta?: number | null;
  performance?: PerformanceSnapshot;
}

export interface LibraryBookSummary {
  id: number;
  title: string;
  author: string;
  source_path?: string | null;
  conversion_mode: ConversionMode;
  tags: string;
  source_available: boolean;
  total_chapters: number;
  completed_chapters: number;
  created_at?: string | null;
}

export interface LibraryListResponse {
  items: LibraryBookSummary[];
  count: number;
}

export interface LibraryChapterDetail {
  id: number;
  number: number;
  title: string;
  duration_ms: number | null;
  mp3_path: string | null;
}

export interface LibraryBookDetail {
  id: number;
  title: string;
  author: string;
  source_path: string | null;
  source_type: string;
  conversion_mode: ConversionMode;
  tags: string;
  source_available: boolean;
  total_chapters: number;
  playback_path: string | null;
  playback_status: string;
  playback_position_sec: number;
  created_at?: string | null;
  chapters: LibraryChapterDetail[];
  playback_url?: string | null;
}

export interface PlaybackPositionUpdateResponse {
  book_id: number;
  position_sec: number;
}

export interface LibraryBookUpdateRequest {
  title?: string | null;
  author?: string | null;
  tags?: string | null;
}

export interface CleaningPreviewResponse {
  book_id: number;
  chapter_number: number;
  title: string;
  available: boolean;
  before_text: string;
  llm_input_text: string;
  cleaned_text: string;
  trace_rows: Array<Record<string, unknown>>;
}

export interface OpenBookFolderResponse {
  book_id: number;
  folder_path: string;
}

export interface DeleteBookResponse {
  book_id: number;
  deleted: boolean;
}

export interface MaintenanceDiagnosticsResponse {
  database_path: string;
  output_path: string;
  temp_path: string;
  app_data_path: string;
  audit: {
    row_counts?: Record<string, number>;
    stale_chapter_rows?: unknown[];
    stale_book_sources?: unknown[];
    orphan_output_dirs?: string[];
    stale_temp_uploads?: string[];
    stale_temp_stage_dirs?: string[];
    [key: string]: unknown;
  };
}

export interface MaintenanceBackupResponse {
  backup_path: string;
}

export interface MaintenanceCleanupResponse {
  deleted_chapter_rows?: unknown[];
  deleted_book_rows?: unknown[];
  deleted_output_dirs?: string[];
  deleted_temp_uploads?: string[];
  deleted_temp_stage_dirs?: string[];
  moved_sources?: unknown[];
  backup_path?: string;
  [key: string]: unknown;
}
