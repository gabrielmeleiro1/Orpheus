import type {
  ConversionJobStatus,
  ConversionJobTerminalEvent,
  ConversionTerminalJobStatus,
  DesktopEventPayload,
  EventStreamLogEntry,
  PerformanceSnapshot,
  ProgressStageDetail,
  ProgressStageName,
  ProgressStageView,
} from "../../types/api";

const MAX_LOG_LINES = 300;
const PROGRESS_STAGE_NAMES: ProgressStageName[] = [
  "ingestion",
  "chunking",
  "cleaning",
  "synthesis",
  "encoding",
  "packaging",
];
const BACKEND_STAGE_NAME_MAP: Record<string, ProgressStageName | ""> = {
  idle: "",
  ingesting: "ingestion",
  chunking: "chunking",
  cleaning: "cleaning",
  synthesizing: "synthesis",
  encoding: "encoding",
  packaging: "packaging",
  complete: "packaging",
  completed: "packaging",
  error: "",
  cancelled: "",
};

export interface EventStreamModelState {
  uiState: ConversionJobStatus;
  statusText: string;
  progressPct: number;
  etaText: string;
  stages: ProgressStageView[];
  currentStage: string;
  currentMessage: string;
  performanceSnapshot: PerformanceSnapshot | null;
  activeJobId: string | null;
  logs: EventStreamLogEntry[];
  isCancelling: boolean;
  cancelledJobId: string | null;
  cancellingJobId: string | null;
  runningJobId: string | null;
}

type LogActionFields = {
  logId: number;
  fallbackTimestamp?: string;
};

export type EventStreamAction =
  | { type: "progress"; payload: DesktopEventPayload }
  | ({ type: "state"; payload: DesktopEventPayload } & Partial<LogActionFields>)
  | ({ type: "log"; payload: DesktopEventPayload } & LogActionFields)
  | ({ type: "pushLog"; level: string; message: string; timestamp?: string } & LogActionFields)
  | { type: "refreshNoClient" }
  | { type: "refreshInactive" }
  | { type: "refreshActive"; job: { id: string; status: ConversionJobStatus } }
  | { type: "cancelStart" }
  | { type: "cancelRejected" }
  | ({ type: "cancelAccepted"; jobId: string } & LogActionFields)
  | { type: "setActiveJobId"; jobId: string | null };

export interface EventStreamReduceResult {
  state: EventStreamModelState;
  terminalEvent?: ConversionJobTerminalEvent;
  ignored?: boolean;
}

export function createProgressStages(): ProgressStageView[] {
  return PROGRESS_STAGE_NAMES.map((name) => ({
    name,
    state: "pending",
    pct: 0,
    detail: [],
  }));
}

export function createInitialEventStreamState(): EventStreamModelState {
  return {
    uiState: "idle",
    statusText: "Waiting for run...",
    progressPct: 0,
    etaText: "",
    stages: createProgressStages(),
    currentStage: "",
    currentMessage: "",
    performanceSnapshot: null,
    activeJobId: null,
    logs: [],
    isCancelling: false,
    cancelledJobId: null,
    cancellingJobId: null,
    runningJobId: null,
  };
}

function normalizeString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function normalizeProgressPercent(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }

  if (numeric <= 1) {
    return Math.max(0, Math.min(100, Math.round(numeric * 100)));
  }

  return Math.max(0, Math.min(100, Math.round(numeric)));
}

function normalizeCounter(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }
  return Math.max(0, Math.round(numeric));
}

function counterPercent(current: number, total: number, fallbackPct = 0): number {
  if (total <= 0) {
    return normalizeProgressPercent(fallbackPct);
  }
  return normalizeProgressPercent(Math.min(total, current) / total);
}

function normalizeOptionalNumber(value: unknown): number | undefined {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return undefined;
  }
  return Math.max(0, Math.round(numeric * 100) / 100);
}

function normalizePerformanceSnapshot(value: unknown): PerformanceSnapshot | null {
  if (!value || typeof value !== "object") {
    return null;
  }

  const source = value as Record<string, unknown>;
  const snapshot: PerformanceSnapshot = {};
  const pipelineMode = normalizeString(source.pipeline_mode).toLowerCase();
  const fallbackReason = normalizeString(source.fallback_reason);
  const activeTitles = Array.isArray(source.active_chapter_titles)
    ? source.active_chapter_titles
        .map((item) => normalizeString(item))
        .filter((item) => item.length > 0)
    : [];

  if (pipelineMode) {
    snapshot.pipeline_mode = pipelineMode;
  }
  if (fallbackReason) {
    snapshot.fallback_reason = fallbackReason;
  }
  const parallelWorkerMode = normalizeString(source.parallel_worker_mode).toLowerCase();
  if (parallelWorkerMode) {
    snapshot.parallel_worker_mode = parallelWorkerMode;
  }
  if (activeTitles.length > 0) {
    snapshot.active_chapter_titles = activeTitles;
  }

  for (const key of [
    "requested_parallel_chapters",
    "effective_parallel_chapters",
    "active_workers",
    "max_workers",
    "queued_chapters",
    "running_chapters",
    "completed_chapters",
    "failed_chapters",
    "cleaner_chunks_per_second",
    "tts_chars_per_second",
    "encoder_chapters_per_second",
    "elapsed_seconds",
    "eta_seconds",
  ] as const) {
    const normalized = normalizeOptionalNumber(source[key]);
    if (normalized !== undefined) {
      snapshot[key] = normalized;
    }
  }

  return Object.keys(snapshot).length > 0 ? snapshot : null;
}

function formatEtaLabel(value: unknown): string {
  const totalSeconds = Math.round(Number(value));
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) {
    return "";
  }

  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];

  if (hours > 0) {
    parts.push(`${hours}h`);
  }
  if (minutes > 0) {
    parts.push(`${minutes}m`);
  }
  if (parts.length === 0 || (hours === 0 && minutes < 5)) {
    parts.push(`${seconds}s`);
  }

  return `eta ~${parts.join(" ")}`;
}

function formatTimestamp(value: string | undefined, fallbackTimestamp = ""): string {
  if (!value) {
    return fallbackTimestamp;
  }

  const parsed = new Date(value);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toLocaleTimeString("en-US", { hour12: false });
  }

  return fallbackTimestamp;
}

function normalizeStageName(stage: string): ProgressStageName | "" {
  const stageValue = stage.toLowerCase();
  if (stageValue in BACKEND_STAGE_NAME_MAP) {
    return BACKEND_STAGE_NAME_MAP[stageValue];
  }
  if (stageValue.includes("ingest")) {
    return "ingestion";
  }
  if (stageValue.includes("chunk")) {
    return "chunking";
  }
  if (stageValue.includes("clean")) {
    return "cleaning";
  }
  if (stageValue.includes("synth")) {
    return "synthesis";
  }
  if (stageValue.includes("encod")) {
    return "encoding";
  }
  if (stageValue.includes("pack")) {
    return "packaging";
  }
  if (stageValue === "complete" || stageValue === "completed") {
    return "packaging";
  }
  return "";
}

function resolveStageIndex(stage: string): number {
  const normalizedStage = normalizeStageName(stage);
  const index = PROGRESS_STAGE_NAMES.indexOf(normalizedStage as ProgressStageName);
  return index >= 0 ? index : 0;
}

function buildStageDetail(
  stage: string,
  payload: DesktopEventPayload,
  stagePct: number,
  message: string,
): ProgressStageDetail[] {
  const details: ProgressStageDetail[] = [];
  const normalizedStage = normalizeStageName(stage);
  const chapterCurrent = normalizeCounter(payload.chapter_current);
  const chapterTotal = normalizeCounter(payload.chapter_total);
  const chunkCurrent = normalizeCounter(payload.chunk_current);
  const chunkTotal = normalizeCounter(payload.chunk_total);

  if (chapterTotal > 0) {
    details.push({
      label: "chapter",
      value: `${Math.min(chapterCurrent, chapterTotal)}/${chapterTotal}`,
      pct: counterPercent(chapterCurrent, chapterTotal),
    });
  }

  if (chunkTotal > 0) {
    details.push({
      label: "chunk",
      value: `${Math.min(chunkCurrent, chunkTotal)}/${chunkTotal}`,
      pct: counterPercent(chunkCurrent, chunkTotal),
    });
  }

  if (details.length === 0) {
    const fallbackValue =
      normalizedStage === "packaging"
        ? "building final file"
        : normalizedStage === "encoding"
          ? "encoding audio"
          : message || "working";
    details.push({
      label: "task",
      value: fallbackValue,
      pct: stagePct,
    });
  }

  return details;
}

function normalizeState(state: string): ConversionJobStatus {
  if (state === "idle") {
    return "idle";
  }
  if (state === "running") {
    return "running";
  }
  if (state === "completed") {
    return "completed";
  }
  if (state === "failed") {
    return "failed";
  }
  if (state === "cancelled") {
    return "cancelled";
  }
  return "unknown";
}

export function normalizeTerminalStatus(value: unknown): ConversionTerminalJobStatus | null {
  const normalized = normalizeString(value).toLowerCase();
  if (normalized === "completed" || normalized === "failed" || normalized === "cancelled") {
    return normalized;
  }
  return null;
}

export function normalizeEventPayload(payload: unknown): DesktopEventPayload | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const source = payload as Record<string, unknown>;
  const eventType = normalizeString(source.event_type).toLowerCase();
  if (!eventType) {
    return null;
  }

  const eventId = normalizeString(source.event_id);
  const timestamp = normalizeString(source.timestamp);
  const state = normalizeString(source.state).toLowerCase();
  const stage = normalizeString(source.stage);
  const message = normalizeString(source.message);
  const level = normalizeString(source.level).toLowerCase();
  const jobId = normalizeString(source.job_id);
  const performance = normalizePerformanceSnapshot(source.performance);

  const normalized: DesktopEventPayload = {
    event_type: eventType,
  };

  if (eventId) {
    normalized.event_id = eventId;
  }
  if (timestamp) {
    normalized.timestamp = timestamp;
  }
  if (state) {
    normalized.state = state;
  }
  if (stage) {
    normalized.stage = stage;
  }
  if (message) {
    normalized.message = message;
  }
  if (level) {
    normalized.level = level;
  }
  if (jobId) {
    normalized.job_id = jobId;
  }
  if (performance) {
    normalized.performance = performance;
  }

  for (const key of [
    "progress",
    "overall_progress",
    "stage_progress",
    "eta",
    "chapter_current",
    "chapter_total",
    "chunk_current",
    "chunk_total",
  ] as const) {
    const numeric = Number(source[key]);
    if (Number.isFinite(numeric)) {
      normalized[key] = numeric;
    }
  }

  return normalized;
}

export function terminalEventFromStatePayload(
  payload: DesktopEventPayload | null,
): ConversionJobTerminalEvent | undefined {
  if (!payload) {
    return undefined;
  }
  const jobId = normalizeString(payload.job_id);
  const status = normalizeTerminalStatus(payload.state);
  if (!jobId || !status) {
    return undefined;
  }
  return {
    jobId,
    status,
    message: normalizeString(payload.message) || `conversion ${status}`,
  };
}

function doneStages(): ProgressStageView[] {
  return createProgressStages().map((item) => ({ ...item, state: "done", pct: 100 }));
}

function resetProgressForRun(state: EventStreamModelState): EventStreamModelState {
  return {
    ...state,
    progressPct: 0,
    etaText: "",
    stages: createProgressStages(),
    currentStage: "",
    currentMessage: "",
    performanceSnapshot: null,
    isCancelling: false,
  };
}

function addLog(
  state: EventStreamModelState,
  level: string,
  message: string,
  timestamp: string | undefined,
  logId: number,
  fallbackTimestamp?: string,
): EventStreamModelState {
  const normalizedMessage = normalizeString(message);
  if (!normalizedMessage) {
    return state;
  }

  const next = [
    {
      id: logId,
      level: normalizeString(level) || "info",
      ts: formatTimestamp(timestamp, fallbackTimestamp),
      msg: normalizedMessage,
    },
    ...state.logs,
  ];
  if (next.length > MAX_LOG_LINES) {
    next.length = MAX_LOG_LINES;
  }
  return { ...state, logs: next };
}

function reduceProgressEvent(
  state: EventStreamModelState,
  payload: DesktopEventPayload,
): EventStreamModelState {
  const jobId = normalizeString(payload.job_id);
  if (state.cancelledJobId && (!jobId || jobId === state.cancelledJobId)) {
    return state;
  }

  const stage = normalizeString(payload.stage);
  const stageLower = stage.toLowerCase();
  const message = normalizeString(payload.message);
  const overallPct = normalizeProgressPercent(payload.overall_progress ?? payload.progress);
  const stagePct = normalizeProgressPercent(payload.stage_progress ?? payload.progress);

  const base: EventStreamModelState = {
    ...state,
    uiState: "running",
    progressPct: overallPct,
    etaText: formatEtaLabel(payload.eta),
    statusText: message || (stage ? `stage: ${stage}` : "conversion running"),
    performanceSnapshot: payload.performance ?? null,
    currentStage: stage || state.currentStage,
    currentMessage: message || state.currentMessage,
  };

  if (overallPct >= 100 || stageLower === "complete" || stageLower === "completed") {
    return {
      ...base,
      stages: doneStages().map((item) => ({ ...item, detail: [] })),
      etaText: "",
    };
  }

  const activeIndex = resolveStageIndex(stageLower);
  const activeDetail = buildStageDetail(stageLower, payload, stagePct, message);
  return {
    ...base,
    stages: state.stages.map((item, index) => {
      if (index < activeIndex) {
        return { ...item, state: "done", pct: 100, detail: [] };
      }
      if (index === activeIndex) {
        return { ...item, state: "active", pct: stagePct, detail: activeDetail };
      }
      return { ...item, state: "pending", pct: 0, detail: [] };
    }),
  };
}

function markJobCanceled(state: EventStreamModelState, jobId?: string): EventStreamModelState {
  const normalizedJobId = normalizeString(jobId);
  return {
    ...state,
    cancelledJobId: normalizedJobId || state.activeJobId,
    cancellingJobId: null,
    runningJobId: null,
    uiState: "cancelled",
    statusText: "job canceled",
    etaText: "",
    currentMessage: "job canceled",
    performanceSnapshot: null,
    activeJobId: null,
    isCancelling: false,
  };
}

function reduceStateEvent(
  state: EventStreamModelState,
  payload: DesktopEventPayload,
): EventStreamReduceResult {
  const rawState = normalizeString(payload.state).toLowerCase();
  const message = normalizeString(payload.message);
  const jobId = normalizeString(payload.job_id);
  const normalizedState = normalizeState(rawState);

  if (
    normalizedState === "running" &&
    jobId &&
    (jobId === state.cancelledJobId || jobId === state.cancellingJobId)
  ) {
    return { state, ignored: true };
  }

  let next: EventStreamModelState = jobId ? { ...state, activeJobId: jobId } : state;

  if (normalizedState === "running" && jobId && jobId !== state.cancelledJobId) {
    next = { ...next, cancelledJobId: null, cancellingJobId: null };
  }

  if (normalizedState === "cancelled") {
    return {
      state: markJobCanceled(next, jobId),
      terminalEvent: terminalEventFromStatePayload(payload),
    };
  }

  next = { ...next, uiState: normalizedState };

  if (normalizedState === "running") {
    const runningJobKey = jobId || "__unknown__";
    if (next.runningJobId !== runningJobKey) {
      next = resetProgressForRun(next);
      next = { ...next, runningJobId: runningJobKey };
    }
    next = { ...next, statusText: message || "conversion running" };
  } else if (normalizedState === "completed") {
    next = {
      ...next,
      runningJobId: null,
      statusText: message || "conversion completed",
      progressPct: 100,
      etaText: "",
      stages: doneStages().map((item) => ({ ...item, detail: [] })),
      activeJobId: null,
      isCancelling: false,
    };
  } else if (normalizedState === "failed") {
    next = {
      ...next,
      runningJobId: null,
      statusText: message || "conversion failed",
      etaText: "",
      activeJobId: null,
      isCancelling: false,
    };
  } else if (normalizedState === "idle") {
    next = {
      ...next,
      runningJobId: null,
      statusText: message || "ready",
      etaText: "",
      performanceSnapshot: null,
      activeJobId: null,
      isCancelling: false,
    };
  } else {
    next = { ...next, statusText: message || `state: ${rawState || "unknown"}` };
  }

  if (message) {
    next = { ...next, currentMessage: message };
  }

  return {
    state: next,
    terminalEvent: terminalEventFromStatePayload(payload),
  };
}

export function eventStreamReducer(
  state: EventStreamModelState,
  action: EventStreamAction,
): EventStreamReduceResult {
  switch (action.type) {
    case "progress":
      return { state: reduceProgressEvent(state, action.payload) };
    case "state": {
      const result = reduceStateEvent(state, action.payload);
      if (action.logId === undefined || result.ignored) {
        return result;
      }
      return {
        ...result,
        state: addLog(
          result.state,
          "info",
          `state ${normalizeString(action.payload.state).toLowerCase() || "unknown"}${
            normalizeString(action.payload.job_id)
              ? ` (${normalizeString(action.payload.job_id)})`
              : ""
          }`,
          action.payload.timestamp,
          action.logId,
          action.fallbackTimestamp,
        ),
      };
    }
    case "log": {
      const withPerformance = action.payload.performance
        ? { ...state, performanceSnapshot: action.payload.performance }
        : state;
      return {
        state: addLog(
          withPerformance,
          action.payload.level ?? "info",
          action.payload.message ?? "",
          action.payload.timestamp,
          action.logId,
          action.fallbackTimestamp,
        ),
      };
    }
    case "pushLog":
      return {
        state: addLog(
          state,
          action.level,
          action.message,
          action.timestamp,
          action.logId,
          action.fallbackTimestamp,
        ),
      };
    case "refreshNoClient":
      return { state: { ...state, activeJobId: null, uiState: "idle" } };
    case "refreshInactive":
      return {
        state: {
          ...state,
          runningJobId: null,
          activeJobId: null,
          isCancelling: false,
          etaText: "",
          performanceSnapshot: null,
          uiState: state.uiState === "running" || state.uiState === "unknown" ? "idle" : state.uiState,
          statusText:
            state.statusText === "conversion running" ||
            state.statusText === "cancellation requested; waiting for backend to stop"
              ? "ready"
              : state.statusText,
        },
      };
    case "refreshActive":
      return {
        state: {
          ...state,
          activeJobId: action.job.id,
          uiState: action.job.status,
          statusText: `active job: ${action.job.status}`,
          runningJobId: action.job.status !== "running" ? null : state.runningJobId,
          isCancelling: action.job.status !== "running" ? false : state.isCancelling,
        },
      };
    case "cancelStart":
      return { state: { ...state, isCancelling: true } };
    case "cancelRejected":
      return { state: { ...state, isCancelling: false } };
    case "cancelAccepted":
      return {
        state: addLog(
          {
            ...state,
            activeJobId: action.jobId,
            cancellingJobId: action.jobId,
            uiState: "running",
            etaText: "",
            currentMessage: "cancellation requested",
            statusText: "cancellation requested; waiting for backend to stop",
          },
          "info",
          `job ${action.jobId} cancellation requested`,
          undefined,
          action.logId,
          action.fallbackTimestamp,
        ),
      };
    case "setActiveJobId":
      return { state: { ...state, activeJobId: action.jobId } };
  }
}
