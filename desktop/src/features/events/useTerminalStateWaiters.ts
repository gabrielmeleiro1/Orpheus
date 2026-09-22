import { useCallback, useEffect, useRef } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ConversionJobTerminalEvent,
  DesktopEventPayload,
} from "../../types/api";
import {
  pollTerminalState as pollTerminalStateFromClient,
  TERMINAL_MISSING_POLL_THRESHOLD,
} from "./terminalStateWaiterModel";

const TERMINAL_POLL_MS = 1500;

interface UseTerminalStateWaitersArgs {
  apiClient: ApiClient | null;
  onTerminalStatus: (payload: DesktopEventPayload) => void;
}

interface TerminalWaiter {
  finish: (event: ConversionJobTerminalEvent) => void;
}

function normalizeString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function useTerminalStateWaiters({
  apiClient,
  onTerminalStatus,
}: UseTerminalStateWaitersArgs) {
  const terminalEventsRef = useRef<Map<string, ConversionJobTerminalEvent>>(new Map());
  const terminalWaitersRef = useRef<Map<string, TerminalWaiter[]>>(new Map());

  const resolveTerminalState = useCallback((event: ConversionJobTerminalEvent) => {
    terminalEventsRef.current.set(event.jobId, event);
    const waiters = terminalWaitersRef.current.get(event.jobId) ?? [];
    terminalWaitersRef.current.delete(event.jobId);
    for (const waiter of waiters) {
      waiter.finish(event);
    }
  }, []);

  const waitForTerminalState = useCallback(
    (jobId: string): Promise<ConversionJobTerminalEvent> => {
      const normalizedJobId = normalizeString(jobId);
      if (!normalizedJobId) {
        return Promise.resolve({
          jobId: "",
          status: "unknown",
          message: "missing job id",
        });
      }

      if (!apiClient) {
        return Promise.resolve({ jobId: normalizedJobId, status: "unknown", message: "backend is not connected" });
      }

      const cached = terminalEventsRef.current.get(normalizedJobId);
      if (cached) {
        return Promise.resolve(cached);
      }

      return new Promise((resolve) => {
        let settled = false;
        let pollId: number | undefined;

        const removeWaiter = (waiter: TerminalWaiter) => {
          const waiters = terminalWaitersRef.current.get(normalizedJobId) ?? [];
          const remaining = waiters.filter((item) => item !== waiter);
          if (remaining.length > 0) {
            terminalWaitersRef.current.set(normalizedJobId, remaining);
          } else {
            terminalWaitersRef.current.delete(normalizedJobId);
          }
        };

        const cleanup = () => {
          if (pollId !== undefined) {
            window.clearInterval(pollId);
          }
        };

        const waiter: TerminalWaiter = {
          finish: (event: ConversionJobTerminalEvent) => {
            if (settled) {
              return;
            }
            settled = true;
            cleanup();
            removeWaiter(waiter);
            resolve(event);
          },
        };

        let pollInFlight = false;
        let missingPollCount = 0;
        let errorPollCount = 0;

        const runTerminalStatePoll = async () => {
          if (!apiClient || settled || pollInFlight) {
            return;
          }
          pollInFlight = true;
          try {
            const result = await pollTerminalStateFromClient({
              apiClient,
              jobId: normalizedJobId,
              missingPollCount,
              errorPollCount,
              missingPollThreshold: TERMINAL_MISSING_POLL_THRESHOLD,
              // pollTerminalStateFromClient invokes this before it returns, so
              // the post-await `settled` check below is too late: a response
              // that lands after the next job started would otherwise apply
              // this job's terminal state and null out activeJobId, disabling
              // Cancel for the run that is actually in flight.
              onTerminalStatus: (payload) => {
                if (!settled) {
                  onTerminalStatus(payload);
                }
              },
            });
            if (settled) {
              return;
            }
            missingPollCount = result.missingPollCount;
            errorPollCount = result.errorPollCount;
            if (result.kind === "missing-threshold") {
              waiter.finish(result.event);
            }
          } catch {
            // SSE remains the primary path; this is only a resilience fallback.
            // Poll exceptions are counted inside pollTerminalState; this catch
            // only guards against unexpected synchronous failures.
          } finally {
            pollInFlight = false;
          }
        };

        const waiters = terminalWaitersRef.current.get(normalizedJobId) ?? [];
        terminalWaitersRef.current.set(normalizedJobId, [...waiters, waiter]);
        pollId = window.setInterval(() => {
          void runTerminalStatePoll();
        }, TERMINAL_POLL_MS);
        void runTerminalStatePoll();
      });
    },
    [apiClient, onTerminalStatus],
  );

  // Flush pending waiters whenever the apiClient identity changes (backend
  // restart mints a new port/token) and on unmount: waiters closed over the
  // old client can never observe the terminal event, so resolving them as
  // "unknown" lets callers re-sync instead of hanging until the poll.
  useEffect(
    () => () => {
      const pendingWaiters = Array.from(terminalWaitersRef.current.entries());
      for (const [jobId, waiters] of pendingWaiters) {
        for (const waiter of waiters) {
          waiter.finish({
            jobId,
            status: "unknown",
            message: "terminal wait cancelled",
          });
        }
      }
      terminalWaitersRef.current.clear();
    },
    [apiClient],
  );

  return {
    resolveTerminalState,
    waitForTerminalState,
  };
}
