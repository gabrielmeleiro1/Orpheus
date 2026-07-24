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
  // Title/author of the last started or reconciled job, so status surfaces
  // label the job itself rather than whatever the library has selected.
  const [activeJobMeta, setActiveJobMeta] = useState<{
    title: string;
    author: string;
  } | null>(null);
  const startInFlightRef = useRef(false);

  const clearFormError = useCallback(() => {
    setFormError("");
  }, []);

  const refreshActiveConversion = useCallback(async () => {
    const response = await eventStream.refreshActiveConversionState();
    if (response?.job) {
      setActiveJobMeta({
        title: String(response.job.title ?? "").trim(),
        author: String(response.job.author ?? "").trim(),
      });
    }
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
        setActiveJobMeta({
          title: String(payload.title ?? "").trim(),
          author: String(payload.author ?? "").trim(),
        });
        eventStream.markConversionStarted(response.job_id);
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
    [apiClient, eventStream.markConversionStarted],
  );

  useEffect(() => {
    void refreshActiveConversion();
  }, [refreshActiveConversion]);

  return {
    ...eventStream,
    activeJobStatus: eventStream.uiState,
    activeJobTitle: activeJobMeta?.title ?? "",
    activeJobAuthor: activeJobMeta?.author ?? "",
    isStarting,
    formError,
    clearFormError,
    startConversion,
    refreshActiveConversion,
  };
}

export type ConversionJobController = ReturnType<typeof useConversionJob>;
