import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../../api/client";
import type { ApiRequestOptions } from "../../api/http";
import type { ActiveConversionResponse, StartConversionRequest } from "../../types/api";
import { useEventStream } from "../events/useEventStream";

interface UseConversionJobArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
}

export type StartConversionResult =
  | { ok: true; jobId: string }
  | { ok: false; error: string; status?: number | null; serverCode?: string | null; ambiguous?: boolean; clientRequestId?: string };

function reconcileStart(
  response: ActiveConversionResponse | null,
  payload: StartConversionRequest,
  error: string,
): StartConversionResult {
  if (!response) {
    return { ok: false, ambiguous: true, clientRequestId: payload.client_request_id ?? undefined,
      error: "Checking conversion start. Refresh before retrying." };
  }
  if (response.job && response.job.client_request_id === payload.client_request_id) {
    return { ok: true, jobId: response.job.id };
  }
  return { ok: false, error };
}

function normalizeErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message.trim();
  }
  return "request failed";
}

function validateStartPayload(payload: StartConversionRequest): string {
  if (!String(payload.source_path ?? "").trim()) {
    return "Provide a source path before starting.";
  }
  if (!String(payload.runtime_options?.tts_engine ?? "").trim()) {
    return "Select a TTS engine.";
  }
  if (!String(payload.runtime_options?.voice ?? "").trim()) {
    return "Select a voice.";
  }
  return "";
}

export function useConversionJob({ apiClient, baseUrl }: UseConversionJobArgs) {
  const [isStarting, setIsStarting] = useState(false);
  const [isCheckingStart, setIsCheckingStart] = useState(false);
  const pendingStartRef = useRef<{ payload: StartConversionRequest; error: string; deadline: number } | null>(null);
  const [startReconciliation, setStartReconciliation] = useState<{
    clientRequestId: string;
    result: StartConversionResult;
  } | null>(null);
  const [formError, setFormError] = useState("");
  const backendContextRef = useRef({ apiClient, baseUrl });
  useLayoutEffect(() => {
    backendContextRef.current = { apiClient, baseUrl };
  });
  // Title/author of the last started or reconciled job, so status surfaces
  // label the job itself rather than whatever the library has selected.
  const [activeJobMeta, setActiveJobMeta] = useState<{
    jobId: string;
    apiClient: ApiClient | null;
    baseUrl: string;
    title: string;
    author: string;
    /** The library book this job writes to, once the backend has assigned one. */
    bookId: number | null;
  } | null>(null);
  // The event stream reconciles on mount and on reconnect; take the job meta
  // from that response instead of issuing a second identical lookup.
  const applyReconciledJobMeta = useCallback((response: ActiveConversionResponse) => {
    if (backendContextRef.current.apiClient !== apiClient || backendContextRef.current.baseUrl !== baseUrl) return;
    const pending = pendingStartRef.current;
    if (pending) {
      const result = reconcileStart(response, pending.payload, pending.error);
      if (result.ok || !result.ambiguous) {
        pendingStartRef.current = null;
        setIsCheckingStart(false);
        setFormError(result.ok ? "" : result.error);
        setStartReconciliation({ clientRequestId: pending.payload.client_request_id!, result });
      }
    }
    if (!response.job) return;
    const jobId = response.job.id;
    const title = String(response.job.title ?? "").trim();
    const author = String(response.job.author ?? "").trim();
    const bookId = response.job.book_id ?? null;
    // Reconcile polls repeat this snapshot every couple of seconds while a job
    // runs; keep the same object so the dashboard is not re-rendered for it.
    setActiveJobMeta((current) => (
      current?.jobId === jobId && current.apiClient === apiClient && current.baseUrl === baseUrl
        && current.title === title && current.author === author && current.bookId === bookId
        ? current
        : { jobId, apiClient, baseUrl, title, author, bookId }
    ));
  }, [apiClient, baseUrl]);
  const eventStream = useEventStream({
    apiClient,
    baseUrl,
    onActiveConversionReconciled: applyReconciledJobMeta,
  });
  useEffect(() => {
    // SSE can announce another job before its title/author snapshot arrives.
    // Retire the previous metadata so it cannot reappear when that job ends.
    if (eventStream.activeJobId !== null) {
      setActiveJobMeta((current) => current?.jobId === eventStream.activeJobId ? current : null);
    }
  }, [eventStream.activeJobId]);
  // The book id is read from the reconcile polls of a running job. A job that
  // ends before one saw it (a cancel moments after Start) may still have made
  // its book: ask once, so a later repair of that book finds this job's row.
  const jobEnded = eventStream.uiState === "completed" || eventStream.uiState === "failed"
    || eventStream.uiState === "cancelled";
  const endedJobWithoutBook = jobEnded && eventStream.activeJobId === null && activeJobMeta?.bookId === null
    ? activeJobMeta.jobId : null;
  const endedJobBookLookupRef = useRef<string | null>(null);
  useEffect(() => {
    if (!apiClient || endedJobWithoutBook === null || endedJobBookLookupRef.current === endedJobWithoutBook) return;
    endedJobBookLookupRef.current = endedJobWithoutBook;
    let stale = false;
    void (async () => {
      try {
        const { job } = await apiClient.getActiveConversion();
        const bookId = job?.id === endedJobWithoutBook ? job.book_id ?? null : null;
        if (stale || bookId === null) return;
        setActiveJobMeta((current) => (
          current?.jobId === endedJobWithoutBook && current.apiClient === apiClient ? { ...current, bookId } : current
        ));
      } catch {
        // Unread, the row keeps its own outcome and is simply not linked.
      }
    })();
    return () => { stale = true; };
  }, [apiClient, endedJobWithoutBook]);
  const startRequestRef = useRef<object | null>(null);
  const currentClientRef = useRef(apiClient);
  useLayoutEffect(() => {
    currentClientRef.current = apiClient;
  });
  useEffect(() => {
    startRequestRef.current = null;
    pendingStartRef.current = null;
    setStartReconciliation(null);
    setIsStarting(false);
    setIsCheckingStart(false);
    setActiveJobMeta(null);
    return () => { startRequestRef.current = null; pendingStartRef.current = null; };
  }, [apiClient, baseUrl]);

  useEffect(() => {
    const pending = pendingStartRef.current;
    if (!isCheckingStart || isStarting || !pending) return;
    let stopped = false;
    let retryTimer: ReturnType<typeof setTimeout>;
    let delay = 500;
    const deadlineTimer = setTimeout(() => {
      if (pendingStartRef.current !== pending) return;
      pendingStartRef.current = null;
      setIsCheckingStart(false);
      const result: StartConversionResult = { ok: false,
        error: "Could not confirm whether the conversion started. Refresh status, then retry manually." };
      setFormError(result.error);
      setStartReconciliation({ clientRequestId: pending.payload.client_request_id!, result });
    }, Math.max(0, pending.deadline - Date.now()));
    const retry = async () => {
      const response = await eventStream.refreshActiveConversionState();
      if (stopped || pendingStartRef.current !== pending) return;
      if (response) {
        applyReconciledJobMeta(response);
        return;
      }
      delay = Math.min(delay * 2, 8000);
      retryTimer = setTimeout(() => void retry(), delay);
    };
    retryTimer = setTimeout(() => void retry(), delay);
    return () => {
      stopped = true;
      clearTimeout(retryTimer);
      clearTimeout(deadlineTimer);
    };
  }, [isCheckingStart, isStarting, eventStream.refreshActiveConversionState, applyReconciledJobMeta]);

  const clearFormError = useCallback(() => {
    setFormError("");
  }, []);

  const refreshActiveConversion = useCallback(async () => {
    const response = await eventStream.refreshActiveConversionState();
    if (response) applyReconciledJobMeta(response);
  }, [eventStream.refreshActiveConversionState, applyReconciledJobMeta]);

  const startConversion = useCallback(
    async (payload: StartConversionRequest, options?: ApiRequestOptions): Promise<StartConversionResult> => {
      const obsoleteResult = { ok: false as const, error: "Backend connection changed. Check the active conversion before retrying." };
      if (currentClientRef.current !== apiClient || backendContextRef.current.baseUrl !== baseUrl) {
        return obsoleteResult;
      }
      if (pendingStartRef.current || isCheckingStart) {
        return { ok: false, ambiguous: true, error: "Checking conversion start. Refresh before retrying." };
      }
      if (startRequestRef.current) {
        const error = "conversion already starting";
        setFormError(error);
        return { ok: false, error };
      }

      const validationError = validateStartPayload(payload);
      if (validationError) {
        setFormError(validationError);
        return { ok: false, error: validationError };
      }

      if (!apiClient) {
        const error = "Backend is not connected.";
        setFormError(error);
        return { ok: false, error };
      }

      const submission = { ...payload, client_request_id: crypto.randomUUID() };
      const request = {};
      startRequestRef.current = request;
      const ownsRequest = () => currentClientRef.current === apiClient && backendContextRef.current.baseUrl === baseUrl
        && startRequestRef.current === request;
      setStartReconciliation(null);
      setIsStarting(true);
      setFormError("");

      try {
        const response = await apiClient.startConversion(submission, options);
        if (!ownsRequest()) {
          return obsoleteResult;
        }
        if (!response.job_id) {
          const error = "conversion failed to start";
          setFormError(error);
          return { ok: false, error };
        }
        setActiveJobMeta({
          jobId: response.job_id,
          apiClient,
          baseUrl,
          title: String(payload.title ?? "").trim(),
          author: String(payload.author ?? "").trim(),
          bookId: null,
        });
        eventStream.markConversionStarted(response.job_id);
        return { ok: true, jobId: response.job_id };
      } catch (error: unknown) {
        if (!ownsRequest()) {
          return obsoleteResult;
        }
        const message = normalizeErrorMessage(error);
        if (!(error instanceof ApiError) || error.status === null) {
          setIsCheckingStart(true);
          const active = await eventStream.refreshActiveConversionState();
          if (!ownsRequest()) return obsoleteResult;
          const result = reconcileStart(active, submission, message);
          if (result.ok) {
            setActiveJobMeta({
              jobId: result.jobId,
              apiClient,
              baseUrl,
              title: String(active?.job?.title ?? "").trim(),
              author: String(active?.job?.author ?? "").trim(),
              bookId: active?.job?.book_id ?? null,
            });
            setFormError("");
            setIsCheckingStart(false);
            return result;
          }
          if (result.ambiguous) {
            pendingStartRef.current = { payload: submission, error: message, deadline: Date.now() + 30_000 };
            return result;
          }
          setIsCheckingStart(false);
        }
        setFormError(message);
        return { ok: false, error: message,
          ...(error instanceof ApiError ? { status: error.status, serverCode: error.serverCode } : {}),
        };
      } finally {
        if (ownsRequest()) {
          startRequestRef.current = null;
          setIsStarting(false);
        }
      }
    },
    [apiClient, baseUrl, isCheckingStart, eventStream.markConversionStarted, eventStream.refreshActiveConversionState],
  );

  const visibleJobMeta = activeJobMeta?.apiClient === apiClient && activeJobMeta?.baseUrl === baseUrl
    && (eventStream.activeJobId === null || activeJobMeta.jobId === eventStream.activeJobId)
    ? activeJobMeta : null;
  const jobBookJobId = visibleJobMeta?.bookId != null ? visibleJobMeta.jobId : null;
  const jobBookId = visibleJobMeta?.bookId ?? null;
  // Which book the current (or just-ended) job belongs to. A repair started
  // from Library shares its book with the conversion that first made it.
  const activeJobBook = useMemo(
    () => (jobBookJobId !== null && jobBookId !== null ? { jobId: jobBookJobId, bookId: jobBookId } : null),
    [jobBookJobId, jobBookId],
  );

  return useMemo(() => ({
    ...eventStream,
    activeJobStatus: eventStream.uiState,
    activeJobTitle: visibleJobMeta?.title ?? "",
    activeJobAuthor: visibleJobMeta?.author ?? "",
    /** False until the running job's title and author have been read from the backend. */
    activeJobMetaLoaded: visibleJobMeta !== null,
    activeJobBook,
    isStarting,
    isCheckingStart,
    startReconciliation,
    formError,
    clearFormError,
    startConversion,
    refreshActiveConversion,
  }), [
    visibleJobMeta,
    activeJobBook,
    clearFormError,
    eventStream,
    formError,
    isStarting,
    isCheckingStart,
    startReconciliation,
    refreshActiveConversion,
    startConversion,
  ]);
}

export type ConversionJobController = ReturnType<typeof useConversionJob>;
