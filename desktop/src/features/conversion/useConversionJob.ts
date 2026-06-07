import { useCallback, useEffect, useState } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ConversionJobTerminalEvent,
  StartConversionRequest,
} from "../../types/api";
import { useEventStream } from "../events/useEventStream";

interface UseConversionJobArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
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
  const eventStream = useEventStream({ apiClient, baseUrl });
  const [isStarting, setIsStarting] = useState(false);
  const [formError, setFormError] = useState("");

  const clearFormError = useCallback(() => {
    setFormError("");
  }, []);

  const refreshActiveConversion = useCallback(async () => {
    await eventStream.refreshActiveConversionState();
  }, [eventStream.refreshActiveConversionState]);

  const startConversion = useCallback(
    async (payload: StartConversionRequest): Promise<string | null> => {
      const validationError = validateStartPayload(payload);
      if (validationError) {
        setFormError(validationError);
        return null;
      }

      if (!apiClient) {
        setFormError("Backend is not connected.");
        return null;
      }

      setIsStarting(true);
      setFormError("");

      try {
        const response = await apiClient.startConversion(payload);
        return response.job_id || null;
      } catch (error: unknown) {
        setFormError(normalizeErrorMessage(error));
        return null;
      } finally {
        setIsStarting(false);
      }
    },
    [apiClient],
  );

  const waitForTerminalState = useCallback(
    async (jobId: string): Promise<ConversionJobTerminalEvent> => (
      eventStream.waitForTerminalState(jobId)
    ),
    [eventStream.waitForTerminalState],
  );

  useEffect(() => {
    void refreshActiveConversion();
  }, [refreshActiveConversion]);

  return {
    ...eventStream,
    activeJobStatus: eventStream.uiState,
    isStarting,
    formError,
    clearFormError,
    startConversion,
    refreshActiveConversion,
    waitForTerminalState,
  };
}

export type ConversionJobController = ReturnType<typeof useConversionJob>;
