import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { SourcePreflight } from "../../types/api";
import {
  VOICE_PREVIEW_DEFAULT_TEXT,
  compactSample,
  normalizeQuantizationValue,
} from "./conversionPanelHelpers";
import type { useConversionOptions } from "./useConversionOptions";
import { isAbortLikeError, useLatestRequest } from "./useLatestRequest";

type ConversionOptionState = ReturnType<typeof useConversionOptions>;

interface UseConversionPreviewArgs {
  apiClient: ApiClient | null;
  optionState: ConversionOptionState;
  sourcePath: string;
  selectedPreflight: SourcePreflight | null;
  sampleText: string | null;
}

export function useConversionPreview({
  apiClient,
  optionState,
  sourcePath,
  selectedPreflight,
  sampleText,
}: UseConversionPreviewArgs) {
  const [voiceSuggestBusy, setVoiceSuggestBusy] = useState(false);
  const [voiceSuggestStatus, setVoiceSuggestStatus] = useState("");
  const [previewText, setPreviewText] = useState(VOICE_PREVIEW_DEFAULT_TEXT);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewStatus, setPreviewStatus] = useState("");
  const [previewError, setPreviewError] = useState(false);

  const previewAudioRef = useRef<HTMLAudioElement | null>(null);
  const previewObjectUrlRef = useRef<string | null>(null);
  const beginPreviewRequest = useLatestRequest();
  const beginVoiceSuggestRequest = useLatestRequest();
  const voiceSuggestPendingRef = useRef(false);

  const applyPreviewSample = useCallback((sample: string) => {
    setPreviewText(compactSample(sample, 220));
  }, []);

  useEffect(() => {
    beginPreviewRequest();
    setPreviewBusy(false);
    setPreviewStatus("");
    setPreviewError(false);
    return () => {
      beginPreviewRequest();
      previewAudioRef.current?.pause();
      if (previewObjectUrlRef.current) {
        URL.revokeObjectURL(previewObjectUrlRef.current);
        previewObjectUrlRef.current = null;
      }
    };
  }, [apiClient, beginPreviewRequest, optionState.selectedEngine, optionState.selectedQuantization,
    optionState.selectedVoice, optionState.speed, previewText]);

  useEffect(() => {
    beginVoiceSuggestRequest();
    if (voiceSuggestPendingRef.current) {
      voiceSuggestPendingRef.current = false;
      setVoiceSuggestBusy(false);
      setVoiceSuggestStatus("");
    }
  }, [apiClient, beginVoiceSuggestRequest, sourcePath, previewText, selectedPreflight?.sample, optionState.selectedEngine,
    optionState.selectedVoice, optionState.cleanerBackend, optionState.cleanerModel, optionState.cleanerApiKey]);

  useEffect(() => {
    if (sampleText) {
      setPreviewText(sampleText);
    }
  }, [sampleText]);

  const onPreviewVoice = useCallback(async () => {
    if (!apiClient || !optionState.selectedVoice) {
      setPreviewError(true);
      setPreviewStatus("Select a voice before preview.");
      return;
    }

    setPreviewBusy(true);
    setPreviewError(false);
    setPreviewStatus("Generating preview...");
    const quantization = normalizeQuantizationValue(optionState.selectedQuantization);
    const isLatest = beginPreviewRequest();

    try {
      const blob = await apiClient.requestBlob("/api/voices/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          voice: optionState.selectedVoice,
          speed: optionState.speed,
          text: previewText.trim() || undefined,
          tts_engine: optionState.selectedEngine,
          tts_quantization: quantization,
          tts_options: { quantization },
        }),
      });
      if (!isLatest()) {
        return;
      }

      const audio = previewAudioRef.current;
      if (!audio) {
        throw new Error("voice preview player unavailable");
      }

      if (previewObjectUrlRef.current) {
        URL.revokeObjectURL(previewObjectUrlRef.current);
      }
      const objectUrl = URL.createObjectURL(blob);
      previewObjectUrlRef.current = objectUrl;
      audio.pause();
      audio.src = objectUrl;
      audio.currentTime = 0;
      await audio.play();
      if (!isLatest()) {
        return;
      }
      setPreviewStatus(`Previewing ${optionState.selectedVoice}`);
    } catch (error: unknown) {
      if (isLatest() && !isAbortLikeError(error)) {
        setPreviewError(true);
        setPreviewStatus(error instanceof Error ? error.message : "Voice preview failed");
      }
    } finally {
      if (isLatest()) {
        setPreviewBusy(false);
      }
    }
  }, [
    apiClient,
    beginPreviewRequest,
    optionState.selectedEngine,
    optionState.selectedQuantization,
    optionState.selectedVoice,
    optionState.speed,
    previewText,
  ]);

  const recommendationUnavailableReason = optionState.cleanerBackend !== "api" || !optionState.cleanerApiKey.trim() || !optionState.cleanerModel.trim()
    ? "Configure an API cleaner model and key in Settings to recommend a voice."
    : "";

  const onRecommendVoice = useCallback(async () => {
    if (recommendationUnavailableReason) {
      setVoiceSuggestStatus(recommendationUnavailableReason);
      return;
    }
    if (!apiClient) {
      setVoiceSuggestStatus("Backend is not connected.");
      return;
    }
    if (!sourcePath.trim()) {
      setVoiceSuggestStatus("Choose a source before asking for a voice.");
      return;
    }

    setVoiceSuggestBusy(true);
    voiceSuggestPendingRef.current = true;
    setVoiceSuggestStatus("Analyzing first chapters...");
    const isLatest = beginVoiceSuggestRequest();
    try {
      const response = await apiClient.suggestVoice(
        sourcePath.trim(),
        optionState.selectedEngine || null,
        undefined,
        {
          cleaner_backend: optionState.cleanerBackend,
          cleaner_api_model: optionState.cleanerModel,
          cleaner_api_key: optionState.cleanerApiKey,
        },
      );
      const voiceId = response.voice_suggestion?.recommended_voice_id?.trim();
      if (!voiceId) {
        throw new Error("voice recommendation did not include a voice id");
      }
      if (!isLatest()) {
        return;
      }
      optionState.setSelectedVoice(voiceId);
      const excerpt = response.book_sample?.excerpt || selectedPreflight?.sample || previewText;
      if (excerpt) {
        applyPreviewSample(excerpt);
      }
      const style = response.voice_suggestion?.recommended_voice_style || "long-form narration";
      const reason =
        response.voice_suggestion?.reasoning || "Selected from the available desktop voices.";
      setVoiceSuggestStatus(`${voiceId} / ${style}. ${reason}`);
    } catch (error: unknown) {
      if (isLatest() && !isAbortLikeError(error)) {
        setVoiceSuggestStatus(error instanceof Error ? error.message : "voice recommendation failed");
      }
    } finally {
      if (isLatest()) {
        voiceSuggestPendingRef.current = false;
        setVoiceSuggestBusy(false);
      }
    }
  }, [
    apiClient,
    applyPreviewSample,
    beginVoiceSuggestRequest,
    optionState,
    recommendationUnavailableReason,
    previewText,
    selectedPreflight?.sample,
    sourcePath,
  ]);

  return {
    previewText,
    setPreviewText,
    previewBusy,
    previewStatus,
    previewError,
    previewAudioRef,
    voiceSuggestBusy,
    voiceSuggestStatus,
    recommendationUnavailableReason,
    onPreviewVoice,
    onRecommendVoice,
    applyPreviewSample,
  };
}
