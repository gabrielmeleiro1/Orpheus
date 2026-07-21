import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ActiveConversionResponse,
  ConversionJobStatus,
  ConversionJobTerminalEvent,
  DesktopEventPayload,
  EventStreamConnectionState,
  EventStreamLogEntry,
  PerformanceSnapshot,
  ProgressStageView,
} from "../../types/api";
import {
  createInitialEventStreamState,
  eventStreamReducer,
  normalizeTerminalStatus,
} from "./eventStreamModel";
import { useEventStreamTransport } from "./useEventStreamTransport";
import { useTerminalStateWaiters } from "./useTerminalStateWaiters";

interface UseEventStreamArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
}

interface UseEventStreamResult {
  streamState: EventStreamConnectionState;
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
  cancelActiveConversion: () => Promise<void>;
  refreshActiveConversionState: () => Promise<ActiveConversionResponse | null>;
  waitForTerminalState: (jobId: string) => Promise<ConversionJobTerminalEvent>;
}

function nowTimestamp(): string {
  return new Date().toLocaleTimeString("en-US", { hour12: false });
}

// After a cancel is accepted the UI normally unlatches on the terminal SSE
// event. Poll the active-job endpoint as a fallback so a missed or delayed
// event cannot leave the interface stuck on "cancelling" forever.
const CANCEL_RECONCILE_INTERVAL_MS = 2_000;
const CANCEL_RECONCILE_MAX_ATTEMPTS = 30;

export function useEventStream({
  apiClient,
  baseUrl,
}: UseEventStreamArgs): UseEventStreamResult {
  const initialModelState = useRef(createInitialEventStreamState());
  const [modelState, setModelState] = useState(initialModelState.current);
  const modelStateRef = useRef(initialModelState.current);
  const logSequenceRef = useRef(0);
  const activeJobIdRef = useRef<string | null>(null);
  const lookupFailedRef = useRef(false);

  const nextLogId = useCallback(() => {
    logSequenceRef.current += 1;
    return logSequenceRef.current;
  }, []);

  const resolveTerminalStateRef = useRef<(event: ConversionJobTerminalEvent) => void>(() => {});

  const applyReducerAction = useCallback(
    (action: Parameters<typeof eventStreamReducer>[1]) => {
      const result = eventStreamReducer(modelStateRef.current, action);
      modelStateRef.current = result.state;
      activeJobIdRef.current = result.state.activeJobId;
      setModelState(result.state);
      if (result.terminalEvent) {
        resolveTerminalStateRef.current(result.terminalEvent);
      }
    },
    [],
  );

  const pushLog = useCallback(
    (level: string, message: string, timestamp?: string) => {
      applyReducerAction({
        type: "pushLog",
        level,
        message,
        timestamp,
        logId: nextLogId(),
        fallbackTimestamp: nowTimestamp(),
      });
    },
    [applyReducerAction, nextLogId],
  );

  const applyEventPayload = useCallback(
    (payload: DesktopEventPayload) => {
      if (payload.event_type === "progress") {
        applyReducerAction({ type: "progress", payload });
        return;
      }

      if (payload.event_type === "log") {
        applyReducerAction({
          type: "log",
          payload,
          logId: nextLogId(),
          fallbackTimestamp: nowTimestamp(),
        });
        return;
      }

      if (payload.event_type === "state") {
        applyReducerAction({
          type: "state",
          payload,
          logId: nextLogId(),
          fallbackTimestamp: nowTimestamp(),
        });
      }
    },
    [applyReducerAction, nextLogId],
  );

  const { resolveTerminalState, waitForTerminalState } = useTerminalStateWaiters({
    apiClient,
    onTerminalStatus: applyEventPayload,
  });

  useEffect(() => {
    resolveTerminalStateRef.current = resolveTerminalState;
  }, [resolveTerminalState]);

  const refreshActiveConversionState = useCallback(async () => {
    if (!apiClient) {
      applyReducerAction({ type: "refreshNoClient" });
      return null;
    }

    try {
      const response = await apiClient.getActiveConversion();
      lookupFailedRef.current = false;
      if (response.job) {
        const terminalStatus = normalizeTerminalStatus(response.job.status);
        if (terminalStatus) {
          applyEventPayload({
            event_type: "state",
            job_id: response.job.id,
            state: terminalStatus,
            message: response.job.error || `conversion ${terminalStatus}`,
          });
          return response;
        }
      }
      if (!response.active || !response.job) {
        applyReducerAction({ type: "refreshInactive" });
        return response;
      }

      applyReducerAction({
        type: "refreshActive",
        job: {
          id: response.job.id,
          status: response.job.status,
        },
      });
      return response;
    } catch {
      // The reconcile fallback polls every couple of seconds while a cancel is
      // in flight; log only the first failure of a streak so a transient (or
      // persistent) outage cannot spam the log. A later success resets the
      // latch, and the reconcile loop's max-attempts guard still surfaces a
      // cancel that never lands.
      if (!lookupFailedRef.current) {
        lookupFailedRef.current = true;
        pushLog("warn", "active conversion lookup failed");
      }
      return null;
    }
  }, [apiClient, applyEventPayload, applyReducerAction, pushLog]);

  const handleStreamOpen = useCallback(() => {
    void refreshActiveConversionState();
  }, [refreshActiveConversionState]);

  const streamState = useEventStreamTransport({
    apiClient,
    baseUrl,
    onLog: pushLog,
    onOpen: handleStreamOpen,
    onPayload: applyEventPayload,
  });

  useEffect(() => {
    void refreshActiveConversionState();
  }, [refreshActiveConversionState]);

  useEffect(() => {
    if (!modelState.isCancelling || !apiClient) {
      return undefined;
    }
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (attempts > CANCEL_RECONCILE_MAX_ATTEMPTS) {
        window.clearInterval(timer);
        pushLog("warn", "cancellation is taking too long; job state may be stale");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }
      void refreshActiveConversionState();
    }, CANCEL_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [
    apiClient,
    applyReducerAction,
    modelState.isCancelling,
    pushLog,
    refreshActiveConversionState,
  ]);

  const cancelActiveConversion = useCallback(async () => {
    if (!apiClient) {
      pushLog("warn", "backend is not connected");
      return;
    }

    applyReducerAction({ type: "cancelStart" });

    try {
      let targetJobId = activeJobIdRef.current;

      if (!targetJobId) {
        const active = await apiClient.getActiveConversion();
        if (active.active && active.job) {
          targetJobId = active.job.id;
          activeJobIdRef.current = targetJobId;
          applyReducerAction({ type: "setActiveJobId", jobId: targetJobId });
        }
      }

      if (!targetJobId) {
        pushLog("warn", "no active job");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }

      const response = await apiClient.cancelConversion(targetJobId);
      if (!response.cancelled) {
        pushLog("error", "cancel failed");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }

      applyReducerAction({
        type: "cancelAccepted",
        jobId: targetJobId,
        logId: nextLogId(),
        fallbackTimestamp: nowTimestamp(),
      });
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "cancel request failed";
      pushLog("error", message);
      applyReducerAction({ type: "cancelRejected" });
    }
  }, [apiClient, applyReducerAction, nextLogId, pushLog]);

  return {
    streamState,
    uiState: modelState.uiState,
    statusText: modelState.statusText,
    progressPct: modelState.progressPct,
    etaText: modelState.etaText,
    stages: modelState.stages,
    currentStage: modelState.currentStage,
    currentMessage: modelState.currentMessage,
    performanceSnapshot: modelState.performanceSnapshot,
    activeJobId: modelState.activeJobId,
    logs: modelState.logs,
    isCancelling: modelState.isCancelling,
    cancelActiveConversion,
    refreshActiveConversionState,
    // Stable memoized callback from useTerminalStateWaiters; it normalizes the
    // job id itself, so no per-render wrapper is needed.
    waitForTerminalState,
  };
}
