import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { StartConversionRequest } from "../../types/api";
import { useEventStream } from "../events/useEventStream";

interface UseConversionJobArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
}

export type StartConversionResult =
  | { ok: true; jobId: string }
  | { ok: false; error: string };

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
  const startInFlightRef = useRef(false);

  const clearFormError = useCallback(() => {
    setFormError("");
  }, []);

  const refreshActiveConversion = useCallback(async () => {
    await eventStream.refreshActiveConversionState();
  }, [eventStream.refreshActiveConversionState]);

  const startConversion = useCallback(
    async (payload: StartConversionRequest): Promise<StartConversionResult> => {
      if (startInFlightRef.current) {
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

      startInFlightRef.current = true;
      setIsStarting(true);
      setFormError("");

      try {
        const response = await apiClient.startConversion(payload);
        if (!response.job_id) {
          const error = "conversion failed to start";
          setFormError(error);
          return { ok: false, error };
        }
        return { ok: true, jobId: response.job_id };
      } catch (error: unknown) {
        const message = normalizeErrorMessage(error);
        setFormError(message);
        return { ok: false, error: message };
      } finally {
        startInFlightRef.current = false;
        setIsStarting(false);
      }
    },
    [apiClient],
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
  };
}

export type ConversionJobController = ReturnType<typeof useConversionJob>;
