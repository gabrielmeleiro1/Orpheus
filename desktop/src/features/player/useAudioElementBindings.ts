import { useEffect, useMemo, useRef, type Dispatch, type ReactEventHandler, type RefObject, type SetStateAction } from "react";

import type { UseLibraryResult } from "../library/useLibrary";
import type { PersistPlaybackOptions } from "./playbackPersistence";

const PLAYBACK_UI_UPDATE_INTERVAL_MS = 1000;

export interface AudioElementProps {
  src: string | null;
  preload: "metadata";
  onLoadedMetadata: () => void;
  onDurationChange: () => void;
  onPlay: () => void;
  onPause: () => void;
  onTimeUpdate: ReactEventHandler<HTMLAudioElement>;
  onVolumeChange: () => void;
  onEmptied: () => void;
  onEnded: () => void;
  onError: () => void;
}

interface UseAudioElementBindingsArgs {
  audioRef: RefObject<HTMLAudioElement | null>;
  audioSrc: string | null;
  loadedBookId: number | null;
  volume: number;
  library: UseLibraryResult;
  persistLoadedBookPlaybackPosition: (options?: PersistPlaybackOptions) => Promise<number | null>;
  markPlaybackSaved: (bookId: number, positionSec: number) => void;
  handleAudioMetadata: (expectedBookId?: number | null) => void;
  updatePlayerStatus: () => void;
  playRelative: (delta: number) => Promise<void>;
  advanceChapter?: () => boolean;
  setIsPlaying: Dispatch<SetStateAction<boolean>>;
  setElapsedSec: Dispatch<SetStateAction<number>>;
  setTotalSec: Dispatch<SetStateAction<number>>;
  setVolume: Dispatch<SetStateAction<number>>;
  setPlayerTitle: Dispatch<SetStateAction<string>>;
  onPlaybackError: () => void;
  onPlaybackHealthy: () => void;
}

/**
 * Builds the React media-event props for the <audio> element and keeps the
 * element's volume in sync with the volume state. React manages attach/detach
 * of these synthetic handlers, so no manual listener cleanup is required here.
 */
export function useAudioElementBindings({
  audioRef,
  audioSrc,
  loadedBookId,
  volume,
  library,
  persistLoadedBookPlaybackPosition,
  markPlaybackSaved,
  handleAudioMetadata,
  updatePlayerStatus,
  playRelative,
  advanceChapter,
  setIsPlaying,
  setElapsedSec,
  setTotalSec,
  setVolume,
  setPlayerTitle,
  onPlaybackError,
  onPlaybackHealthy,
}: UseAudioElementBindingsArgs): AudioElementProps {
  const lastPlaybackUiUpdateMsRef = useRef(0);
  const lastPlaybackUiSecondRef = useRef(-1);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    audio.volume = Math.max(0, Math.min(100, volume)) / 100;
    audio.muted = volume === 0;
  }, [audioRef, volume]);

  return useMemo(() => {
    const handleTimeUpdate: ReactEventHandler<HTMLAudioElement> = () => {
      const audio = audioRef.current;
      if (!audio) {
        return;
      }
      onPlaybackHealthy();

      const nextElapsedSec = Math.max(0, Number(audio.currentTime) || 0);
      const nextElapsedUiSecond = Math.floor(nextElapsedSec);
      if (!audio.paused) {
        void persistLoadedBookPlaybackPosition({ force: false, silent: true });
      }
      const nowMs = window.performance.now();
      const shouldUpdateUi =
        nextElapsedUiSecond !== lastPlaybackUiSecondRef.current ||
        nowMs - lastPlaybackUiUpdateMsRef.current >= PLAYBACK_UI_UPDATE_INTERVAL_MS;

      if (!shouldUpdateUi) {
        return;
      }

      lastPlaybackUiSecondRef.current = nextElapsedUiSecond;
      lastPlaybackUiUpdateMsRef.current = nowMs;
      setElapsedSec(nextElapsedSec);
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        setTotalSec(Math.floor(audio.duration));
      }

      updatePlayerStatus();
    };

    return {
      src: audioSrc,
      preload: "metadata" as const,
      onLoadedMetadata: () => {
        handleAudioMetadata();
      },
      onDurationChange: () => {
        handleAudioMetadata();
      },
      onPlay: () => {
        setIsPlaying(true);
        updatePlayerStatus();
      },
      onPause: () => {
        setIsPlaying(false);
        void persistLoadedBookPlaybackPosition({ force: true, silent: true });
        updatePlayerStatus();
      },
      onTimeUpdate: handleTimeUpdate,
      onVolumeChange: () => {
        const audio = audioRef.current;
        if (!audio) {
          return;
        }

        const nextVolume = audio.muted ? 0 : Math.round((audio.volume || 0) * 100);
        setVolume(nextVolume);
        updatePlayerStatus();
      },
      onEmptied: () => {
        setIsPlaying(false);
        setElapsedSec(0);
        setTotalSec(0);
        updatePlayerStatus();
      },
      onEnded: () => {
        if (advanceChapter?.()) return;
        setIsPlaying(false);
        const finishedBookId = loadedBookId;
        if (finishedBookId) {
          markPlaybackSaved(finishedBookId, 0);
          void persistLoadedBookPlaybackPosition({
            bookId: finishedBookId,
            positionSec: 0,
            force: true,
            silent: true,
          });
        }
        if (library.selectedBookId === finishedBookId) {
          library.setSelectedPlaybackPosition(0);
        }
        updatePlayerStatus();
        void playRelative(1);
      },
      onError: () => {
        setPlayerTitle("playback failed");
        onPlaybackError();
      },
    };
  }, [
    audioRef,
    audioSrc,
    handleAudioMetadata,
    library,
    loadedBookId,
    markPlaybackSaved,
    onPlaybackError,
    onPlaybackHealthy,
    persistLoadedBookPlaybackPosition,
    playRelative,
    advanceChapter,
    setElapsedSec,
    setIsPlaying,
    setPlayerTitle,
    setTotalSec,
    setVolume,
    updatePlayerStatus,
  ]);
}
