import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { SourcePreflight } from "../../types/api";
import {
  VOICE_PREVIEW_DEFAULT_TEXT,
  compactSample,
  normalizeQuantizationValue,
} from "./conversionPanelHelpers";
import type { useConversionOptions } from "./useConversionOptions";

type ConversionOptionState = ReturnType<typeof useConversionOptions>;

interface UseConversionPreviewArgs {
  apiClient: ApiClient | null;
  optionState: ConversionOptionState;
  sourcePath: string;
  selectedPreflight: SourcePreflight | null;
}

export function useConversionPreview({
  apiClient,
  optionState,
  sourcePath,
  selectedPreflight,
}: UseConversionPreviewArgs) {
  const [voiceSuggestBusy, setVoiceSuggestBusy] = useState(false);
  const [voiceSuggestStatus, setVoiceSuggestStatus] = useState("recommendation idle");
  const [previewText, setPreviewText] = useState(VOICE_PREVIEW_DEFAULT_TEXT);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewStatus, setPreviewStatus] = useState("Select a voice and click preview.");
  const [previewError, setPreviewError] = useState(false);

  const previewAudioRef = useRef<HTMLAudioElement | null>(null);
  const previewObjectUrlRef = useRef<string | null>(null);

  const applyPreviewSample = useCallback((sample: string) => {
    setPreviewText(compactSample(sample, 220));
  }, []);

  useEffect(() => {
    return () => {
      if (previewObjectUrlRef.current) {
        URL.revokeObjectURL(previewObjectUrlRef.current);
      }
    };
  }, []);

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
      setPreviewStatus(`Previewing ${optionState.selectedVoice}`);
    } catch (error: unknown) {
      setPreviewError(true);
      setPreviewStatus(error instanceof Error ? error.message : "Voice preview failed");
    } finally {
      setPreviewBusy(false);
    }
  }, [
    apiClient,
    optionState.selectedEngine,
    optionState.selectedQuantization,
    optionState.selectedVoice,
    optionState.speed,
    previewText,
  ]);

  const onRecommendVoice = useCallback(async () => {
    if (!apiClient) {
      setVoiceSuggestStatus("Backend is not connected.");
      return;
    }
    if (!sourcePath.trim()) {
      setVoiceSuggestStatus("Choose a source before asking for a voice.");
      return;
    }

    setVoiceSuggestBusy(true);
    setVoiceSuggestStatus("Analyzing first chapters...");
    try {
      const response = await apiClient.suggestVoice(
        sourcePath.trim(),
        optionState.selectedEngine || null,
      );
      const voiceId = response.voice_suggestion?.recommended_voice_id?.trim();
      if (!voiceId) {
        throw new Error("voice recommendation did not include a voice id");
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
      setVoiceSuggestStatus(error instanceof Error ? error.message : "voice recommendation failed");
    } finally {
      setVoiceSuggestBusy(false);
    }
  }, [
    apiClient,
    applyPreviewSample,
    optionState,
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
    onPreviewVoice,
    onRecommendVoice,
    applyPreviewSample,
  };
}
