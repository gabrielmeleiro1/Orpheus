// Mocked backend payloads for interaction-audit.mjs. Each builder starts from
// the real response where there is one and changes only what the state needs,
// so every mock keeps the backend's wire shape. tests/interactionMocks.test.mjs
// checks each one against the desktop app's wire fixtures
// (desktop/src/test/wire-fixtures), so the mocks cannot drift from the API.

const LIBRARY_PAGE_SIZE = 100;

/**
 * A configured runtime whose voice model went missing: what
 * modules/web/routes/setup.py reports ("unavailable", not ready, with its
 * state_error), never a state the backend does not emit.
 */
export function runtimeNotReadyStatus(real) {
  return {
    ...real,
    configured: true,
    state: "unavailable",
    ready: false,
    state_error: "The Kokoro voice model is missing. Install it to create audio.",
    selected_model_groups: real.selected_model_groups.map((group) => (group.id.startsWith("tts-")
      ? { ...group, ready: false, assets_ready: false }
      : group)),
  };
}

/** A paged library of `total` books, answering the request's own limit and offset. */
export function libraryPage(real, url, total, makeItem) {
  const limit = Number(url.searchParams.get("limit") ?? LIBRARY_PAGE_SIZE);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  const items = [];
  for (let index = offset; index < Math.min(total, offset + limit); index += 1) items.push(makeItem(index));
  return { ...real, items, count: items.length, limit, offset };
}

export function libraryCount(total) {
  return { count: total };
}

/** A model download in progress (model-download-running.json shape). */
export function downloadRunning() {
  return {
    bytes_done: 2_283_000_000,
    bytes_total: 6_170_000_000,
    cleaner_cuda_quantization: "4bit",
    completed_models: 0,
    configure_runtime: false,
    current_model: "Qwen/Qwen2.5-3B-Instruct",
    current_model_group: "cleaner-cuda-large",
    error: null,
    lifecycle: "running",
    model_groups: ["cleaner-cuda-large"],
    profile: "cuda",
    total_models: 1,
  };
}

/**
 * The same download after it failed, with the message the backend's
 * model_download_service gives a network failure.
 */
export function downloadFailed() {
  return {
    ...downloadRunning(),
    bytes_done: 1_000_000,
    error: "Could not reach huggingface.co. Check your internet connection and try again.",
    lifecycle: "failed",
  };
}

const FAILED_JOB_ID = "audit-failed-job";

/** The same job while it still runs (active-running shape). */
export function runningActive({ title, author, clientRequestId = "desktop-audit-failed" }) {
  return {
    active: true,
    job: { author, book_id: null, client_request_id: clientRequestId, error: null, id: FAILED_JOB_ID, status: "running", title, repair: false },
  };
}

/** A finished-with-failure job as GET /api/conversions/active reports it. */
export function failedActive({ title, author, error, clientRequestId = "desktop-audit-failed" }) {
  return {
    active: false,
    job: { author, book_id: null, client_request_id: clientRequestId, error, id: FAILED_JOB_ID, status: "failed", title, repair: false },
  };
}

/**
 * The event stream of that job: a few progress and log events and the final
 * failed state, as text/event-stream. Without them the failed drawer and its
 * log would be audited empty.
 */
export function failedEventStream({ backendInstanceId, error, clientRequestId = "desktop-audit-failed" }) {
  const base = { backend_instance_id: backendInstanceId, timestamp: "2026-01-01T00:00:00+00:00" };
  // Running first, so the app sees the job fail rather than find it failed.
  const events = [
    { ...base, client_request_id: clientRequestId, event_id: "1", event_sequence: 1, event_type: "state", job_id: FAILED_JOB_ID, message: "Running", state: "running" },
    { ...base, event_id: "2", event_sequence: 2, event_type: "log", job_id: FAILED_JOB_ID, level: "info", message: "Starting conversion", performance: {} },
    { ...base, event_id: "3", event_sequence: 3, event_type: "log", job_id: FAILED_JOB_ID, level: "warning", message: "Chapter 6 produced no audio; retrying once", performance: {} },
    { ...base, event_id: "4", event_sequence: 4, event_type: "log", job_id: FAILED_JOB_ID, level: "error", message: error, performance: {} },
    { ...base, client_request_id: clientRequestId, event_id: "5", event_sequence: 5, event_type: "state", job_id: FAILED_JOB_ID, message: error, state: "failed" },
  ];
  // A long retry keeps EventSource from replaying the same events in a loop.
  return `retry: 600000\n\n${events.map((event) => `id: ${event.event_id}\ndata: ${JSON.stringify(event)}\n\n`).join("")}`;
}

// Requests that change data or reach outside the browser (deleting, starting
// work, backing up, opening Explorer). Playback-position saves stay live: the
// player states play and pause on entry. Uploads and voice previews only stage
// temporary files, and later steps need their real handles.
const GUARDED = [
  { method: "DELETE", pattern: /^\/api\// },
  { method: "POST", pattern: /^\/api\/(conversions(\/[^/]+\/cancel)?|conversions\/repair\/.*|maintenance\/(backup|cleanup|backups\/[^/]+\/restore|delete-staging\/open-folder)|setup\/(adopt|models\/download(\/cancel)?)|library\/\d+\/open-folder|system\/storage\/[^/]+\/open)$/ },
  { method: "PATCH", pattern: /^\/api\/library\// },
];

export function isGuardedRequest(method, pathname) {
  return GUARDED.some((rule) => rule.method === method && rule.pattern.test(pathname));
}

/** Answers for requests the audit must not let through but must not leave unanswered. */
export function mockedSuccess(method, pathname, body) {
  let match;
  if (method === "DELETE" && (match = /^\/api\/library\/(\d+)$/.exec(pathname))) {
    return { book_id: Number(match[1]), deleted: true };
  }
  if (method === "POST" && (match = /^\/api\/library\/(\d+)\/open-folder$/.exec(pathname))) {
    return { book_id: Number(match[1]), folder_path: "C:\\Audiobooks\\audit" };
  }
  if (method === "POST" && /^\/api\/system\/storage\/[^/]+\/open$/.test(pathname)) return { folder_path: "C:\\Audiobooks" };
  if (method === "POST" && pathname === "/api/maintenance/delete-staging/open-folder") return { folder_path: "C:\\Audiobooks\\.delete-staging" };
  if (method === "POST" && pathname === "/api/maintenance/backup") return { backup_path: "C:\\Audiobooks\\backups\\audiobooks_audit.db" };
  if (method === "POST" && pathname === "/api/maintenance/cleanup") {
    return { backup_path: "C:\\Audiobooks\\backups\\audiobooks_audit.db", deleted_book_rows: [], deleted_chapter_rows: [], deleted_output_dirs: [], deleted_temp_stage_dirs: [], deleted_temp_uploads: [], moved_sources: [] };
  }
  if (method === "POST" && (match = /^\/api\/maintenance\/backups\/([^/]+)\/restore$/.exec(pathname))) {
    return { restored_id: decodeURIComponent(match[1]), safety_copy_path: "C:\\Audiobooks\\backups\\pre-restore.db" };
  }
  if (method === "DELETE" && /^\/api\/setup\/models\/[^/]+$/.test(pathname)) {
    return { deleted_model_groups: [pathname.split("/").pop()], freed_bytes: 1_610_612_736, state_deleted: false };
  }
  if (method === "DELETE" && pathname === "/api/setup/runtime") {
    return { deleted_model_groups: ["tts-cpu-cuda"], freed_bytes: 3_221_225_472, state_deleted: true };
  }
  if (method === "POST" && pathname === "/api/conversions") {
    return { book_id: null, client_request_id: body?.client_request_id ?? null, error: null, job_id: "audit-started-job", status: "running" };
  }
  if (method === "POST" && /^\/api\/conversions\/[^/]+\/cancel$/.test(pathname)) {
    return { cancelled: true, job_id: pathname.split("/")[3] };
  }
  if (method === "POST" && pathname === "/api/setup/models/download") return downloadRunning();
  if (method === "POST" && pathname === "/api/setup/models/download/cancel") return { ...downloadRunning(), lifecycle: "cancelled" };
  return null;
}
