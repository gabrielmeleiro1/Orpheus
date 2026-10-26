import type { ApiClient } from "../../api/client";
import type {
  ConversionJobTerminalEvent,
  DesktopEventPayload,
} from "../../types/api";
import { normalizeTerminalStatus } from "./eventPayloadNormalizer";

export const TERMINAL_MISSING_POLL_THRESHOLD = 3;
// Match setup's 8-second status reads; one stalled fallback must release the
// in-flight guard so the next 1.5-second tick can retry.
const TERMINAL_POLL_REQUEST_TIMEOUT_MS = 8_000;
// Poll exceptions (e.g. a backend restart left this client pointing at a dead
// port/token) also mean the terminal event can never be observed through this
// client, so consecutive failures must terminate the wait instead of riding
// indefinitely. Slightly higher than the missing threshold so a
// transient network blip does not resolve a healthy wait early.
const TERMINAL_ERROR_POLL_THRESHOLD = 5;

interface PollTerminalStateArgs {
  apiClient: ApiClient;
  jobId: string;
  signal?: AbortSignal;
  missingPollCount: number;
  errorPollCount?: number;
  missingPollThreshold?: number;
  onTerminalStatus: (payload: DesktopEventPayload) => void;
}

type PollTerminalStateResult =
  | { kind: "pending"; missingPollCount: number; errorPollCount: number }
  | { kind: "terminal"; missingPollCount: number; errorPollCount: number }
  | {
      kind: "missing-threshold";
      missingPollCount: number;
      errorPollCount: number;
      event: ConversionJobTerminalEvent;
    };

export async function pollTerminalState({
  apiClient,
  jobId,
  signal,
  missingPollCount,
  errorPollCount = 0,
  missingPollThreshold = TERMINAL_MISSING_POLL_THRESHOLD,
  onTerminalStatus,
}: PollTerminalStateArgs): Promise<PollTerminalStateResult> {
  let response: Awaited<ReturnType<ApiClient["getActiveConversion"]>>;
  try {
    response = await apiClient.getActiveConversion({ signal, timeoutMs: TERMINAL_POLL_REQUEST_TIMEOUT_MS });
  } catch {
    const nextErrorPollCount = errorPollCount + 1;
    if (nextErrorPollCount >= TERMINAL_ERROR_POLL_THRESHOLD) {
      return {
        kind: "missing-threshold",
        missingPollCount,
        errorPollCount: nextErrorPollCount,
        event: {
          jobId,
          status: "unknown",
          message: "Conversion state unreachable; terminal event likely missed",
        },
      };
    }
    return { kind: "pending", missingPollCount, errorPollCount: nextErrorPollCount };
  }

  const activeJob = response.job;
  if (!activeJob || activeJob.id !== jobId) {
    const nextMissingPollCount = missingPollCount + 1;
    if (nextMissingPollCount >= missingPollThreshold) {
      return {
        kind: "missing-threshold",
        missingPollCount: nextMissingPollCount,
        errorPollCount: 0,
        event: {
          jobId,
          status: "unknown",
          message: "Conversion no longer active; terminal event likely missed",
        },
      };
    }
    return { kind: "pending", missingPollCount: nextMissingPollCount, errorPollCount: 0 };
  }

  const terminalStatus = normalizeTerminalStatus(activeJob.status);
  if (!terminalStatus) {
    return { kind: "pending", missingPollCount: 0, errorPollCount: 0 };
  }

  onTerminalStatus({
    event_type: "state",
    job_id: jobId,
    state: terminalStatus,
    message: activeJob.error || `conversion ${terminalStatus}`,
  });

  return { kind: "terminal", missingPollCount: 0, errorPollCount: 0 };
}
