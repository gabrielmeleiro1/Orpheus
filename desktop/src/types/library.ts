import type { ConversionMode } from "./conversion";

export interface LibraryIdentity {
  library_id?: string;
  library_epoch?: string;
}

export interface LibraryBookSummary extends LibraryIdentity {
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
  /** Whether any playable audio exists on disk; absent from older backends. */
  audio_available?: boolean | null;
}

export interface LibraryListResponse extends LibraryIdentity {
  items: LibraryBookSummary[];
  count: number;
  limit?: number;
  offset?: number;
}

export interface LibraryCountResponse {
  count: number;
}

export interface LibraryChapterDetail {
  id: number;
  number: number;
  title: string;
  duration_ms: number | null;
  packaged_start_ms?: number | null;
  mp3_path: string | null;
}

export interface LibraryBookDetail extends LibraryIdentity {
  id: number;
  title: string;
  author: string | null;
  source_path: string | null;
  /** Whether any playable audio exists on disk; absent from older backends. */
  audio_available?: boolean | null;
  /** Chapters with audio; sent by newer backends on the detail too. */
  completed_chapters?: number;
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

export interface PlaybackPositionUpdateResponse extends LibraryIdentity {
  book_id: number;
  position_sec: number;
}

export interface LibraryBookUpdateRequest {
  expected_epoch?: string;
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

export interface DeleteBookResponse extends LibraryIdentity {
  book_id: number;
  deleted: boolean;
  files_pending?: boolean;
  path?: string;
}

export interface MaintenanceDiagnosticsResponse extends LibraryIdentity {
  database_path: string;
  output_path: string;
  temp_path: string;
  app_data_path: string;
  quarantine?: { path: string; created_at: string } | null;
  audit: {
    row_counts?: Record<string, number>;
    stale_chapter_rows?: unknown[];
    stale_book_sources?: unknown[];
    orphan_output_dirs?: string[];
    stale_temp_uploads?: string[];
    stale_temp_stage_dirs?: string[];
    [key: string]: unknown;
  };
  /** What cleanup would do now, and when the check ran (ISO UTC). */
  cleanup_preview: CleanupSummary;
  checked_at: string;
}

interface BookReference {
  id: number;
  title: string;
}

type CleanupKeptGroup =
  | { category: "missing_source"; reason: "needs_user_action"; items: BookReference[] }
  | { category: "missing_chapter_audio"; reason: "book_kept"; items: Array<BookReference & { chapters: number }> }
  | { category: "book_without_audio"; reason: "unowned_audio_present" | "other_audio_present" | "interrupted_conversion"; items: BookReference[] }
  | { category: "unowned_output_folder"; reason: "contains_finished_audio" | "interrupted_conversion" | "recently_modified";
    items: Array<{ name: string; path: string }> }
  | { category: "quarantined_library_files"; reason: "library_quarantined"; items: Array<{ name: string; path: string }> };

export interface CleanupSummary {
  changed: boolean | null;
  grace_hours: number;
  removable: {
    output_folders: number;
    temp_uploads: number;
    temp_stage_folders: number;
    pending_deletions: number;
    orphan_sources: number;
  };
  kept: CleanupKeptGroup[];
  removed?: Record<string, number>;
  /** Newer backends: books with no audio and no source file, whose library entry a cleanup removes. */
  removable_books?: BookReference[];
  /** Newer backends, after a cleanup: the books whose library entry it removed. */
  removed_books?: BookReference[];
}

export interface MaintenanceBackupResponse {
  backup_path: string;
}

export interface MaintenanceBackup {
  id: string;
  filename: string;
  created_at: string;
  size: number;
  book_count: number | null;
  /** "backup", or "safety_copy" for the copy taken before a restore. */
  kind?: string;
  label?: string | null;
}

export interface MaintenanceBackupsResponse {
  items: MaintenanceBackup[];
}

export interface MaintenanceRestoreResponse {
  restored_id: string;
  safety_copy_path: string;
  /** Newer backends: what came back, and what did not. */
  summary?: {
    books_restored: number;
    missing_audio: BookReference[];
    missing_source: BookReference[];
    unowned_output_folders: Array<{ name: string; path: string; has_audio: boolean; has_recovery_inventory: boolean }>;
  };
}

export interface MaintenanceCleanupResponse {
  deleted_chapter_rows?: unknown[];
  deleted_book_rows?: unknown[];
  deleted_output_dirs?: string[];
  deleted_temp_uploads?: string[];
  deleted_temp_stage_dirs?: string[];
  moved_sources?: unknown[];
  backup_path?: string | null;
  /** Newer backends: what was removed and what was kept. */
  summary?: CleanupSummary;
  removed_unowned_folders?: string[];
  unknown_unowned_folders?: string[];
  [key: string]: unknown;
}
