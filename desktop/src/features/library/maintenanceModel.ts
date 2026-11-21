import type {
  CleanupSummary,
  MaintenanceBackup,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
  MaintenanceRestoreResponse,
} from "../../types/library";

/** Library writes are held after a restore: while it runs, or until a check confirms what it restored. */
export const WRITES_PAUSED_MESSAGE =
  "Library changes are paused until the restored library is verified. Press Check under Library in Settings to verify it.";

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function list(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}

/** Names a few items, then counts the rest. */
function names(items: string[], limit = 3): string {
  const shown = items.slice(0, limit).map((name) => name || "Untitled");
  return items.length > limit ? `${shown.join(", ")} and ${items.length - limit} more` : list(shown);
}

const REMOVABLE_LABELS: Array<[keyof CleanupSummary["removable"], string, string]> = [
  ["output_folders", "leftover output folder", "leftover output folders"],
  ["temp_uploads", "old upload", "old uploads"],
  ["temp_stage_folders", "temporary work folder", "temporary work folders"],
  ["pending_deletions", "unfinished deletion", "unfinished deletions"],
  ["orphan_sources", "unused source file", "unused source files"],
];

/** Advice for a book whose audio is missing and whose source file is gone too. */
const NO_SOURCE_ADVICE = "delete the book, or create it again from the original file";

/**
 * One line per thing cleanup keeps, with the reason. Repair audio needs the
 * source file, so a book that misses both is never told to repair.
 */
function keptLines(summary: CleanupSummary): string[] {
  const groups = summary.kept.filter((group) => group.items.length > 0);
  const withoutSource = new Set(groups.flatMap((group) => group.category === "missing_source" ? group.items.map((item) => item.id) : []));
  const audioless = new Set(groups.flatMap((group) => group.category === "missing_chapter_audio" ? group.items.map((item) => item.id) : []));
  const titled = (items: Array<{ title: string }>, text: string) =>
    items.length ? [`${names(items.map((item) => item.title))}: ${text}`] : [];
  return groups.flatMap((group): string[] => {
    switch (group.category) {
      case "missing_source":
        // A book that also misses audio is described once, on its audio line.
        return titled(group.items.filter((item) => !audioless.has(item.id)), "source file missing, so Repair audio is unavailable for it");
      case "missing_chapter_audio":
        return [
          ...titled(group.items.filter((item) => !withoutSource.has(item.id)), "chapter audio missing, book kept (Repair audio rebuilds it)"),
          ...titled(group.items.filter((item) => withoutSource.has(item.id)),
            `chapter audio and source file missing, so the audio cannot be rebuilt (${NO_SOURCE_ADVICE})`),
        ];
      case "book_without_audio":
        return titled(group.items, group.reason === "interrupted_conversion" ? "an interrupted conversion (Repair audio finishes it)"
          : group.reason === "unowned_audio_present" ? "no audio in the library, but a folder with its name holds audio"
          : "no audio; kept in the library while folders not in the library hold audio");
      case "unowned_output_folder": {
        const one = group.items.length === 1;
        const reason = group.reason === "contains_finished_audio" ? (one ? "it contains finished audio" : "they contain finished audio")
          : group.reason === "interrupted_conversion" ? (one ? "it holds an interrupted conversion" : "they hold an interrupted conversion")
          : one ? "it changed recently" : "they changed recently";
        return [`${plural(group.items.length, "folder", "folders")} not in the library kept because ${reason}: ${names(group.items.map((item) => item.name))}`];
      }
      case "quarantined_library_files":
        // Source files, upload files and output folders alike.
        return [`${plural(group.items.length, "item", "items")} kept until a backup replaces the damaged library: ${names(group.items.map((item) => item.name))}`];
    }
  });
}

/** A result message: its sentences, then each kept item on a line of its own. */
function withKept(sentences: string, kept: string[]): string {
  return kept.length ? [`${sentences} Kept:`, ...kept].join("\n") : sentences;
}

/** Splits a message built by this module into its sentences and its kept items. */
export function messageParts(message: string): { text: string; items: string[] } {
  const [text, ...items] = message.split("\n");
  return { text, items };
}

/** "1 book with no audio and no source file: Harbor Lights": the books whose library entry a cleanup removes. */
function fileless(books: Array<{ title: string }>, all = false): string {
  const titles = books.map((book) => book.title || "Untitled");
  return `${plural(books.length, "book", "books")} with no audio and no source file: ${all ? list(titles) : names(titles)}`;
}

function previewSentences(summary: CleanupSummary): string {
  const removable = REMOVABLE_LABELS
    .map(([key, one, many]) => [summary.removable[key] ?? 0, one, many] as const)
    .filter(([count]) => count > 0)
    .map(([count, one, many]) => plural(count, one, many));
  const books = summary.removable_books ?? [];
  const removes = books.length
    ? `A cleanup would remove ${fileless(books)}.${removable.length ? ` It would also remove ${list(removable)}.` : ""}`
    : removable.length ? `A cleanup would remove ${list(removable)}.` : "Nothing needs cleaning up.";
  return withKept(removes, keptLines(summary));
}

/** The Clean up confirmation: names every book whose library entry the cleanup removes. */
export function cleanupConfirmation(report: MaintenanceDiagnosticsResponse | null | undefined): string {
  const books = report?.cleanup_preview?.removable_books ?? [];
  if (!books.length) return "Clean stale storage records and orphan files? A database backup is saved first if anything changes.";
  return `Clean stale storage records and orphan files? This removes the library ${books.length === 1 ? "entry" : "entries"} of ${fileless(books, true)}. A database backup is saved first.`;
}

/** The Check library row: never claims a result before a check has run. */
export function checkLibrarySummary(report: MaintenanceDiagnosticsResponse | null): string {
  if (!report) return "Not checked yet";
  const checked = report.checked_at ? new Date(report.checked_at) : null;
  const when = checked && !Number.isNaN(checked.getTime())
    ? `Checked ${checked.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. ` : "";
  return `${when}${previewSentences(report.cleanup_preview)}`;
}

/** Folders in the output folder that no book owns: cleanup keeps them unless they are named for removal. */
export function unownedFolders(report: MaintenanceDiagnosticsResponse | null | undefined): Array<{ name: string; path: string }> {
  return (report?.cleanup_preview?.kept ?? []).flatMap((group) => group.category === "unowned_output_folder" ? group.items : []);
}

/** After Clean up: what was removed, or that nothing needed it. */
export function cleanupResultMessage(response: MaintenanceCleanupResponse | null | undefined): string {
  const folders = response?.removed_unowned_folders ?? [];
  const gone = response?.unknown_unowned_folders ?? [];
  if (!response?.summary) return "Cleanup complete.";
  const counts = response.summary.removed ?? {};
  const books = response.summary.removed_books ?? [];
  const next = response.summary.removable_books ?? [];
  // Only deletions are cleaned-up items. Removed folders and books are reported
  // by name instead, and a repaired link or a moved source file deletes nothing.
  const named = books.length ? NAMED_WITH_BOOKS : NOT_DELETIONS;
  const removed = Object.entries(counts)
    .reduce((total, [key, count]) => total + (named.has(key) ? 0 : Number(count) || 0), 0);
  const relinked = Number(counts.reconciled_books) || 0;
  const moved = Number(counts.moved_sources) || 0;
  const acted = removed > 0 || relinked > 0 || moved > 0 || folders.length > 0 || gone.length > 0 || books.length > 0;
  const sentences = [
    folders.length ? `Removed ${plural(folders.length, "folder", "folders")} not in the library: ${names(folders)}.` : "",
    gone.length ? `${plural(gone.length, "folder was", "folders were")} already gone: ${names(gone)}.` : "",
    books.length ? `Removed ${fileless(books)}.` : "",
    removed > 0 ? `Cleaned up ${plural(removed, "item", "items")}.` : acted ? "" : "Nothing needed cleaning up.",
    relinked > 0 ? `Reconnected ${plural(relinked, "book", "books")} to audio found on disk.` : "",
    moved > 0 ? `Moved ${plural(moved, "source file", "source files")} into the library's storage.` : "",
    response.backup_path ? "A database backup was saved first." : "",
    next.length ? `The next cleanup would remove ${fileless(next)}.` : "",
  ];
  return withKept(sentences.filter(Boolean).join(" "), keptLines(response.summary));
}

const NOT_DELETIONS = new Set(["unowned_folders", "reconciled_books", "moved_sources"]);
/** A removed book is named, so its row and its chapter rows are not counted as items as well. */
const NAMED_WITH_BOOKS = new Set([...NOT_DELETIONS, "book_rows", "chapter_rows"]);

/** After Back up: the saved backup by its label, or its file name until the list has it; never the raw path. */
export function backupSavedMessage(backupPath: string, backups: MaintenanceBackup[] = []): string {
  const fileName = backupPath.split(/[\\/]/).pop() || backupPath;
  const listed = backups.find((backup) => backup.filename.toLowerCase() === fileName.toLowerCase());
  return `Backup saved: ${listed ? backupDisplayName(listed) : fileName}`;
}

/** After a restore: what came back, and what did not. */
export function restoreResultMessage(response: MaintenanceRestoreResponse): string {
  const summary = response.summary;
  if (!summary) return "Backup restored. A safety copy of the previous library was saved.";
  const sentences = [`Backup restored with ${plural(summary.books_restored, "book", "books")}.`];
  if (summary.missing_audio.length) {
    sentences.push(`${plural(summary.missing_audio.length, "book has", "books have")} no audio: ${names(summary.missing_audio.map((book) => book.title))}.`);
  }
  if (summary.missing_source.length) {
    sentences.push(`${plural(summary.missing_source.length, "book is missing its source file", "books are missing their source files")}: ${names(summary.missing_source.map((book) => book.title))}.`);
  }
  if (summary.unowned_output_folders.length) {
    sentences.push(`${plural(summary.unowned_output_folders.length, "folder on disk is", "folders on disk are")} not in this library: ${names(summary.unowned_output_folders.map((folder) => folder.name))}.`);
  }
  sentences.push("A safety copy of the previous library was saved.");
  return sentences.join(" ");
}

/** A backup's name for people: its label, never the raw file name when a label exists. */
export function backupDisplayName(backup: Pick<MaintenanceBackup, "label" | "filename" | "created_at" | "book_count">): string {
  if (backup.label?.trim()) return backup.label.trim();
  const created = new Date(backup.created_at);
  const date = Number.isNaN(created.getTime()) ? backup.filename : `Backup from ${created.toLocaleString()}`;
  return backup.book_count === null ? date : `${date} · ${plural(backup.book_count, "book", "books")}`;
}
