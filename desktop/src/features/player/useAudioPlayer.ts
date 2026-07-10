import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { formatClock } from "../../format/time";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail, LibraryChapterDetail } from "../../types/api";
import { chapterStartSeconds, currentChapterForPosition } from "./chapterPositionModel";
import { useAudioAnalyser } from "./useAudioAnalyser";
import { useAudioElementBindings } from "./useAudioElementBindings";
import { usePlaybackPersistence } from "./playbackPersistence";

interface UseAudioPlayerArgs {
  apiClient: ApiClient | null;
  library: UseLibraryResult;
}

export function useAudioPlayer({ apiClient, library }: UseAudioPlayerArgs) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  const [loadedBookId, setLoadedBookId] = useState<number | null>(null);
  const loadedBookIdRef = useRef<number | null>(null);
  const [playerDismissedBookId, setPlayerDismissedBookId] = useState<number | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [totalSec, setTotalSec] = useState(0);
  const [volume, setVolume] = useState(80);
  const [playerTitle, setPlayerTitle] = useState("player: unavailable");
  const audioVisualizer = useAudioAnalyser(audioRef, isPlaying);

  const previousSelectedBookIdRef = useRef<number | null>(null);
  // Monotonic load id + latest-selection mirror guard loadBookAudio against
  // rapid next/previous: an older in-flight load must never set src/state late.
  const loadSequenceRef = useRef(0);
  const selectedBookIdLatestRef = useRef<number | null>(library.selectedBookId);
  selectedBookIdLatestRef.current = library.selectedBookId;

  const playerEligible = Boolean(library.selectedBookId && library.selectedBookPlayable);
  const playerVisible = Boolean(playerEligible && library.selectedBookId !== playerDismissedBookId);
  const canGoPrevious = library.selectedBookIndex > 0;
  const canGoNext =
    library.selectedBookIndex >= 0 &&
    library.selectedBookIndex < library.items.length - 1;
  const currentChapter = useMemo(
    () => currentChapterForPosition(library.selectedBookDetail?.chapters ?? [], elapsedSec),
    [elapsedSec, library.selectedBookDetail?.chapters],
  );
  const currentChapterLabel = currentChapter
    ? `chapter ${currentChapter.number}: ${currentChapter.title || "untitled"}`
    : "chapter unknown";

  const updatePlayerStatus = useCallback(() => {
    const audio = audioRef.current;

    if (!playerEligible) {
      setPlayerTitle("player: unavailable");
      return;
    }

    if (!audio || !audio.src || !library.selectedBookId || loadedBookId !== library.selectedBookId) {
      setPlayerTitle("player: ready");
      return;
    }

    if (audio.ended) {
      setPlayerTitle("player: ended");
      return;
    }

    setPlayerTitle(`player: ${audio.paused ? "paused" : "playing"} @ ${formatClock(audio.currentTime || 0)}`);
  }, [library.selectedBookId, loadedBookId, playerEligible]);

  const onSaveFailed = useCallback(() => {
    setPlayerTitle("playback save failed");
  }, []);

  const {
    persistLoadedBookPlaybackPosition,
    restorePendingResumePosition,
    waitForAudioReady,
    setPendingResume,
    clearPendingResume,
    markPlaybackSaved,
  } = usePlaybackPersistence({
    apiClient,
    library,
    audioRef,
    loadedBookId,
    onSaveFailed,
  });

  const clearAudioSource = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }

    setAudioSrc(null);
    loadedBookIdRef.current = null;
    setLoadedBookId(null);
    clearPendingResume();
    setIsPlaying(false);
    setElapsedSec(0);
    setTotalSec(0);
    updatePlayerStatus();
  }, [clearPendingResume, updatePlayerStatus]);

  const handleAudioMetadata = useCallback(
    (expectedBookId?: number | null) => {
      const audio = audioRef.current;
      if (!audio) {
        return;
      }

      restorePendingResumePosition(audio, expectedBookId);
      setTotalSec(Number.isFinite(audio.duration) && audio.duration > 0 ? Math.floor(audio.duration) : 0);
      setElapsedSec(Math.floor(audio.currentTime || 0));
      updatePlayerStatus();
    },
    [restorePendingResumePosition, updatePlayerStatus],
  );

  const loadBookAudio = useCallback(
    async (bookId: number | null, detail: LibraryBookDetail | null, autoPlay: boolean): Promise<void> => {
      if (!apiClient || !bookId || !detail?.playback_url) {
        return;
      }

      const audio = audioRef.current;
      if (!audio) {
        return;
      }

      const requestId = ++loadSequenceRef.current;
      const isStale = () =>
        loadSequenceRef.current !== requestId || selectedBookIdLatestRef.current !== bookId;

      if (loadedBookId !== bookId || !audio.src) {
        setPendingResume(bookId, Number(detail.playback_position_sec || 0));

        const audioUrl = await apiClient.getLibraryAudioUrl(bookId);
        if (isStale()) {
          return;
        }

        const nextAudioUrl = new URL(audioUrl);
        nextAudioUrl.searchParams.set("v", String(Date.now()));

        audio.crossOrigin = "anonymous";
        loadedBookIdRef.current = bookId;
        setLoadedBookId(bookId);
        setAudioSrc(nextAudioUrl.toString());
        audio.src = nextAudioUrl.toString();
        audio.load();

        await waitForAudioReady(audio);
        if (isStale()) {
          return;
        }
        restorePendingResumePosition(audio, bookId);
        handleAudioMetadata(bookId);
      }

      if (autoPlay) {
        if (isStale()) {
          return;
        }
        try {
          await audio.play();
        } catch {
          setPlayerTitle("playback failed");
        }
      }
    },
    [
      apiClient,
      handleAudioMetadata,
      loadedBookId,
      restorePendingResumePosition,
      setPendingResume,
      waitForAudioReady,
    ],
  );

  const loadSelectedBookAudio = useCallback(
    async (autoPlay: boolean): Promise<void> => {
      await loadBookAudio(library.selectedBookId, library.selectedBookDetail, autoPlay);
    },
    [library.selectedBookDetail, library.selectedBookId, loadBookAudio],
  );

  const playRelative = useCallback(
    async (delta: number): Promise<void> => {
      if (!library.items.length || delta === 0) {
        return;
      }

      if (library.selectedBookIndex < 0) {
        const nextBook = library.items[0];
        const detail = await library.selectBook(0);
        setPlayerDismissedBookId(null);
        await loadBookAudio(nextBook?.id ?? null, detail, true);
        return;
      }

      const nextIndex = library.selectedBookIndex + delta;
      if (nextIndex < 0 || nextIndex >= library.items.length) {
        return;
      }

      const nextBook = library.items[nextIndex];
      const detail = await library.selectBook(nextIndex);
      setPlayerDismissedBookId(null);
      await loadBookAudio(nextBook?.id ?? null, detail, true);
    },
    [library, loadBookAudio],
  );

  const togglePlay = useCallback(async (): Promise<void> => {
    if (!library.selectedBookId || !playerEligible) {
      return;
    }

    if (playerDismissedBookId === library.selectedBookId) {
      setPlayerDismissedBookId(null);
    }

    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    if (loadedBookId !== library.selectedBookId || !audio.src) {
      await loadSelectedBookAudio(true);
      return;
    }

    if (audio.paused) {
      try {
        await audio.play();
      } catch {
        setPlayerTitle("playback failed");
      }
      return;
    }

    audio.pause();
    void persistLoadedBookPlaybackPosition({ force: true, silent: true });
  }, [
    library.selectedBookId,
    loadSelectedBookAudio,
    loadedBookId,
    persistLoadedBookPlaybackPosition,
    playerDismissedBookId,
    playerEligible,
  ]);

  const seekBy = useCallback(
    (deltaSeconds: number): void => {
      const audio = audioRef.current;
      if (!audio || !audio.src) {
        return;
      }

      audio.currentTime = Math.max(0, (audio.currentTime || 0) + deltaSeconds);
      setElapsedSec(Math.floor(audio.currentTime || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [persistLoadedBookPlaybackPosition, updatePlayerStatus],
  );

  const seekTo = useCallback(
    (positionSeconds: number): void => {
      const audio = audioRef.current;
      if (!audio || !audio.src) {
        return;
      }

      const duration = Number.isFinite(audio.duration) ? audio.duration : positionSeconds;
      audio.currentTime = Math.max(0, Math.min(duration, positionSeconds));
      setElapsedSec(Math.floor(audio.currentTime || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [persistLoadedBookPlaybackPosition, updatePlayerStatus],
  );

  const jumpToChapter = useCallback(
    async (chapter: LibraryChapterDetail): Promise<void> => {
      if (!library.selectedBookDetail || !playerEligible) {
        return;
      }

      setPlayerDismissedBookId(null);
      await loadSelectedBookAudio(false);

      const target = chapterStartSeconds(library.selectedBookDetail.chapters, chapter.number);
      const audio = audioRef.current;
      if (!audio || !audio.src) {
        return;
      }

      const duration = Number.isFinite(audio.duration) ? audio.duration : target;
      audio.currentTime = Math.max(0, Math.min(duration || target, target));
      setElapsedSec(Math.floor(audio.currentTime || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [
      library.selectedBookDetail,
      loadSelectedBookAudio,
      persistLoadedBookPlaybackPosition,
      playerEligible,
      updatePlayerStatus,
    ],
  );

  const setPlayerVolume = useCallback((nextVolume: number): void => {
    const clamped = Math.max(0, Math.min(100, Math.round(nextVolume)));
    setVolume(clamped);

    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    audio.volume = clamped / 100;
    audio.muted = clamped === 0;
    updatePlayerStatus();
  }, [updatePlayerStatus]);

  const closePlayer = useCallback(() => {
    if (library.selectedBookId) {
      setPlayerDismissedBookId(library.selectedBookId);
    }

    const audio = audioRef.current;
    if (audio && !audio.paused) {
      audio.pause();
    }

    void persistLoadedBookPlaybackPosition({ force: true, silent: true });
  }, [library.selectedBookId, persistLoadedBookPlaybackPosition]);

  useEffect(() => {
    const previousBookId = previousSelectedBookIdRef.current;
    const selectedBookId = library.selectedBookId;
    previousSelectedBookIdRef.current = selectedBookId;

    if (previousBookId === null || previousBookId === selectedBookId) {
      return;
    }

    void persistLoadedBookPlaybackPosition({
      bookId: previousBookId,
      force: true,
      silent: true,
    });
    if (loadedBookIdRef.current === previousBookId) {
      clearAudioSource();
    }
  }, [clearAudioSource, library.selectedBookId, persistLoadedBookPlaybackPosition]);

  useEffect(() => {
    if (library.selectedBookPlayable) {
      updatePlayerStatus();
      return;
    }

    clearAudioSource();
    updatePlayerStatus();
  }, [clearAudioSource, library.selectedBookPlayable, updatePlayerStatus]);

  useEffect(() => {
    updatePlayerStatus();
  }, [elapsedSec, isPlaying, totalSec, updatePlayerStatus]);

  const audioProps = useAudioElementBindings({
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
    setIsPlaying,
    setElapsedSec,
    setTotalSec,
    setVolume,
    setPlayerTitle,
  });

  return {
    audioRef,
    audioProps,
    audioVisualizer,
    playerEligible,
    playerVisible,
    playing: isPlaying,
    title: playerTitle,
    statusText: library.selectedBookDetail?.title
      ? `${library.selectedBookDetail.title} · ${currentChapterLabel}`
      : playerTitle,
    elapsedSec,
    durationSec: totalSec,
    volume,
    currentChapter,
    currentChapterLabel,
    canGoPrevious,
    canGoNext,
    togglePlay,
    seekBy,
    seekTo,
    playRelative,
    jumpToChapter,
    setPlayerVolume,
    closePlayer,
  };
}
