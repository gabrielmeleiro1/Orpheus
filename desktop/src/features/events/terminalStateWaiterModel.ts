import type { ApiClient } from "../../api/client";
import type {
  ConversionJobTerminalEvent,
  DesktopEventPayload,
} from "../../types/api";
import { normalizeTerminalStatus } from "./eventPayloadNormalizer";

export const TERMINAL_MISSING_POLL_THRESHOLD = 3;

interface PollTerminalStateArgs {
  apiClient: ApiClient;
  jobId: string;
  missingPollCount: number;
  missingPollThreshold?: number;
  onTerminalStatus: (payload: DesktopEventPayload) => void;
}

export type PollTerminalStateResult =
  | { kind: "pending"; missingPollCount: number }
  | { kind: "terminal"; missingPollCount: number }
  | {
      kind: "missing-threshold";
      missingPollCount: number;
      event: ConversionJobTerminalEvent;
    };

export async function pollTerminalState({
  apiClient,
  jobId,
  missingPollCount,
  missingPollThreshold = TERMINAL_MISSING_POLL_THRESHOLD,
  onTerminalStatus,
}: PollTerminalStateArgs): Promise<PollTerminalStateResult> {
  const response = await apiClient.getActiveConversion();
  const activeJob = response.job;
  if (!activeJob || activeJob.id !== jobId) {
    const nextMissingPollCount = missingPollCount + 1;
    if (nextMissingPollCount >= missingPollThreshold) {
      return {
        kind: "missing-threshold",
        missingPollCount: nextMissingPollCount,
        event: {
          jobId,
          status: "unknown",
          message: "conversion no longer active; terminal event likely missed",
        },
      };
    }
    return { kind: "pending", missingPollCount: nextMissingPollCount };
  }

  const terminalStatus = normalizeTerminalStatus(activeJob.status);
  if (!terminalStatus) {
    return { kind: "pending", missingPollCount: 0 };
  }

  onTerminalStatus({
    event_type: "state",
    job_id: jobId,
    state: terminalStatus,
    message: activeJob.error || `conversion ${terminalStatus}`,
  });

  return { kind: "terminal", missingPollCount: 0 };
}
