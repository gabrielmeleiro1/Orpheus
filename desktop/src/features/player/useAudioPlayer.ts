import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactEventHandler,
} from "react";

import type { ApiClient } from "../../api/client";
import { formatTimeValue, PLAYBACK_SAVE_INTERVAL_SEC } from "../dashboard/dashboardHelpers";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail, LibraryChapterDetail } from "../../types/api";
import { useAudioAnalyser } from "./useAudioAnalyser";

interface PersistPlaybackOptions {
  bookId?: number | null;
  positionSec?: number;
  force?: boolean;
  silent?: boolean;
}

interface UseAudioPlayerArgs {
  apiClient: ApiClient | null;
  library: UseLibraryResult;
}

const PLAYBACK_UI_UPDATE_INTERVAL_MS = 1000;

function chapterStartSeconds(chapters: LibraryChapterDetail[], targetNumber: number): number {
  let cursor = 0;
  for (const chapter of chapters) {
    if (chapter.number === targetNumber) {
      return cursor;
    }
    cursor += Math.max(0, Number(chapter.duration_ms || 0) / 1000);
  }
  return 0;
}

function currentChapterForPosition(
  chapters: LibraryChapterDetail[],
  elapsedSeconds: number,
): LibraryChapterDetail | null {
  if (chapters.length === 0) {
    return null;
  }

  let cursor = 0;
  let fallback = chapters[0];
  for (const chapter of chapters) {
    const duration = Math.max(0, Number(chapter.duration_ms || 0) / 1000);
    if (duration <= 0) {
      fallback = chapter;
      continue;
    }
    if (elapsedSeconds >= cursor && elapsedSeconds < cursor + duration) {
      return chapter;
    }
    cursor += duration;
    fallback = chapter;
  }
  return fallback;
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

  const pendingResumeBookIdRef = useRef<number | null>(null);
  const pendingResumePositionSecRef = useRef(0);
  const lastSavedPlaybackBookIdRef = useRef<number | null>(null);
  const lastSavedPlaybackSecRef = useRef<number | null>(null);
  const lastPlaybackUiUpdateMsRef = useRef(0);
  const lastPlaybackUiSecondRef = useRef(-1);
  const previousSelectedBookIdRef = useRef<number | null>(null);

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

    setPlayerTitle(`player: ${audio.paused ? "paused" : "playing"} @ ${formatTimeValue(audio.currentTime || 0)}`);
  }, [library.selectedBookId, loadedBookId, playerEligible]);

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
    pendingResumeBookIdRef.current = null;
    pendingResumePositionSecRef.current = 0;
    setIsPlaying(false);
    setElapsedSec(0);
    setTotalSec(0);
    updatePlayerStatus();
  }, [updatePlayerStatus]);

  const persistBookPlaybackPosition = useCallback(
    async (bookId: number, positionSec: number, options: PersistPlaybackOptions = {}): Promise<number | null> => {
      if (!apiClient || !bookId) {
        return null;
      }

      const normalizedPosition = Math.max(0, Math.round((Number(positionSec) || 0) * 10) / 10);
      const force = Boolean(options.force);

      if (
        !force &&
        lastSavedPlaybackBookIdRef.current === bookId &&
        lastSavedPlaybackSecRef.current !== null &&
        Math.abs(normalizedPosition - lastSavedPlaybackSecRef.current) < PLAYBACK_SAVE_INTERVAL_SEC
      ) {
        return lastSavedPlaybackSecRef.current;
      }

      lastSavedPlaybackBookIdRef.current = bookId;
      lastSavedPlaybackSecRef.current = normalizedPosition;

      try {
        const response = await apiClient.updateBookPlaybackPosition(bookId, normalizedPosition);
        const savedPosition = Number(response.position_sec);
        const resolved = Number.isFinite(savedPosition) ? savedPosition : normalizedPosition;
        lastSavedPlaybackSecRef.current = resolved;
        if (library.selectedBookId === bookId) {
          library.setSelectedPlaybackPosition(resolved);
        }
        return resolved;
      } catch {
        if (!options.silent) {
          setPlayerTitle("playback save failed");
        }
        return null;
      }
    },
    [apiClient, library],
  );

  const persistLoadedBookPlaybackPosition = useCallback(
    async (options: PersistPlaybackOptions = {}): Promise<number | null> => {
      const bookId = options.bookId ?? loadedBookId;
      if (!bookId) {
        return null;
      }

      const audio = audioRef.current;
      const fallbackPosition =
        library.selectedBookId === bookId
          ? Number(library.selectedBookDetail?.playback_position_sec || 0)
          : 0;

      const resolvedPosition =
        typeof options.positionSec === "number"
          ? options.positionSec
          : audio && loadedBookId === bookId && audio.src
            ? Number(audio.currentTime || 0)
            : fallbackPosition;

      return persistBookPlaybackPosition(bookId, resolvedPosition, options);
    },
    [library.selectedBookDetail?.playback_position_sec, library.selectedBookId, loadedBookId, persistBookPlaybackPosition],
  );

  const waitForAudioReady = useCallback((audio: HTMLAudioElement | null): Promise<void> => {
    if (!audio || audio.readyState >= 1) {
      return Promise.resolve();
    }

    return new Promise((resolve) => {
      let settled = false;
      let timeoutId: number | null = null;

      const finish = () => {
        if (settled) {
          return;
        }
        settled = true;
        audio.removeEventListener("loadedmetadata", finish);
        audio.removeEventListener("durationchange", finish);
        audio.removeEventListener("canplay", finish);
        audio.removeEventListener("error", finish);
        if (timeoutId !== null) {
          window.clearTimeout(timeoutId);
        }
        resolve();
      };

      audio.addEventListener("loadedmetadata", finish, { once: true });
      audio.addEventListener("durationchange", finish, { once: true });
      audio.addEventListener("canplay", finish, { once: true });
      audio.addEventListener("error", finish, { once: true });
      timeoutId = window.setTimeout(finish, 2000);
    });
  }, []);

  const restorePendingResumePosition = useCallback((audio: HTMLAudioElement | null, expectedBookId?: number | null) => {
    const resolvedBookId = expectedBookId ?? loadedBookId;
    if (!audio || pendingResumeBookIdRef.current !== resolvedBookId) {
      return;
    }

    const target = Math.max(0, Number(pendingResumePositionSecRef.current || 0));
    const duration = Number.isFinite(audio.duration) ? audio.duration : target;
    const safeTarget = Math.max(0, Math.min(duration || target, target));

    if (safeTarget > 0) {
      try {
        audio.currentTime = safeTarget;
      } catch {
        return;
      }
    }

    pendingResumeBookIdRef.current = null;
    pendingResumePositionSecRef.current = 0;
  }, [loadedBookId]);

  const handleAudioMetadata = useCallback((expectedBookId?: number | null) => {
    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    restorePendingResumePosition(audio, expectedBookId);
    setTotalSec(Number.isFinite(audio.duration) && audio.duration > 0 ? Math.floor(audio.duration) : 0);
    setElapsedSec(Math.floor(audio.currentTime || 0));
    updatePlayerStatus();
  }, [restorePendingResumePosition, updatePlayerStatus]);

  const loadBookAudio = useCallback(
    async (bookId: number | null, detail: LibraryBookDetail | null, autoPlay: boolean): Promise<void> => {
      if (!apiClient || !bookId || !detail?.playback_url) {
        return;
      }

      const audio = audioRef.current;
      if (!audio) {
        return;
      }

      if (loadedBookId !== bookId || !audio.src) {
        pendingResumeBookIdRef.current = bookId;
        pendingResumePositionSecRef.current = Number(detail.playback_position_sec || 0);

        const nextAudioUrl = new URL(await apiClient.getLibraryAudioUrl(bookId));
        nextAudioUrl.searchParams.set("v", String(Date.now()));

        audio.crossOrigin = "anonymous";
        loadedBookIdRef.current = bookId;
        setLoadedBookId(bookId);
        setAudioSrc(nextAudioUrl.toString());
        audio.src = nextAudioUrl.toString();
        audio.load();

        await waitForAudioReady(audio);
        restorePendingResumePosition(audio, bookId);
        handleAudioMetadata(bookId);
      }

      if (autoPlay) {
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
    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    audio.volume = Math.max(0, Math.min(100, volume)) / 100;
    audio.muted = volume === 0;
  }, [volume]);

  useEffect(() => {
    updatePlayerStatus();
  }, [elapsedSec, isPlaying, totalSec, updatePlayerStatus]);

  useEffect(() => {
    const onBeforeUnload = () => {
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    };

    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, [persistLoadedBookPlaybackPosition]);

  const audioProps = useMemo(() => {
    const handleTimeUpdate: ReactEventHandler<HTMLAudioElement> = () => {
      const audio = audioRef.current;
      if (!audio) {
        return;
      }

      const nextElapsedSec = Math.floor(audio.currentTime || 0);
      const nowMs = window.performance.now();
      const shouldUpdateUi =
        nextElapsedSec !== lastPlaybackUiSecondRef.current ||
        nowMs - lastPlaybackUiUpdateMsRef.current >= PLAYBACK_UI_UPDATE_INTERVAL_MS;

      if (!shouldUpdateUi) {
        return;
      }

      lastPlaybackUiSecondRef.current = nextElapsedSec;
      lastPlaybackUiUpdateMsRef.current = nowMs;
      setElapsedSec(nextElapsedSec);
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        setTotalSec(Math.floor(audio.duration));
      }

      if (!audio.paused) {
        void persistLoadedBookPlaybackPosition({ force: false, silent: true });
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
        setIsPlaying(false);
        const finishedBookId = loadedBookId;
        if (finishedBookId) {
          lastSavedPlaybackBookIdRef.current = finishedBookId;
          lastSavedPlaybackSecRef.current = 0;
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
      },
    };
  }, [
    audioSrc,
    handleAudioMetadata,
    library,
    loadedBookId,
    persistLoadedBookPlaybackPosition,
    playRelative,
    updatePlayerStatus,
  ]);

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
