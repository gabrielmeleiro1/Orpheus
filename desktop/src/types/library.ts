import type { ConversionMode } from "./conversion";

export interface LibraryBookSummary {
  id: number;
  title: string;
  author: string | null;
  source_path?: string | null;
  conversion_mode: ConversionMode;
  tags: string;
  favorite: boolean;
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
  author: string | null;
  source_path: string | null;
  source_type: string;
  conversion_mode: ConversionMode;
  tags: string;
  favorite: boolean;
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
  favorite?: boolean | null;
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
