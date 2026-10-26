import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useGenerationGuard, useMountedRef } from "../../state/generationGuard";
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
import { addLog } from "./eventLogModel";
import { INTERRUPTED_JOB_TEXT, type EventStreamModelState } from "./eventStreamReducer";
import { useTerminalStateWaiters } from "./useTerminalStateWaiters";

// Active conversion state is an in-memory snapshot; a stalled read must
// release reconciliation and cancellation lookups for the next trigger.
const ACTIVE_STATE_REQUEST_TIMEOUT_MS = 8_000;

interface UseEventStreamArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
  // Notified with every reconciliation response that wins its ownership fence,
  // so a consumer can derive its own state from the lookup this hook already
  // performs instead of issuing a second identical request.
  onActiveConversionReconciled?: (response: ActiveConversionResponse) => void;
}

interface UseEventStreamResult {
  streamState: EventStreamConnectionState;
  /** A job is running but its event stream is down; its progress is not live. */
  connectionLost: boolean;
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
  cancellationError?: string;
  cancelActiveConversion: () => Promise<void>;
  refreshActiveConversionState: () => Promise<ActiveConversionResponse | null>;
  markConversionStarted: (jobId: string) => void;
  waitForTerminalState: (jobId: string) => Promise<ConversionJobTerminalEvent>;
}

function nowTimestamp(): string {
  return new Date().toLocaleTimeString("en-US", { hour12: false });
}

// SSE is the primary source of job state. While a job is active, the
// active-job endpoint is polled as a fallback so a missed terminal event
// cannot leave the interface running (or "cancelling") forever.
const ACTIVE_JOB_RECONCILE_INTERVAL_MS = 2_000;
// A cancel the backend never confirms is released after this many polls.
const CANCEL_RECONCILE_MAX_ATTEMPTS = 30;
const CONNECTION_LOST_TEXT = "Connection lost: reconnecting…";

function interruptedJobState(logs: EventStreamModelState["logs"], logId: number, timestamp: string): EventStreamModelState {
  const state: EventStreamModelState = {
    ...createInitialEventStreamState(),
    uiState: "failed",
    statusText: INTERRUPTED_JOB_TEXT,
    currentMessage: INTERRUPTED_JOB_TEXT,
    logs,
  };
  return addLog(state, "error", "conversion interrupted: the backend restarted", undefined, logId, timestamp);
}

export function useEventStream({
  apiClient,
  baseUrl,
  onActiveConversionReconciled,
}: UseEventStreamArgs): UseEventStreamResult {
  const [modelState, setModelState] = useState(createInitialEventStreamState);
  // Reducer input for callbacks that run between renders; always the latest
  // state handed to setModelState.
  const modelStateRef = useRef(modelState);
  const logSequenceRef = useRef(0);
  const activeJobIdRef = useRef<string | null>(null);
  const lookupFailedRef = useRef(false);
  const runtimeMutationGuard = useGenerationGuard();
  const cancellationAttemptSequenceRef = useRef(0);
  const mountedRef = useMountedRef(true);
  // Every backend client (or base URL) opens a new ownership epoch. The epoch
  // is derived during render with React's state-from-props pattern and is
  // published to async callbacks only once that render commits, so a render
  // React discards can never fence off the committed client's work.
  const [clientContext, setClientContext] = useState({ apiClient, baseUrl, generation: 0 });
  if (clientContext.apiClient !== apiClient || clientContext.baseUrl !== baseUrl) {
    setClientContext({ apiClient, baseUrl, generation: clientContext.generation + 1 });
  }
  const clientGeneration = clientContext.generation;
  const committedClientGenerationRef = useRef(clientGeneration);
  useLayoutEffect(() => {
    committedClientGenerationRef.current = clientGeneration;
  }, [clientGeneration]);
  const ownsClientContext = useCallback(() => mountedRef.current
    && committedClientGenerationRef.current === clientGeneration, [clientGeneration, mountedRef]);
  const onReconciledRef = useRef(onActiveConversionReconciled);
  useLayoutEffect(() => {
    onReconciledRef.current = onActiveConversionReconciled;
  }, [onActiveConversionReconciled]);
  const refreshGuard = useGenerationGuard();
  const streamInstanceRef = useRef<string | null>(null);
  const streamSequenceRef = useRef(0);
  const retiredStreamInstancesRef = useRef(new Set<string>());
  // A job was running when the client changed; a new backend instance means it was lost.
  const interruptedJobRef = useRef(false);

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
      if (
        payload.event_type === "state" ||
        payload.event_type === "progress" ||
        payload.event_type === "log"
      ) {
        const eventJobId = String(payload.job_id ?? "").trim();
        if (
          eventJobId &&
          activeJobIdRef.current &&
          eventJobId !== activeJobIdRef.current
        ) {
          return;
        }
      }
      if (payload.event_type === "progress" || payload.event_type === "state") {
        runtimeMutationGuard.invalidate();
        refreshGuard.invalidate();
      }
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
    if (!ownsClientContext()) return null;
    const ownsReconciliation = runtimeMutationGuard.capture();
    refreshGuard.invalidate();
    const ownsRefresh = refreshGuard.capture();
    if (!apiClient) {
      if (!ownsClientContext() || !ownsReconciliation()) {
        return null;
      }
      applyReducerAction({ type: "refreshNoClient" });
      return null;
    }

    try {
      const response = await apiClient.getActiveConversion({ timeoutMs: ACTIVE_STATE_REQUEST_TIMEOUT_MS });
      if (!ownsClientContext() || !ownsReconciliation()) {
        return null;
      }
      if (!ownsRefresh()) {
        return null;
      }
      // Claim this reconciliation before applying it so a concurrent lookup
      // that captured the same generation cannot land afterward and overwrite
      // the response accepted here.
      runtimeMutationGuard.invalidate();
      lookupFailedRef.current = false;
      onReconciledRef.current?.(response);
      if (response.job) {
        const terminalStatus = normalizeTerminalStatus(response.job.status);
        if (terminalStatus) {
          // This fenced snapshot is authoritative even when another job ran
          // while the stream was unavailable. Only unsolicited events should
          // be filtered against the previously observed active job id.
          applyReducerAction({
            type: "state",
            payload: {
              event_type: "state",
              job_id: response.job.id,
              state: terminalStatus,
              message: response.job.error || `conversion ${terminalStatus}`,
            },
            logId: nextLogId(),
            fallbackTimestamp: nowTimestamp(),
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
      if (
        !ownsClientContext() ||
        !ownsReconciliation() ||
        !ownsRefresh()
      ) {
        return null;
      }
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
  }, [apiClient, applyReducerAction, nextLogId, ownsClientContext, pushLog]);

  const markConversionStarted = useCallback(
    (jobId: string) => {
      const normalizedJobId = String(jobId ?? "").trim();
      if (!normalizedJobId || !ownsClientContext()) {
        return;
      }
      runtimeMutationGuard.invalidate();
      applyReducerAction({ type: "conversionStarted", jobId: normalizedJobId });
    },
    [applyReducerAction, ownsClientContext],
  );

  const handleStreamOpen = useCallback(() => {
    void refreshActiveConversionState();
  }, [refreshActiveConversionState]);

  const handleStreamPayload = useCallback((payload: DesktopEventPayload) => {
    if (!ownsClientContext()) return;
    const instanceId = payload.backend_instance_id;
    let restarted = false;
    if (instanceId) {
      if (retiredStreamInstancesRef.current.has(instanceId)) {
        return;
      }
      if (instanceId !== streamInstanceRef.current) {
        const previousInstance = streamInstanceRef.current;
        streamInstanceRef.current = instanceId;
        streamSequenceRef.current = 0;
        if (previousInstance !== null) {
          retiredStreamInstancesRef.current.add(previousInstance);
          restarted = true;
          runtimeMutationGuard.invalidate();
          refreshGuard.invalidate();
          cancellationAttemptSequenceRef.current += 1;
          // A job cannot survive its backend: say so instead of resetting to idle.
          const interrupted = modelStateRef.current.uiState === "running" || interruptedJobRef.current
            || modelStateRef.current.statusText === INTERRUPTED_JOB_TEXT;
          interruptedJobRef.current = false;
          const state = interrupted
            ? interruptedJobState(modelStateRef.current.logs, nextLogId(), nowTimestamp())
            : { ...createInitialEventStreamState(), logs: modelStateRef.current.logs };
          modelStateRef.current = state;
          activeJobIdRef.current = null;
          setModelState(state);
        }
      }
      if (!restarted) interruptedJobRef.current = false;
      if (payload.event_sequence !== undefined) {
        if (payload.event_sequence <= streamSequenceRef.current) {
          return;
        }
        streamSequenceRef.current = payload.event_sequence;
      }
    }
    applyEventPayload(payload);
    if (restarted) {
      // The open callback can precede identity discovery. Reconcile again with
      // the old job and in-flight requests fenced off by the instance change.
      void refreshActiveConversionState();
    }
  }, [applyEventPayload, nextLogId, ownsClientContext, refreshActiveConversionState]);

  const streamState = useEventStreamTransport({
    apiClient,
    baseUrl,
    onLog: pushLog,
    onOpen: handleStreamOpen,
    onPayload: handleStreamPayload,
  });

  // Retire the old backend's job and cancellation state before discovering the
  // replacement. Retained callbacks also check their context before any I/O.
  const modelClientGenerationRef = useRef(clientGeneration);
  useEffect(() => {
    refreshGuard.invalidate();
    if (modelClientGenerationRef.current === clientGeneration) return;
    modelClientGenerationRef.current = clientGeneration;
    runtimeMutationGuard.invalidate();
    cancellationAttemptSequenceRef.current += 1;
    lookupFailedRef.current = false;
    // Whether that job survived is known once the new stream names its backend.
    interruptedJobRef.current = modelStateRef.current.uiState === "running";
    const state = { ...createInitialEventStreamState(), logs: modelStateRef.current.logs };
    modelStateRef.current = state;
    activeJobIdRef.current = null;
    setModelState(state);
  }, [clientGeneration]);

  // This hook owns startup reconciliation. Consumers that need the response
  // body take it via onActiveConversionReconciled rather than repeating the
  // lookup. Stream opens reconcile again to close the subscription gap.
  useEffect(() => {
    void refreshActiveConversionState();
  }, [refreshActiveConversionState]);

  useEffect(() => {
    if (!apiClient || !modelState.activeJobId || modelState.isCancelling) {
      return undefined;
    }
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void refreshActiveConversionState().finally(() => { inFlight = false; });
    }, ACTIVE_JOB_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [apiClient, modelState.activeJobId, modelState.isCancelling, refreshActiveConversionState]);

  useEffect(() => {
    if (!modelState.isCancelling || !apiClient) {
      return undefined;
    }
    let attempts = 0;
    let inFlight = false;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (attempts > CANCEL_RECONCILE_MAX_ATTEMPTS) {
        window.clearInterval(timer);
        pushLog("warn", "cancellation is taking too long; job state may be stale");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }
      // A newer lookup supersedes earlier snapshots. Let a slow request
      // finish before polling again, while retaining the overall time limit.
      if (inFlight) return;
      inFlight = true;
      void refreshActiveConversionState().finally(() => { inFlight = false; });
    }, ACTIVE_JOB_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [
    apiClient,
    applyReducerAction,
    modelState.isCancelling,
    pushLog,
    refreshActiveConversionState,
  ]);

  const cancelActiveConversion = useCallback(async () => {
    if (!ownsClientContext()) return;
    if (!apiClient) {
      pushLog("warn", "Backend is not connected");
      return;
    }

    const attemptId = ++cancellationAttemptSequenceRef.current;
    const ownsLookup = runtimeMutationGuard.capture();
    applyReducerAction({ type: "cancelStart" });

    let targetJobId: string | null = activeJobIdRef.current;
    try {
      if (!targetJobId) {
        const active = await apiClient.getActiveConversion({ timeoutMs: ACTIVE_STATE_REQUEST_TIMEOUT_MS });
        if (!ownsClientContext() || attemptId !== cancellationAttemptSequenceRef.current) {
          return;
        }
        if (!ownsLookup()) {
          pushLog("warn", "cancellation superseded by a newer conversion");
          applyReducerAction({ type: "cancelRejected" });
          return;
        }
        if (active.active && active.job) {
          targetJobId = active.job.id;
          activeJobIdRef.current = targetJobId;
          runtimeMutationGuard.invalidate();
          applyReducerAction({ type: "setActiveJobId", jobId: targetJobId });
        }
      }

      if (!targetJobId) {
        pushLog("warn", "no active job");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }

      if (!ownsClientContext() || attemptId !== cancellationAttemptSequenceRef.current) {
        return;
      }
      if (
        activeJobIdRef.current === null ||
        modelStateRef.current.terminalJobIds.includes(targetJobId)
      ) {
        return;
      }
      if (activeJobIdRef.current !== targetJobId) {
        if (ownsClientContext()) {
          pushLog("warn", "cancellation superseded by a newer conversion");
          applyReducerAction({ type: "cancelRejected" });
        }
        return;
      }

      const response = await apiClient.cancelConversion(targetJobId);
      if (!ownsClientContext() || attemptId !== cancellationAttemptSequenceRef.current) {
        return;
      }
      if (
        activeJobIdRef.current === null ||
        modelStateRef.current.terminalJobIds.includes(targetJobId)
      ) {
        return;
      }
      if (activeJobIdRef.current !== targetJobId) {
        if (ownsClientContext()) {
          pushLog("warn", "cancellation superseded by a newer conversion");
          applyReducerAction({ type: "cancelRejected" });
        }
        return;
      }
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
      if (
        !ownsClientContext() ||
        attemptId !== cancellationAttemptSequenceRef.current ||
        (targetJobId !== null && activeJobIdRef.current !== targetJobId) ||
        (targetJobId === null && !ownsLookup())
      ) {
        return;
      }
      const message = error instanceof Error ? error.message : "cancel request failed";
      pushLog("error", message);
      applyReducerAction({ type: "cancelRejected" });
    }
  }, [apiClient, applyReducerAction, nextLogId, ownsClientContext, pushLog]);

  // While the stream is down a running job's numbers are frozen: say so and drop the ETA.
  const connectionLost = streamState === "error" && modelState.uiState === "running";
  return useMemo(() => ({
    streamState,
    connectionLost,
    uiState: modelState.uiState,
    statusText: connectionLost ? CONNECTION_LOST_TEXT : modelState.statusText,
    progressPct: modelState.progressPct,
    etaText: connectionLost ? "" : modelState.etaText,
    stages: modelState.stages,
    currentStage: modelState.currentStage,
    currentMessage: modelState.currentMessage,
    performanceSnapshot: modelState.performanceSnapshot,
    activeJobId: modelState.activeJobId,
    logs: modelState.logs,
    isCancelling: modelState.isCancelling,
    cancellationError: modelState.cancellationError,
    cancelActiveConversion,
    refreshActiveConversionState,
    markConversionStarted,
    // Stable memoized callback from useTerminalStateWaiters; it normalizes the
    // job id itself, so no per-render wrapper is needed.
    waitForTerminalState,
  }), [
    cancelActiveConversion,
    connectionLost,
    markConversionStarted,
    modelState,
    refreshActiveConversionState,
    streamState,
    waitForTerminalState,
  ]);
}
