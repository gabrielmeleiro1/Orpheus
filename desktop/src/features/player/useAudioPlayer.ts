import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { formatClock } from "../../format/time";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail, LibraryChapterDetail } from "../../types/api";
import { chapterStartSeconds, currentChapterForPosition } from "./chapterPositionModel";
import { useAudioAnalyser } from "./useAudioAnalyser";
import { useAudioElementBindings } from "./useAudioElementBindings";
import { usePlaybackPersistence } from "./playbackPersistence";
import { resolvePlayerCanSeek, resolvePlayerDurationSec, seekAudioIfReady } from "./playerDurationModel";

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
  const [playerTitle, setPlayerTitle] = useState("No audio loaded");
  const audioVisualizer = useAudioAnalyser(audioRef, isPlaying);

  const previousSelectedBookIdRef = useRef<number | null>(null);
  // Monotonic load id + latest-selection mirror guard loadBookAudio against
  // rapid next/previous: an older in-flight load must never set src/state late.
  const loadSequenceRef = useRef(0);
  const playbackRecoveryAttemptsRef = useRef<Map<number, symbol>>(new Map());
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
    ? `Chapter ${currentChapter.number}: ${currentChapter.title || "Untitled"}`
    : "Chapter unknown";
  const bookDurationMs = useMemo(() => {
    const chapters = library.selectedBookDetail?.chapters ?? [];
    if (chapters.length === 0) {
      return null;
    }
    return chapters.reduce((total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0)), 0);
  }, [library.selectedBookDetail?.chapters]);
  const durationSec = resolvePlayerDurationSec({
    audioDurationSec: totalSec,
    bookDurationMs,
    // Secondary only: whole-book sum is preferred for the packaged stream slider.
    chapterDurationMs: bookDurationMs && bookDurationMs > 0 ? null : (currentChapter?.duration_ms ?? null),
  });
  const canSeek = resolvePlayerCanSeek({
    audioSrc,
    loadedBookId,
    selectedBookId: library.selectedBookId,
  });
  const updatePlayerStatus = useCallback(() => {
    const audio = audioRef.current;

    if (!playerEligible) {
      setPlayerTitle("No audio loaded");
      return;
    }

    if (!audio || !audio.src || !library.selectedBookId || loadedBookId !== library.selectedBookId) {
      setPlayerTitle("Ready to play");
      return;
    }

    if (audio.ended) {
      setPlayerTitle("Finished");
      return;
    }

    setPlayerTitle(`${audio.paused ? "Paused" : "Playing"} · ${formatClock(audio.currentTime || 0)}`);
  }, [library.selectedBookId, loadedBookId, playerEligible]);

  const onSaveFailed = useCallback(() => {
    setPlayerTitle("Could not save playback position");
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
    async (
      bookId: number | null,
      detail: LibraryBookDetail | null,
      autoPlay: boolean,
      forceReload = false,
    ): Promise<void> => {
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

      if (forceReload || loadedBookId !== bookId || !audio.src) {
        setPendingResume(bookId, Number(detail.playback_position_sec || 0));

        // All callers invoke playback actions fire-and-forget (`void ...`), so
        // a rejection here would surface as an unhandled rejection and a dead
        // play button. Fail soft: report and bail instead of throwing.
        let audioUrl: string;
        try {
          audioUrl = await apiClient.getLibraryAudioUrl(bookId);
        } catch {
          if (!isStale()) {
            setPlayerTitle("Playback failed");
          }
          return;
        }
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
          setPlayerTitle("Playback failed");
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
        setPlayerTitle("Playback failed");
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
      const positionSeconds = Math.max(0, (audio?.currentTime || 0) + deltaSeconds);
      if (
        !seekAudioIfReady({
          audio,
          loadedBookId,
          selectedBookId: library.selectedBookId,
          positionSeconds,
        })
      ) {
        return;
      }

      setElapsedSec(Math.floor(audio!.currentTime || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [
      library.selectedBookId,
      loadedBookId,
      persistLoadedBookPlaybackPosition,
      updatePlayerStatus,
    ],
  );

  const seekTo = useCallback(
    (positionSeconds: number): void => {
      const audio = audioRef.current;
      if (
        !seekAudioIfReady({
          audio,
          loadedBookId,
          selectedBookId: library.selectedBookId,
          positionSeconds,
        })
      ) {
        return;
      }

      setElapsedSec(Math.floor(audio!.currentTime || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [
      library.selectedBookId,
      loadedBookId,
      persistLoadedBookPlaybackPosition,
      updatePlayerStatus,
    ],
  );

  const jumpToChapter = useCallback(
    async (chapter: LibraryChapterDetail): Promise<void> => {
      const requestedBookId = library.selectedBookId;
      const requestedDetail = library.selectedBookDetail;
      if (!requestedBookId || !requestedDetail || !playerEligible) {
        return;
      }

      setPlayerDismissedBookId(null);
      await loadBookAudio(requestedBookId, requestedDetail, false);

      if (
        selectedBookIdLatestRef.current !== requestedBookId ||
        loadedBookIdRef.current !== requestedBookId
      ) {
        return;
      }

      const target = chapterStartSeconds(requestedDetail.chapters, chapter.number);
      const audio = audioRef.current;
      if (!audio || !audio.src) {
        return;
      }

      const duration = Number.isFinite(audio.duration) ? audio.duration : target;
      audio.currentTime = Math.max(0, Math.min(duration || target, target));
      setElapsedSec(Math.floor(audio.currentTime || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({
        bookId: requestedBookId,
        positionSec: audio.currentTime,
        force: true,
        silent: true,
      });
    },
    [
      library.selectedBookDetail,
      library.selectedBookId,
      loadBookAudio,
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

  const handlePlaybackError = useCallback(() => {
    const bookId = library.selectedBookId;
    const detail = library.selectedBookDetail;
    const audio = audioRef.current;
    if (!bookId || !detail?.playback_url || !audio) {
      return;
    }
    if (playbackRecoveryAttemptsRef.current.has(bookId)) {
      return;
    }

    const recoveryToken = Symbol("playback-recovery");
    playbackRecoveryAttemptsRef.current.set(bookId, recoveryToken);
    setPendingResume(bookId, Number(audio.currentTime || detail.playback_position_sec || 0));
    setPlayerTitle("Reconnecting playback…");
    void loadBookAudio(bookId, detail, isPlaying, true).finally(() => {
      if (playbackRecoveryAttemptsRef.current.get(bookId) === recoveryToken) {
        playbackRecoveryAttemptsRef.current.delete(bookId);
      }
    });
  }, [isPlaying, library.selectedBookDetail, library.selectedBookId, loadBookAudio, setPendingResume]);

  const markPlaybackHealthy = useCallback(() => {
    const bookId = loadedBookIdRef.current ?? selectedBookIdLatestRef.current;
    if (bookId !== null) {
      playbackRecoveryAttemptsRef.current.delete(bookId);
    }
  }, []);

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
    onPlaybackError: handlePlaybackError,
    onPlaybackHealthy: markPlaybackHealthy,
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
    durationSec,
    canSeek,
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
