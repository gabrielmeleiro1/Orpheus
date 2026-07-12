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
import {
  buildStageDetail,
  createProgressStages,
  doneStages,
  formatEtaLabel,
  normalizeProgressPercent,
  resolveStageIndex,
} from "./progressStageModel";

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
  terminalJobIds: string[];
}

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
