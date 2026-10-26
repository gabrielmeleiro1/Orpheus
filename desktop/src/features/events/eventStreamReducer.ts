import type {
  ConversionJobStatus,
  ConversionJobTerminalEvent,
  DesktopEventPayload,
  EventStreamLogEntry,
  PerformanceSnapshot,
  ProgressStageView,
} from "../../types/api";
import {
  normalizeState,
  normalizeString,
  terminalEventFromStatePayload,
} from "./eventPayloadNormalizer";
import { addLog, type LogActionFields } from "./eventLogModel";
import { mergePerformanceSnapshots } from "./performanceSnapshotModel";
import {
  buildStageDetail,
  createProgressStages,
  doneStages,
  formatEtaLabel,
  normalizeProgressPercent,
  resolveStageIndex,
} from "./progressStageModel";

// Reconciled jobs read as a sentence instead of a raw backend status.
const ACTIVE_JOB_STATUS_TEXT: Partial<Record<ConversionJobStatus, string>> = {
  running: "Conversion running",
  idle: "Waiting for a conversion…",
  completed: "Conversion completed",
  failed: "Conversion failed",
  cancelled: "Conversion cancelled",
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
  cancellationError: string;
  cancelledJobId: string | null;
  cancellingJobId: string | null;
  runningJobId: string | null;
  terminalJobIds: string[];
}

export const INTERRUPTED_JOB_TEXT = "The conversion was interrupted when the backend stopped. Finished chapters are kept: open the book in Library and use Repair audio to complete it.";

// Bounds how many finished job ids we remember. Jobs run sequentially and ids
// are unique, so a small window is enough to drop late events for a job that
// already reached a terminal state without growing without bound.
const TERMINAL_JOB_HISTORY_LIMIT = 16;

function appendTerminalJobId(terminalJobIds: string[], jobId: string): string[] {
  const normalized = normalizeString(jobId);
  if (!normalized || terminalJobIds.includes(normalized)) {
    return terminalJobIds;
  }
  const next = [...terminalJobIds, normalized];
  return next.length > TERMINAL_JOB_HISTORY_LIMIT
    ? next.slice(next.length - TERMINAL_JOB_HISTORY_LIMIT)
    : next;
}

type EventStreamAction =
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
  | { type: "conversionStarted"; jobId: string }
  | { type: "setActiveJobId"; jobId: string | null };

interface EventStreamReduceResult {
  state: EventStreamModelState;
  terminalEvent?: ConversionJobTerminalEvent;
  ignored?: boolean;
}

export function createInitialEventStreamState(): EventStreamModelState {
  return {
    uiState: "idle",
    statusText: "Waiting for a conversion…",
    progressPct: 0,
    etaText: "",
    stages: createProgressStages(),
    currentStage: "",
    currentMessage: "",
    performanceSnapshot: null,
    activeJobId: null,
    logs: [],
    isCancelling: false,
    cancellationError: "",
    cancelledJobId: null,
    cancellingJobId: null,
    runningJobId: null,
    terminalJobIds: [],
  };
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
    cancellationError: "",
  };
}

function reduceProgressEvent(
  state: EventStreamModelState,
  payload: DesktopEventPayload,
): EventStreamModelState {
  const jobId = normalizeString(payload.job_id);
  if (jobId && state.terminalJobIds.includes(jobId)) {
    return state;
  }
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
    activeJobId: jobId || state.activeJobId,
    progressPct: overallPct,
    etaText: formatEtaLabel(payload.eta),
    statusText: message || (stage ? `Stage: ${stage}` : "Conversion running"),
    performanceSnapshot: mergePerformanceSnapshots(state.performanceSnapshot, payload.performance),
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
    statusText: "Job cancelled",
    etaText: "",
    currentMessage: "Job cancelled",
    performanceSnapshot: null,
    activeJobId: null,
    isCancelling: false,
    cancellationError: "",
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
  const isTerminalState =
    normalizedState === "completed" ||
    normalizedState === "failed" ||
    normalizedState === "cancelled";

  // A job id that already reached a terminal state cannot resume. Drop any late
  // non-terminal event for it so a stale progress/running event cannot flip the
  // UI back to "running"; a different (new) job id still transitions normally.
  if (!isTerminalState && jobId && state.terminalJobIds.includes(jobId)) {
    return { state, ignored: true };
  }

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
      state: markJobCanceled(
        { ...next, terminalJobIds: appendTerminalJobId(next.terminalJobIds, jobId) },
        jobId,
      ),
      terminalEvent: terminalEventFromStatePayload(payload),
    };
  }

  if (isTerminalState) {
    next = { ...next, terminalJobIds: appendTerminalJobId(next.terminalJobIds, jobId) };
  }

  next = { ...next, uiState: normalizedState };

  if (normalizedState === "running") {
    const runningJobKey = jobId || "__unknown__";
    if (next.runningJobId !== runningJobKey) {
      next = resetProgressForRun(next);
      // The event log belongs to one conversion: a new one starts it afresh.
      next = { ...next, runningJobId: runningJobKey, logs: [] };
    }
    next = { ...next, statusText: message || "Conversion running" };
  } else if (normalizedState === "completed") {
    next = {
      ...next,
      runningJobId: null,
      statusText: message || "Conversion completed",
      progressPct: 100,
      etaText: "",
      stages: doneStages().map((item) => ({ ...item, detail: [] })),
      activeJobId: null,
      isCancelling: false,
      cancellationError: "",
    };
  } else if (normalizedState === "failed") {
    next = {
      ...next,
      runningJobId: null,
      statusText: message || "Conversion failed",
      etaText: "",
      activeJobId: null,
      isCancelling: false,
      cancellationError: "",
    };
  } else if (normalizedState === "idle") {
    next = {
      ...next,
      runningJobId: null,
      statusText: message || "Ready",
      etaText: "",
      performanceSnapshot: null,
      activeJobId: null,
      isCancelling: false,
      cancellationError: "",
    };
  } else {
    next = { ...next, statusText: message || `State: ${rawState || "unknown"}` };
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
      // Log-carried snapshots are live telemetry; once the job left "running"
      // the terminal progress event holds the final numbers and trailing logs
      // (e.g. the idle pool's "[PERF] worker_pool_*" summary) must not
      // overwrite them.
      const withPerformance =
        action.payload.performance && state.uiState === "running"
          ? {
              ...state,
              performanceSnapshot: mergePerformanceSnapshots(
                state.performanceSnapshot,
                action.payload.performance,
              ),
            }
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
      return {
        state: {
          ...state,
          uiState: "idle",
          statusText: "Backend disconnected",
          progressPct: 0,
          etaText: "",
          stages: createProgressStages(),
          currentStage: "",
          currentMessage: "",
          performanceSnapshot: null,
          activeJobId: null,
          isCancelling: false,
          cancellationError: "",
          cancellingJobId: null,
          runningJobId: null,
        },
      };
    case "refreshInactive": {
      // A job that vanished without a terminal event leaves its last live
      // message behind; only a settled outcome keeps its status text.
      const wasLive = state.uiState === "running" || state.uiState === "unknown";
      // A running job the backend no longer knows was lost with its process
      // (one being cancelled simply finished stopping).
      if (state.uiState === "running" && state.activeJobId && !state.isCancelling && !state.cancellingJobId) {
        return {
          state: {
            ...state,
            runningJobId: null,
            activeJobId: null,
            isCancelling: false,
            cancellationError: "",
            etaText: "",
            performanceSnapshot: null,
            uiState: "failed",
            statusText: INTERRUPTED_JOB_TEXT,
            currentMessage: INTERRUPTED_JOB_TEXT,
          },
        };
      }
      return {
        state: {
          ...state,
          runningJobId: null,
          activeJobId: null,
          isCancelling: false,
          cancellationError: "",
          etaText: "",
          performanceSnapshot: null,
          uiState: wasLive ? "idle" : state.uiState,
          statusText: wasLive ? "Ready" : state.statusText,
        },
      };
    }
    case "refreshActive":
      // The periodic reconcile poll confirming the job already being observed
      // must not replace the live progress message with a generic status.
      if (
        action.job.status === "running" &&
        state.uiState === "running" &&
        state.activeJobId === action.job.id
      ) {
        return { state };
      }
      // The fallback reconcile poll keeps seeing a non-terminal "running" job
      // while the backend winds down a cancellation that was already accepted
      // for this job. Preserve the "cancellation requested…" status/uiState
      // instead of clobbering it with "active job: running"; the poll still
      // flips the UI once the job reaches a terminal state (a state event).
      if (
        state.isCancelling &&
        state.cancellingJobId === action.job.id &&
        action.job.status === "running"
      ) {
        return {
          state:
            state.activeJobId === action.job.id
              ? state
              : { ...state, activeJobId: action.job.id },
        };
      }
      return {
        state: {
          ...state,
          activeJobId: action.job.id,
          uiState: action.job.status,
          statusText: ACTIVE_JOB_STATUS_TEXT[action.job.status] ?? "Conversion status unknown",
          runningJobId: action.job.status !== "running" ? null : state.runningJobId,
          isCancelling: action.job.status !== "running" ? false : state.isCancelling,
        },
      };
    case "cancelStart":
      return { state: { ...state, isCancelling: true, cancellationError: "" } };
    case "cancelRejected":
      return { state: { ...state, isCancelling: false, cancellationError: "Couldn't stop the conversion. Try again." } };
    case "cancelAccepted":
      return {
        state: addLog(
          {
            ...state,
            activeJobId: action.jobId,
            cancellingJobId: action.jobId,
            uiState: "running",
            etaText: "",
            currentMessage: "Cancellation requested",
            statusText: "Cancellation requested — waiting for the backend to stop",
          },
          "info",
          `job ${action.jobId} cancellation requested`,
          undefined,
          action.logId,
          action.fallbackTimestamp,
        ),
      };
    case "conversionStarted":
      if (state.terminalJobIds.includes(normalizeString(action.jobId))) {
        // A very fast failure can arrive on SSE before the start request
        // resolves. Do not re-latch the terminal job when that response
        // eventually calls markConversionStarted.
        return { state, ignored: true };
      }
      return {
        state: {
          ...state,
          activeJobId: action.jobId,
          cancelledJobId: null,
          cancellingJobId: null,
          isCancelling: false,
          cancellationError: "",
        },
      };
    case "setActiveJobId":
      return { state: { ...state, activeJobId: action.jobId } };
  }
}
